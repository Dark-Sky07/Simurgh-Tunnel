"""Private, bounded monthly traffic accounting for the entry node."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_MAX_STATE_BYTES = 16 * 1024
_FLUSH_INTERVAL = 1.0


class QuotaReached(ConnectionError):
    """The configured UTC-month transfer quota has been exhausted."""


def _month_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def read_usage_snapshot(path: Path) -> dict[str, Any] | None:
    """Read a small, regular, private usage state file; never follow symlinks."""
    path = Path(path)
    if path.is_symlink():
        raise OSError(f"refusing symlinked usage state: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_STATE_BYTES:
            raise OSError(f"usage state is not a small regular file: {path}")
        encoded = stream.read(_MAX_STATE_BYTES + 1)
    if len(encoded) > _MAX_STATE_BYTES:
        raise OSError(f"usage state exceeds the size limit: {path}")
    raw = json.loads(encoded.decode("utf-8"))
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
        raise ValueError("usage state has an unsupported format")
    for key in ("upload_bytes", "download_bytes"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("usage state contains an invalid byte counter")
    month = raw.get("month")
    if not isinstance(month, str) or len(month) != 7 or month[4] != "-":
        raise ValueError("usage state contains an invalid month")
    quota = raw.get("quota_bytes")
    if quota is not None and (isinstance(quota, bool) or not isinstance(quota, int) or quota < 0):
        raise ValueError("usage state contains an invalid quota")
    return raw


class UsageMeter:
    """Count entry-side payload bytes and enforce an optional UTC-month quota."""

    def __init__(self, path: Path, quota_mib: int | None = None):
        if quota_mib is not None and (
                isinstance(quota_mib, bool) or not isinstance(quota_mib, int)
                or not 1 <= quota_mib <= 1_048_576):
            raise ValueError("monthly quota must be between 1 and 1,048,576 MiB")
        self.path = Path(path)
        self.quota_bytes = quota_mib * 1024 * 1024 if quota_mib is not None else None
        self.month = _month_now()
        self.upload_bytes = 0
        self.download_bytes = 0
        self._last_flush = 0.0
        state = read_usage_snapshot(self.path)
        if state and state["month"] == self.month:
            self.upload_bytes = state["upload_bytes"]
            self.download_bytes = state["download_bytes"]
        self.flush()

    @property
    def used_bytes(self) -> int:
        self._roll_month()
        return self.upload_bytes + self.download_bytes

    @property
    def exhausted(self) -> bool:
        return self.quota_bytes is not None and self.used_bytes >= self.quota_bytes

    def _roll_month(self) -> None:
        month = _month_now()
        if month != self.month:
            self.month = month
            self.upload_bytes = 0
            self.download_bytes = 0

    def consume(self, direction: str, count: int) -> int:
        """Reserve up to count bytes; return the amount allowed under the quota."""
        if direction not in ("upload", "download") or count < 0:
            raise ValueError("invalid traffic accounting request")
        if count == 0:
            return 0
        self._roll_month()
        allowed = count
        if self.quota_bytes is not None:
            allowed = min(count, max(0, self.quota_bytes - self.upload_bytes - self.download_bytes))
        if direction == "upload":
            self.upload_bytes += allowed
        else:
            self.download_bytes += allowed
        if allowed:
            if self.exhausted:
                self.flush()
            else:
                self._flush_if_due()
        return allowed

    def allow_datagram(self, direction: str, count: int) -> bool:
        """Reserve a datagram atomically; never count or forward a partial UDP packet."""
        self._roll_month()
        if self.quota_bytes is not None and count > self.quota_bytes - self.used_bytes:
            return False
        return self.consume(direction, count) == count

    def snapshot(self) -> dict[str, Any]:
        self._roll_month()
        return {
            "version": 1,
            "month": self.month,
            "upload_bytes": self.upload_bytes,
            "download_bytes": self.download_bytes,
            "quota_bytes": self.quota_bytes,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def _flush_if_due(self) -> None:
        if time.monotonic() - self._last_flush >= _FLUSH_INTERVAL:
            self.flush()

    def flush(self) -> None:
        """Atomically persist counters with mode 0600; a corrupt/symlink state fails closed."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise OSError(f"refusing symlinked usage state: {self.path}")
        fd, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.",
                                               dir=str(self.path.parent))
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.snapshot(), stream, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            self._last_flush = time.monotonic()
        except BaseException:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise


def format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    amount = float(value)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        amount /= 1024
        if amount < 1024 or unit == "TiB":
            return f"{amount:.2f} {unit}"
    return f"{value} B"

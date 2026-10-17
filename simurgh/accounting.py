"""Traffic accounting that survives a restart, and the quotas built on it.

Every mapping, every tunnel token and the tunnel itself keep counters in three
shapes: the running total, a per-day bucket and a per-month bucket.  The file
(``<home>/traffic.json``) is written atomically every few seconds and on the
way out, so ``simurgh traffic --month`` still knows what September cost after
the server rebooted in October.

Quotas read the same numbers: ``quota = "200G"`` + ``quota_period = "month"``
means "this mapping may move 200 GiB per calendar month", and when it is used
up the mapping either stops accepting connections (``quota_action = "block"``,
the default) or falls back to a trickle (``"throttle"``).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import date
from pathlib import Path

from .util import get_logger, human_bytes, parse_size

log = get_logger("simurgh.accounting")

VERSION = 1
SAVE_INTERVAL = 15.0
KEEP_DAYS = 92


def _today() -> str:
    return date.today().isoformat()


def _month() -> str:
    return date.today().isoformat()[:7]


def _blank() -> dict:
    return {"total": {"in": 0, "out": 0},
            "days": {}, "months": {}}


class TrafficAccount:
    """Counters for a set of keys, persisted to one JSON file."""

    def __init__(self, path: str | Path | None = None, interval: float = SAVE_INTERVAL):
        self.path = Path(path) if path else None
        self.interval = interval
        self.keys: dict[str, dict] = {}
        self.started = time.time()
        self._dirty = False
        self._task: asyncio.Task | None = None
        # NB: no asyncio primitives are created here.  On Python 3.9 an
        # ``asyncio.Lock()``/``Event()`` built outside a running loop grabs the
        # thread's loop, and that raises once some earlier ``asyncio.run`` has
        # closed it -- which is exactly what happens when a test (or the panel)
        # builds an account before the node's loop exists.
        if self.path is not None:
            self.load()

    # ------------------------------------------------------------ persistence
    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("could not read %s (%s); starting fresh", self.path, exc)
            return
        if not isinstance(data, dict) or data.get("version") != VERSION:
            return
        keys = data.get("keys")
        if isinstance(keys, dict):
            for key, value in keys.items():
                if isinstance(value, dict):
                    self.keys[key] = self._merge(value)
        log.info("traffic history loaded: %d key(s) from %s", len(self.keys), self.path)

    def _merge(self, value: dict) -> dict:
        out = _blank()
        total = value.get("total") or {}
        out["total"]["in"] = int(total.get("in", 0))
        out["total"]["out"] = int(total.get("out", 0))
        for bucket in ("days", "months"):
            raw = value.get(bucket) or {}
            if isinstance(raw, dict):
                for name, pair in raw.items():
                    if isinstance(pair, dict):
                        out[bucket][name] = {"in": int(pair.get("in", 0)),
                                             "out": int(pair.get("out", 0))}
        return out

    def save(self, force: bool = False) -> None:
        if self.path is None or (not self._dirty and not force):
            return
        payload = {
            "version": VERSION,
            "updated": time.time(),
            "keys": self.keys,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError as exc:  # pragma: no cover - disk problems
            log.warning("could not save traffic history: %s", exc)

    async def autosave(self) -> None:
        """Background flusher; cancelled on shutdown."""
        try:
            while True:
                await asyncio.sleep(self.interval)
                if self._dirty:
                    self.save()
        except asyncio.CancelledError:
            pass

    def start(self) -> None:
        if self.path is not None and self._task is None:
            self._task = asyncio.ensure_future(self.autosave())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self.save(force=True)

    # ---------------------------------------------------------------- counters
    def add(self, key: str, in_bytes: int = 0, out_bytes: int = 0) -> None:
        if not key or (in_bytes <= 0 and out_bytes <= 0):
            return
        entry = self.keys.get(key)
        if entry is None:
            entry = self.keys[key] = _blank()
        entry["total"]["in"] += max(0, int(in_bytes))
        entry["total"]["out"] += max(0, int(out_bytes))
        day, month = _today(), _month()
        bucket = entry["days"].setdefault(day, {"in": 0, "out": 0})
        bucket["in"] += max(0, int(in_bytes))
        bucket["out"] += max(0, int(out_bytes))
        mbucket = entry["months"].setdefault(month, {"in": 0, "out": 0})
        mbucket["in"] += max(0, int(in_bytes))
        mbucket["out"] += max(0, int(out_bytes))
        self._dirty = True

    def usage(self, key: str, period: str = "total") -> int:
        entry = self.keys.get(key)
        if not entry:
            return 0
        if period == "day":
            pair = entry["days"].get(_today()) or {}
        elif period == "month":
            pair = entry["months"].get(_month()) or {}
        else:
            pair = entry["total"]
        return int(pair.get("in", 0)) + int(pair.get("out", 0))

    def totals(self, key: str | None = None) -> dict:
        if key is not None:
            entry = self.keys.get(key) or _blank()
            return {
                "total": entry["total"]["in"] + entry["total"]["out"],
                "today": self.usage(key, "day"),
                "month": self.usage(key, "month"),
                "in": entry["total"]["in"],
                "out": entry["total"]["out"],
            }
        agg = {"total": 0, "today": 0, "month": 0, "in": 0, "out": 0}
        for name in self.keys:
            row = self.totals(name)
            for field in agg:
                agg[field] += row[field]
        return agg

    def prune(self, keep_days: int = KEEP_DAYS) -> None:
        """Drop daily buckets older than *keep_days* (monthly stay)."""
        cutoff = date.fromordinal(date.today().toordinal() - keep_days).isoformat()
        for entry in self.keys.values():
            for day in [d for d in entry["days"] if d < cutoff]:
                entry["days"].pop(day, None)
        self._dirty = True

    def snapshot(self) -> dict:
        return {
            "keys": {
                key: {
                    "total": value["total"]["in"] + value["total"]["out"],
                    "in": value["total"]["in"],
                    "out": value["total"]["out"],
                    "today": self.usage(key, "day"),
                    "month": self.usage(key, "month"),
                    "days": value["days"],
                }
                for key, value in self.keys.items()
            },
            "totals": self.totals(),
            "uptime": time.time() - self.started,
        }

    def rows(self, period: str = "month") -> list[dict]:
        out = []
        for key in sorted(self.keys):
            entry = self.keys[key]
            out.append({
                "key": key,
                "in": entry["total"]["in"],
                "out": entry["total"]["out"],
                "total": entry["total"]["in"] + entry["total"]["out"],
                "today": self.usage(key, "day"),
                "month": self.usage(key, "month"),
                "period": self.usage(key, "total" if period == "total" else period),
            })
        return out


class Quota:
    """``quota = "200G"`` + ``quota_period`` + ``quota_action``, resolved."""

    __slots__ = ("limit", "period", "action", "name")

    PERIODS = ("day", "month", "total")
    ACTIONS = ("block", "throttle", "warn")

    def __init__(self, limit: int = 0, period: str = "month", action: str = "block",
                 name: str = ""):
        self.limit = max(0, int(limit))
        self.period = period if period in self.PERIODS else "month"
        self.action = action if action in self.ACTIONS else "block"
        self.name = name

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def usage(self, account: TrafficAccount, key: str) -> int:
        return account.usage(key, self.period)

    def state(self, account: TrafficAccount, key: str) -> dict:
        used = self.usage(account, key)
        return {
            "limit": self.limit,
            "period": self.period,
            "action": self.action,
            "used": used,
            "remaining": max(0, self.limit - used),
            "exceeded": bool(self.limit) and used >= self.limit,
            "percent": round(100.0 * used / self.limit, 1) if self.limit else 0.0,
        }

    def exceeded(self, account: TrafficAccount, key: str) -> bool:
        return self.enabled and self.usage(account, key) >= self.limit

    def describe(self) -> str:
        if not self.enabled:
            return "unlimited"
        return f"{human_bytes(self.limit)}/{self.period} ({self.action})"


def quota_from(value, period: str = "month", action: str = "block",
               name: str = "") -> Quota:
    """Build a :class:`Quota` from a config value (size string or table)."""
    if value is None or value == "" or value is False:
        return Quota(0, period, action, name)
    if isinstance(value, dict):
        period = str(value.get("period", period))
        action = str(value.get("action", action))
        value = value.get("bytes", value.get("limit", value.get("quota", 0)))
    return Quota(parse_size(value), period, action, name)

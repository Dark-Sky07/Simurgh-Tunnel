"""Bandwidth limiting: one shared token bucket per mapping (or per token).

A bucket is filled with ``rate`` bytes every second up to ``burst`` bytes.  The
relay and the exit both use it, and both directions of a connection draw from
the same bucket, so ``rate_limit = "20M"`` really means 20 Mbit/s for that port
(10 Mbit/s each way when both directions run at once), exactly like frp's
``bandwidthLimit``.

The bucket is deliberately *soft*: when it is empty the caller pauses its
socket for a moment instead of dropping the connection, so a limited user sees
a slower link, never a broken one.
"""

from __future__ import annotations

import time

from .util import get_logger, parse_size

log = get_logger("simurgh.ratelimit")

#: never sleep longer than this in one go (keeps sockets responsive)
MAX_STALL = 0.25


class RateLimiter:
    """Token bucket over bytes, safe to share between many connections."""

    __slots__ = ("rate", "burst", "name", "_tokens", "_last", "waited")

    def __init__(self, rate: int, burst: int = 0, name: str = ""):
        self.rate = max(0, int(rate))
        #: a burst of one second by default, but never less than one max frame,
        #: otherwise a single 64 KiB frame could never be sent at low rates
        self.burst = max(int(burst or 0), self.rate, 64 * 1024) if self.rate else 0
        self.name = name
        self._tokens = float(self.burst)
        self._last = time.monotonic()
        self.waited = 0.0

    @property
    def enabled(self) -> bool:
        return self.rate > 0

    def _refill(self) -> None:
        now = time.monotonic()
        dt = now - self._last
        if dt <= 0:
            return
        self._last = now
        if self.rate:
            self._tokens = min(float(self.burst), self._tokens + dt * self.rate)

    def allow(self, nbytes: int) -> bool:
        """Consume *nbytes* if the bucket has them; never blocks."""
        if not self.rate or nbytes <= 0:
            return True
        self._refill()
        if self._tokens >= nbytes:
            self._tokens -= nbytes
            return True
        return False

    def poll(self, nbytes: int = 1) -> float:
        """Seconds until *nbytes* may pass (0.0 when they may pass now)."""
        if not self.rate:
            return 0.0
        self._refill()
        if self._tokens >= nbytes:
            return 0.0
        need = nbytes - self._tokens
        delay = need / self.rate
        self.waited += delay
        return min(delay, MAX_STALL)

    def take_now(self, nbytes: int) -> bool:
        """Force-consume (the debt is paid by the next refill)."""
        if not self.rate:
            return True
        self._refill()
        self._tokens -= nbytes
        return True

    def snapshot(self) -> dict:
        self._refill()
        return {
            "rate": self.rate,
            "burst": self.burst,
            "tokens": round(self._tokens, 1),
            "name": self.name,
        }


def limiter_from(value, name: str = "") -> RateLimiter | None:
    """Build a limiter from a config value.

    Accepts ``0``/``""``/``None`` (off), a number of bytes per second, a size
    string (``"20M"``, ``"500k"``) or a table ``{rate = "10M", burst = "2M"}``.
    """
    if value is None or value == "" or value is False:
        return None
    burst = 0
    if isinstance(value, dict):
        burst = parse_size(value.get("burst"))
        value = value.get("rate", 0)
    rate = parse_size(value)
    if rate <= 0:
        return None
    return RateLimiter(rate, burst, name=name)

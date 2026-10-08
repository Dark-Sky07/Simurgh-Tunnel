"""Traffic counters and rate sampling (what the panel draws)."""

from __future__ import annotations

import time
from collections import deque


class Counter:
    __slots__ = ("in_bytes", "out_bytes", "conns", "active", "errors", "last_error",
                 "last_conn")

    def __init__(self):
        self.in_bytes = 0
        self.out_bytes = 0
        self.conns = 0
        self.active = 0
        self.errors = 0
        self.last_error = ""
        self.last_conn = 0.0

    def as_dict(self) -> dict:
        return {
            "in_bytes": self.in_bytes,
            "out_bytes": self.out_bytes,
            "conns": self.conns,
            "active": self.active,
            "errors": self.errors,
            "last_error": self.last_error,
        }


class RateMeter:
    """Keeps one minute of samples and answers "how fast right now?"."""

    def __init__(self, window: float = 60.0, points: int = 120):
        self.window = window
        self.points = points
        self._samples: deque[tuple[float, int, int]] = deque(maxlen=points)
        self._last: tuple[float, int, int] | None = None

    def update(self, in_bytes: int, out_bytes: int) -> dict:
        now = time.time()
        if self._last is None:
            self._last = (now, in_bytes, out_bytes)
            self._samples.append((now, in_bytes, out_bytes))
            return {"in_rate": 0.0, "out_rate": 0.0, "in_total": in_bytes,
                    "out_total": out_bytes}
        prev_t, prev_in, prev_out = self._last
        dt = now - prev_t
        if dt >= 0.5:
            self._samples.append((now, in_bytes, out_bytes))
            self._last = (now, in_bytes, out_bytes)
        rate_dt = now - self._samples[0][0]
        if rate_dt <= 0:
            return {"in_rate": 0.0, "out_rate": 0.0, "in_total": in_bytes,
                    "out_total": out_bytes}
        oldest = self._samples[0]
        return {
            "in_rate": max(0.0, (in_bytes - oldest[1]) / rate_dt),
            "out_rate": max(0.0, (out_bytes - oldest[2]) / rate_dt),
            "in_total": in_bytes,
            "out_total": out_bytes,
        }

    def series(self) -> list[dict]:
        """Samples for the live chart: ``[{t, in, out}, ...]``."""
        out = []
        for t, i, o in self._samples:
            out.append({"t": round(t, 1), "in": i, "out": o})
        return out


class Stats:
    """Everything the panel wants to know, in one object."""

    def __init__(self):
        self.started = time.time()
        self.mappings: dict[str, Counter] = {}
        self.tunnel = Counter()
        self.meter = RateMeter()
        self.extra: dict = {}

    def mapping(self, key: str) -> Counter:
        c = self.mappings.get(key)
        if c is None:
            c = self.mappings[key] = Counter()
        return c

    def snapshot(self) -> dict:
        rates = self.meter.update(self.tunnel.in_bytes + self.tunnel.out_bytes * 0,
                                  self.tunnel.out_bytes)
        total_in = self.tunnel.in_bytes
        total_out = self.tunnel.out_bytes
        for c in self.mappings.values():
            total_in += c.in_bytes
            total_out += c.out_bytes
        snap = {
            "uptime": time.time() - self.started,
            "tunnel": {**self.tunnel.as_dict(), "uptime": time.time() - self.started},
            "mappings": {k: v.as_dict() for k, v in self.mappings.items()},
            "rates": rates,
            "series": self.meter.series(),
            "totals": {
                "in": total_in,
                "out": total_out,
                "sum": total_in + total_out,
                "conns": sum(c.conns for c in self.mappings.values())
                + self.tunnel.conns,
            },
        }
        snap.update(self.extra)
        return snap

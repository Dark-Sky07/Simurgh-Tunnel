"""Topology and history for the panel.

The old dashboard was a table of numbers refreshed every two seconds.  A map of
the tunnel and a chart that shows *what just happened* need two things the node
status does not have: a little history, and one place that turns "relay status"
or "exit status" or "the state file a supervised Go engine wrote" into the same
shape.

That translation is this module, and it is deliberately free of asyncio and of
the HTTP layer, so the whole map can be tested against plain dictionaries:

* :class:`History` keeps the last half hour of aggregate throughput, sampled on
  every panel poll (2 s) and downsampled for the wire (10 s resolution);
* :class:`RateTracker` turns the per-mapping byte counters into a rate, which is
  what the map animates;
* :func:`build` produces the single payload the UI draws: role, state, the
  peer(s) and their pool, the mappings with their quota and counters, the
  certificates in use, and the chart series.
"""

from __future__ import annotations

import time
from collections import deque

#: the client polls every 2 s; a sample is kept per poll ...
SAMPLE_SECONDS = 2.0
#: ... for half an hour, and sent to the browser at 10 s resolution
HISTORY_POINTS = 900
WIRE_POINTS = 180


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def human_bytes(value) -> str:
    """The panel prints sizes the way an operator reads them."""
    size = float(_float(value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


class History:
    """Aggregate throughput over the last half hour."""

    def __init__(self, points: int = HISTORY_POINTS,
                 sample_seconds: float = SAMPLE_SECONDS):
        self.points = points
        self.sample_seconds = sample_seconds
        self.samples: deque[tuple[float, float, float]] = deque(maxlen=points)
        self._last_at = 0.0

    def add(self, in_rate: float, out_rate: float, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if self.samples and now - self._last_at < self.sample_seconds:
            # keep the newest value in the last sample instead of adding one
            t, _in, _out = self.samples[-1]
            self.samples[-1] = (t, max(0.0, in_rate), max(0.0, out_rate))
            return
        self.samples.append((now, max(0.0, in_rate), max(0.0, out_rate)))
        self._last_at = now

    def chart(self, now: float | None = None) -> dict:
        """The series the browser draws, at 10 s resolution."""
        now = time.time() if now is None else now
        samples = list(self.samples)
        if not samples:
            return {"t": [], "in": [], "out": [], "window": 0.0}
        step = max(1, -(-len(samples) // WIRE_POINTS))     # ceil, so the cap holds
        picked = samples[::step]
        if picked[-1] is not samples[-1]:
            picked.append(samples[-1])
        base = picked[0][0]
        return {
            "t": [round(t - base, 1) for t, _i, _o in picked],
            "in": [round(i, 1) for _t, i, _o in picked],
            "out": [round(o, 1) for _t, _i, o in picked],
            "started": round(base, 1),
            "window": round(samples[-1][0] - base, 1),
        }

    def last(self) -> tuple[float, float]:
        if not self.samples:
            return 0.0, 0.0
        _t, i, o = self.samples[-1]
        return i, o


class RateTracker:
    """Per-mapping byte counters -> bytes per second."""

    def __init__(self, window: float = 10.0):
        self.window = window
        self._seen: dict[str, tuple[float, int, int]] = {}

    def rate(self, key: str, in_bytes: int, out_bytes: int,
             now: float | None = None) -> dict:
        now = time.time() if now is None else now
        previous = self._seen.get(key)
        self._seen[key] = (now, int(in_bytes), int(out_bytes))
        if previous is None:
            return {"in_rate": 0.0, "out_rate": 0.0}
        then, was_in, was_out = previous
        span = now - then
        if span <= 0.2:
            return {"in_rate": 0.0, "out_rate": 0.0}
        return {
            "in_rate": max(0.0, (int(in_bytes) - was_in) / span),
            "out_rate": max(0.0, (int(out_bytes) - was_out) / span),
        }

    def forget(self, keep: set[str]) -> None:
        for key in list(self._seen):
            if key not in keep:
                self._seen.pop(key, None)


def _state_of(status: dict, role: str) -> str:
    """``up`` / ``down`` / ``connecting`` from either role's status."""
    if role == "relay":
        if status.get("connected"):
            return "up"
        # a relay that never connected yet is "connecting"; one whose last
        # attempt failed is down -- with the error right beside it on the map
        return "down" if status.get("last_error") else "connecting"
    if _int(status.get("tunnel_count")) > 0 or _int(status.get("connections")) > 0:
        return "up"
    if status.get("connected_at"):
        return "down"
    return "connecting"


def _peer_of(status: dict, role: str) -> dict:
    """Who is on the other end of the tunnel, as far as this side knows."""
    if role == "relay":
        endpoint = status.get("current_exit") or ""
        info = status.get("exit_info") or {}
        carrier, _, rest = str(endpoint).partition("://")
        return {
            "endpoint": endpoint,
            "carrier": carrier or "",
            "address": rest,
            "name": info.get("name") or "",
            "version": info.get("version") or "",
            "uptime": _float(info.get("uptime")),
            "speedtest_port": _int(info.get("speedtest_port")),
            "role": "exit",
        }
    reach = list(status.get("reach_out") or status.get("targets") or [])
    listening = [row.get("endpoint", "") for row in status.get("listening") or []
                 if row.get("enabled")]
    return {
        "endpoint": reach[0] if reach else (listening[0] if listening else ""),
        "carrier": (reach[0].partition("://")[0] if reach else ""),
        "address": (reach[0].partition("://")[2] if reach else ""),
        "name": "",
        "version": "",
        "uptime": 0.0,
        "speedtest_port": _int(status.get("speedtest_port")),
        "listening": listening,
        "reach_out": reach,
        "role": "relay",
    }


def _health_rows(status: dict) -> list[dict]:
    """The health report, whichever shape the engine wrote it in.

    The Python relay publishes a list (``[{"endpoint": ..., "up": ...}]``), older
    ``state.json`` files and the Go engine a mapping, and ``/metrics`` has always
    accepted both -- the map should not be the only part that trips over it.
    """
    health = status.get("health")
    if isinstance(health, list):
        return [item for item in health if isinstance(item, dict)]
    if isinstance(health, dict):
        if isinstance(health.get("exits"), list):
            return [item for item in health["exits"] if isinstance(item, dict)]
        rows = []
        for endpoint, state in health.items():
            row = dict(state) if isinstance(state, dict) else {}
            row.setdefault("endpoint", endpoint)
            rows.append(row)
        return rows
    return []


def _pool_of(status: dict, role: str) -> list[dict]:
    """Every endpoint we know about, with the one in use marked."""
    if role == "relay":
        rows: list[dict] = []
        current = status.get("current_exit") or ""
        for endpoint in status.get("tunnel_targets") or []:
            if endpoint and all(row["endpoint"] != endpoint for row in rows):
                rows.append({"endpoint": endpoint, "state": "active"})
        for item in _health_rows(status):
            endpoint = item.get("endpoint") or item.get("address") or ""
            if not endpoint:
                continue
            row = next((r for r in rows if r["endpoint"] == endpoint), None)
            if row is None:
                row = {"endpoint": endpoint, "state": "standby"}
                rows.append(row)
            live = bool(item.get("up", True)) and not item.get("failed_until")
            # the relay names an endpoint "carrier://host:port"; an older state
            # file may have written just "host:port", so match on the tail too
            same = bool(current) and (endpoint == current
                                      or current.endswith("://" + endpoint)
                                      or endpoint.endswith("://" + current))
            row["state"] = "active" if (same and live) else "standby"
            if not live:
                row["state"] = "down"
            row["rtt_ms"] = item.get("rtt_ms")
            row["fails"] = _int(item.get("fails", item.get("failures")))
            row["label"] = item.get("label") or ""
        if not rows and current:
            rows.append({"endpoint": current, "state": "active"})
        return rows
    # an exit is dialled by one or more relays
    rows = []
    for peer in status.get("tunnels") or []:
        if not isinstance(peer, dict):
            continue
        rows.append({"endpoint": peer.get("peer", ""), "state": "active",
                     "streams": _int(peer.get("streams")),
                     "uptime": _float(peer.get("uptime"))})
    return rows


def _mappings_of(status: dict, role: str, tracker: RateTracker,
                 now: float, history: History) -> list[dict]:
    rows: list[dict] = []
    quotas = status.get("quotas") or {}
    traffic = {row.get("key"): row for row in (status.get("traffic") or [])}
    for row in status.get("mappings") or []:
        stats = row.get("stats") or {}
        key = row.get("key", "")
        in_bytes = _int(stats.get("in_bytes"))
        out_bytes = _int(stats.get("out_bytes"))
        rate = tracker.rate(key, in_bytes, out_bytes, now)
        account = traffic.get(key) or {}
        quota = quotas.get(key) or {}
        rows.append({
            "key": key,
            "name": row.get("name") or "",
            "listen": _int(row.get("listen")),
            "target": row.get("target") or "",
            "udp": bool(row.get("udp")),
            "enabled": bool(row.get("enabled", True)),
            "bound": bool(row.get("bound", True)),
            "listener": bool(row.get("listener")),       # a tunnel port, not a user port
            "active": _int(stats.get("active")),
            "conns": _int(stats.get("conns")),
            "errors": _int(stats.get("errors")),
            "last_error": stats.get("last_error") or "",
            "in_bytes": in_bytes,
            "out_bytes": out_bytes,
            "in_rate": round(rate["in_rate"], 1),
            "out_rate": round(rate["out_rate"], 1),
            "today": _int(account.get("today")),
            "month": _int(account.get("month")),
            "total": _int(account.get("total")),
            "quota": {
                "limit": _int(quota.get("limit")),
                "used": _int(quota.get("used", account.get("period"))),
                "period": quota.get("period") or "",
                "action": quota.get("action") or "",
                "exceeded": bool(quota.get("exceeded")),
            } if quota or account else None,
        })
    if role == "exit":
        # an exit has no user ports of its own: what it has is listeners, and
        # (in reverse mode) the relays it dials
        for item in status.get("listening") or []:
            rows.append({
                "key": f"listen:{item.get('endpoint', '')}",
                "name": item.get("carrier") or "",
                "listen": 0,
                "target": item.get("endpoint", ""),
                "udp": (item.get("carrier") == "udp"),
                "enabled": bool(item.get("enabled", True)),
                "bound": True, "active": 0, "conns": 0, "errors": 0,
                "last_error": "", "in_bytes": 0, "out_bytes": 0,
                "in_rate": 0.0, "out_rate": 0.0,
                "today": 0, "month": 0, "total": 0, "quota": None,
                "listener": True,
            })
    tracker.forget({row["key"] for row in rows})
    return rows


def build(status: dict, role: str, history: History, tracker: RateTracker,
          certificates: list[dict] | None = None, engine: str = "",
          now: float | None = None) -> dict:
    """One payload for the map, the cards and the chart."""
    now = time.time() if now is None else now
    node = status.get("node") or {}
    panel = status.get("panel") or {}
    stats = node.get("stats") or {}
    rates = stats.get("rates") or {}
    totals = stats.get("totals") or {}
    in_rate = _float(rates.get("in_rate"))
    out_rate = _float(rates.get("out_rate"))
    history.add(in_rate, out_rate, now)
    mappings = _mappings_of(node, role, tracker, now, history)
    return {
        "now": now,
        "role": role,
        "engine": engine,
        "state": _state_of(node, role),
        "name": node.get("name") or role,
        "dial": node.get("dial") or "relay",
        "uptime": _float(node.get("uptime")),
        "rtt_ms": node.get("rtt_ms"),
        "reconnects": _int(node.get("reconnects")),
        "last_error": node.get("last_error") or "",
        "bind_errors": list(node.get("bind_errors") or []),
        "blocked": _int(node.get("blocked")),
        "peer": _peer_of(node, role),
        "pool": _pool_of(node, role),
        "mappings": mappings,
        "certificates": certificates or [],
        "chart": history.chart(now),
        "rates": {"in": round(in_rate, 1), "out": round(out_rate, 1)},
        "totals": {
            "in": _int(totals.get("in")),
            "out": _int(totals.get("out")),
            "sum": _int(totals.get("sum", _int(totals.get("in")) + _int(totals.get("out")))),
            "conns": _int(totals.get("conns")),
            "today": sum(row["today"] for row in mappings),
            "month": sum(row["month"] for row in mappings),
            "mappings": len([row for row in mappings if not row.get("listener")]),
        },
        "panel": {
            "port": _int(panel.get("port")),
            "user": panel.get("user") or "",
            "requests": _int(panel.get("requests")),
            "lockouts": _int(panel.get("lockouts")),
            "failed_logins": _int(panel.get("failed_logins")),
        },
        "version": status.get("version") or "",
        "product": status.get("product") or "",
    }

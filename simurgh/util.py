"""Small shared helpers: logging, paths, ports, human-readable numbers."""

from __future__ import annotations

import logging
import os
import socket
import sys
from pathlib import Path

DEFAULT_HOME = "/etc/simurgh"


def default_home() -> Path:
    """Where configs/logs/run files live.

    ``$SIMURGH_HOME`` wins; otherwise ``/etc/simurgh`` for root and
    ``~/.simurgh`` for everybody else.
    """
    env = os.environ.get("SIMURGH_HOME")
    if env:
        return Path(env).expanduser()
    if os.geteuid() == 0:
        return Path(DEFAULT_HOME)
    return Path.home() / ".simurgh"


class Home:
    """Layout of a Simurgh installation."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser() if path else default_home()
        self.exit_cfg = self.path / "exit.toml"
        self.relay_cfg = self.path / "relay.toml"
        self.state = self.path / "state.json"
        self.logs = self.path / "logs"
        self.run = self.path / "run"
        self.backup = self.path / "backup"
        self.panel_log = self.logs / "panel.log"
        self.exit_log = self.logs / "exit.log"
        self.relay_log = self.logs / "relay.log"

    def ensure(self) -> "Home":
        for d in (self.path, self.logs, self.run):
            d.mkdir(parents=True, exist_ok=True)
        return self

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Home {self.path}>"


def setup_logging(verbose: bool = False, logfile: str | None = None,
                  name: str = "simurgh") -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if logfile:
        Path(logfile).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def get_logger(name: str = "simurgh") -> logging.Logger:
    return logging.getLogger(name)


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} PB"


def human_rate(bps: float) -> str:
    return human_bytes(bps) + "/s"


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def parse_host_port(s: str, default_port: int | None = None) -> tuple[str, int]:
    """Parse ``host:port`` (IPv6 in brackets supported)."""
    s = s.strip()
    if s.startswith("["):
        host, _, rest = s[1:].partition("]")
        port = rest.lstrip(":")
        if not port:
            if default_port is None:
                raise ValueError(f"expected [host]:port, got {s!r}")
            return host, default_port
        return host, int(port)
    host, sep, port = s.rpartition(":")
    if not sep or ":" in host:  # bare IPv6 without a port
        if default_port is None:
            raise ValueError(f"expected host:port, got {s!r}")
        return s, default_port
    return host, int(port)


def parse_port_list(s: str | list) -> list[int]:
    """``"443, 2053-2055"`` or ``"443 2053"`` -> ``[443, 2053, 2054, 2055]``."""
    if isinstance(s, list):
        items = [str(x) for x in s]
    else:
        import re

        items = [t for t in re.split(r"[,\s]+", str(s).strip()) if t]
    out: list[int] = []
    for item in items:
        if not item:
            continue
        if "-" in item:
            a, _, b = item.partition("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(item))
    return sorted(set(out))


def is_ip(host: str) -> bool:
    try:
        socket.inet_pton(socket.AF_INET, host)
        return True
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host)
        return True
    except OSError:
        return False


def free_port(port: int, host: str = "0.0.0.0") -> bool:
    """True when we can bind ``host:port`` right now."""
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def listening_tcp_ports() -> set[int]:
    """Ports this machine currently listens on (works without ``ss``/``lsof``)."""
    ports: set[int] = set()
    try:
        with open("/proc/net/tcp") as f:
            lines = f.readlines()[1:]
        for line in lines:
            parts = line.split()
            if len(parts) < 4 or parts[3] != "0A":  # 0A = LISTEN
                continue
            ports.add(int(parts[1].split(":")[1], 16))
    except OSError:
        pass
    try:
        with open("/proc/net/tcp6") as f:
            lines = f.readlines()[1:]
        for line in lines:
            parts = line.split()
            if len(parts) < 4 or parts[3] != "0A":
                continue
            ports.add(int(parts[1].split(":")[1], 16))
    except OSError:
        pass
    return ports


def local_ips() -> list[str]:
    """Best-effort list of this machine's addresses (used to detect the
    public/outgoing IP for setup links)."""
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 53))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def clamp(value, low, high):
    return max(low, min(high, value))

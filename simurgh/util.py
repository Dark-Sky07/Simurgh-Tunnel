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


def is_ipv6(host: str) -> bool:
    """True for a literal IPv6 address (``::1``, ``2001:db8::1``)."""
    try:
        socket.inet_pton(socket.AF_INET6, str(host).strip("[]"))
        return True
    except OSError:
        return False


def split_host_port(value: str, default_port: int = 0) -> tuple[str, int]:
    """``"1.2.3.4"``, ``"[2001:db8::1]:443"``, ``"::"`` or ``"::443"`` -> host, port."""
    text = str(value).strip()
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        return host, int(rest.lstrip(":") or default_port or 0)
    if text.count(":") > 1:                      # bare IPv6
        return text, default_port
    host, sep, port = text.rpartition(":")
    if not sep:
        return text, default_port
    return host, int(port or default_port)


def bind_host_of(address: str, default: str = "0.0.0.0") -> str:
    """The listen address part of a ``host:port`` / ``[v6]:port`` string."""
    host, _ = split_host_port(address, 0)
    return host or default


def resolve_hosts(host: str, port: int, prefer: str = "auto",
                  family: int = socket.AF_UNSPEC) -> list[tuple]:
    """``getaddrinfo`` with an IPv6 preference; a literal IP skips DNS.

    ``prefer`` is ``"auto"`` (whatever the resolver says, IPv6 last so a
    working IPv4 path is never delayed by a broken AAAA record), ``"ipv6"``
    (AAAA first, for hosts that only answer over IPv6) or ``"ipv4"``.
    """
    if is_ipv6(host):
        fam = socket.AF_INET6
    elif is_ip(host):
        fam = socket.AF_INET
    else:
        fam = family
    infos = socket.getaddrinfo(host, port, fam, socket.SOCK_STREAM)
    if prefer == "ipv6":
        infos.sort(key=lambda i: 0 if i[0] == socket.AF_INET6 else 1)
    elif prefer == "auto":
        infos.sort(key=lambda i: 1 if i[0] == socket.AF_INET6 else 0)
    elif prefer == "ipv4":
        infos.sort(key=lambda i: 0 if i[0] == socket.AF_INET else 1)
    return infos


def expand_port_spec(spec) -> list[tuple[int, int]]:
    """Expand a port specification into ``(listen, target)`` pairs.

    Everything an operator writes by hand works::

        "443"                     listen 443  -> target 443
        "443:8443"   "443=8443"   listen 443  -> target 8443
        "8080-8090"               8080..8090  -> same
        "8080-8090:9000-9010"     8080..8090  -> 9000..9010 (must match)
        "8080-8090=20000"         8080..8090  -> 20000.. (sequential)
        "443, 2053, 8080-8090"    several rules at once (any separator)
        [443, "8080-8090", "53=2053"]

    A dash range on one side only maps *sequentially* on the other, which is
    what ``443, 2053=53`` style configs mean in practice.
    """
    if spec is None:
        return []
    if isinstance(spec, bool):
        raise ValueError("a port specification cannot be a boolean")
    if isinstance(spec, int):
        return [(spec, spec)]
    if isinstance(spec, dict):
        raise ValueError("a port specification cannot be a table")
    if isinstance(spec, (list, tuple, set)):
        out: list[tuple[int, int]] = []
        for item in spec:
            out.extend(expand_port_spec(item))
        return _dedupe(out)

    text = str(spec).strip()
    if not text:
        return []
    if "," in text or " " in text or "\t" in text:
        return _dedupe(expand_port_spec([t for t in text.replace(",", " ").split() if t]))

    listen_part, target_part = text, ""
    for sep in ("=", ":"):
        if sep in text:
            listen_part, _, target_part = text.partition(sep)
            break

    listens = _port_range(listen_part)
    if not target_part:
        return [(p, p) for p in listens]

    targets = _port_range(target_part)
    if len(targets) == 1 and len(listens) > 1:
        # "8080-8090=20000" -> 20000, 20001, ...
        targets = list(range(targets[0], targets[0] + len(listens)))
    if len(targets) != len(listens):
        raise ValueError(
            f"{text!r}: {len(listens)} listen port(s) but {len(targets)} target port(s)"
        )
    return _dedupe(list(zip(listens, targets)))


def _port_range(part: str) -> list[int]:
    text = part.strip()
    if not text:
        raise ValueError("empty port")
    if "-" in text:
        low, _, high = text.partition("-")
        start, end = int(low), int(high)
        if start > end:
            raise ValueError(f"reversed port range {text!r}")
        return list(range(start, end + 1))
    return [int(text)]


def _dedupe(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    seen: set[int] = set()
    out: list[tuple[int, int]] = []
    for listen, target in pairs:
        if not (0 < listen < 65536 and 0 < target < 65536):
            raise ValueError(f"port out of range: {listen}->{target}")
        if listen in seen:
            continue
        seen.add(listen)
        out.append((listen, target))
    return out


def parse_size(value) -> int:
    """``"10M"``, ``"500k"``, ``"2G"`` or an int -> bytes (0 = unlimited)."""
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    text = str(value).strip().lower().replace(" ", "")
    if not text:
        return 0
    mult = 1
    for suffix, factor in (("kib", 1024), ("kb", 1024), ("k", 1024),
                           ("mib", 1024 ** 2), ("mb", 1024 ** 2), ("m", 1024 ** 2),
                           ("gib", 1024 ** 3), ("gb", 1024 ** 3), ("g", 1024 ** 3),
                           ("tib", 1024 ** 4), ("tb", 1024 ** 4), ("t", 1024 ** 4)):
        if text.endswith(suffix):
            mult = factor
            text = text[: -len(suffix)]
            break
    try:
        return int(float(text) * mult)
    except ValueError as exc:
        raise ValueError(f"cannot read a size from {value!r}") from exc


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


async def dial_stream(loop, factory, host: str, port: int, *, nodelay: bool = True,
                      keepalive: int = 0, so_mark: int = 0, prefer: str = "auto",
                      local_addr=None):
    """``loop.create_connection`` with socket options and address fallback.

    The high level API only grew ``sock_opts`` in Python 3.13, and it has no
    opinion about which family to try first, so the socket is made here: picked
    in the configured preference order, tuned, then handed to asyncio.
    """
    import asyncio

    infos = resolve_hosts(host, port, prefer)
    if not infos:
        raise OSError(f"cannot resolve {host}:{port}")
    last: Exception | None = None
    for family, kind, proto, _canon, sockaddr in infos:
        sock = socket.socket(family, kind, proto)
        try:
            sock.setblocking(False)
            apply_socket_opts(sock, nodelay=nodelay, keepalive=keepalive,
                              so_mark=so_mark)
            if local_addr:
                sock.bind(local_addr)
            await loop.sock_connect(sock, sockaddr)
        except OSError as exc:
            last = exc
            sock.close()
            continue
        try:
            return await loop.create_connection(factory, sock=sock)
        except Exception as exc:                       # pragma: no cover
            last = exc
            sock.close()
    raise last or OSError(f"cannot connect to {host}:{port}")


def apply_socket_opts(sock, *, nodelay: bool = True, keepalive: int = 0,
                      so_mark: int = 0) -> None:
    """Socket tuning an operator can actually reach from the config file.

    * ``nodelay`` -- Nagle off (the default): one small write goes out now
      instead of waiting for company, which is what interactivity needs.
    * ``keepalive`` -- seconds before the kernel starts probing a silent peer;
      0 keeps the system default.
    * ``so_mark`` -- policy-routing mark (Linux, needs CAP_NET_ADMIN); the way
      to send the tunnel out of a specific interface or routing table.
    """
    if sock is None:
        return
    import socket as _socket

    if nodelay:
        try:
            sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
        except OSError:
            pass
    if keepalive:
        try:
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        for option, value in (("TCP_KEEPIDLE", keepalive),
                              ("TCP_KEEPINTVL", max(1, keepalive // 3))):
            opt = getattr(_socket, option, None)
            if opt is None:
                continue
            try:
                sock.setsockopt(_socket.IPPROTO_TCP, opt, value)
            except OSError:
                pass
    if so_mark:
        mark_opt = getattr(_socket, "SO_MARK", None)
        if mark_opt is not None:
            try:
                sock.setsockopt(_socket.SOL_SOCKET, mark_opt, int(so_mark))
            except OSError:
                pass


def set_dual_stack(sock, dual: bool = True) -> None:
    """``IPV6_V6ONLY = 0`` on a ``::`` listener: one socket, both families."""
    if sock is None:
        return
    import socket as _socket

    try:
        if sock.family == _socket.AF_INET6:
            sock.setsockopt(_socket.IPPROTO_IPV6, _socket.IPV6_V6ONLY,
                            0 if dual else 1)
    except (OSError, AttributeError):
        pass

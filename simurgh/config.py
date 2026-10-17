"""Configuration: TOML in, dataclasses out, TOML back again.

Two roles:

``exit.toml``   runs on the foreign server (Kharej) -- where the VPN panels
                live.  It listens for the tunnel and dials whatever the relay
                asks for.
``relay.toml``  runs on the Iranian server.  It listens for *users* and pushes
                their traffic into the tunnel.
"""

from __future__ import annotations

import os
import secrets

try:  # the standard library has a TOML reader from Python 3.11 on
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.9/3.10 (Debian 11/12, Ubuntu 20.04/22.04)
    from . import _tomli as tomllib  # bundled copy with the same API

from dataclasses import dataclass, field
from pathlib import Path

from .util import expand_port_spec

CARRIERS = ("tls", "wss", "raw", "plain", "udp")

#: what a mapping accepts on its listen port
#:   tcp     ordinary TCP forwarding (the default)
#:   udp     datagrams (WireGuard, DNS, games); if the tunnel runs over the
#:           ``udp`` carrier the packets keep their own timing
#:   socks5  a SOCKS5 proxy on the Iranian side -- the client picks the target
#:   http    an HTTP CONNECT proxy on the Iranian side
MODES = ("tcp", "udp", "socks5", "http")

#: which address family to prefer when dialing out ("auto" = IPv4 first, with
#: IPv6 as a fallback; "ipv6" = try AAAA first, for IPv6-only paths)
PREFER = ("auto", "ipv4", "ipv6")

#: what a stranger gets on a mapped port (or on a tunnel port with an unknown
#: SNI): the decoy website, or a silent close
FALLBACKS = ("decoy", "close")

#: which data plane runs the relay/exit role. Both speak the same wire
#: protocol, so switching is a config change (see docs/CONFIGURATION.md).
#: The compiled engine is the default: it uses every core and costs about an
#: eighth of the CPU per gigabyte, which is what busy relays need. When its
#: binary is missing the CLI says so and runs the Python engine instead.
#: Transfer defaults for the compiled engine.  Its window growth is rate based
#: (see Mux._autotune), so a small starting window never grows on a fast path;
#: the Python engine grows happily from 256 KiB and keeps its memory smaller.
GO_STREAM_WINDOW = 1024 * 1024
GO_MAX_STREAM_WINDOW = 8 * 1024 * 1024

ENGINES = ("python", "go")
DEFAULT_ENGINE = "go"
PROXY_PROTOCOLS = ("off", "v1", "v2")
LOG_LEVELS = ("debug", "info", "warning", "error")

DEFAULT_PORTS = [443, 2053, 2083, 2087, 2096, 8443]


class ConfigError(Exception):
    pass


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


# --------------------------------------------------------------------------
# TOML writing (stdlib has no writer)
# --------------------------------------------------------------------------


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"cannot serialise {v!r}")


def toml_dumps(data: dict) -> str:
    """A small but correct TOML writer for the shapes we produce."""
    lines: list[str] = []
    simple = {k: v for k, v in data.items() if not isinstance(v, dict)
              and not (isinstance(v, list) and v and isinstance(v[0], dict))}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    arrays = {k: v for k, v in data.items()
              if isinstance(v, list) and v and isinstance(v[0], dict)}

    for k, v in simple.items():
        lines.append(f"{k} = {_toml_value(v)}")
    for k, v in arrays.items():
        for item in v:
            lines.append("")
            lines.append(f"[[{k}]]")
            for ik, iv in item.items():
                if iv is None:
                    continue
                if isinstance(iv, dict):
                    lines.append(f"[{k}.{ik}]")
                    for iik, iiv in iv.items():
                        lines.append(f"{iik} = {_toml_value(iiv)}")
                else:
                    lines.append(f"{ik} = {_toml_value(iv)}")
    for k, v in tables.items():
        lines.append("")
        lines.append(f"[{k}]")
        for ik, iv in v.items():
            if iv is None:
                continue
            lines.append(f"{ik} = {_toml_value(iv)}")
    return "\n".join(lines).strip() + "\n"


# --------------------------------------------------------------------------
# exit node (foreign / Kharej server)
# --------------------------------------------------------------------------


@dataclass
class ListenSpec:
    """One tunnel endpoint of an exit node.

    Normally this is a socket the exit *listens* on and the relay dials.  When
    ``dial`` holds a host name (reverse mode, ``relay dial = "exit"``) the exit
    dials that address instead -- the Iranian relay is then the listener.
    """

    carrier: str = "tls"
    host: str = "0.0.0.0"
    port: int = 8443
    path: str = "/ws"
    fallback: str = "decoy"
    decoy_file: str | None = None
    padding: bool = True
    split: int = 0                       # tls/wss: first TCP segment size of the hello
    enabled: bool = True
    dial: str = ""                       # reverse mode: dial this host
    fingerprint: str | None = None       # pin the relay certificate (reverse)
    insecure_skip_verify: bool = False
    #: tls/wss only: a certificate just for this listener (the Go engine has
    #: always accepted this; the Python engine now does too)
    cert_file: str | None = None
    key_file: str | None = None
    #: reverse mode: reach the relay through this upstream proxy
    proxy: str = ""
    prefer_ipv6: str = "auto"            # auto | ipv4 | ipv6
    #: udp carrier: datagram payload cap and FEC redundancy (see docs)
    mtu: int = 1200
    fec: str = "off"                     # off | "3", "10:2" (10 data + 2 parity)
    #: udp carrier: omit the handshake ACK-less retransmits tuning
    window: int = 1024                   # packets in flight (udp carrier)

    def endpoint(self) -> str:
        if self.dial:
            return f"{self.carrier}->{self.dial}:{self.port}"
        return f"{self.carrier}://{self.host}:{self.port}"

    @property
    def reverse(self) -> bool:
        return bool(self.dial)


@dataclass
class TunnelToken:
    """One credential a tunnel may authenticate with.

    ``token = "..."`` in a config means "one token, everything allowed" and
    keeps working exactly as before.  ``[[token]]`` blocks turn one exit into a
    multi-tenant one: several Iranian relays, each with its own token, its own
    allowed ports and its own limits.  The token is *also* the encryption key
    of the handshake, so a relay cannot reach ports it was not granted -- the
    exit refuses the stream.
    """

    token: str = ""
    name: str = ""
    #: ports this token may reach on the exit; empty = every port
    ports: list[int] = field(default_factory=list)
    #: "127.0.0.1:8443" style target restrictions (empty = any target)
    targets: list[str] = field(default_factory=list)
    #: may this token open SOCKS5/HTTP style streams (arbitrary destinations)?
    allow_any_target: bool = False
    rate_limit: str | int = 0            # bytes/s, "20M", or a {rate,burst} table
    quota: str | int = 0                 # bytes per quota_period
    quota_period: str = "month"          # day | month | total
    quota_action: str = "block"          # block | throttle | warn
    max_conns: int = 0                   # concurrent user connections (0 = no limit)
    enabled: bool = True

    def key(self) -> str:
        return f"token:{self.name or self.token[:6]}"

    def allows_port(self, port: int) -> bool:
        return not self.ports or port in self.ports

    def allows_target(self, host: str, port: int) -> bool:
        if self.allow_any_target or not self.targets:
            return True
        want = {f"{host}:{port}", f"*:{port}", f"{host}:*", "*:*"}
        return bool(want & {t.strip() for t in self.targets})

    def describe(self) -> str:
        ports = ",".join(str(p) for p in self.ports) if self.ports else "all ports"
        return f"{self.name or 'token'} [{ports}]"


def _tokens_from(data: dict, default_token: str = "") -> list[TunnelToken]:
    """Read the token list of a config (``[[token]]`` blocks + the plain one)."""
    out: list[TunnelToken] = []
    raw = data.get("token")
    if isinstance(raw, list):
        for item in raw:
            out.append(TunnelToken(token=str(item), name=""))
    elif isinstance(raw, str) and raw:
        out.append(TunnelToken(token=raw, name=str(data.get("token_name") or "")))
    for item in data.get("tokens") or data.get("token_block") or []:
        if isinstance(item, str):
            out.append(TunnelToken(token=item))
            continue
        if not isinstance(item, dict):
            continue
        out.append(TunnelToken(
            token=str(item.get("token") or ""),
            name=str(item.get("name") or ""),
            ports=[int(x) for x in _as_list(item.get("ports"))] if item.get("ports") else [],
            targets=[str(x) for x in _as_list(item.get("targets"))],
            allow_any_target=bool(item.get("allow_any_target", False)),
            rate_limit=item.get("rate_limit", 0) or 0,
            quota=item.get("quota", 0) or 0,
            quota_period=str(item.get("quota_period", "month")),
            quota_action=str(item.get("quota_action", "block")),
            max_conns=int(item.get("max_conns", 0) or 0),
            enabled=bool(item.get("enabled", True)),
        ))
    if default_token and not any(t.token == default_token for t in out):
        out.insert(0, TunnelToken(token=default_token))
    return [t for t in out if t.token]


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


@dataclass
class TunnelSpec:
    """The socket the *relay* listens on when the exit dials it (reverse mode)."""

    carrier: str = "tls"
    host: str = "0.0.0.0"
    port: int = 8443
    path: str = "/ws"
    fallback: str = "decoy"
    decoy_file: str | None = None
    cert_file: str | None = None
    key_file: str | None = None
    cert_auto: bool = True
    padding: bool = True
    enabled: bool = True
    #: udp carrier tuning (reverse mode listens on a datagram socket)
    mtu: int = 1200
    fec: str = "off"
    window: int = 1024

    def endpoint(self) -> str:
        return f"{self.carrier}://{self.host}:{self.port}"


@dataclass
class ExitConfig:
    token: str = ""
    tokens: list[TunnelToken] = field(default_factory=list)
    cert_file: str | None = None
    key_file: str | None = None
    cert_auto: bool = True
    name: str = ""
    listen: list[ListenSpec] = field(default_factory=list)
    allow_ips: list[str] = field(default_factory=list)
    push_ports: list[int] = field(default_factory=list)
    push_listen_host: str = "0.0.0.0"
    push_enabled: bool = True
    proxy_protocol: str = "off"
    strict_ports: bool = False
    #: let a token dial anything, not just the pushed/known ports (SOCKS5 mode)
    allow_any_target: bool = False
    speedtest_port: int = 8808
    log_level: str = "info"
    #: flow control: base receive window per stream, and the ceiling the
    #: adaptive window may grow to while a stream keeps draining fast
    stream_window: int = 256 * 1024
    max_stream_window: int = 8 * 1024 * 1024
    chunk: int = 65536
    #: reverse mode: how many tunnel connections this exit opens to the relay
    connections: int = 1
    #: data plane: "python" (works everywhere) or "go" (much faster)
    engine: str = DEFAULT_ENGINE
    #: sockets: Nagle off (lower latency, the default), TCP keepalive seconds,
    #: SO_MARK for policy routing, preferring IPv6 when dialing targets
    tcp_nodelay: bool = True
    tcp_keepalive: int = 0
    so_mark: int = 0
    prefer_ipv6: str = "auto"
    #: accept IPv4 and IPv6 on a "::" listener (Linux dual-stack)
    dual_stack: bool = True
    #: limits that apply to the whole exit
    rate_limit: str | int = 0
    quota: str | int = 0
    quota_period: str = "month"
    quota_action: str = "block"
    max_conns: int = 0
    #: alerts
    telegram_token: str = ""
    telegram_chat: str = ""
    alert_webhook: str = ""
    alert_events: list[str] = field(default_factory=list)
    #: Telegram Bot API root (default https://api.telegram.org; point it at a
    #: local Bot API server when one runs next to this node)
    telegram_api: str = ""

    def token_list(self) -> list[TunnelToken]:
        """Every credential this exit accepts (never empty)."""
        if self.tokens:
            return [t for t in self.tokens if t.enabled]
        return [TunnelToken(token=self.token)] if self.token else []

    def token_by_value(self, token: str) -> TunnelToken | None:
        for item in self.token_list():
            if item.token == token:
                return item
        return None

    def to_dict(self) -> dict:
        d = {
            "token": self.token,
            "name": self.name,
            "engine": self.engine,
            "cert_auto": self.cert_auto,
            "log_level": self.log_level,
            "push_enabled": self.push_enabled,
            "strict_ports": self.strict_ports,
            "speedtest_port": self.speedtest_port,
            "stream_window": self.stream_window,
            "max_stream_window": self.max_stream_window,
            "chunk": self.chunk,
            "connections": self.connections,
        }
        if self.tokens and len(self.tokens) > 1:
            # a multi-tenant exit: the first token stays in `token` so an older
            # relay keeps working, the rest are written as [[token]] blocks
            d["token"] = self.tokens[0].token
        if self.cert_file:
            d["cert_file"] = self.cert_file
        if self.key_file:
            d["key_file"] = self.key_file
        if self.allow_ips:
            d["allow_ips"] = self.allow_ips
        if self.push_ports:
            d["push_ports"] = self.push_ports
        if self.allow_any_target:
            d["allow_any_target"] = True
        if self.proxy_protocol and self.proxy_protocol != "off":
            d["proxy_protocol"] = self.proxy_protocol
        for key in ("tcp_nodelay", "tcp_keepalive", "so_mark", "prefer_ipv6",
                    "dual_stack", "rate_limit", "quota", "quota_period",
                    "quota_action", "max_conns", "telegram_token",
                    "telegram_chat", "alert_webhook", "alert_events",
                    "telegram_api"):
            value = getattr(self, key)
            if value not in (0, "", None, [], True, "auto", "month", "block"):
                d[key] = value
        if self.tcp_nodelay is False:
            d["tcp_nodelay"] = False
        if self.dual_stack is False:
            d["dual_stack"] = False
        if self.tokens and len(self.tokens) > 1:
            d["tokens"] = [
                {k: v for k, v in {
                    "token": t.token, "name": t.name, "ports": t.ports or None,
                    "targets": t.targets or None,
                    "allow_any_target": t.allow_any_target or None,
                    "rate_limit": t.rate_limit or None,
                    "quota": t.quota or None,
                    "quota_period": t.quota_period if t.quota else None,
                    "quota_action": t.quota_action if t.quota else None,
                    "max_conns": t.max_conns or None,
                    "enabled": t.enabled if not t.enabled else None,
                }.items() if v is not None}
                for t in self.tokens
            ]
        d["listen"] = [
            {k: v for k, v in {
                "carrier": ls.carrier, "host": ls.host, "port": ls.port,
                "path": ls.path if ls.carrier in ("wss", "ws") else None,
                "fallback": ls.fallback,
                "decoy_file": ls.decoy_file,
                "padding": ls.padding, "split": ls.split,
                "enabled": ls.enabled,
                "dial": ls.dial or None,
                "proxy": ls.proxy or None,
                "prefer_ipv6": ls.prefer_ipv6 if ls.prefer_ipv6 != "auto" else None,
                "fingerprint": ls.fingerprint,
                "insecure_skip_verify": ls.insecure_skip_verify or None,
                "cert_file": ls.cert_file,
                "key_file": ls.key_file,
                "mtu": ls.mtu if ls.carrier == "udp" else None,
                "fec": ls.fec if ls.carrier == "udp" and ls.fec != "off" else None,
                "window": ls.window if ls.carrier == "udp" else None,
            }.items() if v is not None}
            for ls in self.listen
        ]
        return d


def load_exit(path: str | Path) -> ExitConfig:
    data = _read_toml(path)
    cfg = ExitConfig(token=str(data.get("token") or ""))
    cfg.tokens = _tokens_from(data, cfg.token)
    if cfg.tokens:
        cfg.token = cfg.tokens[0].token
    if not cfg.token:
        raise ConfigError("exit config: 'token' is required")
    cfg.cert_file = data.get("cert_file")
    cfg.key_file = data.get("key_file")
    cfg.cert_auto = bool(data.get("cert_auto", True))
    cfg.name = str(data.get("name") or "")
    cfg.allow_ips = [str(x) for x in data.get("allow_ips", [])]
    cfg.push_ports = [int(x) for x in data.get("push_ports", [])]
    cfg.push_listen_host = str(data.get("push_listen_host", "0.0.0.0"))
    cfg.push_enabled = bool(data.get("push_enabled", True))
    cfg.strict_ports = bool(data.get("strict_ports", False))
    cfg.allow_any_target = bool(data.get("allow_any_target", False))
    cfg.speedtest_port = int(data.get("speedtest_port", 8808))
    cfg.proxy_protocol = str(data.get("proxy_protocol", "off"))
    cfg.log_level = str(data.get("log_level", "info"))
    cfg.chunk = max(4096, min(1024 * 1024, int(data.get("chunk", 65536))))
    cfg.engine = _engine_from(data)
    cfg.tcp_nodelay = bool(data.get("tcp_nodelay", True))
    cfg.tcp_keepalive = max(0, int(data.get("tcp_keepalive", 0) or 0))
    cfg.so_mark = max(0, int(data.get("so_mark", 0) or 0))
    cfg.prefer_ipv6 = _prefer_from(data.get("prefer_ipv6"))
    cfg.dual_stack = bool(data.get("dual_stack", True))
    cfg.rate_limit = data.get("rate_limit", 0) or 0
    cfg.quota = data.get("quota", 0) or 0
    cfg.quota_period = _period_from(data.get("quota_period"))
    cfg.quota_action = _quota_action_from(data.get("quota_action"))
    cfg.max_conns = max(0, int(data.get("max_conns", 0) or 0))
    cfg.telegram_token = str(data.get("telegram_token") or "")
    cfg.telegram_chat = str(data.get("telegram_chat") or "")
    cfg.alert_webhook = str(data.get("alert_webhook") or data.get("webhook") or "")
    cfg.alert_events = [str(x) for x in _as_list(data.get("alert_events"))]
    cfg.telegram_api = str(data.get("telegram_api") or "")
    cfg.stream_window = max(16 * 1024,
                            int(data.get("stream_window", _default_start_window(cfg.engine))))
    cfg.max_stream_window = max(cfg.stream_window,
                                int(data.get("max_stream_window",
                                             _default_max_window(cfg.engine))))
    cfg.connections = max(1, min(16, int(data.get("connections", 1))))
    listen = data.get("listen") or []
    for item in listen:
        if isinstance(item, str):
            carrier, _, rest = item.partition("://")
            host, _, port = rest.rpartition(":")
            cfg.listen.append(ListenSpec(carrier=carrier, host=host or "0.0.0.0",
                                         port=int(port)))
            continue
        carrier = str(item.get("carrier", "tls")).lower()
        if carrier not in CARRIERS:
            raise ConfigError(f"unknown carrier {carrier!r} in listen block")
        cfg.listen.append(ListenSpec(
            carrier=carrier,
            host=str(item.get("host", "0.0.0.0")),
            port=int(item.get("port", 8443)),
            path=str(item.get("path", "/ws")),
            fallback=str(item.get("fallback", "decoy")),
            decoy_file=item.get("decoy_file"),
            padding=bool(item.get("padding", True)),
            split=int(item.get("split", 0) or 0),
            enabled=bool(item.get("enabled", True)),
            dial=str(item.get("dial") or ""),
            fingerprint=item.get("fingerprint"),
            insecure_skip_verify=bool(item.get("insecure_skip_verify", False)),
            cert_file=item.get("cert_file"),
            key_file=item.get("key_file"),
            proxy=str(item.get("proxy") or ""),
            prefer_ipv6=_prefer_from(item.get("prefer_ipv6")),
            mtu=max(512, min(65000, int(item.get("mtu", 1200) or 1200))),
            fec=str(item.get("fec", "off") or "off"),
            window=max(32, min(65536, int(item.get("window", 1024) or 1024))),
        ))
    if not cfg.listen:
        raise ConfigError("exit config: at least one [[listen]] block is required")
    listeners = [ls for ls in cfg.listen if not ls.reverse]
    for ls in listeners:
        if ls.cert_file and not ls.key_file:
            raise ConfigError(f"[listen] port {ls.port}: cert_file needs key_file")
        if ls.key_file and not ls.cert_file:
            raise ConfigError(f"[listen] port {ls.port}: key_file needs cert_file")
    if any(ls.carrier in ("tls", "wss") for ls in listeners):
        have = [(cfg.cert_file and cfg.key_file)
                or (ls.cert_file and ls.key_file) for ls in listeners
                if ls.carrier in ("tls", "wss")]
        if not all(have) and not cfg.cert_auto:
            raise ConfigError("carrier tls/wss needs a certificate (or cert_auto = true)")
    if cfg.proxy_protocol not in PROXY_PROTOCOLS:
        raise ConfigError(f"proxy_protocol must be one of {PROXY_PROTOCOLS}")
    _validate_listen_ports(cfg.listen)
    return cfg


def save_exit(cfg: ExitConfig, path: str | Path) -> None:
    _write_toml(ExitConfig.to_dict(cfg), path)


# --------------------------------------------------------------------------
# relay node (Iranian server)
# --------------------------------------------------------------------------


@dataclass
class Mapping:
    """One port the users connect to, and where it should end up.

    ``sni`` turns the mapping into a *shared* port: several mappings may sit on
    the same ``listen`` port as long as each names the TLS server name it
    answers (``sni = "panel-a.example.com"``).  The relay reads the ClientHello,
    picks the mapping that matches, and gives everything else to the mapping
    without an ``sni`` -- or to the decoy site when there is none.  That is the
    "one public port, many customers" trick (port reuse).
    """

    name: str = ""
    listen: int = 0
    target_port: int = 0
    target_host: str = "127.0.0.1"
    listen_host: str = "0.0.0.0"
    udp: bool = False
    proxy_protocol: str = "off"
    enabled: bool = True
    #: "tcp" (default), "udp", "socks5", "http"
    mode: str = "tcp"
    #: TLS server name this mapping answers on a shared port (port reuse)
    sni: str | None = None
    #: "decoy" (serve the fake website) or "close" for traffic that matches
    #: nothing on a shared port
    fallback: str = "decoy"
    #: group name of the rule this mapping came from (a range expands into
    #: one mapping per port, all sharing the group name)
    group: str = ""
    #: pin this mapping to one exit: the endpoint's ``label`` (or its address)
    exit: str = ""
    #: limits
    rate_limit: str | int = 0            # bytes/s or "20M"
    quota: str | int = 0
    quota_period: str = "month"
    quota_action: str = "block"
    max_conns: int = 0                   # concurrent user connections (0 = no limit)
    #: SOCKS5/HTTP modes: restrict which targets the client may ask for
    allow_targets: list[str] = field(default_factory=list)

    def key(self) -> str:
        """Unique per listen address *and* protocol: TCP 443 and UDP 443 can
        coexist on the relay."""
        proto = self.mode if self.mode != "tcp" else ("udp" if self.udp else "tcp")
        suffix = f"#{self.sni}" if self.sni else ""
        return (f"{proto}:{self.listen_host}:{self.listen}{suffix}/"
                f"{self.target_host}:{self.target_port}")

    @property
    def is_datagram(self) -> bool:
        return self.mode == "udp" or self.udp

    @property
    def is_proxy(self) -> bool:
        return self.mode in ("socks5", "http")

    def describe(self) -> str:
        where = f"{self.listen_host}:{self.listen}"
        if self.sni:
            where += f" (sni {self.sni})"
        if self.is_proxy:
            return f"{where} -> {self.mode} proxy"
        return f"{where} -> {self.target_host}:{self.target_port}"


@dataclass
class ExitEndpoint:
    carrier: str = "tls"
    address: str = ""
    port: int = 8443
    domain: str | None = None
    path: str = "/ws"
    fingerprint: str | None = None
    insecure_skip_verify: bool = False
    padding: bool = True
    split: int = 0                       # tls/wss: first TCP segment size of the hello
    enabled: bool = True
    #: reach this exit through an upstream proxy (socks5://user:pass@host:port)
    proxy: str = ""
    #: address family preference while dialing (auto | ipv4 | ipv6)
    prefer_ipv6: str = "auto"
    #: per-endpoint socket knobs; ``None`` means "take the relay-wide setting"
    tcp_nodelay: bool | None = None
    tcp_keepalive: int | None = None
    so_mark: int | None = None
    #: udp carrier tuning
    mtu: int = 1200
    fec: str = "off"
    window: int = 1024
    #: a label shown in the panel (the foreign server's name, say)
    label: str = ""

    def describe(self) -> str:
        name = f" ({self.label})" if self.label else ""
        return f"{self.carrier}://{self.address}:{self.port}{name}"


@dataclass
class RelayConfig:
    token: str = ""
    name: str = ""
    exit: ExitEndpoint = field(default_factory=ExitEndpoint)
    pool: list[ExitEndpoint] = field(default_factory=list)
    mappings: list[Mapping] = field(default_factory=list)
    accept_push: bool = True
    keepalive: int = 25
    stream_window: int = 256 * 1024
    max_stream_window: int = 8 * 1024 * 1024
    chunk: int = 65536
    #: how many tunnel connections to keep open to the exit (1 = one tunnel).
    #: Several connections beat a single TCP flow on a long, lossy path and
    #: they spread the head-of-line blocking of a busy tunnel.
    connections: int = 1
    speedtest_port: int = 0
    log_level: str = "info"
    panel_port: int = 8787
    #: who dials the tunnel: "relay" (we connect out, default) or "exit"
    #: (the foreign server connects to us -- reverse mode).
    dial: str = "relay"
    tunnel: TunnelSpec = field(default_factory=TunnelSpec)
    #: data plane: "python" (works everywhere) or "go" (much faster)
    engine: str = DEFAULT_ENGINE
    #: reverse mode: the credentials we accept from foreign exits
    tokens: list[TunnelToken] = field(default_factory=list)
    #: sockets
    tcp_nodelay: bool = True
    tcp_keepalive: int = 0
    so_mark: int = 0
    prefer_ipv6: str = "auto"
    dual_stack: bool = True
    #: dial the exit through this upstream proxy (socks5://user:pass@host:port
    #: or http://host:port); a per-exit ``proxy`` overrides it
    proxy: str = ""
    #: limits for the whole relay (and the default for mappings without one)
    rate_limit: str | int = 0
    quota: str | int = 0
    quota_period: str = "month"
    quota_action: str = "block"
    max_conns: int = 0
    #: alerts
    telegram_token: str = ""
    telegram_chat: str = ""
    alert_webhook: str = ""
    alert_events: list[str] = field(default_factory=list)
    telegram_api: str = ""
    #: panel
    panel_tls: bool = False
    panel_host: str = "0.0.0.0"
    #: installed but not paired yet: a relay can be set up in one command and
    #: told where its exit is later (``simurgh join '<link>'`` or the panel).
    #: Only such a config may carry an empty token and no exit address.
    unpaired: bool = False

    @property
    def reverse(self) -> bool:
        return self.dial == "exit"

    @property
    def paired(self) -> bool:
        """True when the relay knows where to dial and with which secret."""
        if self.unpaired or not self.token:
            return False
        return self.reverse or bool(self.endpoints())

    def endpoints(self) -> list[ExitEndpoint]:
        """Primary first, then the failover pool."""
        out = [e for e in [self.exit] + self.pool if e.enabled and e.address]
        return out

    def token_list(self) -> list[TunnelToken]:
        if self.tokens:
            return [t for t in self.tokens if t.enabled]
        return [TunnelToken(token=self.token)] if self.token else []

    def to_dict(self) -> dict:
        d = {
            "token": self.token,
            "name": self.name,
            "engine": self.engine,
            "accept_push": self.accept_push,
            "keepalive": self.keepalive,
            "stream_window": self.stream_window,
            "max_stream_window": self.max_stream_window,
            "chunk": self.chunk,
            "connections": self.connections,
            "log_level": self.log_level,
            "panel_port": self.panel_port,
            "dial": self.dial,
        }
        for key in ("tcp_keepalive", "so_mark", "rate_limit", "quota",
                    "proxy", "max_conns", "telegram_token", "telegram_chat",
                    "alert_webhook", "alert_events", "telegram_api", "panel_host"):
            value = getattr(self, key)
            if value not in (0, "", None, [], "0.0.0.0"):
                d[key] = value
        if self.tcp_nodelay is False:
            d["tcp_nodelay"] = False
        if self.prefer_ipv6 != "auto":
            d["prefer_ipv6"] = self.prefer_ipv6
        if self.dual_stack is False:
            d["dual_stack"] = False
        if self.panel_tls:
            d["panel_tls"] = True
        if self.unpaired:
            # written by `simurgh install --pair-later` (and the one-command
            # installer): no token and no exit address yet, on purpose
            d["unpaired"] = True
        if self.quota:
            d["quota_period"] = self.quota_period
            d["quota_action"] = self.quota_action
        if self.reverse:
            tcarrier_udp = self.tunnel.carrier == "udp"
            d["tunnel"] = {
                "carrier": self.tunnel.carrier, "host": self.tunnel.host,
                "port": self.tunnel.port,
                "path": self.tunnel.path if self.tunnel.carrier in ("wss", "ws") else None,
                "fallback": self.tunnel.fallback,
                "decoy_file": self.tunnel.decoy_file,
                "cert_auto": self.tunnel.cert_auto,
                "cert_file": self.tunnel.cert_file,
                "key_file": self.tunnel.key_file,
                "padding": self.tunnel.padding,
                "enabled": self.tunnel.enabled,
                "mtu": self.tunnel.mtu if tcarrier_udp else None,
                "fec": (self.tunnel.fec
                        if tcarrier_udp and self.tunnel.fec != "off" else None),
                "window": self.tunnel.window if tcarrier_udp else None,
            }
            d["tunnel"] = {k: v for k, v in d["tunnel"].items() if v is not None}
        if self.tokens and len(self.tokens) > 1:
            d["tokens"] = [
                {k: v for k, v in {
                    "token": t.token, "name": t.name, "ports": t.ports or None,
                    "targets": t.targets or None,
                    "allow_any_target": t.allow_any_target or None,
                    "rate_limit": t.rate_limit or None,
                    "quota": t.quota or None,
                    "max_conns": t.max_conns or None,
                }.items() if v is not None}
                for t in self.tokens
            ]
        if self.exit.address or not self.reverse:
            d["exit"] = {
                "carrier": self.exit.carrier, "address": self.exit.address,
                "port": self.exit.port, "domain": self.exit.domain,
                "path": self.exit.path, "fingerprint": self.exit.fingerprint,
                "insecure_skip_verify": self.exit.insecure_skip_verify,
                "padding": self.exit.padding, "split": self.exit.split,
                "enabled": self.exit.enabled,
                "proxy": self.exit.proxy or None,
                "prefer_ipv6": self.exit.prefer_ipv6 if self.exit.prefer_ipv6 != "auto" else None,
                "label": self.exit.label or None,
                "mtu": self.exit.mtu if self.exit.carrier == "udp" else None,
                "fec": self.exit.fec if self.exit.carrier == "udp" and self.exit.fec != "off" else None,
                "window": self.exit.window if self.exit.carrier == "udp" else None,
                "tcp_nodelay": self.exit.tcp_nodelay,
                "tcp_keepalive": self.exit.tcp_keepalive,
                "so_mark": self.exit.so_mark,
            }
            d["exit"] = {k: v for k, v in d["exit"].items() if v is not None}
        if self.pool:
            d["pool"] = [
                {k: v for k, v in {
                    "carrier": e.carrier, "address": e.address, "port": e.port,
                    "domain": e.domain, "path": e.path, "fingerprint": e.fingerprint,
                    "insecure_skip_verify": e.insecure_skip_verify,
                    "padding": e.padding, "split": e.split, "enabled": e.enabled,
                    "proxy": e.proxy or None,
                    "prefer_ipv6": e.prefer_ipv6 if e.prefer_ipv6 != "auto" else None,
                    "label": e.label or None,
                    "mtu": e.mtu if e.carrier == "udp" else None,
                    "fec": e.fec if e.carrier == "udp" and e.fec != "off" else None,
                    "window": e.window if e.carrier == "udp" else None,
                    "tcp_nodelay": e.tcp_nodelay,
                    "tcp_keepalive": e.tcp_keepalive,
                    "so_mark": e.so_mark,
                }.items() if v is not None}
                for e in self.pool
            ]
        d["mapping"] = [
            {k: v for k, v in {
                "name": m.name, "listen": m.listen, "target_port": m.target_port,
                "target_host": m.target_host,
                "listen_host": m.listen_host if m.listen_host != "0.0.0.0" else None,
                "udp": m.udp,
                "mode": m.mode if m.mode != "tcp" else None,
                "sni": m.sni,
                "fallback": m.fallback if m.sni else None,
                "group": m.group or None,
                "rate_limit": m.rate_limit or None,
                "quota": m.quota or None,
                "quota_period": m.quota_period if m.quota else None,
                "quota_action": m.quota_action if m.quota else None,
                "max_conns": m.max_conns or None,
                "allow_targets": m.allow_targets or None,
                "proxy_protocol": m.proxy_protocol if m.proxy_protocol != "off" else None,
                "enabled": m.enabled,
            }.items() if v is not None}
            for m in self.mappings
        ]
        return d


def _default_start_window(engine: str) -> int:
    """Engine-appropriate starting window for configs that do not name one."""
    return GO_STREAM_WINDOW if engine == "go" else 256 * 1024


def _default_max_window(engine: str) -> int:
    return GO_MAX_STREAM_WINDOW if engine == "go" else 8 * 1024 * 1024


def _engine_from(data: dict) -> str:
    """The data plane named in the config, defaulting to the Python engine."""
    engine = str(data.get("engine", DEFAULT_ENGINE)).strip().lower()
    if engine not in ENGINES:
        raise ConfigError(f"unknown engine {engine!r} (use 'python' or 'go')")
    return engine


def _prefer_from(value) -> str:
    text = str(value or "auto").strip().lower()
    if text not in PREFER:
        raise ConfigError(f"prefer_ipv6 must be one of {PREFER}, not {value!r}")
    return text


def _period_from(value) -> str:
    text = str(value or "month").strip().lower()
    if text in ("daily", "day"):
        return "day"
    if text in ("monthly", "month", "30d"):
        return "month"
    if text in ("total", "all", "forever"):
        return "total"
    raise ConfigError(f"quota period must be day, month or total, not {value!r}")


def _quota_action_from(value) -> str:
    text = str(value or "block").strip().lower()
    if text not in ("block", "throttle", "warn"):
        raise ConfigError("quota_action must be block, throttle or warn")
    return text


def _endpoint_from(data: dict, strict: bool = True) -> ExitEndpoint:
    carrier = str(data.get("carrier", "tls")).lower()
    if carrier not in CARRIERS:
        raise ConfigError(f"unknown carrier {carrier!r} for exit")
    address = str(data.get("address") or data.get("host") or "")
    if not address and strict:
        raise ConfigError("exit: 'address' is required")
    return ExitEndpoint(
        carrier=carrier,
        address=address,
        port=int(data.get("port", 8443)),
        domain=data.get("domain"),
        path=str(data.get("path", "/ws")),
        fingerprint=data.get("fingerprint"),
        insecure_skip_verify=bool(data.get("insecure_skip_verify", False)),
        padding=bool(data.get("padding", True)),
        split=int(data.get("split", 0) or 0),
        enabled=bool(data.get("enabled", True)),
        proxy=str(data.get("proxy") or ""),
        prefer_ipv6=_prefer_from(data.get("prefer_ipv6")),
        tcp_nodelay=(None if data.get("tcp_nodelay") is None
                     else bool(data.get("tcp_nodelay"))),
        tcp_keepalive=(None if data.get("tcp_keepalive") is None
                       else max(0, int(data.get("tcp_keepalive") or 0))),
        so_mark=(None if data.get("so_mark") is None
                 else max(0, int(data.get("so_mark") or 0))),
        mtu=max(512, min(65000, int(data.get("mtu", 1200) or 1200))),
        fec=str(data.get("fec", "off") or "off"),
        window=max(32, min(65536, int(data.get("window", 1024) or 1024))),
        label=str(data.get("label") or ""),
    )


def _validate_listen_ports(listen: list[ListenSpec]) -> None:
    """Two listeners on the same address *and* port is a typo, not a feature."""
    seen: set[tuple[str, int, int]] = set()
    for spec in listen:
        if spec.reverse or not spec.enabled:
            continue
        for fam in _families_of(spec.host):
            key = (fam, spec.port, 0 if spec.carrier == "udp" else 1)
            if key in seen:
                raise ConfigError(
                    f"two [[listen]] blocks use {spec.host}:{spec.port} "
                    f"with carrier {spec.carrier!r}; only one may"
                )
            seen.add(key)


def _families_of(host: str) -> list[str]:
    """Which address families a listen host covers (``::`` covers both)."""
    if host in ("::", "[::]", "*", ""):
        return ["v4", "v6"]
    return ["v6"] if ":" in host else ["v4"]


def load_relay(path: str | Path) -> RelayConfig:
    data = _read_toml(path)
    cfg = RelayConfig(token=str(data.get("token") or ""))
    cfg.tokens = _tokens_from(data, cfg.token)
    if cfg.tokens:
        cfg.token = cfg.tokens[0].token
    # `unpaired = true` is written by an install that was deliberately told to
    # wait for the exit (``simurgh install --pair-later``); everything else
    # still needs a token, so a typo can never produce a silent, open relay.
    cfg.unpaired = bool(data.get("unpaired", False))
    if not cfg.token and not cfg.unpaired:
        raise ConfigError("relay config: 'token' is required "
                          "(or `unpaired = true` for a server that is not paired yet)")
    cfg.name = str(data.get("name") or "")
    cfg.accept_push = bool(data.get("accept_push", True))
    cfg.keepalive = int(data.get("keepalive", 25))
    cfg.chunk = max(4096, min(1024 * 1024, int(data.get("chunk", 65536))))
    cfg.connections = max(1, min(16, int(data.get("connections", 1))))
    cfg.engine = _engine_from(data)
    cfg.stream_window = max(16 * 1024,
                            int(data.get("stream_window", _default_start_window(cfg.engine))))
    cfg.max_stream_window = max(cfg.stream_window,
                                int(data.get("max_stream_window",
                                             _default_max_window(cfg.engine))))
    cfg.speedtest_port = int(data.get("speedtest_port", 0))
    cfg.log_level = str(data.get("log_level", "info"))
    cfg.panel_port = int(data.get("panel_port", 8787))
    cfg.panel_tls = bool(data.get("panel_tls", False))
    cfg.panel_host = str(data.get("panel_host", "0.0.0.0"))
    cfg.tcp_nodelay = bool(data.get("tcp_nodelay", True))
    cfg.tcp_keepalive = max(0, int(data.get("tcp_keepalive", 0) or 0))
    cfg.so_mark = max(0, int(data.get("so_mark", 0) or 0))
    cfg.prefer_ipv6 = _prefer_from(data.get("prefer_ipv6"))
    cfg.proxy = str(data.get("proxy") or "")
    cfg.dual_stack = bool(data.get("dual_stack", True))
    cfg.rate_limit = data.get("rate_limit", 0) or 0
    cfg.quota = data.get("quota", 0) or 0
    cfg.quota_period = _period_from(data.get("quota_period"))
    cfg.quota_action = _quota_action_from(data.get("quota_action"))
    cfg.max_conns = max(0, int(data.get("max_conns", 0) or 0))
    cfg.telegram_token = str(data.get("telegram_token") or "")
    cfg.telegram_chat = str(data.get("telegram_chat") or "")
    cfg.alert_webhook = str(data.get("alert_webhook") or data.get("webhook") or "")
    cfg.alert_events = [str(x) for x in _as_list(data.get("alert_events"))]
    cfg.telegram_api = str(data.get("telegram_api") or "")

    cfg.dial = str(data.get("dial", "relay")).lower()
    if cfg.dial not in ("relay", "exit"):
        raise ConfigError("relay config: dial must be 'relay' or 'exit'")

    tunnel_data = data.get("tunnel") or {}
    tcarrier = str(tunnel_data.get("carrier", "tls")).lower()
    if tcarrier not in CARRIERS:
        raise ConfigError(f"unknown carrier {tcarrier!r} in [tunnel]")
    cfg.tunnel = TunnelSpec(
        carrier=tcarrier,
        host=str(tunnel_data.get("host", "0.0.0.0")),
        port=int(tunnel_data.get("port", 8443)),
        path=str(tunnel_data.get("path", "/ws")),
        fallback=str(tunnel_data.get("fallback", "decoy")),
        decoy_file=tunnel_data.get("decoy_file"),
        cert_file=tunnel_data.get("cert_file"),
        key_file=tunnel_data.get("key_file"),
        cert_auto=bool(tunnel_data.get("cert_auto", True)),
        padding=bool(tunnel_data.get("padding", True)),
        enabled=bool(tunnel_data.get("enabled", True)),
        mtu=max(512, min(65000, int(tunnel_data.get("mtu", 1200) or 1200))),
        fec=str(tunnel_data.get("fec", "off") or "off"),
        window=max(32, min(65536, int(tunnel_data.get("window", 1024) or 1024))),
    )
    if cfg.reverse and tcarrier in ("tls", "wss") \
            and not (cfg.tunnel.cert_file and cfg.tunnel.key_file) \
            and not cfg.tunnel.cert_auto:
        raise ConfigError("reverse mode with tls/wss needs a certificate "
                          "(or [tunnel] cert_auto = true)")

    exit_data = data.get("exit") or {}
    if exit_data and (exit_data.get("address") or exit_data.get("host")
                      or cfg.unpaired):
        # an unpaired install still keeps its carrier and port choices
        cfg.exit = _endpoint_from(exit_data, strict=not cfg.unpaired)
    elif not cfg.reverse:
        raise ConfigError("relay config: an [exit] block with an address is required")
    cfg.pool = [_endpoint_from(e) for e in data.get("pool", [])]

    for item in data.get("mapping", []):
        cfg.mappings.extend(_mappings_from(item))
    _validate_mappings(cfg.mappings)
    if cfg.unpaired and cfg.token and (cfg.reverse or cfg.endpoints()):
        # the file has been completed by hand (or by an older `join`): do not
        # keep calling a relay that can dial "unpaired" for ever
        cfg.unpaired = False
    return cfg


def _mappings_from(item) -> list[Mapping]:
    """One ``[[mapping]]`` (or shorthand string) -> one mapping per port.

    ``ports``/``listen`` may be a range or a list, in which case the block
    expands: ``ports = ["443", "8080-8090"]`` becomes eleven mappings that all
    share the block's name and limits.  Ranges keep the operator's config short
    while the panel keeps showing one row per real port.
    """
    if isinstance(item, str):
        pairs = expand_port_spec(item)
        name = ""
        base = {}
    elif isinstance(item, dict):
        base = item
        raw = item.get("ports", item.get("listen", 0))
        pairs = expand_port_spec(raw)
        name = str(item.get("name", "") or "")
        # a single port may state its target explicitly
        if len(pairs) == 1 and "target_port" in item:
            pairs = [(pairs[0][0], int(item["target_port"]))]
        elif "target" in item and len(pairs) == 1:
            pairs = [(pairs[0][0], int(item["target"]))]
    else:
        raise ConfigError(f"cannot read a mapping: {item!r}")
    if not pairs:
        raise ConfigError("mapping without a 'listen' port")

    mode = str(base.get("mode", "") or "").lower()
    if not mode:
        mode = "udp" if base.get("udp") else "tcp"
    if mode not in MODES:
        raise ConfigError(f"unknown mapping mode {mode!r} (use {'/'.join(MODES)})")
    proxy_protocol = str(base.get("proxy_protocol", "off") or "off")
    if proxy_protocol not in PROXY_PROTOCOLS:
        raise ConfigError(f"mapping proxy_protocol must be one of {PROXY_PROTOCOLS}")
    fallback = str(base.get("fallback", "decoy") or "decoy")
    if fallback not in FALLBACKS and not fallback.startswith(("site:", "http")):
        raise ConfigError(f"mapping fallback must be one of {FALLBACKS} "
                          "or 'site:host:port'")
    group = str(base.get("group", "") or name)
    pinned_exit = str(base.get("exit", base.get("via", "")) or "")
    out: list[Mapping] = []
    for index, (listen, target) in enumerate(pairs):
        one = Mapping(
            name=(name if len(pairs) == 1 else
                  f"{name or 'port'}-{listen}"),
            listen=listen,
            target_port=target,
            target_host=str(base.get("target_host", "127.0.0.1")),
            listen_host=str(base.get("listen_host", "0.0.0.0")),
            udp=mode == "udp",
            mode=mode,
            proxy_protocol=proxy_protocol,
            fallback=fallback,
            group=group,
            exit=pinned_exit,
            sni=(str(base["sni"]) if base.get("sni") else None),
            rate_limit=base.get("rate_limit", 0) or 0,
            quota=base.get("quota", 0) or 0,
            quota_period=_period_from(base.get("quota_period")),
            quota_action=_quota_action_from(base.get("quota_action")),
            max_conns=max(0, int(base.get("max_conns", 0) or 0)),
            allow_targets=[str(x) for x in _as_list(base.get("allow_targets"))],
            enabled=bool(base.get("enabled", True)),
        )
        del index
        out.append(one)
    return out


def _validate_mappings(mappings: list[Mapping]) -> None:
    """Catch the mistakes that would otherwise show up as a bind error later."""
    seen: set[str] = set()
    shared: dict[tuple[str, int, bool], list[Mapping]] = {}
    for m in mappings:
        if not m.enabled:
            continue
        key = m.key()
        if key in seen:
            raise ConfigError(f"two mappings are identical: {key}")
        seen.add(key)
        bucket = shared.setdefault(
            (m.listen_host, m.listen, m.is_datagram), [])
        bucket.append(m)
    for (host, port, udp), group in shared.items():
        if len(group) == 1:
            continue
        if udp:
            raise ConfigError(
                f"{host}:{port} is used by {len(group)} UDP mappings; "
                "a UDP port cannot be shared, give each one its own port"
            )
        proxies = [m for m in group if m.is_proxy]
        if proxies:
            raise ConfigError(
                f"{host}:{port} mixes a {proxies[0].mode} proxy with other "
                "mappings; give the proxy its own port"
            )
        names = [m.sni for m in group]
        if len([n for n in names if n]) != len(names):
            # one mapping on the port has no SNI: it becomes the default and
            # catches every client that does not match a name -- that is the
            # documented behaviour, not an error
            continue


def _parse_mapping_string(s: str) -> tuple[int, int]:
    """``"443:443"`` or ``"443"`` (both sides equal)."""
    pairs = expand_port_spec(s)
    if len(pairs) != 1:
        raise ConfigError(f"{s!r} is not a single port pair")
    return pairs[0]


def save_relay(cfg: RelayConfig, path: str | Path) -> None:
    _write_toml(RelayConfig.to_dict(cfg), path)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _read_toml(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{p}: invalid TOML: {exc}") from exc


def _write_toml(data: dict, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = toml_dumps(data)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def write_default_exit(path: str | Path, token: str | None = None,
                       name: str = "", ports: list[int] | None = None,
                       cert_file: str | None = None,
                       key_file: str | None = None) -> ExitConfig:
    cfg = ExitConfig(
        token=token or new_token(),
        name=name,
        cert_file=cert_file,
        key_file=key_file,
        cert_auto=not bool(cert_file),
        push_ports=list(ports or DEFAULT_PORTS),
    )
    cfg.listen = [
        ListenSpec(carrier="tls", host="0.0.0.0", port=8443, fallback="decoy"),
        ListenSpec(carrier="raw", host="0.0.0.0", port=9000),
    ]
    save_exit(cfg, path)
    return cfg


def write_default_relay(path: str | Path, token: str, exit_address: str,
                        exit_port: int = 8443, carrier: str = "tls",
                        domain: str | None = None,
                        ports: list[int] | None = None,
                        name: str = "") -> RelayConfig:
    cfg = RelayConfig(token=token, name=name)
    cfg.exit = ExitEndpoint(carrier=carrier, address=exit_address,
                            port=exit_port, domain=domain)
    cfg.mappings = [Mapping(name=f"port-{p}", listen=p, target_port=p)
                    for p in (ports or DEFAULT_PORTS)]
    save_relay(cfg, path)
    return cfg

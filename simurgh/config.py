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
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CARRIERS = ("tls", "wss", "raw", "plain")

#: which data plane runs the relay/exit role. Both speak the same wire
#: protocol, so switching is a config change (see docs/CONFIGURATION.md).
ENGINES = ("python", "go")
DEFAULT_ENGINE = "python"
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
    enabled: bool = True
    dial: str = ""                       # reverse mode: dial this host
    fingerprint: str | None = None       # pin the relay certificate (reverse)
    insecure_skip_verify: bool = False

    def endpoint(self) -> str:
        if self.dial:
            return f"{self.carrier}->{self.dial}:{self.port}"
        return f"{self.carrier}://{self.host}:{self.port}"

    @property
    def reverse(self) -> bool:
        return bool(self.dial)


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

    def endpoint(self) -> str:
        return f"{self.carrier}://{self.host}:{self.port}"


@dataclass
class ExitConfig:
    token: str = ""
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
    speedtest_port: int = 8808
    log_level: str = "info"
    #: flow control: base receive window per stream, and the ceiling the
    #: adaptive window may grow to while a stream keeps draining fast
    stream_window: int = 256 * 1024
    max_stream_window: int = 16 * 1024 * 1024
    chunk: int = 65536
    #: reverse mode: how many tunnel connections this exit opens to the relay
    connections: int = 1
    #: data plane: "python" (works everywhere) or "go" (much faster)
    engine: str = DEFAULT_ENGINE

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
        if self.cert_file:
            d["cert_file"] = self.cert_file
        if self.key_file:
            d["key_file"] = self.key_file
        if self.allow_ips:
            d["allow_ips"] = self.allow_ips
        if self.push_ports:
            d["push_ports"] = self.push_ports
        if self.proxy_protocol and self.proxy_protocol != "off":
            d["proxy_protocol"] = self.proxy_protocol
        d["listen"] = [
            {k: v for k, v in {
                "carrier": ls.carrier, "host": ls.host, "port": ls.port,
                "path": ls.path if ls.carrier in ("wss", "ws") else None,
                "fallback": ls.fallback,
                "decoy_file": ls.decoy_file,
                "padding": ls.padding,
                "enabled": ls.enabled,
                "dial": ls.dial or None,
                "fingerprint": ls.fingerprint,
                "insecure_skip_verify": ls.insecure_skip_verify or None,
            }.items() if v is not None}
            for ls in self.listen
        ]
        return d


def load_exit(path: str | Path) -> ExitConfig:
    data = _read_toml(path)
    cfg = ExitConfig(token=str(data.get("token") or ""))
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
    cfg.speedtest_port = int(data.get("speedtest_port", 8808))
    cfg.proxy_protocol = str(data.get("proxy_protocol", "off"))
    cfg.log_level = str(data.get("log_level", "info"))
    cfg.stream_window = max(16 * 1024, int(data.get("stream_window", 256 * 1024)))
    cfg.max_stream_window = max(cfg.stream_window,
                                int(data.get("max_stream_window", 16 * 1024 * 1024)))
    cfg.chunk = max(4096, min(1024 * 1024, int(data.get("chunk", 65536))))
    cfg.engine = _engine_from(data)
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
            enabled=bool(item.get("enabled", True)),
            dial=str(item.get("dial") or ""),
            fingerprint=item.get("fingerprint"),
            insecure_skip_verify=bool(item.get("insecure_skip_verify", False)),
        ))
    if not cfg.listen:
        raise ConfigError("exit config: at least one [[listen]] block is required")
    listeners = [ls for ls in cfg.listen if not ls.reverse]
    if any(ls.carrier in ("tls", "wss") for ls in listeners):
        if not (cfg.cert_file and cfg.key_file) and not cfg.cert_auto:
            raise ConfigError("carrier tls/wss needs a certificate (or cert_auto = true)")
    if cfg.proxy_protocol not in PROXY_PROTOCOLS:
        raise ConfigError(f"proxy_protocol must be one of {PROXY_PROTOCOLS}")
    return cfg


def save_exit(cfg: ExitConfig, path: str | Path) -> None:
    _write_toml(ExitConfig.to_dict(cfg), path)


# --------------------------------------------------------------------------
# relay node (Iranian server)
# --------------------------------------------------------------------------


@dataclass
class Mapping:
    """One port the users connect to, and where it should end up."""

    name: str = ""
    listen: int = 0
    target_port: int = 0
    target_host: str = "127.0.0.1"
    listen_host: str = "0.0.0.0"
    udp: bool = False
    proxy_protocol: str = "off"
    enabled: bool = True

    def key(self) -> str:
        """Unique per listen address *and* protocol: TCP 443 and UDP 443 can
        coexist on the relay."""
        proto = "udp" if self.udp else "tcp"
        return (f"{proto}:{self.listen_host}:{self.listen}/"
                f"{self.target_host}:{self.target_port}")


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
    enabled: bool = True

    def describe(self) -> str:
        return f"{self.carrier}://{self.address}:{self.port}"


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
    max_stream_window: int = 16 * 1024 * 1024
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

    @property
    def reverse(self) -> bool:
        return self.dial == "exit"

    def endpoints(self) -> list[ExitEndpoint]:
        """Primary first, then the failover pool."""
        out = [e for e in [self.exit] + self.pool if e.enabled and e.address]
        return out

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
        if self.reverse:
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
            }
        if self.exit.address or not self.reverse:
            d["exit"] = {
                "carrier": self.exit.carrier, "address": self.exit.address,
                "port": self.exit.port, "domain": self.exit.domain,
                "path": self.exit.path, "fingerprint": self.exit.fingerprint,
                "insecure_skip_verify": self.exit.insecure_skip_verify,
                "padding": self.exit.padding, "enabled": self.exit.enabled,
            }
        if self.pool:
            d["pool"] = [
                {"carrier": e.carrier, "address": e.address, "port": e.port,
                 "domain": e.domain, "path": e.path, "fingerprint": e.fingerprint,
                 "insecure_skip_verify": e.insecure_skip_verify,
                 "padding": e.padding, "enabled": e.enabled}
                for e in self.pool
            ]
        d["mapping"] = [
            {k: v for k, v in {
                "name": m.name, "listen": m.listen, "target_port": m.target_port,
                "target_host": m.target_host,
                "listen_host": m.listen_host,
                "udp": m.udp,
                "proxy_protocol": m.proxy_protocol if m.proxy_protocol != "off" else None,
                "enabled": m.enabled,
            }.items() if v is not None}
            for m in self.mappings
        ]
        return d


def _engine_from(data: dict) -> str:
    """The data plane named in the config, defaulting to the Python engine."""
    engine = str(data.get("engine", DEFAULT_ENGINE)).strip().lower()
    if engine not in ENGINES:
        raise ConfigError(f"unknown engine {engine!r} (use 'python' or 'go')")
    return engine


def _endpoint_from(data: dict) -> ExitEndpoint:
    carrier = str(data.get("carrier", "tls")).lower()
    if carrier not in CARRIERS:
        raise ConfigError(f"unknown carrier {carrier!r} for exit")
    address = str(data.get("address") or data.get("host") or "")
    if not address:
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
        enabled=bool(data.get("enabled", True)),
    )


def load_relay(path: str | Path) -> RelayConfig:
    data = _read_toml(path)
    cfg = RelayConfig(token=str(data.get("token") or ""))
    if not cfg.token:
        raise ConfigError("relay config: 'token' is required")
    cfg.name = str(data.get("name") or "")
    cfg.accept_push = bool(data.get("accept_push", True))
    cfg.keepalive = int(data.get("keepalive", 25))
    cfg.stream_window = max(16 * 1024, int(data.get("stream_window", 256 * 1024)))
    cfg.max_stream_window = max(cfg.stream_window,
                                int(data.get("max_stream_window", 16 * 1024 * 1024)))
    cfg.chunk = max(4096, min(1024 * 1024, int(data.get("chunk", 65536))))
    cfg.connections = max(1, min(16, int(data.get("connections", 1))))
    cfg.engine = _engine_from(data)
    cfg.speedtest_port = int(data.get("speedtest_port", 0))
    cfg.log_level = str(data.get("log_level", "info"))
    cfg.panel_port = int(data.get("panel_port", 8787))

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
    )
    if cfg.reverse and tcarrier in ("tls", "wss") \
            and not (cfg.tunnel.cert_file and cfg.tunnel.key_file) \
            and not cfg.tunnel.cert_auto:
        raise ConfigError("reverse mode with tls/wss needs a certificate "
                          "(or [tunnel] cert_auto = true)")

    exit_data = data.get("exit") or {}
    if exit_data.get("address") or exit_data.get("host"):
        cfg.exit = _endpoint_from(exit_data)
    elif not cfg.reverse:
        raise ConfigError("relay config: an [exit] block with an address is required")
    cfg.pool = [_endpoint_from(e) for e in data.get("pool", [])]

    for item in data.get("mapping", []):
        if isinstance(item, str):
            listen, target = _parse_mapping_string(item)
            cfg.mappings.append(Mapping(listen=listen, target_port=target))
            continue
        raw_listen = item.get("listen", 0)
        if isinstance(raw_listen, str) and ":" in raw_listen:
            raw_listen, _, raw_target = raw_listen.partition(":")
            item = {**item, "listen": raw_listen, "target_port": raw_target}
        listen = int(item.get("listen", 0))
        if not listen:
            raise ConfigError("mapping without a 'listen' port")
        cfg.mappings.append(Mapping(
            name=str(item.get("name", "")),
            listen=listen,
            target_port=int(item.get("target_port", listen)),
            target_host=str(item.get("target_host", "127.0.0.1")),
            listen_host=str(item.get("listen_host", "0.0.0.0")),
            udp=bool(item.get("udp", False)),
            proxy_protocol=str(item.get("proxy_protocol", "off")),
            enabled=bool(item.get("enabled", True)),
        ))
    return cfg


def _parse_mapping_string(s: str) -> tuple[int, int]:
    """``"443:443"`` or ``"443"`` (both sides equal)."""
    a, _, b = s.partition(":")
    return int(a), int(b or a)


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

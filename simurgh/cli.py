"""``simurgh`` command line interface.

Everything the operator does day to day lives here: install, configure, run,
watch, test.  Text is Persian (the operators are Iranian) with the English
command names kept short and predictable.

Typical, from zero to a working tunnel on the foreign (exit) server::

    sudo simurgh install --role exit --name omega-de

and on the Iranian (relay) server::

    sudo simurgh install --role relay --exit 203.0.113.9:443 --token <token>
    sudo simurgh mapping add 443 443 --name panel

``simurgh menu`` opens the interactive Persian menu.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import sys
import time
from pathlib import Path

from . import __product__, __version__
from .config import (ConfigError, ExitConfig, ExitEndpoint, ListenSpec, Mapping,
                     RelayConfig, TunnelSpec, load_exit, load_relay, new_token,
                     save_exit, save_relay)
from .util import (Home, free_port, get_logger, human_bytes, human_duration,
                   human_rate, is_ip, local_ips, parse_port_list, setup_logging)

log = get_logger("simurgh.cli")

RELAY_DEFAULT_PANEL = 8787
EXIT_DEFAULT_SPEEDTEST = 8808
DEFAULT_TLS_PORTS = (443, 2053, 2083, 2087, 2096, 8443)


# --------------------------------------------------------------------- utils
def _ok(text: str) -> None:
    print(f"\033[32m✔\033[0m {text}")


def _warn(text: str) -> None:
    print(f"\033[33m!\033[0m {text}")


def _err(text: str) -> None:
    print(f"\033[31m✘\033[0m {text}", file=sys.stderr)


def _info(text: str) -> None:
    print(f"\033[36m•\033[0m {text}")


def _interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:  # pragma: no cover
        return False


def _ask(prompt: str, default: str = "") -> str:
    """Ask, but never hang: without a terminal we take the default."""
    if not _interactive():
        if default:
            return default
        raise SystemExit(_fail(f"{prompt} is required and the terminal is not interactive."))
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(1)
    return answer or default


def _ask_yes(prompt: str, default: bool = True) -> bool:
    answer = _ask(f"{prompt} (yes/no)", "yes" if default else "no").lower()
    return answer in ("y", "yes", "1", "true")


def _fail(text: str) -> int:
    _err(text)
    return 2


def _public_ip() -> str:
    """Best effort: the address other people should connect to."""
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(1.5)
                sock.connect((host, 53))
                return sock.getsockname()[0]
        except OSError:
            continue
    ips = [ip for ip in local_ips() if not ip.startswith("127.")]
    return ips[0] if ips else "127.0.0.1"


def _home_or_fail(args) -> Home:
    home = Home(getattr(args, "home", None))
    if not home.path.exists():
        _err(f"installation not found: {home.path} — run `simurgh init` or `simurgh install` first.")
        raise SystemExit(2)
    return home


def _load_relay_or_fail(home: Home) -> RelayConfig:
    if not home.relay_cfg.exists():
        _err(f"relay config not found: {home.relay_cfg}")
        raise SystemExit(2)
    try:
        return load_relay(home.relay_cfg)
    except ConfigError as exc:
        _err(str(exc))
        raise SystemExit(2)


def _load_exit_or_fail(home: Home) -> ExitConfig:
    if not home.exit_cfg.exists():
        _err(f"exit config not found: {home.exit_cfg}")
        raise SystemExit(2)
    try:
        return load_exit(home.exit_cfg)
    except ConfigError as exc:
        _err(str(exc))
        raise SystemExit(2)


def _write_state(home: Home, role: str, payload: dict) -> None:
    """Keep a small JSON snapshot so `simurgh status` works without the node."""
    state: dict = {}
    if home.state.exists():
        try:
            state = json.loads(home.state.read_text())
        except (OSError, ValueError):
            state = {}
    state[role] = payload
    state["updated"] = time.time()
    try:
        home.state.parent.mkdir(parents=True, exist_ok=True)
        tmp = home.state.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(home.state)
    except OSError:
        pass


# ------------------------------------------------------------------- install
def cmd_init(args) -> int:
    home = Home(args.home).ensure()
    role = args.role
    if role == "auto":
        if not sys.stdin.isatty():
            role = "exit" if args.exit_host == "" else "relay"
        else:
            print("What is the role of this server?")
            print("  1) Foreign server (exit)  — your panel/service lives here")
            print("  2) Iranian server (relay) — your users connect here")
            choice = _ask("choice", "1")
            role = "exit" if choice.strip() in ("1", "exit") else "relay"
    if role == "exit":
        return _init_exit(home, args)
    return _init_relay(home, args)


def _init_exit(home: Home, args) -> int:
    if home.exit_cfg.exists() and not args.force:
        _warn(f"exit config already exists: {home.exit_cfg} (use --force to overwrite)")
        return 1
    name = args.name or _ask("name of this server", socket.gethostname()[:32])
    token = args.token or new_token()
    ports = parse_port_list(args.listen or list(DEFAULT_TLS_PORTS)[:3])
    listen = [ListenSpec(carrier="tls", port=ports[0], path="/ws")] if ports else []
    for port in ports[1:2]:
        listen.append(ListenSpec(carrier="tls", host="0.0.0.0", port=port, path="/ws"))
    for port in ports[2:]:
        listen.append(ListenSpec(carrier="wss", host="0.0.0.0", port=port, path="/ws"))
    cfg = ExitConfig(
        token=token, name=name, cert_auto=True, push_ports=ports,
        speedtest_port=EXIT_DEFAULT_SPEEDTEST,
    )
    cfg.listen = listen or [ListenSpec(carrier="tls", port=443, path="/ws")]
    try:
        from .certs import ensure_certificate

        cert, key = ensure_certificate(home, args.domain or name or "simurgh.local")
        cfg.cert_file, cfg.key_file = cert, key
    except Exception as exc:  # pragma: no cover - depends on environment
        _warn(f"automatic certificate generation failed: {exc}")
    save_exit(cfg, home.exit_cfg)
    _ok(f"exit config written: {home.exit_cfg}")
    print(f"   token: {token}")
    print(f"   ports: {', '.join(str(s.port) for s in cfg.listen)}")
    return 0


def _init_relay(home: Home, args) -> int:
    if home.relay_cfg.exists() and not args.force:
        _warn(f"relay config already exists: {home.relay_cfg} (use --force to overwrite)")
        return 1
    dial = getattr(args, "dial", "relay") or "relay"
    carrier = (args.carrier or _ask("carrier (tls/wss/raw/plain)", "tls")).lower()
    token = args.token or _ask("token (copy it from the foreign server)")
    if not token:
        return _fail("token is required: --token XXX")
    name = args.name or socket.gethostname()[:32]
    panel_port = args.panel_port or RELAY_DEFAULT_PANEL
    if dial == "exit":
        # reverse mode: we listen, the foreign exit connects to us
        exit_host = ""
        exit_port = args.exit_port or int(_ask("tunnel port on THIS server", "8443"))
        tunnel_cfg = TunnelSpec(
            carrier=carrier,
            host=str(getattr(args, "tunnel_host", "") or "0.0.0.0"),
            port=exit_port,
            path=args.path or "/ws",
            fallback=str(getattr(args, "fallback", "") or "decoy"),
        )
        cfg = RelayConfig(token=token, name=name, panel_port=panel_port,
                          dial="exit", tunnel=tunnel_cfg)
    else:
        exit_host = args.exit_host or _ask("foreign server address (IP or domain)")
        if not exit_host:
            return _fail("foreign server address is required: --exit-host 203.0.113.9")
        exit_port = args.exit_port or int(_ask("tunnel port on the foreign server", "443"))
        domain = args.domain or None
        if carrier in ("tls", "wss") and not domain and _interactive():
            if _ask_yes("do you have a domain name for the SNI?", False):
                domain = _ask("domain name")
        cfg = RelayConfig(
            token=token, name=name, panel_port=panel_port,
            exit=ExitEndpoint(carrier=carrier, address=exit_host, port=exit_port,
                              domain=domain, path=args.path or "/ws",
                              insecure_skip_verify=args.insecure,
                              fingerprint=args.fingerprint),
        )
    mappings = []
    for spec in args.mapping or []:
        try:
            mappings.append(_parse_mapping(spec))
        except ValueError as exc:
            _err(f"invalid mapping '{spec}': {exc}")
            return 2
    cfg.mappings = mappings
    save_relay(cfg, home.relay_cfg)
    _ok(f"relay config written: {home.relay_cfg}")
    if cfg.reverse:
        print("   mode:           reverse (the exit dials us)")
        print(f"   tunnel listen:  {cfg.tunnel.endpoint()}")
        _info("run `simurgh link` on this server and paste the link on the exit")
    else:
        print(f"   foreign server: {carrier}://{exit_host}:{exit_port}")
    if not mappings:
        _info("to add a port: simurgh mapping add 443 443")
    return 0


def _parse_mapping(spec: str, udp: bool = False) -> Mapping:
    """``listen:target[:host]`` — e.g. ``443:443`` or ``8443:8443``."""
    parts = spec.split(":")
    if len(parts) < 2:
        raise ValueError("the format must be listen:target, e.g. 443:443")
    try:
        listen = int(parts[0])
        target = int(parts[1])
    except ValueError as exc:
        raise ValueError("the port must be a number") from exc
    host = ":".join(parts[2:]) or "127.0.0.1"
    if not (1 <= listen <= 65535 and 1 <= target <= 65535):
        raise ValueError("the port is outside the 1-65535 range")
    return Mapping(name="", listen=listen, target_port=target,
                   target_host=host, udp=udp)


def cmd_install(args) -> int:
    """Create the config, install systemd units, start everything."""
    from . import systemd

    home = Home(args.home).ensure()
    role = args.role
    if role == "auto":
        # with --exit-host (or --dial exit, which is the reverse direction) we
        # are on the Iranian server, otherwise on the exit
        role = "relay" if (args.exit_host or args.dial == "exit") else "exit"
    roles = ("exit", "relay") if args.both else (role,)
    if not args.config_only:
        for wanted in roles:
            sub = argparse.Namespace(**{**vars(args), "role": wanted})
            code = _init_exit(home, sub) if wanted == "exit" else _init_relay(home, sub)
            cfg_path = home.exit_cfg if wanted == "exit" else home.relay_cfg
            if code != 0 and not cfg_path.exists():
                _err(f"could not build the {wanted} config; installation stopped.")
                return 2
    report = systemd.install_services(
        home, roles=roles, enable=not args.no_enable, start=not args.no_start,
        user=args.user if args.user else None,
    )
    if not report.get("systemd"):
        _warn("systemd is not available; the service was not installed. Start it manually:")
        for wanted in roles:
            print(f"   simurgh {wanted}" + ("" if wanted != "relay" else ""))
        return 0
    for unit in report.get("units", []):
        _ok(f"created {unit}")
    for svc, result in report.get("actions", {}).items():
        (_ok if result == "ok" else _warn)(f"{svc}: {result}")
    print()
    _info(f"web panel: http://{_public_ip()}:{_panel_port_of(home, roles)}")
    _info("text menu: simurgh menu")
    _info("status: simurgh status")
    return 0


def _panel_port_of(home: Home, roles) -> int:
    if "relay" in roles and home.relay_cfg.exists():
        try:
            return load_relay(home.relay_cfg).panel_port or RELAY_DEFAULT_PANEL
        except ConfigError:
            return RELAY_DEFAULT_PANEL
    return RELAY_DEFAULT_PANEL


# ------------------------------------------------------------- service control
def _service_names(role: str) -> tuple[str, ...]:
    return ("simurgh-exit",) if role == "exit" else ("simurgh-relay",)


_SYSTEMCTL_ACTION = {"up": "start", "start": "start", "down": "stop", "stop": "stop",
                     "restart": "restart", "enable": "enable", "disable": "disable",
                     "reload": "reload"}


def cmd_service(args) -> int:
    from . import systemd

    action = _SYSTEMCTL_ACTION.get(args.service_action, args.service_action)
    names = _service_names(args.role)
    if action == "status":
        rows = []
        for name in names:
            state = systemd.service_state(name)
            active = state.get("ActiveState", "unknown")
            sub = state.get("SubState", "")
            enabled = state.get("UnitFileState", "")
            rows.append((name, active, sub, enabled))
        home = Home(args.home)
        role = args.role
        cfg_path = home.exit_cfg if role == "exit" else home.relay_cfg
        print(f"{__product__} v{__version__}   ({role})")
        print(f"  home:   {home.path}")
        print(f"  config: {cfg_path} {'✔' if cfg_path.exists() else '✘ (missing)'}")
        for name, active, sub, enabled in rows:
            mark = "✔" if active == "active" else "✘"
            print(f"  service: {name}: {active}/{sub} enable={enabled} {mark}")
        if home.state.exists():
            try:
                state = json.loads(home.state.read_text())[role]
                print(f"  tunnel:  {'connected' if state.get('connected') or state.get('tunnels') else 'down'}")
                print(f"  stats:   {json.dumps(state.get('stats', {}).get('totals', {}), ensure_ascii=False)}")
            except (OSError, ValueError, KeyError):
                pass
        if args.json:
            print(json.dumps({"services": [
                {"name": n, "active": a, "sub": s, "enabled": e} for n, a, s, e in rows
            ]}, ensure_ascii=False))
        return 0
    if not systemd.available():
        _err("systemd is not available; run `simurgh exit` / `simurgh relay` manually.")
        return 1
    result = systemd.control(action, names)
    code = 0
    for name, text in result.items():
        if text == "ok":
            _ok(f"{name}: {action}")
        else:
            _err(f"{name}: {text}")
            code = 1
    return code


# ------------------------------------------------------------------- runners
async def _run_exit(home: Home, args) -> int:
    from .exit import ExitNode
    from .panel import Panel

    cfg = _load_exit_or_fail(home)
    setup_logging(args.verbose, logfile=str(home.exit_log))
    if cfg.cert_auto and any(not ls.reverse for ls in cfg.listen):
        from .certs import ensure_certificate

        cert, key = ensure_certificate(home, cfg.name or "simurgh.local")
        cfg.cert_file, cfg.key_file = cert, key
    node = ExitNode(cfg)
    try:
        await node.start()
    except Exception as exc:
        _err(f"could not start the exit: {exc}")
        return 1
    panel = None
    if not args.no_panel:
        password = _load_panel_password(home)
        panel = Panel(home, "exit", node=node, host=args.panel_host,
                      port=args.panel_port or _exit_panel_port(home),
                      user=_load_panel_user(home), password=password,
                      allow_ips=tuple(cfg.allow_ips),
                      on_action=lambda name: _service_action(name, "exit"))
        try:
            await panel.start()
        except OSError as exc:
            _warn(f"the panel did not start ({exc}); the tunnel keeps running.")
            panel = None
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    writer = asyncio.ensure_future(_state_writer(home, "exit", node, panel))
    _ok(f"exit \"{cfg.name or 'exit'}\" is up.")
    await stop.wait()
    writer.cancel()
    if panel is not None:
        await panel.stop()
    await node.stop()
    return 0


async def _run_relay(home: Home, args) -> int:
    from .panel import Panel
    from .relay import RelayNode

    cfg = _load_relay_or_fail(home)
    setup_logging(args.verbose, logfile=str(home.relay_log))
    if cfg.reverse and cfg.tunnel.cert_auto:
        from .certs import ensure_certificate

        cert, key = ensure_certificate(home, cfg.name or "simurgh.local",
                                       cert_file=cfg.tunnel.cert_file,
                                       key_file=cfg.tunnel.key_file)
        cfg.tunnel.cert_file, cfg.tunnel.key_file = cert, key
    node = RelayNode(cfg, home=home)
    await node.start()
    conflicts = node.port_conflicts()
    for item in conflicts:
        _warn(item)
    panel = None
    if not args.no_panel:
        panel = Panel(home, "relay", node=node, host=args.panel_host,
                      port=args.panel_port or cfg.panel_port or RELAY_DEFAULT_PANEL,
                      user=_load_panel_user(home), password=_load_panel_password(home),
                      on_action=lambda name: _service_action(name, "relay"))
        try:
            await panel.start()
        except OSError as exc:
            _warn(f"the panel did not start ({exc}); the tunnel keeps running.")
            panel = None
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    writer = asyncio.ensure_future(_state_writer(home, "relay", node, panel))
    where = (f"tunnel listen: {cfg.tunnel.endpoint()}" if cfg.reverse
             else f"target: {cfg.exit.describe()}")
    _ok(f"relay \"{cfg.name or 'relay'}\" is up. {where}")
    await stop.wait()
    writer.cancel()
    if panel is not None:
        await panel.stop()
    await node.stop()
    return 0


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - windows
            signal.signal(sig, lambda *_: stop.set())


async def _state_writer(home: Home, role: str, node, panel) -> None:
    while True:
        try:
            payload = node.status()
            if panel is not None:
                payload["panel"] = {"port": panel.port, "requests": panel.requests}
            _write_state(home, role, payload)
        except Exception:  # pragma: no cover - never kill the tunnel for stats
            pass
        await asyncio.sleep(3.0)


def _service_action(name: str, role: str) -> dict:
    from . import systemd

    if not systemd.available():
        return {"ok": False, "error": "systemd is not available"}
    action = _SYSTEMCTL_ACTION.get(name, name)
    return systemd.control(action, _service_names(role))


def _load_panel_user(home: Home) -> str:
    return _panel_credentials(home)[0]


def _load_panel_password(home: Home) -> str:
    return _panel_credentials(home)[1]


def _panel_credentials(home: Home) -> tuple[str, str]:
    """Panel login, stored in ``state.json`` (0600)."""
    data: dict = {}
    if home.state.exists():
        try:
            data = json.loads(home.state.read_text())
        except (OSError, ValueError):
            data = {}
    panel = data.setdefault("panel_auth", {})
    changed = False
    if not panel.get("user"):
        panel["user"] = "admin"
        changed = True
    if not panel.get("password"):
        import secrets

        panel["password"] = secrets.token_urlsafe(9)
        changed = True
    if changed:
        try:
            home.state.parent.mkdir(parents=True, exist_ok=True)
            tmp = home.state.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
            os.chmod(tmp, 0o600)
            tmp.replace(home.state)
        except OSError:
            pass
    return panel.get("user", "admin"), panel.get("password", "")


def _exit_panel_port(home: Home) -> int:
    """The exit panel listens here; the config has no panel_port of its own."""
    return RELAY_DEFAULT_PANEL


def cmd_run(args) -> int:
    home = Home(args.home)
    try:
        if args.role == "exit":
            return asyncio.run(_run_exit(home, args))
        return asyncio.run(_run_relay(home, args))
    except KeyboardInterrupt:
        return 0


# ------------------------------------------------------------------ mappings
def cmd_mapping(args) -> int:
    home = _home_or_fail(args)
    cfg = _load_relay_or_fail(home)
    action = args.mapping_action
    if action == "list":
        if not cfg.mappings:
            _info("no mappings yet.")
            return 0
        print(f"{'name':<12} {'iran port':>11}  {'target':<22} type  state")
        for m in cfg.mappings:
            print(f"{m.name or '-':<12} {m.listen:>11}  "
                  f"{m.target_host + ':' + str(m.target_port):<22} "
                  f"{'UDP' if m.udp else 'TCP'}  {'on' if m.enabled else 'off'}")
        return 0
    if action == "add":
        try:
            mapping = Mapping(name=args.name or "",
                              listen=int(args.listen), target_port=int(args.target or args.listen),
                              target_host=args.target_host or "127.0.0.1",
                              udp=bool(args.udp), enabled=True)
        except ValueError as exc:
            _err(str(exc))
            return 2
        if not (1 <= mapping.listen <= 65535 and 1 <= mapping.target_port <= 65535):
            _err("the port is outside the allowed range.")
            return 2
        if any(m.key() == mapping.key() for m in cfg.mappings):
            _err(f"port {mapping.listen} is already used.")
            return 2
        if not free_port(mapping.listen, mapping.listen_host):
            _warn(f"port {mapping.listen} is already in use right now (maybe another service).")
        cfg.mappings.append(mapping)
    elif action in ("remove", "rm", "delete"):
        key = str(args.listen)
        if args.id is not None:
            if args.id < 0 or args.id >= len(cfg.mappings):
                _err("invalid mapping number.")
                return 2
            removed = cfg.mappings.pop(args.id)
            _ok(f"deleted: {removed.listen} → {removed.target_port}")
            save_relay(cfg, home.relay_cfg)
            return 0
        before = len(cfg.mappings)
        cfg.mappings = [m for m in cfg.mappings
                        if not (str(m.listen) == key or m.name == args.name)]
        if len(cfg.mappings) == before:
            _err(f"no mapping found with port {key}.")
            return 2
    elif action == "toggle":
        found = False
        for m in cfg.mappings:
            if str(m.listen) == str(args.listen) or (args.name and m.name == args.name):
                m.enabled = not m.enabled
                found = True
                _ok(f"{'on' if m.enabled else 'off'}: {m.listen}")
        if not found:
            _err("not found.")
            return 2
    else:
        _err(f"unknown action: {action}")
        return 2
    save_relay(cfg, home.relay_cfg)
    _ok(f"saved to {home.relay_cfg}")
    _info("to apply: simurgh restart")
    return 0


# ------------------------------------------------------------------- reports
def cmd_status(args) -> int:
    home = Home(args.home)
    role = args.role
    state: dict = {}
    if home.state.exists():
        try:
            state = json.loads(home.state.read_text())
        except (OSError, ValueError):
            state = {}
    node = state.get(role, {})
    if args.json:
        print(json.dumps(node, ensure_ascii=False, indent=2))
        return 0
    print(f"\033[1m{__product__} v{__version__}\033[0m — {home.path}")
    cfg_path = home.exit_cfg if role == "exit" else home.relay_cfg
    print(f"  config: {cfg_path} {'✔' if cfg_path.exists() else '✘'}")
    if not node:
        _warn(f"no live data for {role}. Is the service running? (simurgh status/up)")
        return cmd_service(argparse.Namespace(role=role, service_action="status",
                                             home=args.home, json=False))
    if role == "relay":
        connected = node.get("connected")
        tunnel = "\033[32mconnected\033[0m" if connected else "\033[31mdown\033[0m"
        print(f"  tunnel:   {tunnel}  ({node.get('current_exit') or '—'})")
        print(f"  rtt:      {node.get('rtt_ms') and round(node['rtt_ms'], 1) or '—'} ms")
        print(f"  reconnects: {node.get('reconnects', 0)}   error: {node.get('last_error') or '—'}")
    else:
        print(f"  tunnels:  {node.get('tunnels', 0)}")
    stats = node.get("stats", {})
    totals = stats.get("totals", {})
    rates = stats.get("rates", {})
    print(f"  traffic: {human_bytes(totals.get('in_bytes', 0))} ↓ / "
          f"{human_bytes(totals.get('out_bytes', 0))} ↑")
    print(f"  rate:    {human_rate(rates.get('in_bps', 0))} ↓ / "
          f"{human_rate(rates.get('out_bps', 0))} ↑")
    print(f"  uptime:  {human_duration(node.get('uptime', 0))}")
    mappings = node.get("mappings") or []
    if mappings:
        print("  mappings:")
        for m in mappings:
            print(f"    {m.get('listen')} → {m.get('target')} "
                  f"({'UDP' if m.get('udp') else 'TCP'}) "
                  f"{'✔' if m.get('bound') else '✘'}")
    if args.watch:
        try:
            while True:
                time.sleep(args.interval)
                os.system("clear")
                cmd_status(argparse.Namespace(home=args.home, role=role, json=False,
                                              watch=False, interval=args.interval))
        except KeyboardInterrupt:
            return 0
    return 0


def cmd_logs(args) -> int:
    home = Home(args.home)
    name = {"exit": home.exit_log, "relay": home.relay_log,
            "panel": home.panel_log}.get(args.role, home.relay_log)
    if not name.exists():
        _err(f"no log file: {name}")
        return 1
    try:
        with name.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError as exc:
        _err(str(exc))
        return 1
    tail = lines[-args.lines:]
    print("\n".join(tail))
    if args.follow:
        print("\033[36m— following the log (Ctrl+C to stop) —\033[0m")
        try:
            with name.open("r", errors="replace") as fh:
                fh.seek(0, os.SEEK_END)
                while True:
                    line = fh.readline()
                    if not line:
                        time.sleep(0.4)
                        continue
                    print(line.rstrip())
        except KeyboardInterrupt:
            pass
    return 0


def cmd_speedtest(args) -> int:
    home = _home_or_fail(args)
    cfg = _load_relay_or_fail(home)
    from .relay import RelayNode
    from .speedtest import run_speedtest

    async def go():
        node = RelayNode(cfg, home=home, listen=False)
        await node.start()
        for _ in range(120):
            if node.connected.is_set():
                break
            await asyncio.sleep(0.25)
        if not node.connected.is_set():
            await node.stop()
            _err("the tunnel is not up; check `simurgh status` first.")
            return 3
        try:
            result = await run_speedtest(node, seconds=args.seconds)
        finally:
            await node.stop()
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"  download: \033[32m{result['download_mbps']:.1f} Mbps\033[0m "
                  f"({human_bytes(result['download_bytes'])})")
            print(f"  upload:   \033[32m{result['upload_mbps']:.1f} Mbps\033[0m "
                  f"({human_bytes(result['upload_bytes'])})")
            print(f"  test time: {result['seconds']:.1f} s")
        return 0

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:
        return 0


def _join_exit(home: Home, payload: dict, args) -> int:
    """Reverse mode: the relay handed us a link, so write ``exit.toml``."""
    from .links import exit_config_from_payload

    if home.exit_cfg.exists() and not getattr(args, "force", False):
        _err(f"the exit config already exists ({home.exit_cfg}); "
             "use --force to overwrite.")
        return 2
    try:
        cfg = exit_config_from_payload(payload)
    except (KeyError, ValueError, ConfigError) as exc:
        _err(f"the link is incomplete: {exc}")
        return 2
    if getattr(args, "host", ""):
        for spec in cfg.listen:
            if spec.reverse:
                spec.dial = args.host
    save_exit(cfg, home.exit_cfg)
    _ok(f"exit config built from the link: {home.exit_cfg}")
    print("   mode:  reverse (this server dials the relay)")
    for spec in cfg.listen:
        print(f"   relay: {spec.carrier}://{spec.dial}:{spec.port}")
    _info("to apply: simurgh restart   (or run: simurgh exit)")
    return 0


def cmd_link(args) -> int:
    """Show the one-string setup link for the *other* server.

    On an exit the link is imported by a new relay (direct mode); on a relay
    that listens for the exit (reverse mode) it is imported by the exit.
    """
    home = _home_or_fail(args)
    from .links import build_join_link

    reverse_cfg = None
    if home.relay_cfg.exists() and not home.exit_cfg.exists():
        relay_cfg = load_relay(home.relay_cfg)
        if relay_cfg.reverse:
            reverse_cfg = relay_cfg
    cfg = reverse_cfg or _load_exit_or_fail(home)
    host = args.host or _public_ip()
    user, password = _panel_credentials(home)
    port = args.panel_port or RELAY_DEFAULT_PANEL
    if reverse_cfg is not None:
        port = args.panel_port or reverse_cfg.panel_port or RELAY_DEFAULT_PANEL
    link = build_join_link(host, port, user, password, cfg.name or "")
    if args.show:
        print(link)
        return 0
    if reverse_cfg is not None:
        print(f"\033[1mexit setup link (keep it secret):\033[0m\n{link}")
        print()
        _info("run this on the FOREIGN (exit) server: simurgh join '<link>'")
    else:
        print(f"\033[1mrelay setup link (keep it secret):\033[0m\n{link}")
        print()
        _info("run this on the Iranian server: simurgh join '<link>'")
    if not is_ip(host):
        _warn("a local address was detected; pass the public address with --host.")
    return 0


def cmd_join(args) -> int:
    home = Home(args.home).ensure()
    from .links import LinkError, fetch_setup, relay_config_from_payload

    try:
        payload = fetch_setup(args.link, timeout=args.timeout)
    except LinkError as exc:
        _err(str(exc))
        return 2
    except Exception as exc:
        _err(f"could not fetch the data from the link: {exc}")
        return 2
    if str(payload.get("role") or "") == "exit":
        return _join_exit(home, payload, args)
    cfg = relay_config_from_payload(payload)
    if home.relay_cfg.exists() and not args.force:
        _err(f"the relay config already exists ({home.relay_cfg}); use --force to overwrite.")
        return 2
    if args.host:
        cfg.exit.address = args.host
    save_relay(cfg, home.relay_cfg)
    _ok(f"relay config built from the link: {home.relay_cfg}")
    print(f"   target: {cfg.exit.describe()}")
    for m in cfg.mappings:
        print(f"   mapping: {m.listen} → {m.target_host}:{m.target_port}")
    return 0


def cmd_doctor(args) -> int:
    """Diagnose the usual reasons a tunnel does not come up."""
    home = Home(args.home)
    problems = 0
    print(f"\033[1m{__product__} v{__version__} — health check\033[0m  ({home.path})")
    if not home.path.exists():
        _err(f"installation folder not found: {home.path}")
        return 2

    role = args.role
    cfg_path = home.exit_cfg if role == "exit" else home.relay_cfg
    if not cfg_path.exists():
        _err(f"config file not found: {cfg_path}")
        return 2
    _ok(f"config: {cfg_path}")

    if role == "exit":
        cfg = _load_exit_or_fail(home)
        if not cfg.token:
            _err("the token is empty.")
            problems += 1
        for spec in cfg.listen:
            if not spec.enabled:
                continue
            if free_port(spec.port, spec.host):
                _ok(f"port {spec.carrier}://{spec.host}:{spec.port} is free")
            else:
                _warn(f"port {spec.host}:{spec.port} is already in use")
        for path, label in ((cfg.cert_file, "certificate"), (cfg.key_file, "key")):
            if path and Path(path).exists():
                _ok(f"{label}: {path}")
            elif path:
                _warn(f"{label} not found: {path}")
        from . import systemd

        if systemd.available():
            _ok("systemd is available")
        else:
            _warn("no systemd; start it manually: simurgh exit")
        return 0 if problems == 0 else 1

    cfg = _load_relay_or_fail(home)
    if not cfg.token:
        _err("the token is empty (copy it from the foreign server).")
        problems += 1
    else:
        _ok(f"token: {cfg.token[:6]}…{cfg.token[-4:]}")
    addresses = [(ep.address, ep.port, ep.describe()) for ep in cfg.endpoints()]
    if not addresses:
        _err("no foreign server is configured.")
        problems += 1
    for host, port, desc in addresses:
        try:
            with socket.create_connection((host, port), timeout=5):
                _ok(f"TCP reachable: {desc}")
        except OSError as exc:
            _err(f"cannot reach {desc}: {exc}")
            problems += 1
    for m in cfg.mappings:
        if free_port(m.listen, m.listen_host):
            _ok(f"port {m.listen} is free → {m.target_host}:{m.target_port}")
        else:
            _warn(f"port {m.listen} is in use (maybe our own service or another one)")
    if free_port(cfg.panel_port):
        _ok(f"panel port {cfg.panel_port} is free")
    else:
        _warn(f"panel port {cfg.panel_port} is in use")
    return 0 if problems == 0 else 1


def cmd_web(args) -> int:
    home = _home_or_fail(args)
    cfg = _load_relay_or_fail(home) if args.role == "relay" else _load_exit_or_fail(home)
    port = args.port or (cfg.panel_port if args.role == "relay" else RELAY_DEFAULT_PANEL)
    host = args.host
    user, password = _panel_credentials(home)
    from .panel import Panel

    async def go():
        panel = Panel(home, args.role, node=None, host=host, port=port,
                      user=user, password=password)
        stop = asyncio.Event()
        _install_signal_handlers(stop)
        await panel.start()
        print(f"  panel: http://{host}:{port}   user: {user}   pass: {password}")
        _info("Ctrl+C to quit")
        await stop.wait()
        await panel.stop()
        return 0

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:
        return 0


def cmd_uninstall(args) -> int:
    from . import systemd

    home = Home(args.home)
    if not args.yes and not _ask_yes(f"remove the services and the folder {home.path}?", False):
        _info("cancelled.")
        return 0
    if systemd.available():
        for svc, result in systemd.uninstall_services().items():
            _ok(f"{svc}: {result}") if result == "ok" else _warn(f"{svc}: {result}")
    if args.purge and home.path.exists():
        shutil.rmtree(home.path, ignore_errors=True)
        _ok(f"folder removed: {home.path}")
    else:
        _info(f"config kept in {home.path} (delete it with: --purge)")
    return 0


def cmd_version(args) -> int:
    print(f"{__product__} v{__version__}")
    print(f"python {sys.version.split()[0]}   home={Home(args.home).path}")
    return 0


# ---------------------------------------------------------------------- menu
def cmd_menu(args) -> int:
    from .menu import main as menu_main

    return menu_main(args)


# ------------------------------------------------------------------ argparse
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simurgh",
        description=f"{__product__} v{__version__} — transparent tunnel between an Iranian and a foreign server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  sudo simurgh install --role exit --name omega\n"
            "  sudo simurgh install --role relay --exit 203.0.113.9:443 --token XXX\n"
            "  simurgh mapping add 443 443 --name panel\n"
            "  simurgh status\n  simurgh menu\n  simurgh speedtest\n"
        ),
    )
    parser.add_argument("--version", action="store_true", help="show the version")
    parser.add_argument("--home", help="install folder (default /etc/simurgh or ~/.simurgh)")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("install", help="full install: config + systemd service")
    p.add_argument("--role", choices=("exit", "relay", "auto"), default="auto")
    p.add_argument("--both", action="store_true", help="both services on this server")
    p.add_argument("--name")
    p.add_argument("--token")
    p.add_argument("--listen", help="exit ports, e.g. 443,8443")
    p.add_argument("--exit-host", default="", help="foreign server address (for the relay)")
    p.add_argument("--exit-port", type=int, default=0)
    p.add_argument("--carrier", default="")
    p.add_argument("--domain", default="")
    p.add_argument("--path", default="")
    p.add_argument("--fingerprint", default=None)
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--dial", choices=("relay", "exit"), default="relay",
                   help="who dials the tunnel: relay (default) or exit (reverse)")
    p.add_argument("--tunnel-host", default="", help="reverse: the address we listen on")
    p.add_argument("--mapping", action="append", help="initial mapping: 443:443")
    p.add_argument("--panel-port", type=int, default=0)
    p.add_argument("--user", default="")
    p.add_argument("--config-only", action="store_true")
    p.add_argument("--no-enable", action="store_true")
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("init", help="write the config files only")
    p.add_argument("--role", choices=("exit", "relay", "auto"), default="auto")
    p.add_argument("--name")
    p.add_argument("--token")
    p.add_argument("--listen")
    p.add_argument("--exit-host", default="")
    p.add_argument("--exit-port", type=int, default=0)
    p.add_argument("--carrier", default="")
    p.add_argument("--domain", default="")
    p.add_argument("--path", default="")
    p.add_argument("--fingerprint", default=None)
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--dial", choices=("relay", "exit"), default="relay",
                   help="who dials the tunnel: relay (default) or exit (reverse)")
    p.add_argument("--tunnel-host", default="", help="reverse: the address we listen on")
    p.add_argument("--mapping", action="append")
    p.add_argument("--panel-port", type=int, default=0)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    for role, helptext in (("exit", "run the foreign server (exit)"),
                           ("relay", "run the Iranian server (relay)")):
        p = sub.add_parser(role, help=helptext)
        p.add_argument("--role", default=role, help=argparse.SUPPRESS)
        p.add_argument("--no-panel", action="store_true", help="without the web panel")
        p.add_argument("--panel-host", default="0.0.0.0")
        p.add_argument("--panel-port", type=int, default=0)
        p.add_argument("--verbose", action="store_true")
        p.set_defaults(func=cmd_run)

    p = sub.add_parser("up", help="start the service")
    p.add_argument("--role", choices=("exit", "relay"), default="relay")
    p.add_argument("--fg", action="store_true", help="run in the terminal (no systemd)")
    p.set_defaults(func=lambda a: cmd_run(argparse.Namespace(
        role=a.role, home=a.home, verbose=False, no_panel=False,
        panel_host="0.0.0.0", panel_port=0)) if a.fg else cmd_service(
        argparse.Namespace(role=a.role, service_action="start", home=a.home, json=False)))

    for action, helptext in (("start", "start the service (systemd)"),
                             ("stop", "stop the service"),
                             ("down", "stop the service"),
                             ("restart", "restart the service"),
                             ("enable", "start automatically at boot"),
                             ("disable", "do not start at boot"),
                             ("status", "service status")):
        p = sub.add_parser(action, help=helptext)
        p.add_argument("--role", choices=("exit", "relay"), default="relay")
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=cmd_service, service_action=action)

    p = sub.add_parser("mapping", aliases=["port"], help="manage port forwardings")
    p.add_argument("mapping_action", choices=("list", "add", "remove", "rm", "toggle"),
                   nargs="?", default="list")
    p.add_argument("listen", nargs="?", default="")
    p.add_argument("target", nargs="?", default="")
    p.add_argument("--name", default="")
    p.add_argument("--target-host", default="127.0.0.1")
    p.add_argument("--udp", action="store_true")
    p.add_argument("--id", type=int, default=None)
    p.set_defaults(func=cmd_mapping)

    p = sub.add_parser("logs", help="show the log")
    p.add_argument("--role", choices=("exit", "relay", "panel"), default="relay")
    p.add_argument("-n", "--lines", type=int, default=80)
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("speedtest", help="measure the real speed through the tunnel")
    p.add_argument("--seconds", type=int, default=6)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_speedtest)

    p = sub.add_parser("link", help="build the setup link for a new relay (on the exit)")
    p.add_argument("--host", default="")
    p.add_argument("--panel-port", type=int, default=0)
    p.add_argument("--show", action="store_true", help="print just the raw link")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("join", help="build the relay config from an exit link")
    p.add_argument("link")
    p.add_argument("--host", default="", help="override the exit address")
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_join)

    p = sub.add_parser("doctor", help="health check and diagnostics")
    p.add_argument("--role", choices=("exit", "relay"), default="relay")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("web", help="run only the web panel")
    p.add_argument("--role", choices=("exit", "relay"), default="relay")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=0)
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("uninstall", help="remove the services")
    p.add_argument("--purge", action="store_true", help="also delete the config folder")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("menu", help="interactive text menu")
    p.set_defaults(func=cmd_menu)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "version", False) and not getattr(args, "command", None):
        return cmd_version(args)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    try:
        return int(func(args) or 0)
    except KeyboardInterrupt:
        print()
        return 130
    except ConfigError as exc:
        _err(str(exc))
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

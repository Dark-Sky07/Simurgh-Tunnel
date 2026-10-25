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
from .config import (CARRIERS, ENGINES, GO_MAX_STREAM_WINDOW, GO_STREAM_WINDOW,
                     ConfigError, ExitConfig, ExitEndpoint, ListenSpec, Mapping,
                     RelayConfig, TunnelSpec, load_exit, load_relay, new_token,
                     save_exit, save_relay)
from .util import (Home, free_port, get_logger, human_bytes, human_duration,
                   human_rate, is_ip, local_ips, parse_port_list, setup_logging,
                   split_host_port)

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
def _init_engine(args, home: Home | None = None) -> str:
    """The data plane for a config that is being created.

    An explicit ``--engine`` wins.  Otherwise the compiled engine is the default
    and the Python engine is the fallback: ``auto`` picks Go when this
    installation has the binary (or a Go toolchain can provide one) and Python
    when it does not, so a fresh install never ends up pointing at a binary that
    is not there.
    """
    want = str(getattr(args, "engine", "") or "").strip().lower()
    if want in ENGINES:
        return want
    if home is not None and go_binary(home):
        return "go"
    return "python"


def _apply_engine_windows(cfg) -> None:
    """Give a fresh config the transfer defaults of the engine it will run.

    The two engines want different starting windows: the Go engine cannot grow
    from 256 KiB on a fast path, while the Python engine grows from it happily
    and keeps less memory for quiet streams.  A value the user set is never
    touched -- this only runs while ``init`` builds a new config.
    """
    if cfg.engine == "go":
        cfg.stream_window = GO_STREAM_WINDOW
        cfg.max_stream_window = GO_MAX_STREAM_WINDOW


def _apply_exit_shortcut(args) -> None:
    """Expand ``--exit HOST[:PORT]`` into the two long-standing options.

    The installer and the CLI examples have always used this concise spelling;
    accepting it here keeps those entry points consistent, including IPv6.
    """
    value = str(getattr(args, "exit_address", "") or "").strip()
    if not value:
        return
    if getattr(args, "exit_host", ""):
        raise ConfigError("use either --exit or --exit-host, not both")
    try:
        host, port = split_host_port(value, getattr(args, "exit_port", 0) or 443)
    except (TypeError, ValueError) as exc:
        raise ConfigError("--exit must look like HOST:PORT (IPv6: [ADDR]:PORT)") from exc
    if not host or not 1 <= port <= 65535:
        raise ConfigError("--exit needs a host and a port in the 1-65535 range")
    args.exit_host, args.exit_port = host, port


def cmd_init(args) -> int:
    _apply_exit_shortcut(args)
    home = Home(args.home)
    role = args.role
    if role == "auto":
        if home.exit_cfg.exists() and not home.relay_cfg.exists():
            role = "exit"
        elif home.relay_cfg.exists() and not home.exit_cfg.exists():
            role = "relay"
        else:
            _err("choose the server role in the first-run panel or `simurgh menu`, or pass --role exit/relay. No configuration was changed.")
            return 2
    home.ensure()
    if role == "exit":
        return _init_exit(home, args)
    return _init_relay(home, args)


def _split_arg(args) -> int:
    """``--split N``: how many bytes of the first write travel in a TCP segment
    of their own (anti-DPI). 0 turns the trick off."""
    return max(0, int(getattr(args, "split", 0) or 0))


def _init_exit(home: Home, args) -> int:
    if home.exit_cfg.exists() and not args.force:
        _warn(f"exit config already exists: {home.exit_cfg} (use --force to overwrite)")
        return 1
    name = args.name or _ask("name of this server", socket.gethostname()[:32])
    token = args.token or new_token()
    try:
        ports = parse_port_list(args.listen or list(DEFAULT_TLS_PORTS)[:3])
    except (TypeError, ValueError) as exc:
        _err(f"invalid listen port list: {exc}")
        return 2
    if any(not 1 <= port <= 65535 for port in ports):
        _err("listen ports must be in the 1-65535 range")
        return 2
    listen = [ListenSpec(carrier="tls", port=ports[0], path="/ws")] if ports else []
    for port in ports[1:2]:
        listen.append(ListenSpec(carrier="tls", host="0.0.0.0", port=port, path="/ws"))
    for port in ports[2:]:
        listen.append(ListenSpec(carrier="wss", host="0.0.0.0", port=port, path="/ws"))
    cfg = ExitConfig(
        token=token, name=name, cert_auto=True, push_ports=ports,
        speedtest_port=EXIT_DEFAULT_SPEEDTEST,
        connections=max(0, int(getattr(args, "connections", 0) or 0)) or 1,
        engine=_init_engine(args, home),
    )
    _apply_engine_windows(cfg)
    cfg.listen = listen or [ListenSpec(carrier="tls", port=443, path="/ws")]
    try:
        from .certs import ensure_certificate

        cert, key = ensure_certificate(home, args.domain or name or "simurgh.local")
        cfg.cert_file, cfg.key_file = cert, key
    except Exception as exc:  # pragma: no cover - depends on environment
        _warn(f"automatic certificate generation failed: {exc}")
    save_exit(cfg, home.exit_cfg)
    _ok(f"exit config written: {home.exit_cfg}")
    _info("token: stored in exit.toml; the panel's Pair page shares it securely")
    print(f"   ports: {', '.join(str(s.port) for s in cfg.listen)}")
    return 0


def _init_relay(home: Home, args) -> int:
    """Write ``relay.toml``.

    A relay can be installed *before* its exit exists: with ``--pair-later``
    (which is what the one-command installer passes) no token and no address
    are asked for, the file is marked ``unpaired``, the panel starts, and
    ``simurgh join '<link>'`` (or the panel) completes it later.  Without the
    flag nothing changes: an interactive install still asks, and a silent one
    writes the same unpaired file instead of failing at the last step.
    """
    if home.relay_cfg.exists() and not args.force:
        _warn(f"relay config already exists: {home.relay_cfg} (use --force to overwrite)")
        return 1
    dial = getattr(args, "dial", "relay") or "relay"
    pair_later = bool(getattr(args, "pair_later", False))
    # a "pair later" install must not ask anything either: tls is the carrier
    # the exit serves on its first port, and it is what `join` will find
    carrier = (args.carrier or ("tls" if pair_later
                                else _ask("carrier (tls/wss/raw/plain)", "tls"))).lower()
    if carrier not in CARRIERS:
        _err(f"unknown carrier {carrier!r}; choose one of {', '.join(CARRIERS)}")
        return 2
    name = args.name or socket.gethostname()[:32]
    panel_port = args.panel_port or RELAY_DEFAULT_PANEL
    if not 1 <= panel_port <= 65535:
        _err("the panel port must be in the 1-65535 range")
        return 2
    if dial == "exit":
        # reverse mode: this relay is the one that defines the secret, so it
        # never has to be told a token from the other side
        token = args.token or new_token()
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
        cfg.connections = max(0, int(getattr(args, "connections", 0) or 0)) or 1
    else:
        both = bool(getattr(args, "both", False))
        # `--both` means the exit runs on this machine too, so its address needs
        # no question: it is the loopback address (the tunnel port stays the
        # first of --listen / the default 443)
        can_ask = _interactive() and not pair_later
        token = args.token or ""
        if not token and both:
            # one box, two services: the secret is ours to make
            token = new_token()
        elif not token and can_ask:
            token = _ask("token (copy it from the foreign server)")
        exit_host = args.exit_host or ("127.0.0.1" if both else "")
        if not exit_host and can_ask:
            exit_host = _ask("foreign server address (IP or domain)")
        exit_port = args.exit_port or 443
        if not both and not args.exit_port and can_ask and exit_host:
            exit_port = int(_ask("tunnel port on the foreign server", "443"))
        unpaired = not (token and exit_host)
        domain = args.domain or None
        if carrier in ("tls", "wss") and not domain and can_ask and not unpaired:
            if _ask_yes("do you have a domain name for the SNI?", False):
                domain = _ask("domain name")
        cfg = RelayConfig(
            token=token, name=name, panel_port=panel_port,
            unpaired=unpaired,
            exit=ExitEndpoint(carrier=carrier, address=exit_host, port=exit_port,
                              domain=domain, path=args.path or "/ws",
                              insecure_skip_verify=args.insecure,
                              fingerprint=args.fingerprint,
                              split=_split_arg(args)),
        )
        cfg.connections = max(0, int(getattr(args, "connections", 0) or 0)) or 1
    tunnel_port = cfg.tunnel.port if cfg.reverse else cfg.exit.port
    if not 1 <= tunnel_port <= 65535:
        _err("the tunnel port must be in the 1-65535 range")
        return 2
    cfg.engine = _init_engine(args, home)
    _apply_engine_windows(cfg)
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
        _info("open Pair in this panel, create a setup link, and import it in the exit panel")
    elif cfg.unpaired:
        print("   pairing:        not paired yet (the tunnel stays idle)")
        _info("create a setup link in the FOREIGN panel's Pair page")
        _info("then import it on this server's Pair page")
    else:
        print(f"   foreign server: {carrier}://{exit_host}:{exit_port}")
        split = _split_arg(args)
        if split and carrier in ("tls", "wss"):
            print(f"   anti-DPI:       the first {split} byte(s) of the handshake "
                  f"travel alone (split = {split})")
    if not mappings:
        _info("to add a port, open Ports in the web panel")
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


def _relay_dials_loopback(cfg: RelayConfig) -> bool:
    """True when every exit endpoint of this relay is on this very machine."""
    hosts = {cfg.exit.address} | {item.address for item in cfg.pool}
    hosts = {host for host in hosts if host}
    return bool(hosts) and hosts <= {"127.0.0.1", "localhost", "::1"}


def _finalize_single_box(home: Home) -> None:
    """Both roles on one machine (`--role both`): two things differ from the
    two-server setup.

    * the exit's certificate is self-signed and the setup link -- which normally
      carries its fingerprint to the relay -- does not exist here, so the relay
      has to pin it directly (otherwise every handshake fails);
    * the exit listens on the tunnel port *and* on this same machine, so its
      pushed port list must not be mirrored onto the relay: those ports belong
      to the tunnel itself.  User ports are added with `simurgh mapping add`,
      which forwards to a service on this box.
    """
    if not (home.exit_cfg.exists() and home.relay_cfg.exists()):
        return
    try:
        exit_cfg = load_exit(home.exit_cfg)
        relay_cfg = load_relay(home.relay_cfg)
    except (ConfigError, OSError):
        return
    if relay_cfg.reverse or not _relay_dials_loopback(relay_cfg):
        return
    changed = []
    if relay_cfg.accept_push:
        relay_cfg.accept_push = False
        changed.append("the exit's port list is not mirrored")
    if exit_cfg.cert_file and Path(exit_cfg.cert_file).exists():
        try:
            from .certs import fingerprint_of

            pin = fingerprint_of(exit_cfg.cert_file)
        except Exception as exc:  # pragma: no cover - depends on the install
            _warn(f"could not read the exit certificate: {exc}")
            pin = ""
        if pin and relay_cfg.exit.fingerprint != pin:
            relay_cfg.exit.fingerprint = pin
            changed.append("the exit certificate is pinned")
    if changed:
        save_relay(relay_cfg, home.relay_cfg)
        _ok(f"both roles on one server: {', '.join(changed)} "
            "(add user ports with: simurgh mapping add 443 2087)")


def _start_first_run_setup(home: Home, args) -> int:
    """Install a neutral setup panel; the operator selects the role afterward."""
    custom = any((
        getattr(args, "exit_address", ""), getattr(args, "exit_host", ""),
        getattr(args, "token", ""), getattr(args, "listen", None),
        getattr(args, "mapping", None), getattr(args, "name", ""),
        getattr(args, "domain", ""), getattr(args, "carrier", ""),
        getattr(args, "path", ""), getattr(args, "fingerprint", None),
        getattr(args, "insecure", False), getattr(args, "tunnel_host", ""),
        getattr(args, "connections", 0), getattr(args, "panel_port", 0),
        getattr(args, "pair_later", False), getattr(args, "config_only", False),
        getattr(args, "dial", "relay") == "exit",
        getattr(args, "split", 3) != 3,
    ))
    if custom:
        _err("choose a role in the setup panel or `simurgh menu` first; for custom install options, pass --role exit or --role relay. No config was changed.")
        return 2
    home.ensure()
    from . import systemd

    if getattr(args, "no_units", False) or not systemd.available():
        _info("no role has been selected yet. Run `simurgh menu` in an interactive terminal to choose Exit or Relay.")
        return 0
    user, password = _panel_credentials(home)
    report = systemd.install_setup_service(
        home, enable=not getattr(args, "no_enable", False),
        start=not getattr(args, "no_start", False), write=True)
    for path, why in (report.get("errors") or {}).items():
        _err(f"the first-run setup service could not be written to {path}: {why}")
        return 1
    if not report.get("systemd"):
        _info("no role has been selected yet. Run `simurgh menu` in an interactive terminal to choose Exit or Relay.")
        return 0
    if any(result != "ok" for result in (report.get("actions") or {}).values()):
        _err("the first-run setup panel could not be started; choose a role with `simurgh menu`.")
        return 1
    if report.get("actions"):
        _ok("first-run setup panel is running")
    else:
        _ok("first-run setup service is installed")
    _info(f"panel: http://{_public_ip()}:{RELAY_DEFAULT_PANEL}/login")
    _info(f"login: {user}:{password}")
    _info("choose this server's role in the panel, or run `simurgh menu` over SSH")
    return 0


def cmd_install(args) -> int:
    """Create the config, install systemd units, start everything."""
    from . import systemd

    _apply_exit_shortcut(args)
    home = Home(args.home)
    role = args.role
    if role == "auto":
        if args.both:
            role = "exit"  # the roles tuple below will include both services
        elif home.exit_cfg.exists() and not home.relay_cfg.exists():
            role = "exit"
        elif home.relay_cfg.exists() and not home.exit_cfg.exists():
            role = "relay"
        elif home.exit_cfg.exists() and home.relay_cfg.exists():
            args.both = True
            role = "exit"
        else:
            return _start_first_run_setup(home, args)
    home.ensure()
    if (role == "relay" and args.dial != "exit"
            and not args.token and not args.exit_host):
        # A direct relay with no supplied endpoint is panel-first: create its
        # unpaired config now and complete pairing later from the Pair page.
        args.pair_later = True
    roles = ("exit", "relay") if args.both else (role,)
    if not args.config_only:
        for wanted in roles:
            cfg_path = home.exit_cfg if wanted == "exit" else home.relay_cfg
            if cfg_path.exists() and not args.force:
                # Re-running the universal installer is an upgrade/repair, not
                # permission to silently unpair the relay or erase its ports.
                _ok(f"keeping the existing {wanted} config: {cfg_path}")
                continue
            if cfg_path.exists() and args.force:
                backup = _backup_config(cfg_path, f"install-{int(time.time())}")
                if backup:
                    _info(f"previous {wanted} config backed up to {backup}")
            sub = argparse.Namespace(**{**vars(args), "role": wanted})
            code = _init_exit(home, sub) if wanted == "exit" else _init_relay(home, sub)
            if code != 0 and not cfg_path.exists():
                _err(f"could not build the {wanted} config; installation stopped.")
                return 2
        if args.both:
            _finalize_single_box(home)
    report = systemd.install_services(
        home, roles=roles, enable=not args.no_enable, start=not args.no_start,
        user=args.user if args.user else None, write=not args.no_units,
    )
    for path, why in (report.get("errors") or {}).items():
        _warn(f"the systemd units could not be written to {path}: {why}")
    for unit in report.get("units", []):
        _ok(f"created {unit}")
    for svc, result in report.get("actions", {}).items():
        (_ok if result == "ok" else _warn)(f"{svc}: {result}")
    if any(result != "ok" for result in report.get("actions", {}).values()):
        _err("systemd could not complete every requested action; installation is incomplete.")
        return 1
    started = (report.get("systemd") and report.get("write", True)
               and not args.no_start and bool(report.get("actions")))
    if started:
        healthy, detail = _wait_for_panel(
            home, roles, expected_version=__version__)
        if not healthy:
            _err(f"the service was started but the web panel is not healthy: {detail}")
            _err("the installation is NOT being reported as complete; recent service log:")
            for wanted in roles:
                tail = systemd.journal_tail(f"simurgh-{wanted}", lines=30)
                if tail:
                    print(tail)
            return 1
        _ok(f"web panel health check: {detail}")
    print()
    _info(f"web panel: http://{_public_ip()}:{_panel_port_of(home, roles)}/login")
    _info("text menu: simurgh menu")
    _info("status: simurgh status")
    if not report.get("systemd"):
        _warn("systemd is not available here: no service was installed, start it by hand:")
    elif not report.get("write", True):
        _warn("no systemd unit was written (--no-units), start it by hand:")
    else:
        return 0
    for wanted in roles:
        print(f"   simurgh {wanted}")
    return 0


def _wait_for_panel(home: Home, roles, timeout: float = 20.0,
                    expected_version: str | None = None) -> tuple[bool, str]:
    """Require a real loopback ``/healthz`` response after a service restart.

    ``systemctl restart`` returning zero only means systemd accepted the job;
    it says nothing about argparse, imports or the HTTP listener.  Checking the
    actual endpoint prevents the installer from printing a success banner for
    a process which crashed a fraction of a second later.
    """
    import http.client

    ports = sorted({_panel_port_of(home, (role,)) for role in roles})
    deadline = time.monotonic() + timeout
    last = "not attempted"
    while True:
        ready = []
        for port in ports:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1.0)
            try:
                connection.request("GET", "/healthz", headers={"Connection": "close"})
                response = connection.getresponse()
                raw = response.read(4096)
                payload = json.loads(raw.decode("utf-8"))
                version = payload.get("version", "?")
                if (response.status == 200 and payload.get("ok") is True
                        and (expected_version is None or version == expected_version)):
                    ready.append(port)
                    last = f"HTTP 200 on 127.0.0.1:{port} (v{version})"
                elif response.status == 200 and payload.get("ok") is True:
                    last = (f"127.0.0.1:{port} still runs v{version}; "
                            f"waiting for v{expected_version}")
                else:
                    last = f"HTTP {response.status} on 127.0.0.1:{port}"
            except (OSError, ValueError, http.client.HTTPException) as exc:
                last = f"127.0.0.1:{port}: {exc}"
            finally:
                connection.close()
        if len(ready) == len(ports):
            return True, last
        if time.monotonic() >= deadline:
            return False, last
        time.sleep(0.4)


def _panel_port_of(home: Home, roles) -> int:
    if "relay" in roles and home.relay_cfg.exists():
        try:
            return load_relay(home.relay_cfg).panel_port or RELAY_DEFAULT_PANEL
        except (ConfigError, OSError):
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
    home = Home(args.home)
    role = _detect_role(home, args.role)
    names = _service_names(role)
    if action == "status":
        if getattr(args, "detail", False) or getattr(args, "watch", False):
            return cmd_status(args)
        rows = []
        for name in names:
            state = systemd.service_state(name)
            active = state.get("ActiveState", "unknown")
            sub = state.get("SubState", "")
            enabled = state.get("UnitFileState", "")
            rows.append((name, active, sub, enabled))
        cfg_path = home.exit_cfg if role == "exit" else home.relay_cfg
        pairing = None
        if role == "relay" and cfg_path.exists():
            try:
                relay_cfg = load_relay(cfg_path)
                pairing = "paired" if relay_cfg.paired else "unpaired"
            except (ConfigError, OSError):
                pairing = None
        if args.json:
            payload = {"services": [
                {"name": n, "active": a, "sub": s, "enabled": e} for n, a, s, e in rows
            ]}
            if pairing is not None:
                payload["pairing"] = pairing
            print(json.dumps(payload, ensure_ascii=False))
            return 0
        print(f"{__product__} v{__version__}   ({role})")
        print(f"  home:   {home.path}")
        print(f"  config: {cfg_path} {'✔' if cfg_path.exists() else '✘ (missing)'}")
        if pairing == "unpaired":
            print("  pairing: \033[33mnot paired yet\033[0m — create a setup link "
                  "in the exit panel and import it on this panel's Pair page")
        for name, active, sub, enabled in rows:
            mark = "✔" if active == "active" else "✘"
            print(f"  service: {name}: {active}/{sub} enable={enabled} {mark}")
        if home.state.exists():
            try:
                state = json.loads(home.state.read_text())[role]
                tunnels = state.get("tunnels")
                if isinstance(tunnels, (list, tuple, set)):
                    tunnels = len(tunnels)
                print(f"  tunnel:  {'connected' if state.get('connected') or tunnels else 'down'}")
                print(f"  stats:   {json.dumps(state.get('stats', {}).get('totals', {}), ensure_ascii=False)}")
            except (OSError, ValueError, KeyError):
                pass
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
                      on_action=lambda name: _service_action(name, "exit", home))
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
                      on_action=lambda name: _service_action(name, "relay", home))
        try:
            await panel.start()
        except OSError as exc:
            _warn(f"the panel did not start ({exc}); the tunnel keeps running.")
            panel = None
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    writer = asyncio.ensure_future(_state_writer(home, "relay", node, panel))
    if cfg.reverse:
        where = f"tunnel listen: {cfg.tunnel.endpoint()}"
    elif cfg.unpaired:
        where = "not paired yet -- import the exit's setup link on the panel's Pair page"
    else:
        where = f"target: {cfg.exit.describe()}"
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


def _installed_roles(home: Home, fallback: str | None = None) -> tuple[str, ...]:
    """Roles actually configured on this host, for update/restart handoffs."""
    roles = tuple(role for role in ("exit", "relay")
                  if (home.exit_cfg if role == "exit" else home.relay_cfg).exists())
    return roles or (fallback or _detect_role(home, None),)


def _service_action(name: str, role: str, home: Home | None = None) -> dict:
    from . import systemd

    if not systemd.available():
        return {"ok": False, "error": "systemd is not available"}
    action = _SYSTEMCTL_ACTION.get(name, name)
    roles = _installed_roles(home, role) if action == "restart" and home else (role,)
    services = tuple(f"simurgh-{item}" for item in roles)
    return systemd.control(action, services)


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
    engine = _configured_engine(home, args.role)
    if engine == "go":
        binary = go_binary(home)
        if binary and getattr(args, "exec_engine", False):
            # the old, hand-off behaviour: the engine replaces this process and
            # the panel/metrics/alerts go with it
            _ok(f"engine: go ({binary}), exec")
            argv = [str(binary), args.role, "--home", str(home.path)]
            if args.verbose:
                argv.append("--verbose")
            try:
                os.execv(str(binary), argv)
            except OSError as exc:              # pragma: no cover - install
                _warn(f"cannot start the go engine ({exc}); using python")
        elif binary:
            _ok(f"engine: go ({binary}), supervised")
            try:
                return asyncio.run(_run_go(home, args, binary))
            except KeyboardInterrupt:
                return 0
        else:
            _warn("engine is 'go' but no simurgh-go binary was found; "
                  "run tools/build-go.sh (or set SIMURGH_GO_BIN). Using python.")
    try:
        if args.role == "exit":
            return asyncio.run(_run_exit(home, args))
        return asyncio.run(_run_relay(home, args))
    except KeyboardInterrupt:
        return 0


async def _run_go(home: Home, args, binary: str) -> int:
    """Supervise the compiled engine: it does the packets, we do the operator.

    The Go engine has no HTTP listener of its own, so this keeps the panel,
    `/metrics` and the alert channels alive next to it, follows the state file
    it writes every three seconds and restarts it if it falls over.
    """
    from .panel import Panel
    from .supervise import EngineSupervisor

    role = args.role
    cfg = _load_exit_or_fail(home) if role == "exit" else _load_relay_or_fail(home)
    logfile = home.exit_log if role == "exit" else home.relay_log
    setup_logging(args.verbose, logfile=str(logfile))
    supervisor = EngineSupervisor(home, role, binary, cfg, verbose=args.verbose)
    try:
        await supervisor.start()
    except OSError:
        return 1
    panel = None
    if not args.no_panel:
        port = args.panel_port or (RELAY_DEFAULT_PANEL if role == "exit"
                                   else cfg.panel_port or RELAY_DEFAULT_PANEL)
        panel = Panel(home, role, node=supervisor.node, host=args.panel_host,
                      port=port, user=_load_panel_user(home),
                      password=_load_panel_password(home),
                      allow_ips=tuple(getattr(cfg, "allow_ips", ()) or ()),
                      on_action=lambda name: _service_action(name, role, home))
        try:
            await panel.start()
        except OSError as exc:
            _warn(f"the panel did not start ({exc}); the tunnel keeps running.")
            panel = None
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    _ok(f"{role} \"{cfg.name or role}\" is up (engine: go, pid {supervisor.pid}).")
    await stop.wait()
    if panel is not None:
        await panel.stop()
    await supervisor.stop()
    return 0


def go_binary(home: Home) -> str | None:
    """Where the compiled engine lives, if this installation has it."""
    from .engines import find_binary

    return find_binary(home)


def _configured_engine(home: Home, role: str) -> str:
    path = home.exit_cfg if role == "exit" else home.relay_cfg
    try:
        cfg = load_exit(path) if role == "exit" else load_relay(path)
    except (ConfigError, OSError):
        return "python"
    return cfg.engine


def cmd_engine(args) -> int:
    """Show or switch the data plane (python | go) of this installation."""
    home = _home_or_fail(args)
    role = _detect_role(home, args.role)
    path = home.exit_cfg if role == "exit" else home.relay_cfg
    try:
        cfg = load_exit(path) if role == "exit" else load_relay(path)
    except (ConfigError, OSError) as exc:
        _err(str(exc))
        return 2
    want = (args.engine or "").strip().lower()
    if not want:
        binary = go_binary(home)
        print(f"engine: {cfg.engine}")
        print(f"go binary: {binary or 'not built (run tools/build-go.sh)'}")
        return 0
    if want not in ENGINES:
        _err(f"unknown engine {want!r} (use 'python' or 'go')")
        return 2
    if want == "go" and not go_binary(home):
        _warn("no simurgh-go binary yet; run tools/build-go.sh first "
              "(the config is written anyway, python is used until it exists)")
    cfg.engine = want
    try:
        if role == "exit":
            save_exit(cfg, path)
        else:
            save_relay(cfg, path)
    except OSError as exc:
        _err(str(exc))
        return 2
    _ok(f"engine set to {want}. Restart the service to apply: simurgh restart")
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
    role = _detect_role(home, getattr(args, "role", None))
    state: dict = {}
    if home.state.exists():
        try:
            state = json.loads(home.state.read_text())
        except (OSError, ValueError):
            state = {}
    node = state.get(role, {})
    if getattr(args, "json", False):
        print(json.dumps(node, ensure_ascii=False, indent=2))
        return 0
    if not node:
        _warn(f"no live data for {role}. Is the service running? (simurgh status/up)")
        return cmd_service(argparse.Namespace(role=role, service_action="status",
                                             home=args.home, json=False))
    print(f"\033[1m{__product__} v{__version__}\033[0m — {home.path}")
    cfg_path = home.exit_cfg if role == "exit" else home.relay_cfg
    print(f"  config: {cfg_path} {'✔' if cfg_path.exists() else '✘'}")
    if role == "relay" and cfg_path.exists():
        try:
            relay_cfg = load_relay(cfg_path)
        except (ConfigError, OSError):
            relay_cfg = None
        if relay_cfg is not None and not relay_cfg.paired:
            print("  pairing:  \033[33mnot paired yet\033[0m — on the exit run "
                  "`simurgh link`, then here: simurgh join '<link>'")
    if role == "relay":
        connected = node.get("connected")
        tunnel = "\033[32mconnected\033[0m" if connected else "\033[31mdown\033[0m"
        print(f"  tunnel:   {tunnel}  ({node.get('current_exit') or '—'})")
        print(f"  rtt:      {node.get('rtt_ms') and round(node['rtt_ms'], 1) or '—'} ms")
        print(f"  reconnects: {node.get('reconnects', 0)}   error: {node.get('last_error') or '—'}")
    else:
        count = node.get("tunnel_count")
        if count is None:
            tunnels = node.get("tunnels", 0)
            count = len(tunnels) if isinstance(tunnels, (list, tuple, set)) else tunnels
        print(f"  tunnels:  {count}")
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
    if getattr(args, "watch", False):
        interval = getattr(args, "interval", 2.0)
        try:
            while True:
                time.sleep(interval)
                print("\033[2J\033[H", end="")
                cmd_status(argparse.Namespace(home=args.home, role=role, json=False,
                                              watch=False, interval=interval))
        except KeyboardInterrupt:
            return 0
    return 0


def cmd_logs(args) -> int:
    home = Home(args.home)
    role = args.role if args.role == "panel" else _detect_role(home, args.role)
    name = {"exit": home.exit_log, "relay": home.relay_log,
            "panel": home.panel_log}.get(role, home.relay_log)
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
    if _detect_role(home, None) == "exit" and not home.relay_cfg.exists():
        _err("the speed test runs on the Iranian (relay) side; on the exit it only "
             "needs speedtest_port to stay open (localhost).")
        return 2
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
            result = await run_speedtest(
                node, seconds=args.seconds, streams=args.streams,
                udp=args.udp, latency=not args.no_latency)
        finally:
            await node.stop()
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            _print_speedtest(result)
        return 0

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:
        return 0


def cmd_tune(args) -> int:
    """`simurgh tune [gaming|balanced|speed]` -- the behaviour in one word."""
    from . import tune

    if args.show or not args.profile:
        # listing the profiles needs no installation: it is documentation
        rows = tune.describe(args.profile or None)
        if args.json:
            print(json.dumps({"profiles": rows}, indent=2))
            return 0
        for row in rows:
            print(f"  {row['name']}  --  {row['title']}")
            print(f"    {row['summary']}")
            shared = [(key, value) for key, value in row["relay"].items()
                      if row["exit"].get(key) == value]
            print("    both files: " + ", ".join(
                f"{key}={_tune_value(value)}" for key, value in shared))
            relay_only = [(key, value) for key, value in row["relay"].items()
                          if row["exit"].get(key) != value]
            if relay_only:
                print("    relay only: " + ", ".join(
                    f"{key}={_tune_value(value)}" for key, value in relay_only))
        if not args.profile:
            print()
            print("  use it: simurgh tune gaming      (add --dry-run to look first)")
        return 0

    home = _home_or_fail(args)

    try:
        profile = tune.get(args.profile)
    except KeyError as exc:
        _err(str(exc.args[0] if exc.args else exc))
        return 2

    role = _detect_role(home, args.role)
    if role == "relay":
        cfg = _load_relay_or_fail(home)
        path = home.relay_cfg
    else:
        cfg = _load_exit_or_fail(home)
        path = home.exit_cfg

    changes = tune.apply(cfg, role, profile.name)
    # `wrote` must only turn true once the file is really on disk, so the JSON
    # is printed once, on the way out
    payload = {"profile": profile.name, "role": role, "config": str(path),
               "changes": changes, "wrote": False}
    if not args.json:
        print(f"  profile: {profile.name} -- {profile.title}")
        for change in changes:
            print(f"  {path.name}: {change['key']} "
                  f"{_tune_value(change['old'])} -> {_tune_value(change['new'])}")
    if not changes:
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            _ok(f"{path.name} already matches the {profile.name} profile")
        return 0
    if args.dry_run:
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            _warn("nothing was written (--dry-run)")
        return 0
    try:
        save_exit(cfg, path) if role == "exit" else save_relay(cfg, path)
    except OSError as exc:
        _err(f"cannot write {path}: {exc}")
        if args.json:
            print(json.dumps(payload, indent=2))
        return 1
    payload["wrote"] = True
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _ok(f"{path.name}: {len(changes)} key(s) written")
        print("apply it now: simurgh restart")
    return 0


def _tune_value(value) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _print_speedtest(result: dict) -> None:
    """Human output: throughput first, then what users actually feel."""
    print(f"  download: \033[32m{result['download_mbps']:.1f} Mbps\033[0m "
          f"({human_bytes(result['download_bytes'])})")
    print(f"  upload:   \033[32m{result['upload_mbps']:.1f} Mbps\033[0m "
          f"({human_bytes(result['upload_bytes'])})")
    latency = result.get("latency")
    if latency and latency.get("idle_ms") is not None:
        loaded = latency.get("loaded_ms")
        grade = latency.get("grade") or "?"
        if loaded is None:
            print(f"  latency:  {latency['idle_ms']:.1f} ms idle "
                  "(the load phase produced no samples)")
        else:
            colour = {"A": "\033[32m", "B": "\033[32m", "C": "\033[33m",
                      "D": "\033[33m", "F": "\033[31m"}.get(grade, "")
            print(f"  latency:  {latency['idle_ms']:.1f} ms idle -> "
                  f"{colour}{loaded:.1f} ms\033[0m under load "
                  f"({latency['increase_ms']:+.1f} ms, bufferbloat "
                  f"{colour}{grade}\033[0m)")
            print(f"  jitter:   {latency.get('jitter_ms') or 0:.1f} ms under load "
                  f"({latency.get('jitter_idle_ms') or 0:.1f} ms idle, "
                  f"p95 {latency.get('loaded_p95_ms') or 0:.1f} ms)")
    udp = result.get("udp")
    if udp:
        if udp.get("error"):
            print(f"  udp:      \033[33mnot measured\033[0m ({udp['error']})")
        else:
            loss = udp.get("loss_percent") or 0.0
            colour = "\033[32m" if loss < 1 else (
                "\033[33m" if loss < 5 else "\033[31m")
            print(f"  udp:      {colour}{loss:.2f}% loss\033[0m of "
                  f"{udp['sent']} datagrams, "
                  f"{udp.get('rtt_ms') or 0:.1f} ms rtt, "
                  f"jitter {udp.get('jitter_ms') or 0:.1f} ms")
    streams = result.get("streams") or 1
    extra = f", {streams} streams" if streams > 1 else ""
    print(f"  test time: {result['seconds']:.1f} s{extra}")


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
    split = _split_arg(args)
    for spec in cfg.listen:
        if spec.reverse:
            # the exit is the one that dials here, so the anti-DPI split lives
            # on this side; the relay only sees two TCP segments of one record
            spec.split = split
            if getattr(args, "host", ""):
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

    # A reverse relay waits for the exit, so the link the *other* server needs
    # is the exit's one. That holds whether or not this server also runs an
    # exit itself (`--role both`), which is why exit.toml is not decisive here:
    # with both configs present, the "import me on the Iranian server" link
    # would be useless.
    reverse_cfg = None
    if home.relay_cfg.exists():
        try:
            relay_cfg = load_relay(home.relay_cfg)
        except (ConfigError, OSError):
            relay_cfg = None
        if relay_cfg is not None and relay_cfg.reverse:
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
    """Complete a server from the other side's one-string setup link.

    On a relay this is also how a server that was installed with
    ``--pair-later`` is paired: an unpaired config is completed in place (no
    ``--force`` needed, and nothing the install chose is thrown away), then the
    service is restarted so the tunnel comes up right away.
    """
    home = Home(args.home).ensure()
    from .links import (LinkError, fetch_setup, merge_relay_payload,
                        relay_config_from_payload)

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
    existing = None
    if home.relay_cfg.exists():
        try:
            existing = load_relay(home.relay_cfg)
        except (ConfigError, OSError):
            existing = None
    if existing is not None and existing.paired and not args.force:
        _err(f"the relay config already exists ({home.relay_cfg}); use --force to overwrite.")
        return 2
    if existing is not None:
        # installed but never paired: keep the panel port, the engine, the
        # limits and the ports this server already forwards
        try:
            merged = merge_relay_payload(existing, payload)
        except (KeyError, ValueError, ConfigError) as exc:
            _err(f"the link is incomplete: {exc}")
            return 2
        if args.host:
            existing.exit.address = args.host
        backup = _backup_config(home.relay_cfg, "pair")
        save_relay(existing, home.relay_cfg)
        cfg = existing
        _ok(f"relay paired with {merged['exit']}")
        if backup:
            _info(f"previous config kept at {backup}")
    else:
        cfg = relay_config_from_payload(payload)
        if args.host:
            cfg.exit.address = args.host
        save_relay(cfg, home.relay_cfg)
        _ok(f"relay config built from the link: {home.relay_cfg}")
    print(f"   target: {cfg.exit.describe()}")
    for m in cfg.mappings:
        print(f"   mapping: {m.listen} → {m.target_host}:{m.target_port}")
    _restart_after_pairing(home, "relay", args)
    return 0


def _backup_config(path, tag: str) -> str:
    """Copy a config aside before pairing; returns the backup path ('' if none)."""
    if not path.exists():
        return ""
    target = path.with_suffix(path.suffix + f".bak-{tag}")
    try:
        shutil.copy2(path, target)
        target.chmod(0o600)
        return str(target)
    except OSError:
        return ""


def _restart_after_pairing(home: Home, role: str, args) -> None:
    """Make the new config live now, not at the next reboot.

    Only touches systemd when the unit is actually running, and never fails the
    pairing because of it: the config is already on disk either way.
    """
    if getattr(args, "no_restart", False):
        return
    from . import systemd

    if not systemd.available():
        _info("apply it with: simurgh restart")
        return
    service = f"simurgh-{role}"
    state = systemd.service_state(service)
    if state.get("ActiveState") != "active":
        _info("apply it with: simurgh restart")
        return
    answer = systemd.control("restart", (service,)).get(service)
    if answer == "ok":
        _ok(f"{service}: restarted with the new config")
    else:
        _warn(f"{service}: {answer}")


def _detect_role(home: Home, role: str | None) -> str:
    """Resolve the role from ``--role``, or from whichever config exists.

    Installations normally have exactly one config file, so commands like
    ``status``, ``logs``, ``doctor`` and ``web`` do not need ``--role``.
    """
    if role in ("exit", "relay"):
        return role
    exit_cfg, relay_cfg = home.exit_cfg.exists(), home.relay_cfg.exists()
    if exit_cfg and not relay_cfg:
        return "exit"
    if relay_cfg and not exit_cfg:
        return "relay"
    return "relay"


def _service_running(home: Home, role: str) -> bool:
    """True when this installation's service is alive.

    ``state.json`` is rewritten every three seconds while a node runs, so a
    recent timestamp is a reliable "yes, this home is up" signal.
    """
    try:
        data = json.loads(home.state.read_text())
        updated = float(data.get("updated") or 0)
    except (OSError, ValueError):
        return False
    return bool(data.get(role)) and (time.time() - updated) < 15


def cmd_doctor(args) -> int:
    """Diagnose the usual reasons a tunnel does not come up."""
    home = Home(args.home)
    problems = 0
    print(f"\033[1m{__product__} v{__version__} — health check\033[0m  ({home.path})")
    if not home.path.exists():
        _err(f"installation folder not found: {home.path}")
        return 2

    role = _detect_role(home, args.role)
    cfg_path = home.exit_cfg if role == "exit" else home.relay_cfg
    if not cfg_path.exists():
        _err(f"config file not found: {cfg_path}")
        return 2
    _ok(f"config: {cfg_path}  (role: {role})")
    from .engines import describe

    engine = describe(home)
    if engine["binary"]:
        _ok(f"data engine: go ({engine['version'] or engine['binary']})")
    else:
        _info("data engine: python (build the go engine with tools/build-go.sh)")
    running = _service_running(home, role)
    if running:
        _ok(f"the {role} service is running")

    if role == "exit":
        cfg = _load_exit_or_fail(home)
        if not cfg.token:
            _err("the token is empty.")
            problems += 1
        reverse = [s for s in cfg.listen if s.enabled and s.reverse]
        listening = [s for s in cfg.listen if s.enabled and not s.reverse]
        if reverse:
            _info("reverse mode: the relay listens, this server dials it")
        for spec in reverse:
            try:
                with socket.create_connection((spec.dial, spec.port), timeout=5):
                    _ok(f"relay reachable: {spec.carrier}://{spec.dial}:{spec.port}")
            except OSError as exc:
                _err(f"cannot reach the relay {spec.dial}:{spec.port}: {exc}")
                problems += 1
            if not (spec.fingerprint or spec.insecure_skip_verify):
                _warn("no fingerprint pinned for the relay certificate "
                      "(set fingerprint in [[listen]] once you have it)")
        for spec in listening:
            if free_port(spec.port, spec.host):
                _ok(f"port {spec.carrier}://{spec.host}:{spec.port} is free")
            elif running:
                _ok(f"port {spec.host}:{spec.port} is in use by our service")
            else:
                _warn(f"port {spec.host}:{spec.port} is already in use")
        if not (reverse or listening):
            _err("no enabled tunnel endpoint in exit.toml")
            problems += 1
        if listening:
            for path, label in ((cfg.cert_file, "certificate"), (cfg.key_file, "key")):
                if path and Path(path).exists():
                    _ok(f"{label}: {path}")
                elif path:
                    _warn(f"{label} not found: {path}")
        from . import systemd

        if systemd.available():
            _ok("systemd is available")
        elif running:
            _ok("running without systemd")
        else:
            _warn("no systemd; start it manually: simurgh exit")
        return 0 if problems == 0 else 1

    cfg = _load_relay_or_fail(home)
    if cfg.unpaired:
        _warn("this relay is not paired yet — that is a state, not a fault:")
        _info("on the exit run `simurgh link`, then here: simurgh join '<link>'")
        _info("(or paste the link on the panel's Pair page)")
    elif not cfg.token:
        _err("the token is empty (copy it from the foreign server).")
        problems += 1
    else:
        _ok(f"token: {cfg.token[:6]}…{cfg.token[-4:]}")
    if cfg.reverse:
        spec = cfg.tunnel
        _info("reverse mode: this server listens and the foreign exit dials it")
        if not spec.enabled:
            _err("the [tunnel] listener is disabled.")
            problems += 1
        elif free_port(spec.port, spec.host):
            _warn(f"nothing is listening on {spec.host}:{spec.port} yet "
                  "(is the service running?)")
        else:
            _ok(f"port {spec.host}:{spec.port} is in use"
                + (" by our service" if running else " (another program?)"))
        if spec.carrier in ("tls", "wss"):
            cert = spec.cert_file or str(Path(home.path) / "cert" / "cert.pem")
            if Path(cert).exists():
                try:
                    from .certs import fingerprint_of

                    _ok(f"certificate fingerprint: {fingerprint_of(cert)}")
                    _info("the exit must pin this fingerprint (it arrives in the "
                          "setup link)")
                except Exception as exc:               # pragma: no cover
                    _warn(f"could not read the certificate: {exc}")
            else:
                _warn(f"certificate not found yet: {cert} "
                      "(it is created when the service starts)")
    else:
        addresses = [(ep.address, ep.port, ep.describe()) for ep in cfg.endpoints()]
        if cfg.unpaired:
            _info("the exit address arrives with the setup link, so there is "
                  "nothing to dial yet")
        elif not addresses:
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
        elif running:
            _ok(f"port {m.listen} is in use by our service → "
                f"{m.target_host}:{m.target_port}")
        else:
            _warn(f"port {m.listen} is in use (maybe our own service or another one)")
    if free_port(cfg.panel_port):
        _ok(f"panel port {cfg.panel_port} is free")
    elif running:
        _ok(f"panel port {cfg.panel_port} is in use by our service")
    else:
        _warn(f"panel port {cfg.panel_port} is in use")
    return 0 if problems == 0 else 1


def cmd_web(args) -> int:
    home = _home_or_fail(args)
    role = _detect_role(home, args.role)
    args.role = role
    cfg = _load_relay_or_fail(home) if role == "relay" else _load_exit_or_fail(home)
    port = args.port or (cfg.panel_port if role == "relay" else RELAY_DEFAULT_PANEL)
    host = args.host
    user, password = _panel_credentials(home)
    link_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    from .panel import Panel

    async def go():
        panel = Panel(home, args.role, node=None, host=host, port=port,
                      user=user, password=password)
        stop = asyncio.Event()
        _install_signal_handlers(stop)
        await panel.start()
        print(f"  panel: http://{link_host}:{port}   user: {user}   pass: {password}")
        if args.link:
            # one link, one login: hand it to the operator instead of a password
            ttl = float(args.link_ttl or 600)
            made = panel.login_tokens.create("cli", ttl)
            _ok(f"one-time link (valid {int(ttl)}s, single use): "
                f"http://{link_host}:{port}/?t={made['token']}")
            _info("open it in the browser; the password prompt is skipped")
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


def cmd_cert(args) -> int:
    """`simurgh cert [status|issue|renew]` -- Let's Encrypt, wired in."""
    from . import acme

    home = _home_or_fail(args)
    role = _detect_role(home, args.role)
    if role == "relay":
        cfg = _load_relay_or_fail(home)
        path = home.relay_cfg
    else:
        cfg = _load_exit_or_fail(home)
        path = home.exit_cfg

    if args.action == "issue":
        if not args.domain:
            _err("a domain is required: simurgh cert issue panel.example.com "
                 "[--webroot DIR | --standalone]")
            return 2
        reload_cmd = args.reload or (
            f"systemctl restart simurgh-{role}"
            if shutil.which("systemctl") else "simurgh restart")
        try:
            result = acme.issue(
                args.domain, additional_domains=args.extra_domains,
                email=args.email or "", webroot=args.webroot or "",
                standalone=bool(args.standalone), staging=bool(args.staging),
                prefer=args.client, reload_cmd=reload_cmd,
                config_dir=args.config_dir or "", binary=args.acme_sh or "")
        except acme.AcmeError as exc:
            _err(str(exc))
            return 1
        for step in result.steps:
            _info(step)
        if args.no_write:
            _warn("certificate obtained; the config was left alone (--no-write)")
            return 0
        try:
            changed = acme.wire(cfg, role, result.cert_file, result.key_file,
                                port=args.port or None)
        except acme.AcmeError as exc:
            _err(str(exc))
            return 1
        try:
            save_exit(cfg, path) if role == "exit" else save_relay(cfg, path)
        except OSError as exc:
            _err(f"cannot write {path}: {exc}")
            return 1
        if args.port:
            _ok(f"{path.name}: [[listen]] port {args.port} now serves "
                f"{result.cert_file}")
        else:
            _ok(f"{path.name}: {', '.join(changed)} -> {result.cert_file}")
        _info("the certificate is renewed by the client itself; the reload hook "
              f"runs `{reload_cmd}` after each renewal")
        if args.apply:
            _info(f"applying now: {reload_cmd}")
            ok, output = acme.reload_now(reload_cmd)
            for line in output.splitlines():
                print(f"  {line}")
            (_ok if ok else _warn)(
                f"reload hook: {reload_cmd} "
                + ("ran" if ok else "failed -- run it yourself"))
            return 0 if ok else 1
        print("apply it now: simurgh restart")
        return 0

    if args.action == "renew":
        try:
            name, ok, output = acme.renew(prefer=args.client,
                                          config_dir=args.config_dir or "")
        except acme.AcmeError as exc:
            _err(str(exc))
            return 1
        for line in output.splitlines():
            print(f"  {line}")
        if ok:
            _ok(f"{name} renewed everything that was due")
            return 0
        _err(f"{name} reported a problem (see above)")
        return 1

    # status
    found = acme.describe_clients()
    rows = acme.status(cfg, role)
    if getattr(args, "json", False):
        print(json.dumps({"role": role, "config": str(path), "client": found,
                          "ok": all(r["exists"] and (r["days"] or 0) > 14
                                    for r in rows) if rows else True,
                          "certificates": rows}, indent=2))
        return 0 if all(r["exists"] and (r["days"] or 0) > 14
                         for r in rows) else 1
    print(f"{__product__} v{__version__}   role={role}   "
          f"acme client: {found or 'none'}")
    if not found:
        print("  " + acme.install_hint().replace("\n", "\n  "))
    if not rows:
        print(f"  no tls/wss listener in {path.name}; nothing to certify")
        return 0
    problems = 0
    self_signed = 0
    for row in rows:
        days = row["days"]
        if not row["cert_file"]:
            _warn(f"{row['endpoint']}: no certificate configured "
                  "(cert_auto generates a self-signed one at start)")
            problems += 1
            continue
        if not row["exists"]:
            _warn(f"{row['endpoint']}: {row['cert_file']} is missing")
            problems += 1
            continue
        if days is None:
            _warn(f"{row['endpoint']}: no certificate could be read from "
                  f"{row['cert_file']}")
            problems += 1
            continue
        state = "ok" if days > 14 else "expiring"
        if state == "expiring":
            problems += 1
        mark = "  (self-signed)" if row["note"] == "generated" else ""
        if mark:
            self_signed += 1
        line = (f"  {row['endpoint']}: {row['cert_file']}  "
                f"{days if days is not None else '?'} day(s) left  "
                f"{row['fingerprint'][:23]}…{mark}")
        (_ok if state == "ok" else _warn)(line)
    if self_signed:
        _info("self-signed certificates are fine for the tunnel itself; for a "
              "panel or a website use `simurgh cert issue <domain>`")
    return 1 if problems else 0


def cmd_alert(args) -> int:
    """`simurgh alert [status|test|events]` -- does the operator's phone ring?"""
    from .alerts import EVENTS, alerts_from_config

    home = _home_or_fail(args)
    role = _detect_role(home, args.role)
    path = home.exit_cfg if role == "exit" else home.relay_cfg
    try:
        cfg = load_exit(path) if role == "exit" else load_relay(path)
    except (ConfigError, OSError) as exc:
        _err(f"cannot read {path}: {exc}")
        return 2
    alerts = alerts_from_config(cfg, name=cfg.name or role)

    if args.action == "test":
        if not alerts.enabled:
            _warn("no alert channel is configured (telegram_token/telegram_chat "
                  "or alert_webhook) -- nothing was sent")
            return 1
        ok = alerts.test()
        if ok:
            _ok(f"test alert sent through {alerts.describe()}")
            return 0
        _err(f"the alert channel did not accept the message "
             f"({alerts.last_error or 'no answer'})")
        return 1

    if args.action == "events":
        if args.extra:
            wanted = [str(x) for x in args.extra]
            unknown = [x for x in wanted if x not in EVENTS and x != "all"]
            if unknown:
                _err(f"unknown event(s): {', '.join(unknown)}")
                print("known events: " + ", ".join(EVENTS) + ", all")
                return 2
            if role == "exit":
                cfg.alert_events = wanted
                save_exit(cfg, path)
            else:
                cfg.alert_events = wanted
                save_relay(cfg, path)
            _ok(f"alert events for the {role}: {', '.join(wanted)}")
            print("restart the service to apply: simurgh restart")
            return 0
        current = alerts.events
        print(f"alert events for the {role}: {', '.join(current) or '(none)'}")
        print("known events: " + ", ".join(EVENTS) + ", all")
        return 0

    # status
    print(f"{__product__} v{__version__}   role={role}   name={cfg.name or '-'}")
    print(f"  channels: {alerts.describe()}")
    if alerts.telegram_token and alerts.telegram_chat:
        print(f"  telegram: chat {alerts.telegram_chat} "
              f"(api {alerts.telegram_api}, token {_mask_token(alerts.telegram_token)})")
    if alerts.webhook:
        print(f"  webhook:  {alerts.webhook}")
    print(f"  events:   {', '.join(alerts.events) or '(none)'}")
    if not alerts.enabled:
        print("  add telegram_token + telegram_chat, or alert_webhook, to "
              f"{path.name} and restart")
    return 0


def _mask_token(value: str) -> str:
    value = str(value or "")
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def cmd_update(args) -> int:
    """Bring this install to the newest release (see simurgh/update.py)."""
    from . import update as updater

    home = Home(args.home)
    try:
        if args.check:
            info = updater.check()
            if getattr(args, "json", False):
                print(json.dumps(info, ensure_ascii=False, indent=2))
                return 0 if not info["available"] else 3
            print(f"{__product__} {info['current']}   newest release: {info['latest']}")
            if info["available"]:
                published = f" ({info['published'][:10]})" if info["published"] else ""
                print(f"  an update is available: {info['tag']}{published}")
                if info.get("self_update_supported") is False:
                    print("  this system-library install must be upgraded with apt/dnf "
                          "or by installing the matching release package/tarball")
                    print(f"  release: {info['notes_url']}")
                else:
                    print(f"  run `simurgh update` to install it   {info['notes_url']}")
                return 3
            print("  this install is already the newest release")
            return 0
        result = updater.update(home.path, force=args.force)
        if getattr(args, "json", False):
            restart_code = 0
            if args.restart:
                # Keep stdout machine-readable while still honouring --restart.
                import contextlib
                import io

                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    restart_code = _restart_updated_service(
                        home, result.get("latest"))
                result["restart_ok"] = restart_code == 0
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return restart_code
        if result.get("available") is False and result.get("action") == "up to date":
            print(f"nothing to do: {__version__} is the newest release "
                  f"({result['latest']})")
            return 0
        _ok(f"updated to {result.get('tag') or result.get('latest')}")
        print(f"  {result.get('action', '')}")
        for step in result.get("steps", []) or []:
            print(f"  - {step}")
        if result.get("notes_url"):
            print(f"  release notes: {result['notes_url']}")
        if args.restart:
            return _restart_updated_service(home, result.get("latest"))
        print("  restart the service to run the new version: "
              "simurgh restart" if shutil.which("systemctl") else
              "  restart the node to run the new version")
        return 0
    except updater.UpdateError as exc:
        _err(str(exc))
        return 1


def _restart_updated_service(home: Home, expected_version: str | None = None) -> int:
    """Restart and require the new panel version, not a surviving old one."""
    roles = _installed_roles(home)
    role = roles[0]
    result = _service_action("restart", role, home)
    if result.get("ok") is False:
        _err(str(result.get("error") or "service control is not available"))
        return 1
    failed = False
    for service, text in result.items():
        if text == "ok":
            _ok(f"{service}: restart")
        else:
            _err(f"{service}: {text}")
            failed = True
    if failed:
        return 1
    healthy, detail = _wait_for_panel(
        home, roles, timeout=25.0, expected_version=expected_version)
    if not healthy:
        _err(f"updated files were installed, but the panel did not recover: {detail}")
        _err("inspect: simurgh logs -n 80  (or journalctl -u simurgh-" + role + ")")
        return 1
    _ok(f"updated service is healthy: {detail}")
    return 0


def cmd_version(args) -> int:
    print(f"{__product__} v{__version__}")
    print(f"python {sys.version.split()[0]}   home={Home(args.home).path}")
    return 0


# ----------------------------------------------------------- first-run panel
def cmd_setup(args) -> int:
    """Serve the authenticated first-run role chooser."""
    from .panel import Panel

    home = Home(args.home).ensure()
    user, password = _panel_credentials(home)

    async def go() -> int:
        panel = Panel(home, "setup", node=None,
                      host=getattr(args, "panel_host", "0.0.0.0"),
                      port=getattr(args, "panel_port", RELAY_DEFAULT_PANEL),
                      user=user, password=password)
        try:
            await panel.start()
        except OSError as exc:
            _err(f"the setup panel could not start: {exc}")
            return 1
        stop = asyncio.Event()
        _install_signal_handlers(stop)
        _ok(f"first-run setup panel is listening on {panel.host}:{panel.port}")
        await stop.wait()
        await panel.stop()
        return 0

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:
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
    p.add_argument("--role", choices=("exit", "relay", "auto"), default="auto",
                   help="advanced override; fresh hosts choose in the setup panel or menu")
    p.add_argument("--both", action="store_true", help="both services on this server")
    p.add_argument("--name")
    p.add_argument("--token")
    p.add_argument("--listen", help="exit ports, e.g. 443,8443")
    p.add_argument("--exit", dest="exit_address", default="",
                   help="foreign endpoint: HOST[:PORT] (default port 443; IPv6 [ADDR]:PORT)")
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
    p.add_argument("--connections", type=int, default=0,
                   help="tunnel connections to keep open (2-16 help on long, lossy paths)")
    p.add_argument("--mapping", action="append", help="initial mapping: 443:443")
    p.add_argument("--split", type=int, default=3, help="bytes of the first write that travel in their own TCP segment (anti-DPI); 0 disables it")
    p.add_argument("--panel-port", type=int, default=0)
    p.add_argument("--engine", choices=ENGINES, default="",
                   help="data plane: python (default) or go (see tools/build-go.sh)")
    p.add_argument("--user", default="")
    p.add_argument("--pair-later", "--unpaired", dest="pair_later",
                   action="store_true",
                   help="write the relay config without its exit (no token or "
                        "address questions); pair it later with `simurgh join` "
                        "or from the panel")
    # keep the long-standing behaviour, but say what it does: this skips the
    # config and only (re)installs the services
    p.add_argument("--config-only", action="store_true",
                   help="keep the existing config and only install the service")
    p.add_argument("--no-units", action="store_true",
                   help="write the config but no systemd unit (run it by hand)")
    p.add_argument("--no-enable", action="store_true")
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("init", help="write the config files only")
    p.add_argument("--role", choices=("exit", "relay", "auto"), default="auto",
                   help="advanced override; fresh hosts choose in the setup panel or menu")
    p.add_argument("--name")
    p.add_argument("--token")
    p.add_argument("--listen")
    p.add_argument("--exit", dest="exit_address", default="",
                   help="foreign endpoint: HOST[:PORT] (default port 443; IPv6 [ADDR]:PORT)")
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
    p.add_argument("--connections", type=int, default=0,
                   help="tunnel connections to keep open (2-16 help on long, lossy paths)")
    p.add_argument("--mapping", action="append")
    p.add_argument("--split", type=int, default=3, help="bytes of the first write that travel in their own TCP segment (anti-DPI); 0 disables it")
    p.add_argument("--panel-port", type=int, default=0)
    p.add_argument("--engine", choices=ENGINES, default="",
                   help="data plane to use (default: python; go needs tools/build-go.sh)")
    p.add_argument("--pair-later", "--unpaired", dest="pair_later",
                   action="store_true",
                   help="write a relay config without its exit; pair it later")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    for role, helptext in (("exit", "run the foreign server (exit)"),
                           ("relay", "run the Iranian server (relay)")):
        p = sub.add_parser(role, help=helptext)
        p.add_argument("--role", default=role, help=argparse.SUPPRESS)
        # v3.2.0 generated `simurgh relay --home ...` in its systemd unit.
        # Accept that legacy order during upgrades; new units put --home first.
        p.add_argument("--home", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        p.add_argument("--no-panel", action="store_true", help="without the web panel")
        p.add_argument("--panel-host", default="0.0.0.0")
        p.add_argument("--panel-port", type=int, default=0)
        p.add_argument("--verbose", action="store_true")
        p.add_argument("--exec", dest="exec_engine", action="store_true",
                       help="hand the process over to the go engine (no panel, "
                            "no /metrics, no alerts)")
        p.set_defaults(func=cmd_run)

    p = sub.add_parser("up", help="start the service")
    p.add_argument("--role", choices=("exit", "relay"), default=None, help="exit | relay (auto-detected by default)")
    p.add_argument("--fg", action="store_true", help="run in the terminal (no systemd)")
    p.set_defaults(func=lambda a: cmd_run(argparse.Namespace(
        role=a.role, home=a.home, verbose=False, no_panel=False,
        panel_host="0.0.0.0", panel_port=0, exec_engine=False)) if a.fg else cmd_service(
        argparse.Namespace(role=a.role, service_action="start", home=a.home, json=False)))

    for action, helptext in (("start", "start the service (systemd)"),
                             ("stop", "stop the service"),
                             ("down", "stop the service"),
                             ("restart", "restart the service"),
                             ("enable", "start automatically at boot"),
                             ("disable", "do not start at boot"),
                             ("status", "service status")):
        p = sub.add_parser(action, help=helptext)
        p.add_argument("--role", choices=("exit", "relay"), default=None, help="exit | relay (auto-detected by default)")
        p.add_argument("--json", action="store_true")
        if action == "status":
            p.add_argument("--detail", "-d", action="store_true",
                           help="detailed live tunnel, traffic and mapping report")
            p.add_argument("--watch", "-w", action="store_true",
                           help="refresh the detailed report every few seconds")
            p.add_argument("--interval", type=float, default=2.0)
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
    p.add_argument("--role", choices=("exit", "relay", "panel"), default=None, help="exit | relay (auto-detected by default)")
    p.add_argument("-n", "--lines", type=int, default=80)
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("speedtest",
                       help="measure the real speed through the tunnel")
    p.add_argument("--seconds", type=int, default=6)
    p.add_argument("--streams", type=int, default=1,
                   help="tunnel streams to sum (default 1)")
    p.add_argument("--udp", action="store_true",
                   help="also measure UDP loss and jitter through the tunnel")
    p.add_argument("--no-latency", action="store_true",
                   help="skip the latency probe (idle and under load)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_speedtest)

    p = sub.add_parser("tune", help="pick a behaviour profile for this server")
    p.add_argument("profile", nargs="?",
                   help="gaming | balanced | speed (omit to list them)")
    p.add_argument("--role", choices=("exit", "relay"), default=None,
                   help="exit | relay (auto-detected by default)")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would change, write nothing")
    p.add_argument("--show", action="store_true", help="list the profiles")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tune)

    p = sub.add_parser("link", help="build the setup link for the other server")
    p.add_argument("--host", default="")
    p.add_argument("--panel-port", type=int, default=0)
    p.add_argument("--show", action="store_true", help="print just the raw link")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("join", help="pair this server from a setup link")
    p.add_argument("link")
    p.add_argument("--host", default="", help="override the exit address")
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("--split", type=int, default=3, help="bytes of the first write that travel in their own TCP segment (anti-DPI); 0 disables it")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-restart", action="store_true",
                   help="write the config but do not restart the service")
    p.set_defaults(func=cmd_join)

    p = sub.add_parser("engine", help="show or switch the data plane (python | go)")
    p.add_argument("engine", nargs="?", choices=ENGINES, default=None)
    p.add_argument("--role", choices=("exit", "relay"), default=None)
    p.set_defaults(func=cmd_engine)

    p = sub.add_parser("doctor", help="health check and diagnostics")
    p.add_argument("--role", choices=("exit", "relay"), default=None, help="exit | relay (auto-detected by default)")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("web", help="run only the web panel")
    p.add_argument("--role", choices=("exit", "relay"), default=None, help="exit | relay (auto-detected by default)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--link", action="store_true",
                   help="print a one-time login link instead of a password")
    p.add_argument("--link-ttl", dest="link_ttl", type=int, default=600,
                   help="how long that link stays valid, in seconds (default: 600)")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("cert", help="Let's Encrypt certificates (acme.sh / certbot)")
    p.add_argument("action", nargs="?", default="status",
                   choices=["status", "issue", "renew"])
    p.add_argument("domain", nargs="?", default="", help="the primary host name to certify")
    p.add_argument("extra_domains", nargs="*",
                   help="optional subdomains/SANs for the same certificate")
    p.add_argument("--role", choices=["relay", "exit"], default=None,
                   help="exit | relay (auto-detected by default)")
    p.add_argument("--email", default="", help="ACME account e-mail")
    p.add_argument("--webroot", default="", help="directory a webserver already serves")
    p.add_argument("--standalone", action="store_true",
                   help="let the client bind port 80 for a moment")
    p.add_argument("--staging", action="store_true", help="use the test CA (dry runs)")
    p.add_argument("--client", choices=["acme.sh", "certbot"], default="",
                   help="which client to use (default: whichever is installed)")
    p.add_argument("--acme-sh", dest="acme_sh", default="",
                   help=argparse.SUPPRESS)
    p.add_argument("--config-dir", dest="config_dir", default="",
                   help="certbot's store (default /etc/letsencrypt)")
    p.add_argument("--port", type=int, default=0,
                   help="wire the certificate into this [[listen]] port only")
    p.add_argument("--no-write", action="store_true",
                   help="obtain the certificate but leave the config alone")
    p.add_argument("--reload", default="",
                   help="command to run after a renewal (default: this service)")
    p.add_argument("--apply", action="store_true",
                   help="run that command now as well (skip waiting for a restart)")
    p.add_argument("--json", action="store_true",
                   help="print the status as JSON (for scripts)")
    p.set_defaults(func=cmd_cert)

    p = sub.add_parser("alert", aliases=["alerts"], help="telegram / webhook alerts")
    p.add_argument("action", nargs="?", default="status",
                   choices=["status", "test", "events"])
    p.add_argument("extra", nargs="*", help="events to enable (with `events`)")
    p.add_argument("--role", choices=["relay", "exit"], default="relay")
    p.set_defaults(func=cmd_alert)

    p = sub.add_parser("update", help="install the newest release")
    p.add_argument("--check", action="store_true",
                   help="only report whether an update is available")
    p.add_argument("--force", action="store_true",
                   help="reinstall even when this version looks newest")
    # Kept as a hidden compatibility flag: updates now use the checksummed
    # release assets rather than requiring a local Go rebuild.
    p.add_argument("--no-rebuild", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--restart", action="store_true",
                   help="restart the service afterwards and verify /healthz")
    p.add_argument("--json", action="store_true", help="machine readable output")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("uninstall", help="remove the services")
    p.add_argument("--purge", action="store_true", help="also delete the config folder")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("setup", help=argparse.SUPPRESS)
    p.add_argument("--panel-host", default="0.0.0.0")
    p.add_argument("--panel-port", type=int, default=RELAY_DEFAULT_PANEL)
    p.set_defaults(func=cmd_setup)

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

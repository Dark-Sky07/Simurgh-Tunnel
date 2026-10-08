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
                     RelayConfig, load_exit, load_relay, new_token, save_exit,
                     save_relay)
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
        raise SystemExit(_fail(f"مقدار «{prompt}» لازم است و ترمینال تعاملی نیست."))
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(1)
    return answer or default


def _ask_yes(prompt: str, default: bool = True) -> bool:
    answer = _ask(f"{prompt} (بله/خیر)", "بله" if default else "خیر").lower()
    return answer in ("بله", "y", "yes", "ب", "1", "true")


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
        _err(f"نصب پیدا نشد: {home.path} — اول `simurgh init` یا `simurgh install` را اجرا کنید.")
        raise SystemExit(2)
    return home


def _load_relay_or_fail(home: Home) -> RelayConfig:
    if not home.relay_cfg.exists():
        _err(f"فایل تنظیمات رله پیدا نشد: {home.relay_cfg}")
        raise SystemExit(2)
    try:
        return load_relay(home.relay_cfg)
    except ConfigError as exc:
        _err(str(exc))
        raise SystemExit(2)


def _load_exit_or_fail(home: Home) -> ExitConfig:
    if not home.exit_cfg.exists():
        _err(f"فایل تنظیمات اگزیت پیدا نشد: {home.exit_cfg}")
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
            print("این سرور چه نقشی دارد؟")
            print("  1) سرور خارج (Exit)  — پنل/سرویس اصلی اینجاست")
            print("  2) سرور ایران (Relay) — کاربران به این وصل می‌شوند")
            choice = _ask("انتخاب", "1")
            role = "exit" if choice.strip() in ("1", "exit", "خارج") else "relay"
    if role == "exit":
        return _init_exit(home, args)
    return _init_relay(home, args)


def _init_exit(home: Home, args) -> int:
    if home.exit_cfg.exists() and not args.force:
        _warn(f"تنظیمات اگزیت از قبل هست: {home.exit_cfg} (برای بازنویسی --force)")
        return 1
    name = args.name or _ask("نام این سرور", socket.gethostname()[:32])
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
        _warn(f"ساخت گواهی خودکار ناموفق بود: {exc}")
    save_exit(cfg, home.exit_cfg)
    _ok(f"تنظیمات اگزیت ساخته شد: {home.exit_cfg}")
    print(f"   توکن: {token}")
    print(f"   پورت‌ها: {', '.join(str(s.port) for s in cfg.listen)}")
    return 0


def _init_relay(home: Home, args) -> int:
    if home.relay_cfg.exists() and not args.force:
        _warn(f"تنظیمات رله از قبل هست: {home.relay_cfg} (برای بازنویسی --force)")
        return 1
    exit_host = args.exit_host or _ask("آدرس سرور خارج (IP یا دامنه)")
    if not exit_host:
        return _fail("آدرس سرور خارج لازم است: --exit-host 203.0.113.9")
    exit_port = args.exit_port or int(_ask("پورت تونل روی سرور خارج", "443"))
    carrier = (args.carrier or _ask("نوع حامل (tls/wss/raw/plain)", "tls")).lower()
    token = args.token or _ask("توکن (از سرور خارج بگیرید)")
    if not token:
        return _fail("توکن لازم است: --token XXX")
    domain = args.domain or None
    if carrier in ("tls", "wss") and not domain and _interactive():
        if _ask_yes("برای SNI دامنه دارید؟", False):
            domain = _ask("دامنه")
    name = args.name or socket.gethostname()[:32]
    panel_port = args.panel_port or RELAY_DEFAULT_PANEL
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
            _err(f"مپینگ نامعتبر «{spec}»: {exc}")
            return 2
    cfg.mappings = mappings
    save_relay(cfg, home.relay_cfg)
    _ok(f"تنظیمات رله ساخته شد: {home.relay_cfg}")
    print(f"   سرور خارج: {carrier}://{exit_host}:{exit_port}")
    if not mappings:
        _info("برای افزودن پورت: simurgh mapping add 443 443")
    return 0


def _parse_mapping(spec: str, udp: bool = False) -> Mapping:
    """``listen:target[:host]`` — e.g. ``443:443`` or ``8443:8443``."""
    parts = spec.split(":")
    if len(parts) < 2:
        raise ValueError("قالب باید listen:target باشد، مثلاً 443:443")
    try:
        listen = int(parts[0])
        target = int(parts[1])
    except ValueError as exc:
        raise ValueError("پورت باید عدد باشد") from exc
    host = ":".join(parts[2:]) or "127.0.0.1"
    if not (1 <= listen <= 65535 and 1 <= target <= 65535):
        raise ValueError("پورت بیرون از بازه ۱ تا ۶۵۵۳۵ است")
    return Mapping(name="", listen=listen, target_port=target,
                   target_host=host, udp=udp)


def cmd_install(args) -> int:
    """Create the config, install systemd units, start everything."""
    from . import systemd

    home = Home(args.home).ensure()
    role = args.role
    if role == "auto":
        # if an exit config already exists we are probably on the exit
        role = "exit" if not args.exit_host else "relay"
    roles = ("exit", "relay") if args.both else (role,)
    if not args.config_only:
        for wanted in roles:
            sub = argparse.Namespace(**{**vars(args), "role": wanted})
            code = _init_exit(home, sub) if wanted == "exit" else _init_relay(home, sub)
            cfg_path = home.exit_cfg if wanted == "exit" else home.relay_cfg
            if code != 0 and not cfg_path.exists():
                _err(f"ساخت تنظیمات «{wanted}» ناموفق بود؛ نصب متوقف شد.")
                return 2
    report = systemd.install_services(
        home, roles=roles, enable=not args.no_enable, start=not args.no_start,
        user=args.user if args.user else None,
    )
    if not report.get("systemd"):
        _warn("systemd در دسترس نیست؛ سرویس نصب نشد. حالا دستی اجرا کنید:")
        for wanted in roles:
            print(f"   simurgh {wanted}" + ("" if wanted != "relay" else ""))
        return 0
    for unit in report.get("units", []):
        _ok(f"ساخت {unit}")
    for svc, result in report.get("actions", {}).items():
        (_ok if result == "ok" else _warn)(f"{svc}: {result}")
    print()
    _info(f"پنل مدیریت: http://{_public_ip()}:{_panel_port_of(home, roles)}")
    _info("حالت متنی: simurgh menu")
    _info("وضعیت: simurgh status")
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


def cmd_service(args) -> int:
    from . import systemd

    action = args.service_action
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
        print(f"  config: {cfg_path} {'✔' if cfg_path.exists() else '✘ (نیست)'}")
        for name, active, sub, enabled in rows:
            mark = "✔" if active == "active" else "✘"
            print(f"  سرویس: {name}: {active}/{sub} enable={enabled} {mark}")
        if home.state.exists():
            try:
                state = json.loads(home.state.read_text())[role]
                print(f"  تونل:  {'متصل' if state.get('connected') or state.get('tunnels') else 'قطع'}")
                print(f"  آمار:  {json.dumps(state.get('stats', {}).get('totals', {}), ensure_ascii=False)}")
            except (OSError, ValueError, KeyError):
                pass
        if args.json:
            print(json.dumps({"services": [
                {"name": n, "active": a, "sub": s, "enabled": e} for n, a, s, e in rows
            ]}, ensure_ascii=False))
        return 0
    if not systemd.available():
        _err("systemd در دسترس نیست؛ با `simurgh exit` / `simurgh relay` دستی اجرا کنید.")
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
    if cfg.cert_auto:
        from .certs import ensure_certificate

        cert, key = ensure_certificate(home, cfg.name or "simurgh.local")
        cfg.cert_file, cfg.key_file = cert, key
    node = ExitNode(cfg)
    try:
        await node.start()
    except Exception as exc:
        _err(f"اجرای اگزیت ناموفق بود: {exc}")
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
            _warn(f"پنل بالا نیامد ({exc}); خود تونل ادامه می‌دهد.")
            panel = None
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    writer = asyncio.ensure_future(_state_writer(home, "exit", node, panel))
    _ok(f"اگزیت «{cfg.name or 'exit'}» بالا آمد.")
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
            _warn(f"پنل بالا نیامد ({exc}); تونل ادامه می‌دهد.")
            panel = None
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    writer = asyncio.ensure_future(_state_writer(home, "relay", node, panel))
    _ok(f"رله «{cfg.name or 'relay'}» بالا آمد. مقصد: {cfg.exit.describe()}")
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
        return {"ok": False, "error": "systemd نیست"}
    return systemd.control(name, _service_names(role))


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
            _info("هیچ مپینگی ندارید.")
            return 0
        print(f"{'نام':<12} {'پورت ایران':>11}  {'مقصد':<22} نوع   وضعیت")
        for m in cfg.mappings:
            print(f"{m.name or '-':<12} {m.listen:>11}  "
                  f"{m.target_host + ':' + str(m.target_port):<22} "
                  f"{'UDP' if m.udp else 'TCP'}  {'فعال' if m.enabled else 'خاموش'}")
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
            _err("پورت بیرون از بازه مجاز است.")
            return 2
        if any(m.key() == mapping.key() for m in cfg.mappings):
            _err(f"پورت {mapping.listen} از قبل استفاده می‌شود.")
            return 2
        if not free_port(mapping.listen, mapping.listen_host):
            _warn(f"پورت {mapping.listen} همین حالا اشغال است (شاید سرویس دیگری).")
        cfg.mappings.append(mapping)
    elif action in ("remove", "rm", "delete"):
        key = str(args.listen)
        if args.id is not None:
            if args.id < 0 or args.id >= len(cfg.mappings):
                _err("شماره مپینگ نامعتبر است.")
                return 2
            removed = cfg.mappings.pop(args.id)
            _ok(f"حذف شد: {removed.listen} → {removed.target_port}")
            save_relay(cfg, home.relay_cfg)
            return 0
        before = len(cfg.mappings)
        cfg.mappings = [m for m in cfg.mappings
                        if not (str(m.listen) == key or m.name == args.name)]
        if len(cfg.mappings) == before:
            _err(f"مپینگی با پورت {key} پیدا نشد.")
            return 2
    elif action == "toggle":
        found = False
        for m in cfg.mappings:
            if str(m.listen) == str(args.listen) or (args.name and m.name == args.name):
                m.enabled = not m.enabled
                found = True
                _ok(f"{'فعال' if m.enabled else 'خاموش'} شد: {m.listen}")
        if not found:
            _err("پیدا نشد.")
            return 2
    else:
        _err(f"دستور نامعتبر: {action}")
        return 2
    save_relay(cfg, home.relay_cfg)
    _ok(f"ذخیره شد در {home.relay_cfg}")
    _info("برای اعمال: simurgh restart")
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
    print(f"  تنظیمات: {cfg_path} {'✔' if cfg_path.exists() else '✘'}")
    if not node:
        _warn(f"داده زنده‌ای برای «{role}» نیست. سرویس روشن است؟ (simurgh status/up)")
        return cmd_service(argparse.Namespace(role=role, service_action="status",
                                             home=args.home, json=False))
    if role == "relay":
        connected = node.get("connected")
        tunnel = "\033[32mمتصل\033[0m" if connected else "\033[31mقطع\033[0m"
        print(f"  تونل:   {tunnel}  ({node.get('current_exit') or '—'})")
        print(f"  پینگ:   {node.get('rtt_ms') and round(node['rtt_ms'], 1) or '—'} ms")
        print(f"  اتصال‌ها: {node.get('reconnects', 0)}   خطا: {node.get('last_error') or '—'}")
    else:
        print(f"  تونل‌ها: {node.get('tunnels', 0)}")
    stats = node.get("stats", {})
    totals = stats.get("totals", {})
    rates = stats.get("rates", {})
    print(f"  ترافیک: {human_bytes(totals.get('in_bytes', 0))} ↓ / "
          f"{human_bytes(totals.get('out_bytes', 0))} ↑")
    print(f"  سرعت:   {human_rate(rates.get('in_bps', 0))} ↓ / "
          f"{human_rate(rates.get('out_bps', 0))} ↑")
    print(f"  مدت:    {human_duration(node.get('uptime', 0))}")
    mappings = node.get("mappings") or []
    if mappings:
        print("  مپینگ‌ها:")
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
        _err(f"فایل لاگ نیست: {name}")
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
        print("\033[36m— دنبال کردن لاگ (Ctrl+C برای خروج) —\033[0m")
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
            _err("تونل برقرار نشد؛ اول `simurgh status` را بررسی کنید.")
            return 3
        try:
            result = await run_speedtest(node, seconds=args.seconds)
        finally:
            await node.stop()
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"  دانلود: \033[32m{result['download_mbps']:.1f} Mbps\033[0m "
                  f"({human_bytes(result['download_bytes'])})")
            print(f"  آپلود:  \033[32m{result['upload_mbps']:.1f} Mbps\033[0m "
                  f"({human_bytes(result['upload_bytes'])})")
            print(f"  مدت تست: {result['seconds']:.1f} ثانیه")
        return 0

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:
        return 0


def cmd_link(args) -> int:
    """Show the one-string setup link for a new relay (exit side)."""
    home = _home_or_fail(args)
    cfg = _load_exit_or_fail(home)
    from .links import build_join_link

    host = args.host or _public_ip()
    user, password = _panel_credentials(home)
    port = args.panel_port or RELAY_DEFAULT_PANEL
    link = build_join_link(host, port, user, password, cfg.name or "")
    if args.show:
        print(link)
        return 0
    print(f"\033[1mلینک اتصال رله (محرمانه):\033[0m\n{link}")
    print()
    _info("روی سرور ایران: simurgh join '<link>'")
    if not is_ip(host):
        _warn("آدرس محلی تشخیص داده شد؛ با --host آدرس عمومی را بدهید.")
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
        _err(f"دریافت اطلاعات از لینک ناموفق بود: {exc}")
        return 2
    cfg = relay_config_from_payload(payload)
    if home.relay_cfg.exists() and not args.force:
        _err(f"تنظیمات رله از قبل هست ({home.relay_cfg}); با --force بازنویسی کنید.")
        return 2
    if args.host:
        cfg.exit.address = args.host
    save_relay(cfg, home.relay_cfg)
    _ok(f"تنظیمات رله از لینک ساخته شد: {home.relay_cfg}")
    print(f"   مقصد: {cfg.exit.describe()}")
    for m in cfg.mappings:
        print(f"   مپینگ: {m.listen} → {m.target_host}:{m.target_port}")
    return 0


def cmd_doctor(args) -> int:
    """Diagnose the usual reasons a tunnel does not come up."""
    home = Home(args.home)
    problems = 0
    print(f"\033[1m{__product__} v{__version__} — بررسی سلامت\033[0m  ({home.path})")
    if not home.path.exists():
        _err(f"پوشه نصب نیست: {home.path}")
        return 2

    role = args.role
    cfg_path = home.exit_cfg if role == "exit" else home.relay_cfg
    if not cfg_path.exists():
        _err(f"فایل تنظیمات نیست: {cfg_path}")
        return 2
    _ok(f"تنظیمات: {cfg_path}")

    if role == "exit":
        cfg = _load_exit_or_fail(home)
        if not cfg.token:
            _err("توکن خالی است.")
            problems += 1
        for spec in cfg.listen:
            if not spec.enabled:
                continue
            if free_port(spec.port, spec.host):
                _ok(f"پورت {spec.carrier}://{spec.host}:{spec.port} آزاد است")
            else:
                _warn(f"پورت {spec.host}:{spec.port} همین حالا اشغال است")
        for path, label in ((cfg.cert_file, "گواهی"), (cfg.key_file, "کلید")):
            if path and Path(path).exists():
                _ok(f"{label}: {path}")
            elif path:
                _warn(f"{label} پیدا نشد: {path}")
        from . import systemd

        if systemd.available():
            _ok("systemd در دسترس است")
        else:
            _warn("systemd نیست؛ دستی اجرا کنید: simurgh exit")
        return 0 if problems == 0 else 1

    cfg = _load_relay_or_fail(home)
    if not cfg.token:
        _err("توکن خالی است (از سرور خارج بگیرید).")
        problems += 1
    else:
        _ok(f"توکن: {cfg.token[:6]}…{cfg.token[-4:]}")
    addresses = [(ep.address, ep.port, ep.describe()) for ep in cfg.endpoints()]
    if not addresses:
        _err("هیچ سرور خارجی تنظیم نشده.")
        problems += 1
    for host, port, desc in addresses:
        try:
            with socket.create_connection((host, port), timeout=5):
                _ok(f"دسترسی TCP به {desc}")
        except OSError as exc:
            _err(f"اتصال به {desc} ناموفق: {exc}")
            problems += 1
    for m in cfg.mappings:
        if free_port(m.listen, m.listen_host):
            _ok(f"پورت {m.listen} آزاد است → {m.target_host}:{m.target_port}")
        else:
            _warn(f"پورت {m.listen} اشغال است (شاید خود سرویس ما یا سرویس دیگری)")
    if free_port(cfg.panel_port):
        _ok(f"پورت پنل {cfg.panel_port} آزاد است")
    else:
        _warn(f"پورت پنل {cfg.panel_port} اشغال است")
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
        print(f"  پنل: http://{host}:{port}   کاربر: {user}   رمز: {password}")
        _info("Ctrl+C برای خروج")
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
    if not args.yes and not _ask_yes(f"حذف کامل سرویس‌ها و پوشه {home.path}؟", False):
        _info("لغو شد.")
        return 0
    if systemd.available():
        for svc, result in systemd.uninstall_services().items():
            _ok(f"{svc}: {result}") if result == "ok" else _warn(f"{svc}: {result}")
    if args.purge and home.path.exists():
        shutil.rmtree(home.path, ignore_errors=True)
        _ok(f"پوشه حذف شد: {home.path}")
    else:
        _info(f"تنظیمات نگه داشته شد در {home.path} (برای حذف: --purge)")
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
        description=f"{__product__} v{__version__} — تونل شفاف بین سرور ایران و خارج",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "نمونه‌ها:\n"
            "  sudo simurgh install --role exit --name omega\n"
            "  sudo simurgh install --role relay --exit 203.0.113.9:443 --token XXX\n"
            "  simurgh mapping add 443 443 --name panel\n"
            "  simurgh status\n  simurgh menu\n  simurgh speedtest\n"
        ),
    )
    parser.add_argument("--version", action="store_true", help="نمایش نسخه")
    parser.add_argument("--home", help="پوشه نصب (پیش‌فرض /etc/simurgh یا ~/.simurgh)")
    parser.add_argument("-v", "--verbose", action="store_true", help="لاگ کامل")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("install", help="نصب کامل: ساخت تنظیمات + سرویس systemd")
    p.add_argument("--role", choices=("exit", "relay", "auto"), default="auto")
    p.add_argument("--both", action="store_true", help="هر دو سرویس روی همین سرور")
    p.add_argument("--name")
    p.add_argument("--token")
    p.add_argument("--listen", help="پورت‌های اگزیت، مثلاً 443,8443")
    p.add_argument("--exit-host", default="", help="آدرس سرور خارج (برای رله)")
    p.add_argument("--exit-port", type=int, default=0)
    p.add_argument("--carrier", default="")
    p.add_argument("--domain", default="")
    p.add_argument("--path", default="")
    p.add_argument("--fingerprint", default=None)
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--mapping", action="append", help="مپینگ اولیه: 443:443")
    p.add_argument("--panel-port", type=int, default=0)
    p.add_argument("--user", default="")
    p.add_argument("--config-only", action="store_true")
    p.add_argument("--no-enable", action="store_true")
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("init", help="فقط ساخت فایل تنظیمات")
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
    p.add_argument("--mapping", action="append")
    p.add_argument("--panel-port", type=int, default=0)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    for role, helptext in (("exit", "اجرای سرور خارج (اگزیت)"),
                           ("relay", "اجرای سرور ایران (رله)")):
        p = sub.add_parser(role, help=helptext)
        p.add_argument("--role", default=role, help=argparse.SUPPRESS)
        p.add_argument("--no-panel", action="store_true", help="بدون پنل وب")
        p.add_argument("--panel-host", default="0.0.0.0")
        p.add_argument("--panel-port", type=int, default=0)
        p.add_argument("--verbose", action="store_true")
        p.set_defaults(func=cmd_run)

    p = sub.add_parser("up", help="روشن کردن سرویس")
    p.add_argument("--role", choices=("exit", "relay"), default="relay")
    p.add_argument("--fg", action="store_true", help="اجرا در ترمینال (بدون systemd)")
    p.set_defaults(func=lambda a: cmd_run(argparse.Namespace(
        role=a.role, home=a.home, verbose=False, no_panel=False,
        panel_host="0.0.0.0", panel_port=0)) if a.fg else cmd_service(
        argparse.Namespace(role=a.role, service_action="start", home=a.home, json=False)))

    for action, helptext in (("down", "خاموش کردن سرویس"),
                             ("restart", "ری‌استارت سرویس"),
                             ("enable", "فعال‌سازی خودکار در بوت"),
                             ("disable", "غیرفعال کردن در بوت"),
                             ("status", "وضعیت سرویس")):
        p = sub.add_parser(action, help=helptext)
        p.add_argument("--role", choices=("exit", "relay"), default="relay")
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=cmd_service, service_action=action)

    p = sub.add_parser("mapping", aliases=["port"], help="مدیریت پورت‌های فوروارد")
    p.add_argument("mapping_action", choices=("list", "add", "remove", "rm", "toggle"),
                   nargs="?", default="list")
    p.add_argument("listen", nargs="?", default="")
    p.add_argument("target", nargs="?", default="")
    p.add_argument("--name", default="")
    p.add_argument("--target-host", default="127.0.0.1")
    p.add_argument("--udp", action="store_true")
    p.add_argument("--id", type=int, default=None)
    p.set_defaults(func=cmd_mapping)

    p = sub.add_parser("logs", help="نمایش لاگ")
    p.add_argument("--role", choices=("exit", "relay", "panel"), default="relay")
    p.add_argument("-n", "--lines", type=int, default=80)
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("speedtest", help="تست سرعت از داخل تونل")
    p.add_argument("--seconds", type=int, default=6)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_speedtest)

    p = sub.add_parser("link", help="ساخت لینک اتصال برای رله جدید (روی اگزیت)")
    p.add_argument("--host", default="")
    p.add_argument("--panel-port", type=int, default=0)
    p.add_argument("--show", action="store_true", help="فقط لینک خام")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("join", help="ساخت تنظیمات رله از لینک اگزیت")
    p.add_argument("link")
    p.add_argument("--host", default="", help="جایگزینی آدرس اگزیت")
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_join)

    p = sub.add_parser("doctor", help="بررسی سلامت و عیب‌یابی")
    p.add_argument("--role", choices=("exit", "relay"), default="relay")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("web", help="اجرای پنل وب به‌تنهایی")
    p.add_argument("--role", choices=("exit", "relay"), default="relay")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=0)
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("uninstall", help="حذف سرویس‌ها")
    p.add_argument("--purge", action="store_true", help="حذف پوشه تنظیمات هم")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("menu", help="منوی فارسی متنی")
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

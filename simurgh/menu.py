"""Persian interactive text menu (``simurgh menu``).

Pure stdin/stdout, no dependency, works over SSH on a phone.  Every action the
menu performs goes through the same functions the CLI uses, so behaviour can
never drift between the two interfaces.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time

from . import __product__, __version__
from .config import ConfigError, Mapping, load_exit, load_relay, save_relay
from .util import (Home, free_port, human_bytes, human_duration, human_rate,
                   local_ips, setup_logging)

C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "green": "\033[32m", "yellow": "\033[33m", "red": "\033[31m", "cyan": "\033[36m",
}


def c(text: str, colour: str) -> str:
    return f"{C.get(colour, '')}{text}{C['reset']}"


def clear() -> None:
    os.system("clear" if os.name != "nt" else "cls")


def pause() -> None:
    try:
        input(c("\n  برای بازگشت Enter بزنید…", "dim"))
    except (EOFError, KeyboardInterrupt):
        print()


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"  {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(0)
    return answer or default


def ask_yes(prompt: str, default: bool = True) -> bool:
    answer = ask(f"{prompt} (بله/خیر)", "بله" if default else "خیر").lower()
    return answer in ("بله", "y", "yes", "ب", "1")


def detect_role(home: Home) -> str:
    if home.exit_cfg.exists() and not home.relay_cfg.exists():
        return "exit"
    if home.relay_cfg.exists() and not home.exit_cfg.exists():
        return "relay"
    return "relay"


def load_state(home: Home) -> dict:
    if not home.state.exists():
        return {}
    try:
        return json.loads(home.state.read_text())
    except (OSError, ValueError):
        return {}


def service_state(service: str) -> str:
    from . import systemd

    if not systemd.available():
        return "بدون systemd"
    state = systemd.service_state(service)
    active = state.get("ActiveState", "?")
    sub = state.get("SubState", "")
    return {"active": c("فعال", "green"), "inactive": c("خاموش", "red"),
            "failed": c("خطا", "red"), "activating": c("در حال شروع", "yellow")}.get(
        active, active) + (f"/{sub}" if sub else "")


def role_title(role: str) -> str:
    return "سرور خارج (Exit)" if role == "exit" else "سرور ایران (Relay)"


# --------------------------------------------------------------------- header
def header(home: Home, role: str) -> None:
    state = load_state(home).get(role, {})
    service = f"simurgh-{role}"
    if role == "relay":
        connected = state.get("connected")
        status = c("متصل", "green") if connected else c("قطع", "red")
        extra = state.get("current_exit") or "—"
        rtt = state.get("rtt_ms")
        rtt_text = f"{rtt:.0f} ms" if isinstance(rtt, (int, float)) else "—"
    else:
        tunnels = state.get("tunnels", 0)
        status = c(f"{tunnels} تونل", "green") if tunnels else c("بدون تونل", "red")
        extra = "—"
        rtt_text = "—"
    totals = (state.get("stats") or {}).get("totals") or {}
    rates = (state.get("stats") or {}).get("rates") or {}
    print(c("═" * 62, "cyan"))
    print(f" {c(__product__, 'bold')} v{__version__}   {c(role_title(role), 'cyan')}")
    print(f" پوشه: {c(str(home.path), 'dim')}")
    print(c("─" * 62, "dim"))
    print(f" وضعیت سرویس: {service_state(service)}    تونل: {status}   پینگ: {rtt_text}")
    print(f" مقصد: {extra}")
    print(f" ترافیک کل: {human_bytes(totals.get('in_bytes', 0))} ↓ / "
          f"{human_bytes(totals.get('out_bytes', 0))} ↑")
    print(f" سرعت لحظه‌ای: {human_rate(rates.get('in_bps', 0))} ↓ / "
          f"{human_rate(rates.get('out_bps', 0))} ↑")
    uptime = state.get("uptime")
    if uptime:
        print(f" مدت اتصال: {human_duration(uptime)}")
    if state.get("last_error"):
        print(f" آخرین خطا: {c(str(state['last_error'])[:70], 'yellow')}")
    print(c("═" * 62, "cyan"))


# -------------------------------------------------------------------- actions
def show_status(home: Home, role: str) -> None:
    clear()
    header(home, role)
    state = load_state(home).get(role, {})
    mappings = state.get("mappings") or []
    if role == "relay" and mappings:
        print(c("\n پورت‌های فوروارد:", "bold"))
        for m in mappings:
            mark = c("✔", "green") if m.get("bound") else c("✘", "red")
            kind = "UDP" if m.get("udp") else "TCP"
            en = "" if m.get("enabled", True) else c(" (خاموش)", "dim")
            print(f"   {mark} {m.get('listen'):>6} → {m.get('target'):<22} {kind}{en}")
    conflicts = state.get("bind_errors") or []
    if conflicts:
        print(c("\n خطاهای پورت:", "yellow"))
        for item in conflicts:
            print(f"   ! {item}")
    logs = home.logs / f"{role}.log"
    if logs.exists():
        print(c("\n آخرین خطوط لاگ:", "bold"))
        try:
            with logs.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 4096))
                lines = fh.read().decode("utf-8", "replace").splitlines()[-10:]
            for line in lines:
                print(f"   {c(line[:100], 'dim')}")
        except OSError:
            pass
    pause()


def service_action(role: str, action: str) -> None:
    from . import systemd

    if not systemd.available():
        print(c("\n  systemd در دسترس نیست.", "red"))
        print("  می‌توانید دستی اجرا کنید: " + c(f"simurgh {role}", "cyan"))
        pause()
        return
    result = systemd.control(action, (f"simurgh-{role}",))
    for name, text in result.items():
        mark = c("✔", "green") if text == "ok" else c("✘", "red")
        print(f"  {mark} {name}: {action} {'' if text == 'ok' else text}")
    time.sleep(0.8)


def edit_mappings(home: Home) -> None:
    while True:
        clear()
        try:
            cfg = load_relay(home.relay_cfg)
        except (ConfigError, OSError) as exc:
            print(c(f"  خطا در خواندن تنظیمات: {exc}", "red"))
            pause()
            return
        print(c("═" * 62, "cyan"))
        print(c("  پورت‌های فوروارد (مپینگ‌ها)", "bold"))
        print(c("═" * 62, "cyan"))
        if not cfg.mappings:
            print("  هیچ مپینگی ندارید.")
        for i, m in enumerate(cfg.mappings, 1):
            state = c("فعال", "green") if m.enabled else c("خاموش", "dim")
            kind = "UDP" if m.udp else "TCP"
            print(f"  {i}) پورت {m.listen:>6} → {m.target_host}:{m.target_port} "
                  f"({kind}) {state}  {c(m.name, 'dim')}")
        print()
        print("  [a] افزودن   [d] حذف   [t] روشن/خاموش   [ب] بازگشت")
        choice = ask("انتخاب", "ب").lower()
        if choice in ("ب", "b", "q", ""):
            return
        if choice == "a":
            try:
                listen = int(ask("پورت روی سرور ایران"))
            except ValueError:
                print(c("  پورت نامعتبر.", "red"))
                pause()
                continue
            try:
                target = int(ask("پورت مقصد روی سرور خارج", str(listen)))
            except ValueError:
                print(c("  پورت نامعتبر.", "red"))
                pause()
                continue
            host = ask("آدرس مقصد", "127.0.0.1")
            udp = ask_yes("UDP است؟", False)
            name = ask("نام (اختیاری)", "")
            if any(m.listen == listen for m in cfg.mappings):
                print(c("  این پورت از قبل هست.", "red"))
                pause()
                continue
            cfg.mappings.append(Mapping(name=name, listen=listen, target_host=host,
                                        target_port=target, udp=udp, enabled=True))
            save_relay(cfg, home.relay_cfg)
            print(c("  ذخیره شد.", "green"))
            if not free_port(listen):
                print(c(f"  هشدار: پورت {listen} اشغال است.", "yellow"))
            print(c("  برای اعمال، سرویس را ری‌استارت کنید (گزینه ۵).", "dim"))
            pause()
        elif choice in ("d", "remove", "ح"):
            index = ask("شماره مپینگ برای حذف")
            try:
                idx = int(index) - 1
                if 0 <= idx < len(cfg.mappings):
                    removed = cfg.mappings.pop(idx)
                    save_relay(cfg, home.relay_cfg)
                    print(c(f"  حذف شد: {removed.listen}", "green"))
                else:
                    print(c("  شماره نامعتبر.", "red"))
            except ValueError:
                print(c("  شماره نامعتبر.", "red"))
            pause()
        elif choice == "t":
            index = ask("شماره مپینگ")
            try:
                idx = int(index) - 1
                if 0 <= idx < len(cfg.mappings):
                    cfg.mappings[idx].enabled = not cfg.mappings[idx].enabled
                    save_relay(cfg, home.relay_cfg)
                    print(c("  تغییر کرد.", "green"))
                else:
                    print(c("  شماره نامعتبر.", "red"))
            except ValueError:
                print(c("  شماره نامعتبر.", "red"))
            pause()


def edit_exit_target(home: Home) -> None:
    clear()
    try:
        cfg = load_relay(home.relay_cfg)
    except (ConfigError, OSError) as exc:
        print(c(f"  خطا: {exc}", "red"))
        pause()
        return
    print(c("═" * 62, "cyan"))
    print(c("  تنظیمات اتصال به سرور خارج", "bold"))
    print(c("═" * 62, "cyan"))
    print(f"  ۱) آدرس/دامنه : {cfg.exit.address}")
    print(f"  ۲) پورت       : {cfg.exit.port}")
    print(f"  ۳) حامل        : {cfg.exit.carrier}")
    print(f"  ۴) مسیر (path) : {cfg.exit.path}")
    print(f"  ۵) اثر انگشت   : {cfg.exit.fingerprint or '—'}")
    print(f"  ۶) توکن        : {cfg.token[:6]}…{cfg.token[-4:]}" if len(cfg.token) > 12
          else f"  ۶) توکن        : {cfg.token}")
    print()
    field = ask("کدام مورد را تغییر می‌دهیم؟ (۱-۶ یا Enter)", "")
    changed = True
    if field in ("1", "۱"):
        cfg.exit.address = ask("آدرس جدید", cfg.exit.address)
    elif field in ("2", "۲"):
        cfg.exit.port = int(ask("پورت جدید", str(cfg.exit.port)))
    elif field in ("3", "۳"):
        carrier = ask("حامل (tls/wss/raw/plain)", cfg.exit.carrier).lower()
        if carrier not in ("tls", "wss", "raw", "plain"):
            print(c("  حامل نامعتبر.", "red"))
            pause()
            return
        cfg.exit.carrier = carrier
    elif field in ("4", "۴"):
        cfg.exit.path = ask("مسیر", cfg.exit.path)
    elif field in ("5", "۵"):
        cfg.exit.fingerprint = ask("اثر انگشت گواهی (خالی = حذف)", cfg.exit.fingerprint or "") or None
    elif field in ("6", "۶"):
        cfg.token = ask("توکن جدید", cfg.token)
    else:
        changed = False
    if changed:
        save_relay(cfg, home.relay_cfg)
        print(c("  ذخیره شد. سرویس را ری‌استارت کنید.", "green"))
        pause()


def speedtest(home: Home) -> None:
    clear()
    print(c("  تست سرعت از داخل تونل (چند ثانیه طول می‌کشد)…", "cyan"))
    from .relay import RelayNode
    from .speedtest import run_speedtest

    async def go():
        cfg = load_relay(home.relay_cfg)
        node = RelayNode(cfg, home=home, listen=False)
        await node.start()
        for _ in range(80):
            if node.connected.is_set():
                break
            await asyncio.sleep(0.25)
        if not node.connected.is_set():
            await node.stop()
            return None
        try:
            return await run_speedtest(node, seconds=6)
        finally:
            await node.stop()

    try:
        result = asyncio.run(go())
    except Exception as exc:
        print(c(f"  خطا: {exc}", "red"))
        pause()
        return
    if result is None:
        print(c("  تونل برقرار نشد؛ وضعیت را ببینید.", "red"))
        pause()
        return
    print(f"  دانلود: {c('%.1f Mbps' % result['download_mbps'], 'green')}"
          f"  ({human_bytes(result['download_bytes'])})")
    print(f"  آپلود:  {c('%.1f Mbps' % result['upload_mbps'], 'green')}"
          f"  ({human_bytes(result['upload_bytes'])})")
    pause()


def show_join_link(home: Home) -> None:
    clear()
    from .links import build_join_link

    try:
        cfg = load_exit(home.exit_cfg)
    except (ConfigError, OSError) as exc:
        print(c(f"  خطا: {exc}", "red"))
        pause()
        return
    state = load_state(home)
    auth = state.get("panel_auth", {})
    host = ask("آدرس عمومی این سرور", _guess_ip())
    port = int(ask("پورت پنل", "8787"))
    link = build_join_link(host, port, auth.get("user", "admin"),
                           auth.get("password", ""), cfg.name or "")
    print()
    print(c("  لینک اتصال رله (محرمانه):", "bold"))
    print(f"  {c(link, 'cyan')}")
    print()
    print(c("  روی سرور ایران اجرا کنید:", "dim"))
    print(f"  {c('simurgh join ' + repr(link), 'yellow')}")
    pause()


def _guess_ip() -> str:
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


def join_from_link(home: Home) -> None:
    clear()
    link = ask("لینک اتصال را بچسبانید")
    if not link:
        return
    from .links import LinkError, fetch_setup, relay_config_from_payload

    print(c("  در حال دریافت اطلاعات…", "cyan"))
    try:
        payload = fetch_setup(link)
    except (LinkError, Exception) as exc:
        print(c(f"  ناموفق: {exc}", "red"))
        pause()
        return
    cfg = relay_config_from_payload(payload)
    if home.relay_cfg.exists() and not ask_yes("تنظیمات فعلی بازنویسی شود؟", False):
        return
    save_relay(cfg, home.relay_cfg)
    print(c("  تنظیمات ساخته شد.", "green"))
    if ask_yes("سرویس همین حالا ری‌استارت شود؟", True):
        service_action("relay", "restart")
    else:
        pause()


def panel_info(home: Home, role: str) -> None:
    clear()
    state = load_state(home)
    auth = state.get("panel_auth", {})
    port = 8787
    try:
        cfg = load_relay(home.relay_cfg) if role == "relay" else load_exit(home.exit_cfg)
        port = getattr(cfg, "panel_port", 8787) or 8787
    except (ConfigError, OSError):
        pass
    ip = _guess_ip()
    print(c("═" * 62, "cyan"))
    print(c("  پنل وب", "bold"))
    print(c("═" * 62, "cyan"))
    print(f"  آدرس: {c('http://%s:%d' % (ip, port), 'cyan')}")
    print(f"  کاربر: {auth.get('user', 'admin')}")
    print(f"  رمز:   {auth.get('password', '—')}")
    print()
    print(c("  نکته: اگر پنل باز نمی‌شود، پورت را در فایروال باز کنید.", "dim"))
    pause()


def show_logs(home: Home, role: str) -> None:
    clear()
    path = home.logs / f"{role}.log"
    if not path.exists():
        print(c("  لاگی موجود نیست.", "yellow"))
        pause()
        return
    os.system(f"tail -n 25 {path} 2>/dev/null || cat {path}")


def doctor(home: Home, role: str) -> None:
    from .cli import cmd_doctor

    clear()
    try:
        code = cmd_doctor(type("A", (), {"home": str(home.path), "role": role})())
    except SystemExit as exc:
        code = exc.code
    print()
    print(c("  نتیجه: " + ("همه چیز سالم است." if code == 0 else "موارد بالا را بررسی کنید."),
            "green" if code == 0 else "yellow"))
    pause()


# ----------------------------------------------------------------------- loop
MENU_EXIT = {
    "1": "وضعیت و آمار",
    "2": "روشن کردن سرویس",
    "3": "خاموش کردن سرویس",
    "4": "نمایش لاگ زنده",
    "5": "ری‌استارت سرویس",
    "6": "پورت‌های فوروارد",
    "7": "تنظیمات اتصال به سرور خارج",
    "8": "پنل وب",
    "9": "تست سرعت",
}

MENU_RELAY = {
    "10": "ساخت لینک برای رله جدید",
    "11": "وارد کردن لینک اتصال",
    "12": "عیب‌یابی خودکار",
    "13": "فعال‌سازی روشن شدن خودکار",
}


def menu_loop(home: Home, role: str) -> int:
    while True:
        clear()
        header(home, role)
        print()
        for key, label in MENU_EXIT.items():
            print(f"  {c('[' + key + ']', 'cyan')} {label}")
        if role == "relay":
            for key, label in MENU_RELAY.items():
                print(f"  {c('[' + key + ']', 'cyan')} {label}")
        print(f"  {c('[q]', 'cyan')} خروج")
        choice = ask("انتخاب", "1")
        if choice in ("q", "خروج", "exit"):
            return 0
        if choice == "1":
            show_status(home, role)
        elif choice == "2":
            service_action(role, "start")
        elif choice == "3":
            if ask_yes("سرویس خاموش شود؟", False):
                service_action(role, "stop")
        elif choice == "4":
            show_logs(home, role)
        elif choice == "5":
            service_action(role, "restart")
        elif choice == "6":
            if role == "relay":
                edit_mappings(home)
            else:
                print(c("  این بخش مخصوص سرور ایران است.", "yellow"))
                pause()
        elif choice == "7":
            if role == "relay":
                edit_exit_target(home)
            else:
                print(c("  این بخش مخصوص سرور ایران است.", "yellow"))
                pause()
        elif choice == "8":
            panel_info(home, role)
        elif choice == "9":
            if role == "relay":
                speedtest(home)
            else:
                print(c("  تست سرعت روی سرور ایران اجرا می‌شود.", "yellow"))
                pause()
        elif choice == "10":
            if role == "relay":
                show_join_link(home)
            else:
                print(c("  لینک اتصال را سرور خارج می‌سازد.", "yellow"))
                pause()
        elif choice == "11":
            if role == "relay":
                join_from_link(home)
            else:
                print(c("  این بخش مخصوص سرور ایران است.", "yellow"))
                pause()
        elif choice == "12":
            doctor(home, role)
        elif choice == "13":
            service_action(role, "enable")
        else:
            time.sleep(0.3)


def main(args=None) -> int:
    home = Home(getattr(args, "home", None))
    setup_logging(False)
    if not home.path.exists() or not (home.exit_cfg.exists() or home.relay_cfg.exists()):
        clear()
        print(c("  هنوز نصبی وجود ندارد.", "red"))
        print("  اول این را اجرا کنید:")
        print(c("    sudo simurgh install --role exit    # روی سرور خارج", "cyan"))
        print(c("    sudo simurgh install --role relay   # روی سرور ایران", "cyan"))
        return 2
    role = detect_role(home)
    try:
        return menu_loop(home, role)
    except (EOFError, KeyboardInterrupt):
        print()
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

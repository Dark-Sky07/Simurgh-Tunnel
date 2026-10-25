"""Panel assets, one-time login links and the guided-setup wizard.

The dashboard itself is three files that ship inside the package
(``assets/index.html``, ``assets/app.css``, ``assets/app.js``) plus the icon
taken from the Simurgh emblem; nothing is fetched from anywhere, which is what
makes the panel work on a filtered server and lets it carry a strict
Content-Security-Policy.  This module is the Python half of that: it serves the
files, mints the single-use login links, and turns a wizard "plan" into a config
edit that can be rolled back.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import shutil
import time
from pathlib import Path

from .panel_i18n import EN as I18N_EN, FA as I18N_FA

PANEL_I18N = {"en": I18N_EN, "fa": I18N_FA}

ASSETS_DIR = Path(__file__).resolve().parent / "assets"

#: assets are immutable for a given release, so the browser may cache them; the
#: dashboard itself must not be cached (it carries the version and the role)
ASSET_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".svg": "image/svg+xml",
}

#: nothing external may load and no inline script may run.  Inline *style
#: attributes* are part of the shipped layout and are allowed explicitly;
#: blocking them made real browsers report CSP errors and broke the design.
CSP = ("default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; connect-src 'self'; font-src 'self'; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'")

#: how long a one-time login link lives, and how many may be outstanding
LOGIN_TOKEN_TTL = 600.0
LOGIN_TOKEN_MAX = 5


def read_asset(name: str) -> tuple[bytes, str] | None:
    """Read one asset by name (never a path: no traversal is possible)."""
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    path = ASSETS_DIR / name
    if not path.is_file():
        return None
    return path.read_bytes(), ASSET_TYPES.get(path.suffix, "application/octet-stream")


def setup_dashboard_html(version: str, product: str, nonce: str,
                         language: str = "en") -> str:
    """First-run role chooser; it has no role-specific config yet."""
    import html

    fa = language == "fa"
    title = "انتخاب نقش این سرور" if fa else "Choose this server's role"
    subtitle = ("این سرور چه کاری در تونل سیمرغ انجام می‌دهد؟"
                if fa else "What should this server do in your Simurgh tunnel?")
    exit_title = "سرور خارج — Exit" if fa else "Foreign server — Exit"
    relay_title = "سرور ایران — Relay" if fa else "Iranian server — Relay"
    exit_copy = ("اینجا مقصدهای نهایی سرویس‌ها قرار دارند؛ سرور ایران به این سرور وصل می‌شود."
                 if fa else "Your destination services live here; the Iranian relay connects to this server.")
    relay_copy = ("کاربران به این سرور وصل می‌شوند؛ سپس ترافیک از تونل به سرور خارج می‌رود."
                  if fa else "Your users connect here; traffic is forwarded through the tunnel to the exit.")
    choose_exit = "انتخاب سرور خارج" if fa else "Choose Exit"
    choose_relay = "انتخاب سرور ایران" if fa else "Choose Relay"
    note = ("این انتخاب فقط نقش همین سرور را تعیین می‌کند؛ جفت‌سازی و تنظیمات بعداً در پنل انجام می‌شود."
            if fa else "This selects only this server's role. Pairing and settings can be completed in the panel afterward.")
    switch = '<a href="/?lang=en">English</a>' if fa else '<a href="/?lang=fa" lang="fa">فارسی</a>'
    direction = "rtl" if fa else "ltr"
    html_doc = '''<!doctype html>
<html lang="__LANG__" dir="__DIR__"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light"><title>__TITLE__ · __PRODUCT__</title>
<style>
:root{color-scheme:dark;--bg:#0b1220;--card:#111d30;--line:#243650;--ink:#eef5ff;--muted:#a8bad0;--teal:#61e3ce;--blue:#8bb8ff}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 50% -15%,#1b3656 0,transparent 55%),var(--bg);color:var(--ink);font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;padding:28px 18px}
main{max-width:980px;margin:0 auto}.top{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:48px}.brand{display:flex;align-items:center;gap:12px;font-weight:750}.brand img{width:38px;height:38px}.brand span{letter-spacing:.02em}.lang{color:var(--muted);text-decoration:none;border:1px solid var(--line);border-radius:99px;padding:7px 13px;white-space:nowrap}
.kicker{color:var(--teal);font-size:.8rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase}h1{font-size:clamp(2rem,5vw,3.2rem);line-height:1.15;letter-spacing:-.035em;margin:10px 0 12px}.sub{max-width:680px;color:var(--muted);font-size:1.1rem;margin:0 0 32px}.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.card{min-width:0;padding:26px;border:1px solid var(--line);border-radius:22px;background:linear-gradient(145deg,rgba(23,39,61,.96),rgba(15,26,43,.96));box-shadow:0 18px 55px #02071155;display:flex;flex-direction:column}.mark{font-size:.78rem;color:var(--blue);font-weight:750;letter-spacing:.1em;text-transform:uppercase}.card h2{font-size:1.45rem;margin:14px 0 10px;overflow-wrap:anywhere}.card p{color:var(--muted);margin:0 0 24px;flex:1}.card button{width:100%;min-height:48px;border:0;border-radius:12px;background:linear-gradient(100deg,#62e4ce,#8bb8ff);color:#07111e;font:inherit;font-weight:700;cursor:pointer;padding:12px 16px;overflow-wrap:anywhere}.card button:hover{filter:brightness(1.08)}.note{border-inline-start:3px solid var(--teal);padding:12px 16px;margin:26px 0 0;background:#132238;border-radius:0 12px 12px 0;color:var(--muted)}footer{margin-top:38px;color:#8398b2;font-size:.85rem;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
@media(max-width:680px){body{padding:18px 14px}.top{margin-bottom:34px}.cards{grid-template-columns:1fr}.card{padding:22px}}
</style></head><body><main><header class="top"><div class="brand"><img src="/assets/simurgh.png" alt=""><span>__PRODUCT__</span></div>__SWITCH__</header>
<div class="kicker">__KICKER__</div><h1>__TITLE__</h1><p class="sub">__SUBTITLE__</p>
<section class="cards" aria-label="__TITLE__">
<article class="card"><div class="mark">01 · EXIT</div><h2>__EXIT_TITLE__</h2><p>__EXIT_COPY__</p>
<form method="post" action="/api/setup-role"><input type="hidden" name="role" value="exit"><input type="hidden" name="nonce" value="__NONCE__"><input type="hidden" name="lang" value="__LANG__"><button type="submit">__CHOOSE_EXIT__</button></form></article>
<article class="card"><div class="mark">02 · RELAY</div><h2>__RELAY_TITLE__</h2><p>__RELAY_COPY__</p>
<form method="post" action="/api/setup-role"><input type="hidden" name="role" value="relay"><input type="hidden" name="nonce" value="__NONCE__"><input type="hidden" name="lang" value="__LANG__"><button type="submit">__CHOOSE_RELAY__</button></form></article>
</section><p class="note">__NOTE__</p><footer><span>Simurgh Tunnel v__VERSION__</span><span>Local-first · No external assets</span></footer></main></body></html>'''
    values = {
        "LANG": "fa" if fa else "en", "DIR": direction, "TITLE": title,
        "PRODUCT": product, "SWITCH": switch,
        "KICKER": "راه‌اندازی اولیه" if fa else "FIRST-RUN SETUP",
        "SUBTITLE": subtitle, "EXIT_TITLE": exit_title,
        "EXIT_COPY": exit_copy, "RELAY_TITLE": relay_title,
        "RELAY_COPY": relay_copy, "CHOOSE_EXIT": choose_exit,
        "CHOOSE_RELAY": choose_relay, "NOTE": note,
        "VERSION": version, "NONCE": nonce,
    }
    for key, value in values.items():
        html_doc = html_doc.replace(f"__{key}__", html.escape(str(value), quote=True))
    return html_doc


def setup_recovery_html(role: str, language: str = "en", detail: str = "") -> str:
    """Explain that the role config was saved but service handoff needs attention."""
    import html

    fa = language == "fa"
    role_name = "سرور خارج (Exit)" if role == "exit" else "سرور ایران (Relay)"
    title = "تنظیم نقش ذخیره شد" if fa else "Role configuration saved"
    message = (f"تنظیمات {role_name} ذخیره شده است، اما راه‌اندازی سرویس کامل نشد. "
               "برای ادامه، در ترمینال `simurgh menu` را اجرا کنید." if fa else
               f"The {role_name} configuration was saved, but the service handoff did not finish. "
               "Run `simurgh menu` in a terminal to continue.")
    direction = "rtl" if fa else "ltr"
    extra = f"<p class=detail>{html.escape(detail)}</p>" if detail else ""
    return (f"<!doctype html><html lang={'fa' if fa else 'en'} dir={direction}><head>"
            "<meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)} · Simurgh Tunnel</title><style>body{{margin:0;min-height:100vh;"
            "display:grid;place-items:center;background:#0b1220;color:#eef5ff;font:16px/1.7 system-ui,sans-serif;"
            "padding:24px}}main{max-width:640px;overflow-wrap:anywhere;border:1px solid #243650;"
            "border-radius:20px;background:#111d30;padding:28px}p{color:#a8bad0}</style></head>"
            f"<body><main><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>{extra}</main></body></html>")


def setup_complete_html(role: str, language: str = "en") -> str:
    """Confirmation shown while the setup service switches to the chosen role."""
    import html

    fa = language == "fa"
    role_name = (("سرور خارج (Exit)" if role == "exit" else "سرور ایران (Relay)"))
    title = "انتخاب ذخیره شد" if fa else "Role saved"
    detail = (f"نقش {role_name} تنظیم شد. سرویس در حال راه‌اندازی است؛ چند لحظهٔ دیگر همین آدرس را دوباره باز کنید."
              if fa else f"{role_name} is configured. The service is switching now; reopen this address in a few seconds.")
    direction = "rtl" if fa else "ltr"
    return ("<!doctype html><html lang=\"" + ("fa" if fa else "en") + "\" dir=\"" + direction + "\"><head>"
            "<meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta http-equiv=\"refresh\" content=\"8;url=/\"><title>" + html.escape(title)
            + " · Simurgh Tunnel</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;"
            "background:#0b1220;color:#eef5ff;font:16px/1.7 system-ui,sans-serif;padding:24px}.box{max-width:620px;"
            "border:1px solid #243650;border-radius:20px;background:#111d30;padding:28px}p{color:#a8bad0}</style></head>"
            "<body><main class=\"box\"><h1>" + html.escape(title) + "</h1><p>" + html.escape(detail)
            + "</p></main></body></html>")


def dashboard_html(role: str, version: str, product: str = "Simurgh Tunnel") -> str:
    """The dashboard shell, with the meta block the UI reads at boot.

    The data lives in a ``<script type="application/json">`` block and not in an
    inline script, because a strict CSP (which this panel sets) forbids the
    latter -- and the block is data, not code, so it stays allowed.
    """
    asset = ASSETS_DIR / "index.html"
    html = asset.read_text(encoding="utf-8")
    meta = json.dumps({"i18n": PANEL_I18N, "role": role, "version": version,
                       "product": product}, ensure_ascii=False)
    # never let a string end the data block early
    meta = meta.replace("<", "\\u003c").replace("&", "\\u0026")
    return (html.replace("__META__", meta)
                .replace("__ROLE__", role)
                .replace("__VERSION__", version))


def login_page(lang: str = "en", error: str = "", version: str = "",
               product: str = "Simurgh Tunnel", signed_out: bool = False) -> str:
    """A real login form (the Basic-auth prompt is a fallback for scripts)."""
    text = I18N_FA if lang == "fa" else I18N_EN
    other = "en" if lang == "fa" else "fa"
    strings = {
        "title": text.get("login_title", "Sign in"),
        "kicker": text.get("login_kicker", "Secure control plane"),
        "notice": text.get("login_signed_out", "Signed out") if signed_out else "",
        "sub": text.get("login_sub", "the tunnel keeps running without this panel"),
        "user": text.get("login_user", "user"),
        "password": text.get("login_password", "password"),
        "go": text.get("login_go", "Sign in"),
        "hint": text.get("login_hint", "one-time links sign you in without the password"),
        "other": text.get("login_other", "فارسی" if lang == "en" else "English"),
        "dir": "rtl" if lang == "fa" else "ltr",
        "version": version,
        "product": product,
        "error": error,
    }
    return LOGIN_HTML.replace("__STRINGS__", json.dumps(strings, ensure_ascii=False)) \
                     .replace("__LANG__", lang) \
                     .replace("__DIR__", strings["dir"]) \
                     .replace("__OTHER__", other) \
                     .replace("__PRODUCT__", html_escape(product)) \
                     .replace("__VERSION__", html_escape(version)) \
                     .replace("__ERROR__", html_escape(error))


def login_error(lang: str, reason: str = "bad") -> str:
    """Why a login failed, in the reader's language ('bad' or 'locked')."""
    text = I18N_FA if str(lang).lower().startswith("fa") else I18N_EN
    return text.get("login_locked" if reason == "locked" else "login_bad", reason)


def html_escape(value: str) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


LOGIN_HTML = """<!DOCTYPE html>
<html lang="__LANG__" dir="__DIR__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0c1020">
<title>__PRODUCT__</title>
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="stylesheet" href="/app.css">
<script type="application/json" id="panel-meta">__STRINGS__</script>
<script src="/login.js" defer></script>
</head>
<body>
<div class="login-body">
  <div class="login-aura" aria-hidden="true"></div>
  <div class="login">
    <div class="login-emblem"><img src="/assets/simurgh.png" alt="Simurgh" width="108" height="108"></div>
    <div class="eyebrow login-kicker" data-s="kicker">Secure control plane</div>
    <h1 data-s="product">__PRODUCT__</h1>
    <p data-s="sub">the tunnel keeps running without this panel</p>
    <p class="login-notice" data-s="notice" hidden></p>
    <form method="post" action="/login">
      <input type="hidden" name="lang" value="__LANG__">
      <label for="user" data-s="user">user</label>
      <input id="user" name="user" autocomplete="username" autofocus>
      <label for="password" data-s="password">password</label>
      <input id="password" name="password" type="password" autocomplete="current-password">
      <div class="row" style="margin-top:16px">
        <button class="primary" type="submit" data-s="go">Sign in</button>
        <a class="btn ghost" href="/login?lang=__OTHER__" data-s="other">English</a>
      </div>
      <p class="hint" data-s="error"></p>
    </form>
    <p class="hint"><code>simurgh web --link</code> · v__VERSION__</p>
  </div>
</div>
</body>
</html>
"""


class LoginTokens:
    """Single-use, expiring links that sign a browser in without the password.

    The value is compared with ``hmac.compare_digest`` against a hash, so a
    token that leaks from a log line is useless once it has been used once, and
    unusable at all after :data:`LOGIN_TOKEN_TTL`.
    """

    def __init__(self, ttl: float = LOGIN_TOKEN_TTL, capacity: int = LOGIN_TOKEN_MAX):
        self.ttl = ttl
        self.capacity = capacity
        self.entries: list[dict] = []

    @staticmethod
    def _hash(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def create(self, label: str = "", ttl: float | None = None) -> dict:
        self.prune()
        raw = secrets.token_urlsafe(24)
        entry = {
            "hash": self._hash(raw),
            "created": time.time(),
            "expires": time.time() + (self.ttl if ttl is None else float(ttl)),
            "label": label,
        }
        self.entries.append(entry)
        while len(self.entries) > self.capacity:
            self.entries.pop(0)
        return {"token": raw, "expires_at": entry["expires"],
                "expires_in": round(entry["expires"] - time.time(), 1)}

    def prune(self) -> int:
        now = time.time()
        before = len(self.entries)
        self.entries = [e for e in self.entries if e["expires"] > now]
        return before - len(self.entries)

    def consume(self, raw: str) -> bool:
        """True the first time a live token is presented, then never again."""
        if not raw:
            return False
        digest = self._hash(raw)
        for index, entry in enumerate(self.entries):
            if hmac.compare_digest(entry["hash"], digest):
                del self.entries[index]
                return entry["expires"] > time.time()
        return False

    def revoke(self) -> int:
        count = len(self.entries)
        self.entries = []
        return count

    def snapshot(self) -> dict:
        self.prune()
        return {"outstanding": len(self.entries),
                "oldest": min((e["created"] for e in self.entries), default=None)}


# ------------------------------------------------------------- the wizard
def _port_from(text: str, default: int = 0) -> int:
    try:
        return int(str(text).split(":")[0])
    except (TypeError, ValueError, IndexError):
        return default


def plan_changes(cfg, role: str, plan: dict) -> list[str]:
    """Human-readable differences between the config and a wizard plan."""
    changes: list[str] = []
    carrier = str(plan.get("carrier") or "").strip().lower()
    port = int(plan.get("port") or 0)
    path = str(plan.get("path") or "/ws").strip() or "/ws"
    split = max(0, int(plan.get("split") or 0))
    host = str(plan.get("host") or "").strip()

    if role == "relay":
        current = cfg.exit
        if carrier and carrier != current.carrier:
            changes.append(f"[exit] carrier {current.carrier or '—'} -> {carrier}")
        if host and host != current.address:
            changes.append(f"[exit] address {current.address or '—'} -> {host}")
        if port and port != current.port:
            changes.append(f"[exit] port {current.port} -> {port}")
        if path != current.path:
            changes.append(f"[exit] path {current.path} -> {path}")
        if split != current.split:
            changes.append(f"[exit] split {current.split} -> {split}")
        mapping = str(plan.get("mapping") or "").strip()
        if mapping:
            listen, _, target = mapping.partition(":")
            listen_port = _port_from(listen)
            target_port = _port_from(target, listen_port)
            if listen_port and not any(m.listen == listen_port for m in cfg.mappings):
                changes.append(f"[[mapping]] {listen_port} -> {target_port} (added)")
    else:
        spec = cfg.listen[0] if cfg.listen else None
        if spec is None:
            changes.append(f"[[listen]] {carrier or 'tls'} on port {port or 443} (added)")
        else:
            if carrier and carrier != spec.carrier:
                changes.append(f"[[listen]] carrier {spec.carrier} -> {carrier}")
            if port and port != spec.port:
                changes.append(f"[[listen]] port {spec.port} -> {port}")
            if split != spec.split:
                changes.append(f"[[listen]] split {spec.split} -> {split}")
    return changes


def apply_plan(cfg, role: str, plan: dict) -> list[str]:
    """Set the plan on a loaded config; returns what changed (see plan_changes)."""
    changes = plan_changes(cfg, role, plan)
    carrier = str(plan.get("carrier") or "").strip().lower()
    port = int(plan.get("port") or 0)
    path = str(plan.get("path") or "").strip()
    split = max(0, int(plan.get("split") or 0))
    host = str(plan.get("host") or "").strip()

    if role == "relay":
        if carrier:
            cfg.exit.carrier = carrier
        if host:
            cfg.exit.address = host
        if port:
            cfg.exit.port = port
        if path:
            cfg.exit.path = path
        cfg.exit.split = split
        mapping = str(plan.get("mapping") or "").strip()
        if mapping:
            from .config import Mapping

            listen_text, _, target_text = mapping.partition(":")
            listen_port = _port_from(listen_text)
            target_port = _port_from(target_text, listen_port)
            if listen_port and not any(m.listen == listen_port for m in cfg.mappings):
                cfg.mappings.append(Mapping(listen=listen_port, target_port=target_port,
                                            name=f"wizard-{listen_port}"))
    else:
        from .config import ListenSpec

        if not cfg.listen:
            cfg.listen.append(ListenSpec())
        spec = cfg.listen[0]
        if carrier:
            spec.carrier = carrier
        if port:
            spec.port = port
        if path:
            spec.path = path
        spec.split = split
    return changes


def backup_path(path: Path, tag: str = "wizard") -> Path:
    return path.with_suffix(path.suffix + f".bak-{tag}")


def backup(path: Path, tag: str = "wizard") -> str:
    """Copy the config aside; returns the backup path ('' if there is none)."""
    if not path.exists():
        return ""
    target = backup_path(path, tag)
    shutil.copy2(path, target)
    try:
        target.chmod(0o600)
    except OSError:  # pragma: no cover - exotic filesystems
        pass
    return str(target)


def rollback(path: Path, backup_file: str) -> tuple[bool, str]:
    source = Path(backup_file) if backup_file else backup_path(path)
    if not source.is_file():
        return False, f"no backup to restore ({source})"
    shutil.copy2(source, path)
    return True, str(source)

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

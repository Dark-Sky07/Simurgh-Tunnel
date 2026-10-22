"""The web panel: a small, self-contained asyncio HTTP server.

No framework, no build step, no CDN (it has to work from inside Iran where
external assets may be blocked).  The whole dashboard is one HTML string with
inline CSS/JS that polls ``/api/status`` every two seconds.

Endpoints
---------
``GET  /``                dashboard (basic auth)
``GET  /api/status``      live status of whichever node hosts the panel
``GET  /api/config``      current configuration as JSON
``POST /api/config``      change a few safe fields (carrier, address, ...)
``POST /api/mappings``    add / remove / toggle a port mapping
``POST /api/action``      restart | start | stop | speedtest | reconnect
``GET  /api/logs``        tail of the log file
``GET  /api/join``        (exit only) the join payload for a new relay
``POST /api/pair``        pair this relay with the exit's setup link
``GET  /healthz``         unauthenticated liveness probe
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __product__, __version__
from .config import (ConfigError, ExitConfig, Mapping, load_exit, load_relay,
                     save_exit, save_relay)
from .layout import History, RateTracker, build as build_layout
from .panel_i18n import EN as I18N_EN, FA as I18N_FA
from .panel_ui import (CSP, LOGIN_TOKEN_TTL, LoginTokens, apply_plan, backup,
                       dashboard_html, login_error, login_page, plan_changes,
                       read_asset, rollback)
from .stats import Stats
from .util import Home, clamp, get_logger

#: the dashboard's words, in both languages (see simurgh/panel_i18n.py)
PANEL_I18N = {"en": I18N_EN, "fa": I18N_FA}
#: what the panel may be told to load: no other carrier is a legal config
CARRIERS = ("tls", "wss", "raw", "plain", "udp")

log = get_logger("simurgh.panel")

SESSION_COOKIE = "simurgh_session"


# ---------------------------------------------------------------- http bits
class Request:
    __slots__ = ("method", "path", "query", "headers", "body", "peer")

    def __init__(self, method: str, path: str, query: dict, headers: dict,
                 body: bytes, peer: str):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.peer = peer

    def json(self) -> dict:
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8", "replace"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def form(self) -> dict:
        """Form fields of a POST (the login page posts the old-fashioned way)."""
        from urllib.parse import parse_qs

        if not self.body:
            return {}
        try:
            fields = parse_qs(self.body.decode("utf-8", "replace"), keep_blank_values=True)
        except Exception:                                      # pragma: no cover
            return {}
        return {key: value[0] for key, value in fields.items()}


def http_response(body: bytes, status: int = 200, ctype: str = "text/html; charset=utf-8",
                  extra: dict | None = None, cache: str = "no-store") -> bytes:
    reason = {200: "OK", 204: "No Content", 302: "Found", 303: "See Other",
              400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
              404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
              429: "Too Many Requests", 500: "Internal Server Error"}.get(status, "OK")
    head = [
        f"HTTP/1.1 {status} {reason}",
        f"Content-Type: {ctype}",
        f"Content-Length: {len(body)}",
        f"Cache-Control: {cache}",
        "X-Content-Type-Options: nosniff",
        "Referrer-Policy: no-referrer",
        "Connection: close",
    ]
    if ctype.startswith("text/html"):
        # the panel is a page an operator opens through the tunnel: it may not
        # pull anything in, and nothing may frame it
        head.append(f"Content-Security-Policy: {CSP}")
        head.append("X-Frame-Options: DENY")
    for key, value in (extra or {}).items():
        head.append(f"{key}: {value}")
    return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body


def json_response(data, status: int = 200) -> bytes:
    return http_response(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                         status=status, ctype="application/json; charset=utf-8")


async def read_request(reader: asyncio.StreamReader, peer: str,
                       max_body: int = 1 << 20) -> Request | None:
    """Read one HTTP request.  Deliberately forgiving: the panel is not a
    general-purpose web server and must never stay stuck."""
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
        return None
    lines = head.split(b"\r\n")
    if not lines or not lines[0]:
        return None
    try:
        method, target, _ = lines[0].decode("latin-1").split(" ", 2)
    except ValueError:
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    total = int(headers.get("content-length") or 0)
    total = clamp(total, 0, max_body)
    body = b""
    if total and method in ("POST", "PUT"):
        try:
            body = await asyncio.wait_for(reader.readexactly(total), 15)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            return None
    parsed = urlparse(target)
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    return Request(method, unquote(parsed.path), query, headers, body, peer)


# ------------------------------------------------------------------- panel
class Panel:
    """Live panel bound to a running node (exit or relay).

    ``node`` is either an ``ExitNode`` or a ``RelayNode``; both expose
    ``status()`` and share enough of a shape for the dashboard.
    """

    def __init__(self, home: Home, role: str, node=None, host: str = "0.0.0.0",
                 port: int = 8787, user: str = "admin", password: str = "",
                 allow_ips: tuple[str, ...] = (), state_path: Path | None = None,
                 on_action=None):
        self.home = home
        self.role = role
        self.node = node
        self.host = host
        self.port = port
        self.user = user or "admin"
        self.password = password
        self.allow_ips = tuple(allow_ips or ())
        self.state_path = state_path or home.state
        self.on_action = on_action
        self.stats = Stats()
        self.started_at = time.time()
        self.requests = 0
        self.last_error = ""
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()
        #: the map and the chart need a little history of their own
        self.history = History()
        self.rates = RateTracker()
        #: single-use login links (simurgh web --link, Security page)
        self.login_tokens = LoginTokens()
        self.last_layout: dict = {}
        #: brute-force protection: failed logins per peer, and when they are
        #: blocked until (see _register_failure / _locked_out)
        self.failed: dict[str, int] = {}
        self.locked: dict[str, float] = {}
        self.lockouts = 0

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        try:
            self._server = await asyncio.start_server(
                self._handle, self.host, self.port, backlog=64)
        except OSError as exc:
            self.last_error = f"cannot listen on {self.host}:{self.port}: {exc}"
            log.error("%s", self.last_error)
            raise
        if not self.port:
            for sock in self._server.sockets or ():
                name = sock.getsockname()
                if len(name) > 1 and name[1]:
                    self.port = name[1]
                    break
        log.info("panel on http://%s:%d (login: %s)", self.host, self.port, self.user)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
        for task in list(self._tasks):
            task.cancel()

    async def serve_forever(self) -> None:
        await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    # ----------------------------------------------------------- connection
    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if isinstance(peer, tuple) else str(peer)
        try:
            request = await read_request(reader, peer_ip)
            if request is None:
                return
            self.requests += 1
            response = await self._route(request, peer_ip)
            if response is not None:
                writer.write(response)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception as exc:  # pragma: no cover - defensive
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.debug("panel request failed", exc_info=True)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    # -------------------------------------------------------------- routing
    async def _route(self, req: Request, peer_ip: str) -> bytes | None:
        if req.path == "/healthz":
            return json_response({"ok": True, "product": __product__,
                                  "version": __version__, "role": self.role})
        if not self._allowed(peer_ip):
            return json_response({"ok": False, "error": "forbidden"}, 403)
        # ---- the page, its assets and the login form: no password needed
        if req.method == "GET" and (req.path.startswith("/assets/") or req.path in
                                    ("/app.css", "/app.js", "/login.js",
                                     "/manifest.webmanifest", "/favicon.ico",
                                     "/assets/simurgh.png")):
            return self.asset_response(req.path)
        if req.method == "GET" and req.path.rstrip("/") in ("/login", ""):
            if req.path.startswith("/login"):
                lang = "fa" if (req.query.get("lang") or "").lower().startswith("fa") else "en"
                return http_response(login_page(lang, version=__version__,
                                                product=__product__).encode("utf-8"))
        if req.method == "POST" and req.path == "/login":
            return self.handle_login(req, peer_ip)
        if req.method == "GET" and req.path == "/logout":
            return http_response(
                b"<html><body>signed out</body></html>", 200,
                extra={"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly"})
        # a one-time link signs this browser in and dies right here
        if req.method == "GET" and req.query.get("t") \
                and self.login_tokens.consume(req.query["t"]):
            return http_response(b"", 303, extra={
                "Location": "/",
                "Set-Cookie": (f"{SESSION_COOKIE}={self._session_value()}; "
                               "Path=/; HttpOnly; SameSite=Lax")})
        if req.path == "/api/join":
            if self.role != "exit" and not self._relay_is_reverse():
                return json_response(
                    {"ok": False, "error": "join links are served by the exit server "
                                           "(or by a relay that listens: dial = \"exit\")"},
                    400)
            # the other server presents the credentials from its link (POST body)
            if self._locked_out(peer_ip):
                return json_response(
                    {"ok": False, "error": "too many failed logins; try again later"},
                    429)
            body = req.json() if req.method == "POST" else {}
            if not self._credentials_ok(body.get("user"), body.get("password"), req):
                if req.method == "POST":
                    self._register_failure(peer_ip, req)
                return json_response({"ok": False, "error": "bad credentials"}, 401)
            return self.join_payload(req)
        if self._locked_out(peer_ip):
            return json_response(
                {"ok": False, "error": "too many failed logins; try again later"},
                429)
        if not self._authorised(req):
            self._register_failure(peer_ip, req)
            return http_response(
                b"<html><body><h3>Simurgh Tunnel</h3><p>Login required.</p></body></html>",
                401, extra={"WWW-Authenticate": 'Basic realm="simurgh"'})
        path = req.path
        if req.method == "GET" and path in ("/", "/index.html"):
            # Exchange the legacy password link for a cookie, then redirect to
            # a clean URL so the password does not remain in the address bar or
            # browser history. Prefer the single-use login links for sharing.
            if self.password and self._query_password_ok(req):
                return http_response(b"", 303, extra={
                    "Location": "/",
                    "Set-Cookie": (f"{SESSION_COOKIE}={self._session_value()}; "
                                   "Path=/; HttpOnly; SameSite=Lax"),
                })
            return http_response(self.dashboard().encode("utf-8"))
        if req.method == "GET" and path == "/api/status":
            return json_response(self.status())
        if req.method == "GET" and path == "/metrics":
            return http_response(self.metrics().encode("utf-8"), 200,
                                 ctype="text/plain; version=0.0.4; charset=utf-8")
        if req.method == "GET" and path == "/api/config":
            return json_response(self.config_dict())
        if req.method == "GET" and path == "/api/layout":
            return json_response(self.layout_payload())
        if req.method == "POST" and path == "/api/login-token":
            return self.create_login_token(req)
        if req.method == "POST" and path == "/api/tune":
            return self.set_profile(req.json())
        if req.method == "POST" and path == "/api/pair":
            return await self.pair(req.json())
        if req.method == "POST" and path == "/api/wizard":
            return self.wizard(req.json())
        if req.method == "GET" and path == "/api/update":
            return await self.update_check()
        if req.method == "POST" and path == "/api/config":
            return await self.update_config(req.json())
        if req.method == "POST" and path == "/api/mappings":
            return await self.update_mappings(req.json())
        if req.method == "POST" and path == "/api/action":
            return await self.action(req.json())
        if req.method == "GET" and path == "/api/logs":
            return json_response(self.logs(req.query.get("lines", "120")))
        return json_response({"ok": False, "error": "not found"}, 404)

    # brute-force protection: a handful of wrong passwords from one address
    # earns that address a quiet few minutes, and the operator gets told
    MAX_FAILURES = 5
    LOCKOUT_SECONDS = 300.0

    def _locked_out(self, peer_ip: str) -> bool:
        until = self.locked.get(peer_ip, 0.0)
        if not until:
            return False
        if time.monotonic() < until:
            return True
        self.locked.pop(peer_ip, None)
        self.failed.pop(peer_ip, None)
        return False

    def _register_failure(self, peer_ip: str, req: Request | None = None) -> None:
        """Count a failed login; lock the address and alert after too many."""
        count = self.failed.get(peer_ip, 0) + 1
        self.failed[peer_ip] = count
        if count < self.MAX_FAILURES:
            return
        self.locked[peer_ip] = time.monotonic() + self.LOCKOUT_SECONDS
        self.failed.pop(peer_ip, None)
        self.lockouts += 1
        log.warning("panel: %d failed logins from %s; blocked for %.0f s",
                    count, peer_ip, self.LOCKOUT_SECONDS)
        alerts = getattr(self.node, "alerts", None)
        if alerts is not None:
            try:
                alerts.notify(
                    "login",
                    f"panel: {count} failed logins from {peer_ip}; "
                    f"address blocked for {int(self.LOCKOUT_SECONDS / 60)} minutes",
                    level="warning", peer=peer_ip,
                    path=getattr(req, "path", "") if req is not None else "")
            except Exception:                                  # pragma: no cover
                pass

    def _allowed(self, peer_ip: str) -> bool:
        if not self.allow_ips:
            return True
        return peer_ip in self.allow_ips

    def _credentials_ok(self, user, password, req: Request | None = None) -> bool:
        """Compare credentials either from a JSON body or from Basic auth."""
        if not self.password:
            return True
        if user is not None and password is not None:
            return (hmac.compare_digest(str(user), self.user)
                    and hmac.compare_digest(str(password), self.password))
        return self._authorised(req) if req is not None else False

    def _session_value(self) -> str:
        import hashlib

        return hashlib.sha256(("simurgh-panel|" + self.password).encode()).hexdigest()[:32]

    def _query_password_ok(self, req: Request) -> bool:
        given = req.query.get("k", "")
        return bool(given) and hmac.compare_digest(given, self.password)

    def _authorised(self, req: Request) -> bool:
        if not self.password:
            return True
        if req.method == "GET" and self._query_password_ok(req):
            return True          # one-click link: /?k=<panel password>
        cookie = req.headers.get("cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and hmac.compare_digest(value.strip(),
                                                              self._session_value()):
                return True
        header = req.headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return False
        try:
            raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8", "replace")
        except Exception:
            return False
        user, _, password = raw.partition(":")
        return (hmac.compare_digest(user, self.user)
                and hmac.compare_digest(password, self.password))

    # ------------------------------------------------------------- assets
    def asset_response(self, path: str) -> bytes:
        """Serve one file that ships inside the package."""
        name = "simurgh.png" if path.endswith("simurgh.png") else \
            "favicon.png" if path == "/favicon.ico" else path.lstrip("/").split("/")[-1]
        found = read_asset(name)
        if found is None:
            return json_response({"ok": False, "error": "not found"}, 404)
        body, ctype = found
        # the image never changes: let the browser keep it, the API never
        cache = "public, max-age=86400" if name.endswith(".png") else "no-store"
        return http_response(body, ctype=ctype, cache=cache)

    def login_page(self, lang: str = "en", error: str = "") -> str:
        return login_page(lang, error=error, version=__version__, product=__product__)

    # ------------------------------------------------------- login handling
    def handle_login(self, req: Request, peer_ip: str) -> bytes:
        """A real form login; the Basic-auth prompt stays for scripts."""
        if self._locked_out(peer_ip):
            return http_response(
                self.login_page(str(req.query.get("lang") or "en"),
                                login_error(str(req.query.get("lang") or "en"),
                                            "locked")).encode("utf-8"),
                429)
        form = req.form()
        if not self.password:
            return http_response(b"", 303, extra={"Location": "/"})
        user, password = form.get("user", ""), form.get("password", "")
        if not (hmac.compare_digest(str(user), self.user)
                and hmac.compare_digest(str(password), self.password)):
            self._register_failure(peer_ip, req)
            return http_response(
                self.login_page(str(form.get("lang") or "en"),
                                login_error(str(form.get("lang") or "en"))).encode("utf-8"),
                401)
        return http_response(b"", 303, extra={
            "Location": "/",
            "Set-Cookie": (f"{SESSION_COOKIE}={self._session_value()}; "
                           "Path=/; HttpOnly; SameSite=Lax")})

    def create_login_token(self, req: Request) -> bytes:
        """Mint a single-use link for this panel (the Kariz-style hand-off)."""
        body = req.json()
        ttl = clamp(float(body.get("ttl") or LOGIN_TOKEN_TTL), 30.0, 86400.0)
        made = self.login_tokens.create(str(body.get("label") or "panel"), ttl)
        host = req.headers.get("host") or f"{self.host}:{self.port}"
        return json_response({"ok": True, "url": f"http://{host}/?t={made['token']}",
                              "expires_in": made["expires_in"],
                              "tokens": self.login_tokens.snapshot()})

    # ------------------------------------------------------- profile/tuning
    def set_profile(self, body: dict) -> bytes:
        """`simurgh tune <profile>` from the panel."""
        from . import tune

        cfg = self.load_config()
        if cfg is None:
            return json_response({"ok": False, "error": "no readable config"}, 400)
        try:
            profile = tune.get(str(body.get("profile") or ""))
        except KeyError as exc:
            return json_response({"ok": False,
                                  "error": str(exc.args[0] if exc.args else exc)}, 400)
        changes = tune.apply(cfg, self.role, profile.name)
        if changes:
            try:
                save_exit(cfg, self.home.exit_cfg) if self.role == "exit" \
                    else save_relay(cfg, self.home.relay_cfg)
            except OSError as exc:
                return json_response({"ok": False, "error": str(exc)}, 500)
            if self.node is not None:
                self.node.cfg = cfg
        return json_response({"ok": True, "profile": profile.name, "changes": changes,
                              "note": "take effect after a restart"})

    # ----------------------------------------------------------- the wizard
    def wizard(self, body: dict) -> bytes:
        """Preview, apply (with a backup) or roll back a configuration plan."""
        step = str(body.get("step") or "preview")
        plan = body.get("plan") or {}
        if not isinstance(plan, dict):
            return json_response({"ok": False, "error": "the plan must be an object"}, 400)
        path = self.home.exit_cfg if self.role == "exit" else self.home.relay_cfg
        if step == "rollback":
            ok, info = rollback(path, str(body.get("backup") or ""))
            if ok and self.node is not None:
                reloaded = self.load_config()
                if reloaded is not None:
                    self.node.cfg = reloaded
            return json_response({"ok": ok, "from": info,
                                  "error": "" if ok else info})
        cfg = self.load_config()
        if cfg is None:
            return json_response({"ok": False, "error": "no readable config"}, 400)
        problem = self.plan_problem(plan)
        if step == "preview":
            return json_response({"ok": not problem, "error": problem,
                                  "changes": [] if problem else plan_changes(cfg, self.role, plan)})
        if step != "apply":
            return json_response({"ok": False, "error": f"unknown step {step!r}"}, 400)
        if problem:
            return json_response({"ok": False, "error": problem}, 400)
        saved = backup(path)
        changes = apply_plan(cfg, self.role, plan)
        try:
            save_exit(cfg, path) if self.role == "exit" else save_relay(cfg, path)
        except OSError as exc:
            return json_response({"ok": False, "error": str(exc)}, 500)
        if self.node is not None:
            self.node.cfg = cfg
        return json_response({"ok": True, "changes": changes, "backup": saved})

    def plan_problem(self, plan: dict) -> str:
        """The first thing wrong with a wizard plan, or '' when it is sane."""
        carrier = str(plan.get("carrier") or "").strip().lower()
        if carrier and carrier not in CARRIERS:
            return f"unknown carrier {carrier!r}"
        try:
            port = int(plan.get("port") or 0)
        except (TypeError, ValueError):
            return "the tunnel port must be a number"
        if port and not (1 <= port <= 65535):
            return "the tunnel port is outside 1-65535"
        mapping = str(plan.get("mapping") or "").strip()
        if mapping:
            listen, _, target = mapping.partition(":")
            try:
                listen_port = int(listen)
                target_port = int(target or listen)
            except ValueError:
                return "the first port must look like listen:target"
            if not (1 <= listen_port <= 65535 and 1 <= target_port <= 65535):
                return "the first port is outside 1-65535"
        try:
            if int(plan.get("split") or 0) < 0:
                return "the split must be 0 or more"
        except (TypeError, ValueError):
            return "the split must be a number"
        if self.role == "relay" and not str(plan.get("host") or "").strip() \
                and not mapping and not str(plan.get("carrier") or "").strip():
            return "the foreign server address is required"
        return ""

    # ------------------------------------------------------------ updates
    async def update_check(self) -> bytes:
        from . import update as updater

        loop = asyncio.get_running_loop()
        try:
            info = await loop.run_in_executor(None, updater.check)
        except Exception as exc:
            return json_response({"ok": False, "error": str(exc)}, 502)
        return json_response({"ok": True, "update_available": bool(info.get("available")),
                              "version": info.get("current"), "latest": info.get("latest"),
                              "tag": info.get("tag"), "notes_url": info.get("notes_url")})

    # ---------------------------------------------------------- status/data
    def load_config(self):
        """This role's config, or None when it cannot be read."""
        path = self.home.exit_cfg if self.role == "exit" else self.home.relay_cfg
        try:
            return load_exit(path) if self.role == "exit" else load_relay(path)
        except (ConfigError, OSError):
            return None

    def engine_name(self) -> str:
        """Which data plane is actually going to run (see simurgh/engines.py)."""
        from . import engines

        cfg = self.load_config()
        declared = str(getattr(cfg, "engine", "") or "")
        found = engines.find_binary(self.home)
        if declared == "python":
            return "python"
        if declared == "go" and found:
            return "go"
        return "go" if found else "python"

    def certificates(self) -> list[dict]:
        """Every certificate this role serves, with days left (see simurgh/acme)."""
        from .acme import status as cert_status

        cfg = self.load_config()
        if cfg is None:
            return []
        try:
            return cert_status(cfg, self.role)
        except Exception:                                      # pragma: no cover
            return []

    def layout_payload(self) -> dict:
        """The map, the cards and the chart in one payload."""
        payload = build_layout(self.status(), self.role, self.history, self.rates,
                               certificates=self.certificates(),
                               engine=self.engine_name())
        payload["ok"] = True
        payload["panel"]["tokens"] = self.login_tokens.snapshot()
        self.last_layout = payload
        return payload

    # --------------------------------------------------------------- pairing
    def pairing_state(self) -> dict:
        """Whether this server still waits for its other half.

        A relay installed with ``--pair-later`` has no token and no exit yet;
        the panel then shows the pairing page instead of pretending to be a
        working tunnel.
        """
        if self.role != "relay":
            return {"paired": True, "required": False, "exit": ""}
        cfg = getattr(self.node, "cfg", None)
        if cfg is None:
            try:
                cfg = load_relay(self.home.relay_cfg)
            except (ConfigError, OSError):
                return {"paired": True, "required": False, "exit": ""}
        return {"paired": cfg.paired, "required": not cfg.paired,
                "exit": cfg.exit.describe() if cfg.paired else ""}

    async def pair(self, body: dict) -> bytes:
        """Complete an unpaired relay from the exit's setup link.

        The browser half of ``simurgh join '<link>'``: the link brings the
        secret and the exit, while everything this install already chose (the
        panel port, the engine, the limits, the ports added here) stays.  With
        a live node the tunnel starts dialing immediately.
        """
        if self.role != "relay":
            return json_response({"ok": False, "error": "only the relay is paired"}, 400)
        try:
            cfg = load_relay(self.home.relay_cfg)
        except (ConfigError, OSError) as exc:
            return json_response({"ok": False, "error": str(exc)}, 400)
        if cfg.reverse:
            return json_response(
                {"ok": False, "error": "this relay waits for its exit; run "
                                       "`simurgh link` here and import that link "
                                       "on the foreign server"}, 400)
        if cfg.paired:
            return json_response(
                {"ok": False, "error": "this relay is already paired; "
                                       "`simurgh join --force` replaces the exit"},
                409)
        link = str((body or {}).get("link") or "").strip()
        if not link:
            return json_response({"ok": False, "error": "paste the setup link first"}, 400)
        from .links import LinkError, merge_relay_payload, relay_payload_from_link

        try:
            payload = await asyncio.to_thread(relay_payload_from_link, link, 12.0)
            merged = merge_relay_payload(cfg, payload)
        except LinkError as exc:
            return json_response({"ok": False, "error": str(exc)}, 400)
        except (KeyError, ValueError, ConfigError) as exc:
            return json_response({"ok": False, "error": f"the link is incomplete: {exc}"}, 400)
        backup_file = backup(self.home.relay_cfg, "pair")
        try:
            save_relay(cfg, self.home.relay_cfg)
        except OSError as exc:
            return json_response({"ok": False, "error": str(exc)}, 500)
        applied = "restart"
        if self.node is not None:
            self.node.cfg = cfg
            supervisor = getattr(self.node, "supervisor", None)
            if supervisor is not None:
                supervisor.cfg = cfg
            try:
                await self.node.rebind()
                self.node.reconnect()
                applied = "live"
            except Exception as exc:        # pragma: no cover - defensive
                self.last_error = f"pairing applied on restart: {exc}"
                applied = "restart"
        self.last_layout = {}
        return json_response({
            "ok": True, "exit": merged["exit"], "mappings": merged["mappings"],
            "added": merged["added"], "applied": applied, "backup": backup_file,
            "note": ("the tunnel is dialing now" if applied == "live"
                     else "restart the service to apply"),
        })

    # ---------------------------------------------------------- status/data
    def status(self) -> dict:
        node_status: dict = {}
        if self.node is not None:
            try:
                node_status = self.node.status()
            except Exception as exc:  # pragma: no cover
                node_status = {"error": str(exc)}
        elif self.state_path.exists():
            try:
                node_status = json.loads(self.state_path.read_text()).get(self.role, {})
            except (OSError, ValueError):
                node_status = {}
        alerts = getattr(self.node, "alerts", None)
        pairing = self.pairing_state()
        panel = {
            "role": self.role,
            "paired": pairing["paired"],
            "pairing_required": pairing["required"],
            "uptime": time.time() - self.started_at,
            "requests": self.requests,
            "port": self.port,
            "user": self.user,
            "error": self.last_error,
            "lockouts": self.lockouts,
            "failed_logins": sum(self.failed.values()),
            "alerts": alerts.snapshot() if alerts is not None else None,
        }
        return {
            "ok": True, "product": __product__, "version": __version__,
            "now": time.time(), "panel": panel, "node": node_status,
        }

    # ------------------------------------------------------------- metrics
    def metrics(self) -> str:
        """Prometheus exposition text for the node behind this panel.

        Everything here comes from the same status the panel shows, so it works
        for either engine: with the Python engine the numbers are read live, and
        with the Go engine they come from the ``state.json`` that engine keeps
        up to date.  Scrapers can authenticate with Basic auth (or the session
        cookie), exactly like the rest of the API.
        """
        status = self.status()
        node = status.get("node") or {}
        panel = status.get("panel") or {}
        role = str(status.get("role") or self.role)

        def esc(value) -> str:
            text = str(value if value is not None else "")
            return (text.replace("\\", "\\\\").replace('"', '\\"')
                    .replace("\n", " "))

        def labels(**pairs) -> str:
            items = [f'{k}="{esc(v)}"' for k, v in pairs.items()
                     if v is not None and v != ""]
            return "{" + ",".join(items) + "}" if items else ""

        def number(value) -> str:
            try:
                if isinstance(value, bool):
                    return "1" if value else "0"
                if isinstance(value, int):
                    return str(value)
                return repr(float(value))
            except (TypeError, ValueError):
                return "0"

        lines: list[str] = []
        meta = labels(role=role, version=__version__, product=__product__)

        def emit(name: str, help_text: str, kind: str = "gauge") -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")

        emit("simurgh_up", "1 while this node is serving.")
        lines.append(f"simurgh_up{meta} 1")

        emit("simurgh_uptime_seconds", "Seconds since the node started.", "counter")
        lines.append(f"simurgh_uptime_seconds{meta} "
                     f"{number(node.get('uptime', panel.get('uptime', 0)))}")

        emit("simurgh_panel_requests_total", "Panel requests served.", "counter")
        lines.append(f"simurgh_panel_requests_total{meta} "
                     f"{number(panel.get('requests', 0))}")

        emit("simurgh_panel_lockouts_total",
             "Addresses blocked after too many failed logins.", "counter")
        lines.append(f"simurgh_panel_lockouts_total{meta} "
                     f"{number(panel.get('lockouts', 0))}")
        emit("simurgh_panel_failed_logins",
             "Wrong passwords currently counted against an address.")
        lines.append(f"simurgh_panel_failed_logins{meta} "
                     f"{number(panel.get('failed_logins', 0))}")

        alerts = panel.get("alerts") or {}
        emit("simurgh_alerts_sent_total", "Alerts delivered.", "counter")
        lines.append(f"simurgh_alerts_sent_total{meta} "
                     f"{number(alerts.get('sent', 0))}")
        emit("simurgh_alerts_failed_total", "Alerts that could not be delivered.",
             "counter")
        lines.append(f"simurgh_alerts_failed_total{meta} "
                     f"{number(alerts.get('failed', 0))}")
        emit("simurgh_alerts_enabled",
             "1 when a telegram/webhook alert channel is configured.")
        lines.append(f"simurgh_alerts_enabled{meta} "
                     f"{number(bool(alerts.get('enabled')))}")

        emit("simurgh_tunnel_connected", "1 when a tunnel to the other side is up.")
        lines.append(f"simurgh_tunnel_connected{meta} "
                     f"{number(bool(node.get('connected')))}")
        emit("simurgh_tunnel_connections", "Live tunnel connections.")
        lines.append(f"simurgh_tunnel_connections{meta} "
                     f"{number(node.get('connections', 0))}")
        emit("simurgh_tunnel_rtt_seconds", "RTT of the first live tunnel.")
        lines.append(f"simurgh_tunnel_rtt_seconds{meta} "
                     f"{number(float(node.get('rtt_ms') or 0) / 1000.0)}")
        emit("simurgh_reconnects_total", "Tunnel (re)connections since start.",
             "counter")
        lines.append(f"simurgh_reconnects_total{meta} "
                     f"{number(node.get('reconnects', 0))}")
        emit("simurgh_streams", "Live multiplexed streams.")
        lines.append(f"simurgh_streams{meta} {number(node.get('streams', 0))}")

        totals = ((node.get("stats") or {}).get("totals") or {})
        emit("simurgh_bytes_in_total", "Bytes received from users.", "counter")
        lines.append(f"simurgh_bytes_in_total{meta} "
                     f"{number(totals.get('in', totals.get('in_bytes', 0)))}")
        emit("simurgh_bytes_out_total", "Bytes sent to users.", "counter")
        lines.append(f"simurgh_bytes_out_total{meta} "
                     f"{number(totals.get('out', totals.get('out_bytes', 0)))}")
        emit("simurgh_blocked_total",
             "Connections refused because a mapping was over its quota.",
             "counter")
        lines.append(f"simurgh_blocked_total{meta} {number(node.get('blocked', 0))}")

        mappings = node.get("mappings") or []
        if mappings:
            emit("simurgh_mapping_bytes_in_total", "Bytes per mapping (users).",
                 "counter")
            emit("simurgh_mapping_bytes_out_total", "Bytes per mapping (answers).",
                 "counter")
            emit("simurgh_mapping_connections_total",
                 "Connections accepted per mapping.", "counter")
            emit("simurgh_mapping_active", "Live connections per mapping.")
            emit("simurgh_mapping_errors_total", "Errors per mapping.", "counter")
            emit("simurgh_mapping_up", "1 when the mapping is bound.")
            stats_by_key = ((node.get("stats") or {}).get("mappings") or {})
            for mapping in mappings:
                stats = mapping.get("stats") or stats_by_key.get(mapping.get("key")) or {}
                name = labels(key=mapping.get("key"), name=mapping.get("name"),
                              listen=mapping.get("listen"),
                              target=mapping.get("target"), role=role)
                lines.append("simurgh_mapping_bytes_in_total"
                             f"{name} {number(stats.get('in_bytes', 0))}")
                lines.append("simurgh_mapping_bytes_out_total"
                             f"{name} {number(stats.get('out_bytes', 0))}")
                lines.append("simurgh_mapping_connections_total"
                             f"{name} {number(stats.get('conns', 0))}")
                lines.append("simurgh_mapping_active"
                             f"{name} {number(stats.get('active', 0))}")
                lines.append("simurgh_mapping_errors_total"
                             f"{name} {number(stats.get('errors', 0))}")
                lines.append("simurgh_mapping_up"
                             f"{name} {number(bool(mapping.get('bound', True)))}")

        # traffic ledger (day / month / all time) -- both engines write it
        rows = node.get("traffic") or []
        if rows:
            emit("simurgh_traffic_bytes_total",
                 "Ledger traffic per key and period.", "counter")
            for row in rows:
                key = row.get("key")
                for period in ("today", "month", "total"):
                    lines.append("simurgh_traffic_bytes_total"
                                 f'{labels(key=key, period=period, role=role)} '
                                 f"{number(row.get(period, 0))}")

        # quotas, when the node reports them
        quotas = node.get("quotas") or {}
        if isinstance(quotas, dict) and quotas:
            emit("simurgh_quota_limit_bytes", "Configured quota per mapping.")
            emit("simurgh_quota_used_bytes", "Quota already used per mapping.")
            emit("simurgh_quota_exceeded", "1 when a mapping is over its quota.")
            for key, state in quotas.items():
                if not isinstance(state, dict):
                    continue
                name = labels(key=key, period=state.get("period"), role=role)
                lines.append("simurgh_quota_limit_bytes"
                             f"{name} {number(state.get('limit', 0))}")
                lines.append("simurgh_quota_used_bytes"
                             f"{name} {number(state.get('used', 0))}")
                lines.append("simurgh_quota_exceeded"
                             f"{name} {number(bool(state.get('exceeded')))}")

        health = node.get("health") or {}
        if isinstance(health, dict) and health:
            emit("simurgh_exit_up", "1 when a pool endpoint answers.")
            emit("simurgh_exit_rtt_seconds", "RTT per pool endpoint.")
            for endpoint, state in health.items():
                if not isinstance(state, dict):
                    continue
                name = labels(endpoint=endpoint, role=role)
                lines.append("simurgh_exit_up"
                             f"{name} {number(bool(state.get('up', True)))}")
                rtt = state.get("rtt_ms")
                if rtt is not None:
                    lines.append("simurgh_exit_rtt_seconds"
                                 f"{name} {number(float(rtt) / 1000.0)}")

        return "\n".join(lines) + "\n"

    def config_dict(self) -> dict:
        path = self.home.exit_cfg if self.role == "exit" else self.home.relay_cfg
        try:
            cfg = load_exit(path) if self.role == "exit" else load_relay(path)
        except (ConfigError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        data = cfg.to_dict()
        data["ok"] = True
        data["token_masked"] = _mask(cfg.token)
        data.pop("token", None)
        return data

    async def update_config(self, body: dict) -> bytes:
        """Only a safe subset can be edited from the panel."""
        path = self.home.exit_cfg if self.role == "exit" else self.home.relay_cfg
        try:
            if self.role == "exit":
                cfg = load_exit(path)
            else:
                cfg = load_relay(path)
        except (ConfigError, OSError) as exc:
            return json_response({"ok": False, "error": str(exc)}, 400)
        changed: list[str] = []
        if self.role == "relay":
            if "name" in body:
                cfg.name = str(body["name"])[:64]
                changed.append("name")
            if "accept_push" in body:
                cfg.accept_push = bool(body["accept_push"])
                changed.append("accept_push")
            endpoint = body.get("exit")
            if isinstance(endpoint, dict):
                for field in ("carrier", "address", "domain", "fingerprint"):
                    if field in endpoint:
                        value = str(endpoint[field])[:255]
                        if field == "carrier":
                            value = value.strip().lower()
                            if value not in CARRIERS:
                                return json_response(
                                    {"ok": False, "error": f"unknown carrier {value!r}"}, 400)
                        if field == "address" and not value.strip() and not cfg.reverse:
                            return json_response(
                                {"ok": False, "error": "the exit address cannot be empty"}, 400)
                        setattr(cfg.exit, field, value)
                        changed.append(f"exit.{field}")
                if "port" in endpoint:
                    try:
                        port = int(endpoint["port"])
                    except (TypeError, ValueError):
                        return json_response(
                            {"ok": False, "error": "the exit port must be a number"}, 400)
                    if not 1 <= port <= 65535:
                        return json_response(
                            {"ok": False, "error": "the exit port is outside 1-65535"}, 400)
                    cfg.exit.port = port
                    changed.append("exit.port")
                if "insecure_skip_verify" in endpoint:
                    cfg.exit.insecure_skip_verify = bool(endpoint["insecure_skip_verify"])
                    changed.append("exit.insecure_skip_verify")
        else:
            if "name" in body:
                cfg.name = str(body["name"])[:64]
                changed.append("name")
            if "strict_ports" in body:
                cfg.strict_ports = bool(body["strict_ports"])
                changed.append("strict_ports")
            if "proxy_protocol" in body and body["proxy_protocol"] in ("off", "v1", "v2"):
                cfg.proxy_protocol = body["proxy_protocol"]
                changed.append("proxy_protocol")
        if "connections" in body:
            # one tunnel per core-flow is the default; a pool spreads the load
            try:
                cfg.connections = clamp(int(body["connections"]), 1, 16)
                changed.append("connections")
            except (TypeError, ValueError):
                return json_response({"ok": False, "error": "connections must be a number"}, 400)
        if not changed:
            return json_response({"ok": False, "error": "no editable field given"}, 400)
        try:
            if self.role == "exit":
                save_exit(cfg, path)
            else:
                save_relay(cfg, path)
        except OSError as exc:
            return json_response({"ok": False, "error": str(exc)}, 500)
        if self.node is not None:
            self.node.cfg = cfg
        return json_response({"ok": True, "changed": changed,
                              "note": "take effect after a restart/reconnect"})

    async def update_mappings(self, body: dict) -> bytes:
        if self.role != "relay" or self.node is None:
            return json_response({"ok": False, "error": "mappings live on the relay"}, 400)
        try:
            cfg = load_relay(self.home.relay_cfg)
        except (ConfigError, OSError) as exc:
            return json_response({"ok": False, "error": str(exc)}, 400)
        action = str(body.get("action") or "")
        if action == "add":
            try:
                mapping = Mapping(
                    name=str(body.get("name") or "")[:48],
                    listen=clamp(int(body["listen"]), 1, 65535),
                    target_port=clamp(int(body.get("target_port") or body["listen"]),
                                      1, 65535),
                    target_host=str(body.get("target_host") or "127.0.0.1")[:64],
                    udp=bool(body.get("udp")),
                    enabled=True,
                )
            except (KeyError, TypeError, ValueError):
                return json_response({"ok": False, "error": "listen port is required"}, 400)
            if any(m.key() == mapping.key() for m in cfg.mappings):
                return json_response({"ok": False, "error": "that listen port is taken"}, 409)
            cfg.mappings.append(mapping)
        elif action in ("remove", "delete"):
            key = str(body.get("key") or "")
            before = len(cfg.mappings)
            cfg.mappings = [m for m in cfg.mappings if m.key() != key]
            if len(cfg.mappings) == before:
                return json_response({"ok": False, "error": "mapping not found"}, 404)
        elif action == "toggle":
            key = str(body.get("key") or "")
            found = False
            for m in cfg.mappings:
                if m.key() == key:
                    m.enabled = not m.enabled
                    found = True
            if not found:
                return json_response({"ok": False, "error": "mapping not found"}, 404)
        else:
            return json_response({"ok": False, "error": "unknown action"}, 400)
        try:
            save_relay(cfg, self.home.relay_cfg)
        except OSError as exc:
            return json_response({"ok": False, "error": str(exc)}, 500)
        self.node.cfg = cfg
        await self.node.rebind()
        return json_response({"ok": True, "mappings": self.mappings_list(),
                              "port_conflicts": getattr(self.node, "port_conflicts", list)()})

    async def action(self, body: dict) -> bytes:
        name = str(body.get("name") or body.get("action") or "")
        if name == "speedtest":
            from .speedtest import run_speedtest

            if self.role != "relay" or self.node is None:
                return json_response({"ok": False, "error": "speed test runs on the relay"}, 400)
            seconds = clamp(float(body.get("seconds") or 6), 2, 20)
            streams = int(clamp(float(body.get("streams") or 1), 1, 8))
            try:
                result = await run_speedtest(self.node, seconds=seconds,
                                             streams=streams,
                                             udp=bool(body.get("udp")))
            except Exception as exc:
                return json_response({"ok": False, "error": str(exc)}, 500)
            return json_response({"ok": True, "speedtest": result})
        if name == "reconnect":
            if self.role != "relay" or self.node is None:
                return json_response({"ok": False, "error": "only the relay reconnects"}, 400)
            self.node.reconnect()
            return json_response({"ok": True})
        if name in ("restart", "start", "stop"):
            if self.on_action is None:
                return json_response({"ok": False,
                                      "error": "service control is not available here"}, 400)
            result = self.on_action(name)
            return json_response({"ok": True, "result": result})
        return json_response({"ok": False, "error": "unknown action"}, 400)

    def mappings_list(self) -> list[dict]:
        cfg = self.node.cfg if self.node is not None else None
        if cfg is None:
            try:
                cfg = load_relay(self.home.relay_cfg)
            except (ConfigError, OSError):
                return []
        return [
            {"key": m.key(), "name": m.name, "listen": m.listen,
             "target_host": m.target_host, "target_port": m.target_port,
             "udp": m.udp, "enabled": m.enabled}
            for m in cfg.mappings
        ]

    def logs(self, lines: str = "120") -> dict:
        try:
            count = clamp(int(lines), 10, 2000)
        except ValueError:
            count = 120
        name = {"exit": "exit.log", "relay": "relay.log"}.get(self.role, "panel.log")
        path = self.home.logs / name
        if not path.exists():
            return {"ok": True, "lines": [], "file": str(path)}
        try:
            with path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                block = min(size, 96 * 1024)
                fh.seek(size - block)
                data = fh.read()
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        text = data.decode("utf-8", "replace").splitlines()[-count:]
        return {"ok": True, "lines": text, "file": str(path)}

    def _tunnel_fingerprint(self, cfg) -> str | None:
        """SHA-256 of the certificate the exit should pin (may be None)."""
        from .certs import fingerprint_of

        candidates = [cfg.tunnel.cert_file,
                      str(Path(self.home.path) / "cert" / "cert.pem")]
        for path in candidates:
            if path and Path(path).exists():
                try:
                    return fingerprint_of(path)
                except Exception:
                    continue
        return None

    def _relay_is_reverse(self) -> bool:
        """True when this relay accepts the tunnel (``dial = "exit"``)."""
        try:
            return load_relay(self.home.relay_cfg).reverse
        except (ConfigError, OSError):
            return False

    def join_payload(self, req: Request) -> bytes:
        """The link payload for the *other* server.

        On an exit this hands a new relay its tunnel endpoint (direct mode).
        On a relay that listens for the exit (``dial = "exit"``) it is the
        other way round: the exit asks us where to dial.
        """
        if self.role != "exit":
            return self._join_payload_reverse(req)
        try:
            cfg = load_exit(self.home.exit_cfg)
        except (ConfigError, OSError) as exc:
            return json_response({"ok": False, "error": str(exc)}, 500)
        host = req.query.get("host") or (req.headers.get("host", "").split(":")[0])
        listen = [spec for spec in cfg.listen if spec.enabled]
        if not listen:
            return json_response({"ok": False, "error": "no listener is enabled on the exit"}, 400)
        primary = listen[0]
        payload = {
            "ok": True,
            "version": __version__,
            "name": cfg.name or "exit",
            "token": cfg.token,
            "exit": {
                "carrier": primary.carrier,
                "address": host,
                "port": primary.port,
                "domain": None,
                "path": primary.path,
                "fingerprint": _fingerprint(cfg),
            },
            "pool": [
                {"carrier": spec.carrier, "address": host, "port": spec.port,
                 "path": spec.path,
                 "fingerprint": _fingerprint(cfg)}
                for spec in listen[1:]
            ],
            "push_ports": cfg.push_ports,
            "mappings": [{"port": p, "target": p, "name": f"port-{p}"}
                         for p in cfg.push_ports],
            "note": ("Keep this payload secret: the token is inside it. "
                     "Import it on the relay with `simurgh join <link>`."),
        }
        return json_response(payload)

    def _join_payload_reverse(self, req: Request) -> bytes:
        """Reverse mode: tell the exit where to dial us."""
        from .links import reverse_payload

        try:
            cfg = load_relay(self.home.relay_cfg)
        except (ConfigError, OSError) as exc:
            return json_response({"ok": False, "error": str(exc)}, 500)
        if not cfg.reverse:
            return json_response({
                "ok": False,
                "error": ("this relay dials the exit (direct mode): ask the exit "
                          "server for its setup link instead"),
            }, 400)
        host = req.query.get("host") or (req.headers.get("host", "").split(":")[0])
        fingerprint = self._tunnel_fingerprint(cfg)
        payload = reverse_payload(cfg, host, fingerprint)
        payload["version"] = __version__
        return json_response(payload)

    # ------------------------------------------------------------ dashboard
    def dashboard(self) -> str:
        """Serve the shell; the UI language is chosen in the browser."""
        return dashboard_html(self.role, __version__, __product__)


def _mask(token: str) -> str:
    if not token:
        return ""
    return f"{token[:6]}...{token[-4:]}"


def _fingerprint(cfg: ExitConfig) -> str | None:
    try:
        from .certs import fingerprint_of

        if cfg.cert_file and Path(cfg.cert_file).exists():
            return fingerprint_of(cfg.cert_file)
    except Exception:
        return None
    return None


# ------------------------------------------------------------------- html
# UI strings for the dashboard: the panel ships English and Persian.
# Keys are shared by the HTML (data-i18n attributes) and the JavaScript (t()).

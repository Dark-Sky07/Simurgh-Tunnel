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
from .stats import Stats
from .util import Home, clamp, get_logger

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


def http_response(body: bytes, status: int = 200, ctype: str = "text/html; charset=utf-8",
                  extra: dict | None = None) -> bytes:
    reason = {200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized",
              403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
              409: "Conflict", 500: "Internal Server Error"}.get(status, "OK")
    head = [
        f"HTTP/1.1 {status} {reason}",
        f"Content-Type: {ctype}",
        f"Content-Length: {len(body)}",
        "Cache-Control: no-store",
        "X-Content-Type-Options: nosniff",
        "Referrer-Policy: no-referrer",
        "Connection: close",
    ]
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

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        try:
            self._server = await asyncio.start_server(
                self._handle, self.host, self.port, backlog=64)
        except OSError as exc:
            self.last_error = f"cannot listen on {self.host}:{self.port}: {exc}"
            log.error("%s", self.last_error)
            raise
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
        if req.path == "/api/join":
            if self.role != "exit" and not self._relay_is_reverse():
                return json_response(
                    {"ok": False, "error": "join links are served by the exit server "
                                           "(or by a relay that listens: dial = \"exit\")"},
                    400)
            # the other server presents the credentials from its link (POST body)
            body = req.json() if req.method == "POST" else {}
            if not self._credentials_ok(body.get("user"), body.get("password"), req):
                return json_response({"ok": False, "error": "bad credentials"}, 401)
            return self.join_payload(req)
        if not self._authorised(req):
            return http_response(
                b"<html><body><h3>Simurgh Tunnel</h3><p>Login required.</p></body></html>",
                401, extra={"WWW-Authenticate": 'Basic realm="simurgh"'})
        path = req.path
        if req.method == "GET" and path in ("/", "/index.html"):
            extra = None
            # one-click login: /?k=<panel password> turns into a session cookie
            if self.password and self._query_password_ok(req):
                extra = {"Set-Cookie":
                         f"{SESSION_COOKIE}={self._session_value()}; Path=/; HttpOnly; SameSite=Lax"}
            return http_response(self.dashboard().encode("utf-8"), extra=extra)
        if req.method == "GET" and path == "/api/status":
            return json_response(self.status())
        if req.method == "GET" and path == "/api/config":
            return json_response(self.config_dict())
        if req.method == "POST" and path == "/api/config":
            return await self.update_config(req.json())
        if req.method == "POST" and path == "/api/mappings":
            return await self.update_mappings(req.json())
        if req.method == "POST" and path == "/api/action":
            return await self.action(req.json())
        if req.method == "GET" and path == "/api/logs":
            return json_response(self.logs(req.query.get("lines", "120")))
        return json_response({"ok": False, "error": "not found"}, 404)

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
        panel = {
            "role": self.role,
            "uptime": time.time() - self.started_at,
            "requests": self.requests,
            "port": self.port,
            "user": self.user,
            "error": self.last_error,
        }
        return {
            "ok": True, "product": __product__, "version": __version__,
            "now": time.time(), "panel": panel, "node": node_status,
        }

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
                        setattr(cfg.exit, field, str(endpoint[field])[:255])
                        changed.append(f"exit.{field}")
                if "port" in endpoint:
                    cfg.exit.port = clamp(int(endpoint["port"]), 1, 65535)
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
            try:
                result = await run_speedtest(self.node, seconds=seconds)
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
        """Serve the dashboard; the UI language is chosen in the browser."""
        return DASHBOARD_HTML.replace("__ROLE__", self.role) \
                             .replace("__VERSION__", __version__) \
                             .replace("__PRODUCT__", __product__) \
                             .replace("__I18N__", json.dumps(PANEL_I18N, ensure_ascii=False))


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
PANEL_I18N = {
    "en": {
        "state_connecting": "connecting…",
        "refresh": "Refresh",
        "card_tunnel": "Tunnel status",
        "lbl_exit": "current exit",
        "lbl_rtt": "round-trip time",
        "lbl_uptime": "uptime",
        "lbl_reconnects": "reconnects",
        "lbl_last_error": "last error",
        "card_speed": "Live speed",
        "download": "download",
        "upload": "upload",
        "lbl_total_down": "total download",
        "lbl_total_up": "total upload",
        "lbl_active": "active connections",
        "card_tools": "Tools",
        "btn_speedtest": "Speed test through the tunnel",
        "btn_reconnect": "Reconnect",
        "btn_restart": "Restart service",
        "lbl_panel_port": "panel port",
        "lbl_panel_requests": "panel requests",
        "card_mappings": "Port forwardings (mappings)",
        "th_name": "name",
        "th_iran_port": "Iranian port",
        "th_target": "target",
        "th_type": "type",
        "th_state": "state",
        "form_listen": "Iranian port",
        "form_target": "target port (foreign)",
        "form_name": "name (optional)",
        "btn_add": "Add",
        "hint_mappings": "A new mapping starts working right away and is saved to the config file.",
        "card_logs": "Live log",
        "btn_load_logs": "Load the log",
        "footer": "transparent tunnel between an Iranian and a foreign server",
        "no": "no",
        "yes": "yes",
        "state_connected": "connected",
        "state_down": "down",
        "state_error": "cannot reach the panel",
        "role_relay": "Iranian server (relay)",
        "role_exit": "Foreign server (exit)",
        "up": "up",
        "down": "down",
        "waiting_exit": "waiting for the foreign server",
        "waiting_relay": "waiting for a relay",
        "relays_connected": "connected relays: {n}",
        "no_error": "none",
        "dur_days": "{d} days and {h} h",
        "dur_hours": "{h} h and {m} min",
        "dur_minutes": "{m} min",
        "dur_seconds": "{s} s",
        "state_on": "on",
        "state_off": "off",
        "btn_turn_off": "turn off",
        "btn_turn_on": "turn on",
        "btn_delete": "delete",
        "confirm_delete": "Delete this mapping?",
        "toast_done": "done",
        "toast_error": "error: {msg}",
        "toast_conflict": "port conflict: {list}",
        "toast_need_listen": "enter the Iranian port",
        "toast_speed": "download: {down} Mbps / upload: {up} Mbps",
        "logs_empty": "no log yet"
    },
    "fa": {
        "state_connecting": "در حال اتصال…",
        "refresh": "به‌روزرسانی",
        "card_tunnel": "وضعیت تونل",
        "lbl_exit": "اکسای مقصد",
        "lbl_rtt": "پینگ",
        "lbl_uptime": "مدت اتصال",
        "lbl_reconnects": "تعداد اتصال‌ها",
        "lbl_last_error": "آخرین خطا",
        "card_speed": "سرعت لحظه‌ای",
        "download": "دانلود",
        "upload": "آپلود",
        "lbl_total_down": "کل دانلود",
        "lbl_total_up": "کل آپلود",
        "lbl_active": "اتصال‌های فعال",
        "card_tools": "ابزارها",
        "btn_speedtest": "تست سرعت از تونل",
        "btn_reconnect": "اتصال مجدد",
        "btn_restart": "ری‌استارت سرویس",
        "lbl_panel_port": "پورت پنل",
        "lbl_panel_requests": "درخواست‌های پنل",
        "card_mappings": "پورت‌های فوروارد (مپینگ‌ها)",
        "th_name": "نام",
        "th_iran_port": "پورت ایران",
        "th_target": "مقصد",
        "th_type": "نوع",
        "th_state": "وضعیت",
        "form_listen": "پورت ایران",
        "form_target": "پورت مقصد (خارج)",
        "form_name": "نام (اختیاری)",
        "btn_add": "افزودن",
        "hint_mappings": "مپینگ‌ها پس از افزودن، بلافاصله فعال می‌شوند و در فایل تنظیمات ذخیره می‌شوند.",
        "card_logs": "لاگ زنده",
        "btn_load_logs": "بارگذاری لاگ",
        "footer": "تونل شفاف بین سرور ایران و سرور خارج",
        "no": "خیر",
        "yes": "بله",
        "state_connected": "متصل",
        "state_down": "قطع",
        "state_error": "خطا در اتصال به پنل",
        "role_relay": "سرور ایران (رله)",
        "role_exit": "سرور خارج (اگزیت)",
        "up": "برقرار",
        "down": "قطع",
        "waiting_exit": "در انتظار اتصال به سرور خارج",
        "waiting_relay": "در انتظار اتصال رله",
        "relays_connected": "رله‌های متصل: {n}",
        "no_error": "بدون خطا",
        "dur_days": "{d} روز و {h} ساعت",
        "dur_hours": "{h} ساعت و {m} دقیقه",
        "dur_minutes": "{m} دقیقه",
        "dur_seconds": "{s} ثانیه",
        "state_on": "فعال",
        "state_off": "غیرفعال",
        "btn_turn_off": "خاموش",
        "btn_turn_on": "روشن",
        "btn_delete": "حذف",
        "confirm_delete": "حذف شود؟",
        "toast_done": "انجام شد",
        "toast_error": "خطا: {msg}",
        "toast_conflict": "هشدار پورت تکراری: {list}",
        "toast_need_listen": "پورت ایران را وارد کنید",
        "toast_speed": "دانلود: {down} Mbps / آپلود: {up} Mbps",
        "logs_empty": "لاگی موجود نیست"
    }
}

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en" dir="ltr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__PRODUCT__</title>
<style>
:root{
  --bg:#0b1020; --card:#141b31; --card2:#1b2440; --line:#26324f;
  --fg:#e8edf7; --dim:#93a0bd; --ok:#3ddc97; --warn:#ffb454; --err:#ff6b81;
  --accent:#5b8cff; --accent2:#22d3ee;
}
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(180deg,#0b1020,#0e1428 60%);color:var(--fg);
  font-family:Vazirmatn,Tahoma,"Segoe UI",system-ui,sans-serif;font-size:15px}
header{display:flex;align-items:center;gap:14px;padding:16px 22px;border-bottom:1px solid var(--line);
  background:rgba(20,27,49,.7);backdrop-filter:blur(8px);position:sticky;top:0;z-index:5;flex-wrap:wrap}
header .logo{width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,var(--accent),var(--accent2));
  display:grid;place-items:center;font-weight:700}
header h1{font-size:17px;margin:0;font-weight:600}
header .sp{flex:1}
.badge{padding:4px 10px;border-radius:999px;font-size:12px;border:1px solid var(--line);color:var(--dim)}
.badge.ok{color:var(--ok);border-color:rgba(61,220,151,.4);background:rgba(61,220,151,.1)}
.badge.err{color:var(--err);border-color:rgba(255,107,129,.4);background:rgba(255,107,129,.1)}
main{padding:20px;max-width:1200px;margin:0 auto;display:grid;gap:16px;
  grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
.card h2{font-size:14px;margin:0 0 12px;color:var(--dim);font-weight:600}
.big{font-size:26px;font-weight:700}
.sub{color:var(--dim);font-size:12px;margin-top:4px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.kv{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-bottom:1px dashed var(--line)}
.kv:last-child{border-bottom:0}
.kv span:first-child{color:var(--dim)}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px 6px;text-align:start;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:600;font-size:12px}
button{background:var(--card2);color:var(--fg);border:1px solid var(--line);border-radius:10px;
  padding:8px 14px;cursor:pointer;font-family:inherit;font-size:14px}
button:hover{border-color:var(--accent)}
button.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));border:0;color:#04122b;font-weight:700}
button.danger{color:var(--err);border-color:rgba(255,107,129,.4)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input,select{background:#0f1730;border:1px solid var(--line);border-radius:10px;color:var(--fg);
  padding:8px 10px;font-family:inherit;font-size:14px;width:100%}
label{display:block;color:var(--dim);font-size:12px;margin:8px 0 4px}
pre{background:#0d1428;border:1px solid var(--line);border-radius:10px;padding:12px;overflow:auto;
  max-height:320px;font-size:12px;direction:ltr;text-align:left}
canvas{width:100%;height:70px}
.muted{color:var(--dim)}
.pill{padding:2px 8px;border-radius:999px;font-size:11px;border:1px solid var(--line)}
.pill.on{color:var(--ok);border-color:rgba(61,220,151,.4)}
.pill.off{color:var(--dim)}
.toast{position:fixed;inset-inline-start:20px;bottom:20px;background:var(--card2);border:1px solid var(--line);
  padding:10px 16px;border-radius:12px;opacity:0;transition:.25s;z-index:9}
.toast.show{opacity:1}
footer{color:var(--dim);text-align:center;padding:24px;font-size:12px}
</style>
</head>
<body>
<header>
  <div class="logo">S</div>
  <h1>__PRODUCT__ <span class="muted">v__VERSION__</span></h1>
  <span class="badge" id="role">__ROLE__</span>
  <span class="badge" id="state" data-i18n="state_connecting">connecting…</span>
  <div class="sp"></div>
  <button id="lang" onclick="toggleLang()" title="Switch language">فا</button>
  <button onclick="load()" data-i18n="refresh">Refresh</button>
</header>

<main>
  <div class="card">
    <h2 data-i18n="card_tunnel">Tunnel status</h2>
    <div class="big" id="tunnel">—</div>
    <div class="sub" id="tunnel_sub">—</div>
    <div class="kv"><span data-i18n="lbl_exit">current exit</span><span id="exit">—</span></div>
    <div class="kv"><span data-i18n="lbl_rtt">round-trip time</span><span id="rtt">—</span></div>
    <div class="kv"><span data-i18n="lbl_uptime">uptime</span><span id="uptime">—</span></div>
    <div class="kv"><span data-i18n="lbl_reconnects">reconnects</span><span id="reconnects">—</span></div>
    <div class="kv"><span data-i18n="lbl_last_error">last error</span><span id="last_error">—</span></div>
  </div>

  <div class="card">
    <h2 data-i18n="card_speed">Live speed</h2>
    <div class="grid2">
      <div><div class="big" id="down">0</div><div class="sub" data-i18n="download">download</div></div>
      <div><div class="big" id="up">0</div><div class="sub" data-i18n="upload">upload</div></div>
    </div>
    <canvas id="spark" width="600" height="140"></canvas>
    <div class="kv"><span data-i18n="lbl_total_down">total download</span><span id="tot_in">0</span></div>
    <div class="kv"><span data-i18n="lbl_total_up">total upload</span><span id="tot_out">0</span></div>
    <div class="kv"><span data-i18n="lbl_active">active connections</span><span id="active">0</span></div>
  </div>

  <div class="card">
    <h2 data-i18n="card_tools">Tools</h2>
    <div class="row">
      <button class="primary" onclick="act('speedtest',{seconds:6})" data-i18n="btn_speedtest">Speed test through the tunnel</button>
      <button onclick="act('reconnect')" data-i18n="btn_reconnect">Reconnect</button>
      <button onclick="act('restart')" data-i18n="btn_restart">Restart service</button>
    </div>
    <div style="margin-top:12px">
      <div class="kv"><span data-i18n="lbl_panel_port">panel port</span><span id="panel_port">—</span></div>
      <div class="kv"><span data-i18n="lbl_panel_requests">panel requests</span><span id="panel_req">—</span></div>
    </div>
  </div>

  <div class="card" style="grid-column:1/-1">
    <h2 data-i18n="card_mappings">Port forwardings (mappings)</h2>
    <table id="mappings"><thead><tr>
      <th data-i18n="th_name">name</th><th data-i18n="th_iran_port">Iranian port</th>
      <th data-i18n="th_target">target</th><th data-i18n="th_type">type</th>
      <th data-i18n="th_state">state</th><th></th>
    </tr></thead><tbody></tbody></table>
    <div class="row" style="margin-top:12px">
      <div style="flex:1;min-width:120px"><label data-i18n="form_listen">Iranian port</label><input id="m_listen" placeholder="443"></div>
      <div style="flex:1;min-width:120px"><label data-i18n="form_target">target port (foreign)</label><input id="m_target" placeholder="443"></div>
      <div style="flex:1;min-width:120px"><label data-i18n="form_name">name (optional)</label><input id="m_name" placeholder="panel"></div>
      <div style="min-width:110px"><label>UDP</label><select id="m_udp"><option value="no">no</option><option value="yes">yes</option></select></div>
      <button class="primary" style="align-self:flex-end" onclick="addMapping()" data-i18n="btn_add">Add</button>
    </div>
    <div class="sub" id="mapping_hint" data-i18n="hint_mappings"></div>
  </div>

  <div class="card" style="grid-column:1/-1">
    <h2 data-i18n="card_logs">Live log</h2>
    <pre id="logs">…</pre>
    <div class="row"><button onclick="loadLogs()" data-i18n="btn_load_logs">Load the log</button></div>
  </div>
</main>
<div class="toast" id="toast"></div>
<footer>__PRODUCT__ v__VERSION__ — <span data-i18n="footer"></span></footer>

<script>
const I18N = __I18N__;
const $ = (id) => document.getElementById(id);
let series = [];
let LANG = localStorage.getItem('simurgh_lang');
if(!LANG){
  LANG = ((navigator.language||navigator.userLanguage||'en').toLowerCase().startsWith('fa')) ? 'fa' : 'en';
}
if(!I18N[LANG]){ LANG = 'en'; }
function t(key, vars){
  const table = I18N[LANG] || {};
  let s = (key in table) ? table[key] : (I18N.en[key] || key);
  if(vars){ for(const k in vars){ s = s.split('{'+k+'}').join(vars[k]); } }
  return s;
}
function applyLang(){
  document.documentElement.lang = LANG;
  document.documentElement.dir = (LANG === 'fa') ? 'rtl' : 'ltr';
  document.querySelectorAll('[data-i18n]').forEach(el=>{ el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll('[data-i18n-ph]').forEach(el=>{ el.placeholder = t(el.dataset.i18nPh); });
  const btn = $('lang'); if(btn){ btn.textContent = (LANG === 'fa') ? 'EN' : 'فا'; }
  const sel = $('m_udp');
  if(sel){ sel.options[0].textContent = t('no'); sel.options[1].textContent = t('yes'); }
}
function toggleLang(){
  LANG = (LANG === 'fa') ? 'en' : 'fa';
  localStorage.setItem('simurgh_lang', LANG);
  applyLang(); load(); loadLogs();
}
function fmtBytes(n){
  n = Number(n)||0; const u=['B','KB','MB','GB','TB']; let i=0;
  while(n>=1024 && i<u.length-1){n/=1024;i++;}
  return n.toFixed(n<10&&i>0?1:0)+' '+u[i];
}
function nTunnels(n){var t=n.tunnels;return Array.isArray(t)?t.length:(Number(t)||0);}
function fmtRate(bps){ return fmtBytes(bps)+'/s'; }
function fmtDuration(s){
  s=Math.max(0,Math.floor(Number(s)||0));
  const d=Math.floor(s/86400), h=Math.floor(s%86400/3600), m=Math.floor(s%3600/60);
  if(d) return t('dur_days', {d:d, h:h});
  if(h) return t('dur_hours', {h:h, m:m});
  if(m) return t('dur_minutes', {m:m});
  return t('dur_seconds', {s:s});
}
function toast(msg, bad){
  const el=$('toast'); el.textContent=msg; el.style.borderColor = bad? 'var(--err)':'var(--line)';
  el.classList.add('show'); setTimeout(()=>el.classList.remove('show'),2600);
}
async function load(){
  try{
    const r = await fetch('/api/status', {cache:'no-store'});
    const d = await r.json();
    const n = d.node || {}, rates = (n.stats && n.stats.rates) || {}, tot = (n.stats && n.stats.totals) || {};
    const nTunnels = Array.isArray(n.tunnels) ? n.tunnels.length : (Number(n.tunnels)||0);
    const connected = (d.panel.role==='relay') ? !!n.connected : (nTunnels > 0);
    $('state').textContent = connected ? t('state_connected') : t('state_down');
    $('state').className = 'badge ' + (connected ? 'ok' : 'err');
    $('role').textContent = d.panel.role==='relay' ? t('role_relay') : t('role_exit');
    $('tunnel').textContent = connected ? t('up') : t('down');
    $('tunnel').style.color = connected ? 'var(--ok)' : 'var(--err)';
    $('tunnel_sub').textContent = (d.panel.role==='relay')
       ? (n.current_exit || t('waiting_exit'))
       : (nTunnels ? t('relays_connected', {n:nTunnels}) : t('waiting_relay'));
    $('exit').textContent = (d.panel.role==='relay') ? (n.current_exit || '—')
       : ((n.exit_info && (n.exit_info.name||'-')) || '—');
    $('rtt').textContent = n.rtt_ms ? (n.rtt_ms.toFixed(1)+' ms') : '—';
    $('uptime').textContent = fmtDuration(n.uptime);
    $('reconnects').textContent = (n.reconnects!=null?n.reconnects:'—');
    $('last_error').textContent = n.last_error || (n.error||'') || t('no_error');
    const down = rates.in_bps||0, up = rates.out_bps||0;
    $('down').textContent = fmtRate(down); $('up').textContent = fmtRate(up);
    $('tot_in').textContent = fmtBytes(tot.in_bytes||0); $('tot_out').textContent = fmtBytes(tot.out_bytes||0);
    $('active').textContent = (tot.active!=null?tot.active:(n.active_conns||0));
    $('panel_port').textContent = d.panel.port; $('panel_req').textContent = d.panel.requests;
    series.push(down); if(series.length>90) series.shift();
    draw();
    renderMappings(n.mappings||[]);
  }catch(e){ $('state').textContent = t('state_error'); $('state').className='badge err'; }
}
function draw(){
  const c = $('spark'), ctx = c.getContext('2d');
  const w = c.width, h = c.height; ctx.clearRect(0,0,w,h);
  const max = Math.max(1, ...series);
  ctx.strokeStyle='#26324f'; ctx.beginPath(); ctx.moveTo(0,h-1); ctx.lineTo(w,h-1); ctx.stroke();
  const g = ctx.createLinearGradient(0,0,w,0); g.addColorStop(0,'#5b8cff'); g.addColorStop(1,'#22d3ee');
  ctx.strokeStyle=g; ctx.lineWidth=2; ctx.beginPath();
  series.forEach((v,i)=>{ const x = series.length<2?0:(i/(series.length-1))*w; const y = h-4-(v/max)*(h-12);
    i?ctx.lineTo(x,y):ctx.moveTo(x,y); });
  ctx.stroke();
}
function renderMappings(rows){
  const tb = $('mappings').querySelector('tbody'); tb.innerHTML='';
  rows.forEach(m=>{
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${m.name||'—'}</td><td>${m.listen}</td>
      <td>${m.target_host||'127.0.0.1'}:${m.target_port}</td>
      <td>${m.udp?'UDP':'TCP'}</td>
      <td><span class="pill ${m.enabled?'on':'off'}">${m.enabled?t('state_on'):t('state_off')}</span></td>
      <td></td>`;
    const cell = tr.lastElementChild;
    const tog = document.createElement('button'); tog.textContent = m.enabled? t('btn_turn_off') : t('btn_turn_on');
    tog.onclick = ()=>mapping({action:'toggle', key:m.key});
    const del = document.createElement('button'); del.textContent = t('btn_delete'); del.className='danger';
    del.style.marginInlineStart='6px';
    del.onclick = ()=>{ if(confirm(t('confirm_delete'))) mapping({action:'remove', key:m.key}); };
    cell.append(tog, del); tb.appendChild(tr);
  });
}
async function mapping(payload){
  const r = await fetch('/api/mappings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(payload)});
  const d = await r.json();
  toast(d.ok ? t('toast_done') : t('toast_error', {msg:(d.error||'')}), !d.ok);
  if(d.port_conflicts && d.port_conflicts.length){ toast(t('toast_conflict', {list:d.port_conflicts.join(', ')}), true); }
  load();
}
function addMapping(){
  const listen = parseInt($('m_listen').value||'0',10);
  if(!listen){ toast(t('toast_need_listen'), true); return; }
  mapping({action:'add', listen, target_port: parseInt($('m_target').value||listen,10),
           name:$('m_name').value, udp: $('m_udp').value==='yes'});
  $('m_listen').value=''; $('m_target').value=''; $('m_name').value='';
}
async function act(name, extra){
  const body = Object.assign({name}, extra||{});
  const r = await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  const d = await r.json();
  if(name==='speedtest' && d.ok){
    toast(t('toast_speed', {down:d.speedtest.download_mbps.toFixed(1), up:d.speedtest.upload_mbps.toFixed(1)}));
  } else { toast(d.ok ? t('toast_done') : t('toast_error', {msg:(d.error||'')}), !d.ok); }
  load();
}
async function loadLogs(){
  const r = await fetch('/api/logs?lines=150'); const d = await r.json();
  $('logs').textContent = (d.lines||[]).join('\n') || t('logs_empty');
}
applyLang();
load(); loadLogs(); setInterval(load, 2000);
</script>
</body>
</html>
"""

"""The web panel: authentication, status, mappings and the join payload."""

from __future__ import annotations

import asyncio
import base64
import pathlib
import json
import threading
import time

import pytest

from conftest import free_port
from simurgh.config import (ExitConfig, ExitEndpoint, ListenSpec, Mapping,
                            RelayConfig, load_exit, load_relay, save_exit, save_relay)
from simurgh import acme, panel_ui
from simurgh.panel import Panel

pytestmark = pytest.mark.asyncio

USER, PASSWORD = "admin", "s3cret-pw"


class FakeRelayNode:
    """Just enough surface for the panel (the real node needs sockets)."""

    def __init__(self, cfg: RelayConfig):
        self.cfg = cfg
        self.rebound = 0
        self.reconnected = 0

    def status(self):
        return {"role": "relay", "name": self.cfg.name, "connected": True,
                "current_exit": self.cfg.exit.describe(), "rtt_ms": 12.5,
                "uptime": 3.0, "reconnects": 1, "last_error": "",
                "bind_errors": [], "mappings": [],
                "stats": {"totals": {"in_bytes": 10, "out_bytes": 20},
                          "rates": {"in_bps": 1000, "out_bps": 2000}}}

    async def rebind(self):
        self.rebound += 1

    def port_conflicts(self):
        return []

    def reconnect(self):
        self.reconnected += 1


class _Raw:
    """A response with its status line and headers still intact."""

    def __init__(self, status: int, headers: dict, body: bytes) -> None:
        self.status, self.headers, self.body = status, headers, body

    def __getitem__(self, name: str) -> str:
        return self.headers[name.lower()]


async def _raw(port, path, *, method="GET", body=None, auth=None, cookie=None) -> _Raw:
    """One request, headers and all (``_request`` decodes JSON for us)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    head = [f"{method} {path} HTTP/1.1", "Host: localhost", "Connection: close"]
    if auth:
        token = base64.b64encode(auth.encode()).decode()
        head.append(f"Authorization: Basic {token}")
    if cookie:
        head.append(f"Cookie: simurgh_session={cookie}")
    payload = b""
    if body is not None:
        payload = (json.dumps(body).encode() if not isinstance(body, bytes) else body)
        head.append("Content-Type: application/json")
        head.append(f"Content-Length: {len(payload)}")
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + payload)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(-1), 10)
    writer.close()
    lines = raw.split(b"\r\n")
    status = int(lines[0].split(b" ", 2)[1])
    headers = {}
    for line in lines[1:]:
        if not line:
            break
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    return _Raw(status, headers, raw.partition(b"\r\n\r\n")[2])


async def _request(port, path, *, method="GET", body=None, auth=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    head = [f"{method} {path} HTTP/1.1", "Host: localhost", "Connection: close"]
    if auth:
        token = base64.b64encode(auth.encode()).decode()
        head.append(f"Authorization: Basic {token}")
    payload = b""
    if body is not None:
        payload = json.dumps(body).encode()
        head.append("Content-Type: application/json")
        head.append(f"Content-Length: {len(payload)}")
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + payload)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(-1), 10)
    writer.close()
    status = int(raw.split(b" ", 2)[1])
    _, _, data = raw.partition(b"\r\n\r\n")
    if data.startswith(b"{"):
        return status, json.loads(data)
    return status, data


async def _panel(home, tmp_path, role="relay", node=None, password=PASSWORD,
                 on_action=None):
    port = free_port()
    panel = Panel(home, role, node=node, host="127.0.0.1", port=port,
                  user=USER, password=password, on_action=on_action)
    await panel.start()
    return panel, port


@pytest.fixture()
def relay_cfg():
    return RelayConfig(
        token="tok", name="ir-tehran",
        exit=ExitEndpoint(carrier="tls", address="203.0.113.9", port=443),
        mappings=[Mapping(name="panel", listen=443, target_port=443)],
        panel_port=8787,
    )


async def test_healthz_needs_no_credentials(home, tmp_path, relay_cfg):
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg),
                               password="")
    try:
        status, data = await _request(port, "/healthz")
        assert status == 200 and data["ok"] is True
        version = data["version"]
        assert version.count(".") == 2 and all(part.isdigit() for part in version.split("."))
    finally:
        await panel.stop()


async def test_status_requires_authentication(home, tmp_path, relay_cfg):
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg))
    try:
        status, _ = await _request(port, "/api/status")
        assert status == 401
        status, _ = await _request(port, "/api/status", auth="admin:wrong")
        assert status == 401
        status, data = await _request(port, "/api/status", auth=f"{USER}:{PASSWORD}")
        assert status == 200
        assert data["node"]["connected"] is True
        assert data["panel"]["port"] == port
    finally:
        await panel.stop()


async def test_status_falls_back_to_the_state_file(home, tmp_path):
    home.state.write_text(json.dumps({"relay": {"connected": False,
                                                "last_error": "gone"}}))
    panel, port = await _panel(home, tmp_path, node=None)
    try:
        status, data = await _request(port, "/api/status", auth=f"{USER}:{PASSWORD}")
        assert status == 200
        assert data["node"]["last_error"] == "gone"
    finally:
        await panel.stop()


async def test_dashboard_is_a_page(home, tmp_path, relay_cfg):
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg))
    try:
        status, body = await _request(port, "/", auth=f"{USER}:{PASSWORD}")
        assert status == 200
        text = body.decode()
        assert "<!DOCTYPE html>" in text
        assert "Simurgh" in text
        # the layout direction follows the language picked in the browser
        assert 'dir="ltr"' in text and 'data-i18n' in text
        assert "lang-toggle" in text        # English / Persian switch
        assert "/app.css" in text and "/app.js" in text   # the panel's own assets
        assert "/api/layout" in panel_ui.read_asset("app.js")[0].decode()
        raw = await _raw(port, "/", auth=f"{USER}:{PASSWORD}")
        assert "content-security-policy" in raw.headers
        csp = raw.headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "style-src 'self' 'unsafe-inline'" in csp
        assert "script-src 'self';" in csp  # inline scripts remain forbidden
        assert "x-frame-options" in raw.headers
    finally:
        await panel.stop()


async def test_dashboard_shell_carries_no_inline_code(home, tmp_path, relay_cfg):
    """Strict CSP: the shell links its assets and inlines nothing."""
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg))
    try:
        status, body = await _request(port, "/", auth=f"{USER}:{PASSWORD}")
        assert status == 200
        text = body.decode()
        assert "<script>" not in text        # data block only, no inline code
        assert "<style" not in text
        assert 'id="map"' in text            # the Simurgh map
        assert 'id="chart"' in text          # live speed chart
        assert "/api/layout" in panel_ui.read_asset("app.js")[0].decode()
    finally:
        await panel.stop()


async def test_assets_are_served_from_the_package(home, tmp_path, relay_cfg):
    """The panel ships its CSS/JS/icon inside the wheel: no CDN, no network."""
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg))
    try:
        for path, ctype in (("/app.css", "text/css"), ("/app.js", "javascript"),
                            ("/login.js", "javascript"), ("/manifest.webmanifest", "json"),
                            ("/favicon.ico", "image/png"),
                            ("/assets/simurgh.png", "image/png")):
            raw = await _raw(port, path)
            assert raw.status == 200, path
            assert ctype in raw.headers.get("content-type", ""), (path, raw.headers)
            assert len(raw.body) > 100, path
        # the icon never changes, so the browser may keep it; code and JSON may not
        assert "max-age" in (await _raw(port, "/assets/simurgh.png")).headers["cache-control"]
        for path in ("/app.js", "/app.css", "/api/status"):
            assert "no-store" in (await _raw(port, path,
                                             auth=f"{USER}:{PASSWORD}")
                                  ).headers["cache-control"], path
        assert (await _raw(port, "/assets/nope.png")).status == 404
        assert (await _raw(port, "/assets/../panel.py")).status == 404
    finally:
        await panel.stop()


async def test_certificate_issue_accepts_subdomains_and_backs_up_config(
        home, tmp_path, monkeypatch):
    cfg = ExitConfig(token="tok", name="de", cert_auto=True,
                     listen=[ListenSpec(carrier="tls", port=443)])
    save_exit(cfg, home.exit_cfg)
    before = home.exit_cfg.read_text()
    cert_dir = tmp_path / "acme-store"
    cert_dir.mkdir()
    cert_file, key_file = cert_dir / "fullchain.cer", cert_dir / "example.com.key"
    cert_file.write_text("certificate")
    key_file.write_text("key")
    called = {}

    def fake_issue(domain, **kwargs):
        called["domain"] = domain
        called.update(kwargs)
        return acme.Result(
            client="acme.sh", domains=[domain, "www.example.com", "api.example.com"],
            cert_file=str(cert_file), key_file=str(key_file), steps=["issued all names"])

    monkeypatch.setattr(acme, "issue", fake_issue)
    panel, port = await _panel(home, tmp_path, role="exit")
    body = {"domain": "example.com", "subdomains": "www.example.com, api.example.com",
            "email": "admin@example.com", "method": "standalone"}
    try:
        status, _ = await _request(port, "/api/certificates/issue", method="POST", body=body)
        assert status == 401 and not called
        status, data = await _request(port, "/api/certificates/issue", method="POST",
                                      body=body, auth=f"{USER}:{PASSWORD}")
        assert status == 200, data
        assert data["ok"] is True and data["restart_required"] is True
        assert data["domains"] == ["example.com", "www.example.com", "api.example.com"]
        assert data["certificate"]["cert_file"] == str(cert_file)
        assert called["additional_domains"] == body["subdomains"]
        assert called["standalone"] is True and called["webroot"] == ""
        assert called["email"] == "admin@example.com"
        assert called["reload_cmd"].endswith("simurgh-exit") or called["reload_cmd"] == "simurgh restart"
        saved = load_exit(home.exit_cfg)
        assert saved.cert_file == str(cert_file) and saved.key_file == str(key_file)
        assert saved.cert_auto is False
        assert saved.token == "tok" and saved.name == "de"
        backup = pathlib.Path(data["backup"])
        assert backup.name.endswith(".bak-cert") and backup.read_text() == before
        assert panel._cert_issue_running is False
    finally:
        await panel.stop()


async def test_certificate_issue_failure_does_not_rewrite_config(home, tmp_path, monkeypatch):
    cfg = ExitConfig(token="tok", cert_auto=True,
                     listen=[ListenSpec(carrier="tls", port=443)])
    save_exit(cfg, home.exit_cfg)
    before = home.exit_cfg.read_bytes()

    def fail_issue(domain, **kwargs):
        raise acme.AcmeError("port 80 is not reachable")

    monkeypatch.setattr(acme, "issue", fail_issue)
    panel, port = await _panel(home, tmp_path, role="exit")
    try:
        status, data = await _request(
            port, "/api/certificates/issue", method="POST", auth=f"{USER}:{PASSWORD}",
            body={"domain": "example.com", "method": "standalone"})
        assert status == 400 and "port 80" in data["error"]
        assert home.exit_cfg.read_bytes() == before
        assert panel._cert_issue_running is False
        # A webroot request without its directory is rejected before ACME runs.
        status, data = await _request(
            port, "/api/certificates/issue", method="POST", auth=f"{USER}:{PASSWORD}",
            body={"domain": "example.com", "method": "webroot", "webroot": ""})
        assert status == 400 and "webroot" in data["error"]
    finally:
        await panel.stop()


async def test_certificate_issue_rejects_a_concurrent_request(home, tmp_path, monkeypatch):
    save_exit(ExitConfig(token="tok", cert_auto=True,
                         listen=[ListenSpec(carrier="tls", port=443)]), home.exit_cfg)
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_text("certificate")
    key_file.write_text("key")
    started, release = threading.Event(), threading.Event()

    def slow_issue(domain, **kwargs):
        started.set()
        release.wait(2)
        return acme.Result(client="acme.sh", domains=[domain], cert_file=str(cert_file),
                           key_file=str(key_file))

    monkeypatch.setattr(acme, "issue", slow_issue)
    panel, port = await _panel(home, tmp_path, role="exit")
    body = {"domain": "example.com", "method": "standalone"}
    first = asyncio.create_task(_request(port, "/api/certificates/issue", method="POST",
                                         body=body, auth=f"{USER}:{PASSWORD}"))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        second_status, second = await _request(
            port, "/api/certificates/issue", method="POST", body=body,
            auth=f"{USER}:{PASSWORD}")
    finally:
        release.set()
    first_status, first = await first
    await panel.stop()
    assert second_status == 409 and "already running" in second["error"]
    assert first_status == 200 and first["ok"] is True
    assert panel._cert_issue_running is False


async def test_mapping_lifecycle_is_saved_and_applied(home, tmp_path, relay_cfg):
    save_relay(relay_cfg, home.relay_cfg)
    node = FakeRelayNode(relay_cfg)
    panel, port = await _panel(home, tmp_path, node=node)
    auth = f"{USER}:{PASSWORD}"
    try:
        status, data = await _request(port, "/api/mappings", method="POST",
                                      auth=auth,
                                      body={"action": "add", "listen": 8443,
                                            "target_port": 8443, "name": "alt"})
        assert status == 200 and data["ok"]
        assert node.rebound == 1
        assert [m.listen for m in load_relay(home.relay_cfg).mappings] == [443, 8443]

        status, data = await _request(port, "/api/mappings", method="POST",
                                      auth=auth,
                                      body={"action": "add", "listen": 8443})
        assert status == 409                      # duplicate port

        key = [m.key() for m in node.cfg.mappings if m.listen == 8443][0]
        status, data = await _request(port, "/api/mappings", method="POST",
                                      auth=auth, body={"action": "toggle", "key": key})
        assert status == 200
        assert load_relay(home.relay_cfg).mappings[1].enabled is False

        status, data = await _request(port, "/api/mappings", method="POST",
                                      auth=auth, body={"action": "remove", "key": key})
        assert status == 200
        assert [m.listen for m in load_relay(home.relay_cfg).mappings] == [443]
    finally:
        await panel.stop()


async def test_config_endpoint_hides_the_token(home, tmp_path, relay_cfg):
    save_relay(relay_cfg, home.relay_cfg)
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg))
    try:
        status, data = await _request(port, "/api/config", auth=f"{USER}:{PASSWORD}")
        assert status == 200
        assert "token" not in data
        assert data["token_masked"] == "tok...tok"
        assert data["ok"] is True
    finally:
        await panel.stop()


async def test_unknown_path_is_404(home, tmp_path, relay_cfg):
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg))
    try:
        status, _ = await _request(port, "/nope", auth=f"{USER}:{PASSWORD}")
        assert status == 404
    finally:
        await panel.stop()


async def test_join_payload_only_on_the_exit(home, tmp_path, relay_cfg):
    """A relay has nothing to hand out: only the exit serves join links."""
    save_relay(relay_cfg, home.relay_cfg)
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg))
    try:
        status, data = await _request(port, "/api/join", method="POST",
                                      body={"user": USER, "password": PASSWORD})
        assert status == 400 and "exit" in data["error"]
    finally:
        await panel.stop()


async def test_join_payload_from_an_exit(home, tmp_path, certs):
    save_exit(ExitConfig(token="join-token", name="omega-de",
                         cert_file=certs[0], key_file=certs[1], cert_auto=False,
                         push_ports=[443, 2053],
                         listen=[ListenSpec(carrier="tls", port=443, path="/ws"),
                                 ListenSpec(carrier="wss", port=2053, path="/ws")]),
              home.exit_cfg)
    panel, port = await _panel(home, tmp_path, role="exit", node=None)
    try:
        status, data = await _request(port, "/api/join", method="POST",
                                      body={"user": USER, "password": PASSWORD})
        assert status == 200 and data["ok"]
        assert data["token"] == "join-token"
        assert data["exit"]["address"] == "localhost"
        assert data["exit"]["port"] == 443
        assert data["exit"]["fingerprint"]
        assert [m["port"] for m in data["mappings"]] == [443, 2053]
        assert data["pool"][0]["port"] == 2053
        assert data["pool"][0]["fingerprint"] == data["exit"]["fingerprint"]

        status, _ = await _request(port, "/api/join", method="POST",
                                   body={"user": USER, "password": "nope"})
        assert status == 401
        status, _ = await _request(port, "/api/join", method="GET")   # no creds
        assert status == 401
    finally:
        await panel.stop()


async def test_logs_endpoint_reads_the_file(home, tmp_path):
    save_exit(ExitConfig(token="t"), home.exit_cfg)
    home.logs.mkdir(exist_ok=True)
    (home.logs / "exit.log").write_text(
        "".join(f"line-{i:02d}\n" for i in range(1, 13)))
    panel, port = await _panel(home, tmp_path, role="exit", node=None)
    try:
        status, data = await _request(port, "/api/logs?lines=12",
                                      auth=f"{USER}:{PASSWORD}")
        assert status == 200
        assert data["lines"] == [f"line-{i:02d}" for i in range(1, 13)]
    finally:
        await panel.stop()


async def test_service_action_calls_the_hook(home, tmp_path, relay_cfg):
    calls: list[str] = []

    def hook(name: str):
        calls.append(name)
        return {"simurgh-relay": "ok"}

    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg),
                               on_action=hook)
    try:
        status, data = await _request(port, "/api/action", method="POST",
                                      auth=f"{USER}:{PASSWORD}",
                                      body={"name": "restart"})
        assert status == 200 and data["ok"]
        assert calls == ["restart"]
        status, data = await _request(port, "/api/action", method="POST",
                                      auth=f"{USER}:{PASSWORD}",
                                      body={"name": "nonsense"})
        assert status == 400
    finally:
        await panel.stop()


async def test_speedtest_action_reports_a_result(home, tmp_path, relay_cfg,
                                                 monkeypatch):
    import simurgh.speedtest as speedtest_module

    async def fake_speedtest(node, seconds=6, port=0, udp=False, streams=1,
                            latency=True):
        assert udp is True and streams == 1
        return {"seconds": seconds, "download_mbps": 42.0, "upload_mbps": 21.0,
                "download_bytes": 1000, "upload_bytes": 500,
                "latency": {"idle_ms": 1.0, "loaded_ms": 5.0, "grade": "A"},
                "udp": {"sent": 10, "received": 10, "loss_percent": 0.0}}

    monkeypatch.setattr(speedtest_module, "run_speedtest", fake_speedtest)
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg))
    try:
        status, data = await _request(port, "/api/action", method="POST",
                                      auth=f"{USER}:{PASSWORD}",
                                      body={"name": "speedtest", "seconds": 2,
                                            "udp": True})
        assert status == 200
        assert data["speedtest"]["download_mbps"] == 42.0
        assert data["speedtest"]["latency"]["grade"] == "A"
    finally:
        await panel.stop()


async def test_one_click_login_link_sets_a_session_cookie(home, tmp_path, relay_cfg):
    save_relay(relay_cfg, home.relay_cfg)
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg))
    try:
        # wrong key: still asks for credentials
        status, _ = await _request(port, "/?k=nope")
        assert status == 401
        # right key: a cookie plus a redirect that removes the password from
        # the address bar instead of rendering the dashboard at /?k=...
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET /?k={PASSWORD} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
                     .encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), 10)
        writer.close()
        assert b"303 See Other" in raw
        assert b"Location: /" in raw
        assert b"simurgh_session=" in raw
        cookie = raw.split(b"simurgh_session=")[1].split(b";")[0].decode()
        # and the cookie alone is enough for the API
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET /api/status HTTP/1.1\r\nHost: x\r\n"
                     f"Cookie: simurgh_session={cookie}\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), 10)
        writer.close()
        assert b"200 OK" in raw and b'"ok": true' in raw
    finally:
        await panel.stop()


def test_dashboard_is_bilingual_and_has_a_language_switch():
    """The web panel ships English + Persian; the text menu stays English."""
    import re

    from simurgh import panel_ui

    html = panel_ui.read_asset("index.html")[0].decode()
    script = panel_ui.read_asset("app.js")[0].decode()
    login_script = panel_ui.read_asset("login.js")[0].decode()
    ui_source = pathlib.Path(panel_ui.__file__).read_text(encoding="utf-8")
    # the shell itself is language neutral ("فا" is the switch's pre-JS fallback)
    assert not re.search(r"[\u0600-\u06FF]", html.replace('id="lang-toggle" title="language">فا', ""))
    assert "lang-toggle" in html            # the switch itself
    langs = panel_ui.PANEL_I18N
    assert set(langs) == {"en", "fa"}
    assert set(langs["en"]) == set(langs["fa"])
    # every key the UI asks for exists in both languages
    used = set(re.findall(r'data-i18n[a-z-]*="([a-z_0-9]+)"', html))
    used |= {key for key in re.findall(r"(?<![A-Za-z0-9_$.])t\('([a-z_0-9]+)'",
                                       script + html) if not key.endswith("_")}
    assert used, "no i18n keys found in the panel"
    assert used <= set(langs["en"]), sorted(used - set(langs["en"]))
    # families the JS builds by name (t('confirm_' + name)) and bare key strings
    for family in ("confirm_restart", "confirm_stop", "confirm_delete"):
        assert family in langs["en"] and family in langs["fa"]
    quoted = set(re.findall(r"['\"]([a-z][a-z_0-9]{2,})['\"]", script + login_script))
    for call in re.findall(r"text\.get\(([^)]*)\)", ui_source):
        quoted |= set(re.findall(r"['\"]([a-z_0-9]+)['\"]", call))
    quoted |= set(re.findall(r'"([a-z_0-9]+)"', panel_ui.LOGIN_HTML))
    unused = set(langs["en"]) - used - quoted
    assert not unused, sorted(unused)
    # and the two languages say the same things
    assert {k for k, v in langs["en"].items() if "{n}" in v} == \
        {k for k, v in langs["fa"].items() if "{n}" in v}
    # English says "فارسی" on the switch, and nothing else in Persian
    persian = {k: v for k, v in langs["en"].items()
               if re.search(r"[\u0600-\u06FF]", v) and k != "login_other"}
    assert not persian, sorted(persian)
    # Persian prose, with the commands and protocol names it has to quote
    prose = re.compile(r"[A-Za-z]{3,}\s+[A-Za-z]{3,}")
    for key, value in langs["fa"].items():
        leaked = prose.search(re.sub(r"`[^`]*`|simurgh \w+", "", value))
        assert not leaked, (key, value)

    # the language the login page was asked for is the language it renders
    assert "ورود" in panel_ui.login_page("fa")
    assert "Sign in" in panel_ui.login_page("en")
    # the panel remembers the language and the theme it was opened with
    assert "localStorage" in script and "lang-toggle" in script


async def test_dashboard_served_over_http_carries_both_languages(home, tmp_path, relay_cfg):
    save_relay(relay_cfg, home.relay_cfg)
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg))
    try:
        status, raw = await _request(port, "/", auth=f"{USER}:{PASSWORD}")
        assert status == 200
        assert b'data-i18n="card_mappings"' in raw
        assert "تونل".encode() in raw and b"Tunnel" in raw
        assert b"lang-toggle" in raw
        # the words travel to the browser in the shell's data block
        assert b'id="panel-meta"' in raw
        assert raw.index(b"panel-meta") < raw.index(b"<main")
    finally:
        await panel.stop()


async def test_metrics_endpoint_speaks_prometheus(home, tmp_path, relay_cfg):
    """`/metrics` is scrapable, authenticated, and reads both engines' status."""
    node = FakeRelayNode(relay_cfg)
    node.status = lambda: {
        "role": "relay", "connected": True, "connections": 2, "rtt_ms": 25.0,
        "uptime": 42.5, "reconnects": 3, "streams": 7, "blocked": 4,
        "stats": {"totals": {"in": 1234, "out": 5678}},
        "traffic": [{"key": "tcp:0.0.0.0:443/127.0.0.1:8443", "today": 11,
                     "month": 22, "total": 33}],
        "quotas": {"tcp:0.0.0.0:443/127.0.0.1:8443": {"limit": 100, "used": 60,
                                                      "period": "month",
                                                      "exceeded": False}},
        "health": {"tls://exit.test:443": {"up": True, "rtt_ms": 30.0}},
        "mappings": [{"key": "tcp:0.0.0.0:443/127.0.0.1:8443", "name": "web",
                      "listen": 443, "target": "127.0.0.1:8443", "bound": True,
                      "stats": {"in_bytes": 10, "out_bytes": 20, "conns": 2,
                                "active": 1, "errors": 0}}],
    }
    panel, port = await _panel(home, tmp_path, role="relay", node=node)
    try:
        status, body = await _request(port, "/metrics", auth=f"{USER}:{PASSWORD}")
        assert status == 200
        text = body.decode()
        assert "simurgh_up{" in text and 'role="relay"' in text
        assert 'simurgh_tunnel_connected{role="relay"' in text
        assert "simurgh_tunnel_connections" in text
        assert "simurgh_tunnel_rtt_seconds" in text and "0.025" in text
        assert "simurgh_bytes_in_total" in text and "1234" in text
        assert "simurgh_blocked_total" in text and " 4" in text
        assert ('simurgh_mapping_bytes_out_total{key="tcp:0.0.0.0:443/'
                '127.0.0.1:8443",name="web",listen="443"') in text
        assert 'simurgh_traffic_bytes_total{key="tcp:0.0.0.0:443/127.0.0.1:8443",period="month"' in text
        assert 'simurgh_quota_used_bytes{key="tcp:0.0.0.0:443/127.0.0.1:8443",period="month"' in text
        assert "simurgh_exit_rtt_seconds" in text and "0.03" in text
        # every metric line is `name{labels} value` with a numeric value
        import re
        pattern = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})? '
                             r'[-+0-9.eE]+$')
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            assert pattern.match(line), line
        # and every HELP has its TYPE
        for line in text.splitlines():
            if line.startswith("# HELP "):
                name = line.split()[2]
                assert f"# TYPE {name} " in text, name
    finally:
        await panel.stop()


async def test_metrics_needs_credentials_too(home, tmp_path, relay_cfg):
    panel, port = await _panel(home, tmp_path, role="relay",
                               node=FakeRelayNode(relay_cfg))
    try:
        status, _body = await _request(port, "/metrics")
        assert status == 401
    finally:
        await panel.stop()


async def test_metrics_from_a_go_engine_state_file(home, tmp_path):
    """The Go engine writes state.json; the panel turns it into metrics."""
    (home.path / "state.json").write_text(json.dumps({"relay": {
        "role": "relay", "engine": "go", "connected": True, "connections": 1,
        "rtt_ms": 10.0, "uptime": 5.0, "streams": 2, "blocked": 0,
        "stats": {"totals": {"in": 100, "out": 200}},
        "traffic": [{"key": "tcp:0.0.0.0:8080/127.0.0.1:80", "today": 1,
                     "month": 2, "total": 3}],
        "mappings": [{"key": "tcp:0.0.0.0:8080/127.0.0.1:80", "name": "web",
                      "listen": 8080, "target": "127.0.0.1:80", "bound": True,
                      "stats": {"in_bytes": 100, "out_bytes": 200, "conns": 1,
                                "active": 1, "errors": 0}}],
    }}))
    panel, port = await _panel(home, tmp_path, role="relay", node=None)
    try:
        status, body = await _request(port, "/metrics", auth=f"{USER}:{PASSWORD}")
        assert status == 200
        text = body.decode()
        assert "simurgh_bytes_out_total" in text and "200" in text
        assert "simurgh_mapping_up" in text and 'listen="8080"' in text
        assert "simurgh_traffic_bytes_total" in text
    finally:
        await panel.stop()


# ------------------------------------------------- brute force protection
async def test_anonymous_page_views_are_not_bad_passwords(home, tmp_path, relay_cfg):
    """The first 401 is a login challenge, not a failed login attempt.

    Browsers can make that challenge several times before the user submits a
    password.  Counting those visits used to lock the operator out after five
    ordinary page loads, and then even the correct password received 429.
    """
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg))
    try:
        for _ in range(Panel.MAX_FAILURES + 2):
            status, _ = await _request(port, "/")
            assert status == 401
        assert panel.failed == {} and panel.lockouts == 0
        status, body = await _request(port, "/", auth=f"{USER}:{PASSWORD}")
        assert status == 200 and b"Simurgh" in body
    finally:
        await panel.stop()


class RecordingAlerts:
    """Stands in for ``node.alerts`` and remembers what was announced."""

    def __init__(self):
        self.events = []
        self.sent = 0
        self.failed = 0

    def notify(self, event, message, level="info", **fields):
        self.events.append({"event": event, "text": message, "level": level,
                            **fields})

    def snapshot(self):
        return {"enabled": True, "channels": "webhook", "events": ["all"],
                "sent": self.sent, "failed": self.failed, "last_error": ""}


async def test_the_panel_blocks_an_address_that_keeps_guessing(home, tmp_path,
                                                              relay_cfg):
    node = FakeRelayNode(relay_cfg)
    node.alerts = RecordingAlerts()
    panel, port = await _panel(home, tmp_path, node=node)
    try:
        for _ in range(Panel.MAX_FAILURES):
            status, _ = await _request(port, "/api/status", auth="admin:wrong")
            assert status == 401
        status, data = await _request(port, "/api/status", auth="admin:wrong")
        assert status == 429 and "too many failed logins" in data["error"]

        # knowing the password does not help while the address is blocked
        status, _ = await _request(port, "/api/status", auth=f"{USER}:{PASSWORD}")
        assert status == 429
        assert panel.lockouts == 1
        assert node.alerts.events[0]["event"] == "login"
        assert node.alerts.events[0]["level"] == "warning"
        assert "failed logins" in node.alerts.events[0]["text"]

        # monitoring must keep working: /healthz is unauthenticated on purpose
        assert (await _request(port, "/healthz"))[0] == 200

        # four wrong passwords later on are not yet a lockout...
        panel.locked = {ip: 0.0 for ip in panel.locked}
        panel.failed.clear()
        for _ in range(Panel.MAX_FAILURES - 1):
            status, _ = await _request(port, "/api/status", auth="admin:wrong")
            assert status == 401
        # ...and a good password still works, because the window has passed
        status, _ = await _request(port, "/api/status", auth=f"{USER}:{PASSWORD}")
        assert status == 200
    finally:
        await panel.stop()


async def test_a_lockout_expires_and_is_forgotten(home, tmp_path, relay_cfg):
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg))
    try:
        for _ in range(Panel.MAX_FAILURES):
            await _request(port, "/api/status", auth="admin:wrong")
        assert (await _request(port, "/api/status"))[0] == 429

        # pretend the lockout window is over
        panel.locked = {ip: time.monotonic() - 1 for ip in panel.locked}
        status, _ = await _request(port, "/api/status", auth=f"{USER}:{PASSWORD}")
        assert status == 200
        assert panel.locked == {} and panel.failed == {}
    finally:
        await panel.stop()


async def test_join_credentials_are_guarded_too(home, tmp_path):
    home.exit_cfg.write_text(
        'token = "tok"\nname = "kharej"\npanel_port = 8788\n'
        "[[listen]]\ncarrier = \"raw\"\nport = 8080\n")
    panel, port = await _panel(home, tmp_path, role="exit")
    try:
        for _ in range(Panel.MAX_FAILURES):
            status, _ = await _request(port, "/api/join", method="POST",
                                       body={"user": "x", "password": "no"})
            assert status == 401
        status, data = await _request(port, "/api/join", method="POST",
                                      body={"user": "x", "password": "no"})
        assert status == 429 and panel.lockouts == 1
        # a GET (the human view) is not counted, so it does not extend a lockout
        status, _ = await _request(port, "/api/join")
        assert status == 429
    finally:
        await panel.stop()


async def test_status_reports_the_alert_state_and_the_guard(home, tmp_path,
                                                            relay_cfg):
    node = FakeRelayNode(relay_cfg)
    node.alerts = RecordingAlerts()
    panel, port = await _panel(home, tmp_path, node=node)
    try:
        status, data = await _request(port, "/api/status", auth=f"{USER}:{PASSWORD}")
        assert status == 200
        assert data["panel"]["alerts"]["channels"] == "webhook"
        assert data["panel"]["lockouts"] == 0
        assert data["panel"]["failed_logins"] == 0
    finally:
        await panel.stop()


async def test_metrics_count_the_guard_and_the_alerts(home, tmp_path, relay_cfg):
    node = FakeRelayNode(relay_cfg)
    node.alerts = RecordingAlerts()
    panel, port = await _panel(home, tmp_path, node=node)
    try:
        for _ in range(Panel.MAX_FAILURES):
            await _request(port, "/api/status", auth="admin:wrong")
        # an operator whose address is blocked cannot scrape either; the
        # counter has already been bumped, so end the lockout and look
        panel.locked = {ip: 0.0 for ip in panel.locked}
        status, body = await _request(port, "/metrics", auth=f"{USER}:{PASSWORD}")
        assert status == 200
        text = body.decode()
        assert "simurgh_panel_lockouts_total{" in text
        assert "simurgh_alerts_enabled{" in text
        assert any(line.startswith("simurgh_panel_lockouts_total{")
                   and line.endswith(" 1") for line in text.splitlines())
        assert any(line.startswith("simurgh_alerts_sent_total{") for line in text.splitlines())
        assert any(line.startswith("simurgh_alerts_enabled{")
                   and line.endswith(" 1") for line in text.splitlines())
    finally:
        await panel.stop()


# ------------------------------------------------------------- the v3 panel
class FullNode(FakeRelayNode):
    """A relay that reports everything the new map draws."""

    def status(self):
        status = super().status()
        status.update({
            "mappings": [
                {"key": "tcp:0.0.0.0:443/127.0.0.1:8443", "name": "panel",
                 "listen": 443, "target": "127.0.0.1:8443", "enabled": True,
                 "bound": True, "stats": {"in_bytes": 4096, "out_bytes": 8192,
                                          "conns": 2, "active": 2}},
            ],
            "quotas": {"tcp:0.0.0.0:443/127.0.0.1:8443": {"limit": 1024 ** 3,
                                                          "used": 512 * 1024 ** 2,
                                                          "period": "month",
                                                          "action": "block"}},
            "traffic": [{"key": "tcp:0.0.0.0:443/127.0.0.1:8443", "today": 1234,
                         "month": 5678, "total": 9012}],
            "stats": {"totals": {"in": 4096, "out": 8192, "sum": 12288, "conns": 2},
                      "rates": {"in_rate": 2048.0, "out_rate": 1024.0}},
            "health": {"exits": [{"endpoint": "203.0.113.9:443", "rtt_ms": 12.5}]},
            "tunnel_targets": ["203.0.113.9:443"],
        })
        return status


async def test_layout_endpoint_feeds_the_map(home, tmp_path, relay_cfg):
    """One poll returns everything the page draws: map, cards and chart."""
    save_relay(relay_cfg, home.relay_cfg)
    panel, port = await _panel(home, tmp_path, node=FullNode(relay_cfg))
    try:
        status, one = await _request(port, "/api/layout", auth=f"{USER}:{PASSWORD}")
        assert status == 200 and one["ok"] is True
        assert one["role"] == "relay" and one["state"] == "up"
        assert one["peer"]["endpoint"] == relay_cfg.exit.describe()
        assert one["pool"][0]["state"] == "active"
        assert one["mappings"][0]["quota"]["limit"] == 1024 ** 3
        assert one["totals"]["today"] == 1234
        assert one["engine"] in ("python", "go")
        assert one["panel"]["port"] == port and one["panel"]["user"] == USER
        assert "outstanding" in one["panel"]["tokens"]
        # a second poll builds history for the chart
        status, two = await _request(port, "/api/layout", auth=f"{USER}:{PASSWORD}")
        assert status == 200
        assert len(two["chart"]["t"]) >= len(one["chart"]["t"])
        assert two["rates"]["in"] == 2048.0
    finally:
        await panel.stop()


async def test_layout_still_works_without_a_live_node(home, tmp_path, relay_cfg):
    """A supervised Go engine: the panel reads the state file it writes."""
    save_relay(relay_cfg, home.relay_cfg)
    home.state.write_text(json.dumps({"relay": {"connected": True, "name": "go-relay",
                                                "stats": {"totals": {"in": 5, "out": 6}}}}))
    panel, port = await _panel(home, tmp_path, node=None)
    try:
        status, data = await _request(port, "/api/layout", auth=f"{USER}:{PASSWORD}")
        assert status == 200
        assert data["name"] == "go-relay" and data["state"] == "up"
        assert data["totals"]["sum"] == 11
        assert data["chart"]["t"] is not None
    finally:
        await panel.stop()


async def test_one_time_login_link(home, tmp_path, relay_cfg):
    """`/api/login-token` mints a link that signs in once and dies."""
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg))
    try:
        status, made = await _request(port, "/api/login-token", method="POST",
                                      auth=f"{USER}:{PASSWORD}",
                                      body={"label": "laptop", "ttl": 120})
        assert status == 200 and made["ok"] is True
        assert made["url"].startswith("http://") and "?t=" in made["url"]
        assert made["expires_in"] == 120
        token = made["url"].split("?t=", 1)[1]

        # unknown token: still a 401, no cookie
        raw = await _raw(port, "/?t=nope")
        assert raw.status == 401 and b"set-cookie" not in raw.headers

        # the real one: a session cookie and a redirect to the panel
        raw = await _raw(port, f"/?t={token}")
        assert raw.status == 303
        assert raw.headers["location"] == "/"
        cookie = raw.headers["set-cookie"].split("simurgh_session=")[1].split(";")[0]
        assert "HttpOnly" in raw.headers["set-cookie"]

        # and it is spent: the second visit is not a login
        assert (await _raw(port, f"/?t={token}")).status == 401

        # The opaque cookie works until logout. Logout both expires it in the
        # browser and revokes it server-side, so replaying the old value fails.
        assert (await _raw(port, "/api/layout", cookie=cookie)).status == 200
        raw = await _raw(port, "/logout?lang=en", cookie=cookie)
        assert raw.status == 303 and "Max-Age=0" in raw.headers["set-cookie"]
        assert raw.headers["location"] == "/login?lang=en&signed_out=1"
        assert (await _raw(port, "/api/layout", cookie=cookie)).status == 401
        notice = await _raw(port, raw.headers["location"])
        assert notice.status == 200 and b"signed out safely" in notice.body

        # A fresh login creates a different session and does not resurrect the
        # revoked token. This guards the old deterministic-cookie behaviour.
        fresh = await _raw(port, "/login", method="POST",
                           body=f"user={USER}&password={PASSWORD}".encode())
        new_cookie = fresh.headers["set-cookie"].split("simurgh_session=")[1].split(";")[0]
        assert fresh.status == 303 and new_cookie != cookie
        assert (await _raw(port, "/api/layout", cookie=new_cookie)).status == 200
        assert (await _raw(port, "/api/layout", cookie=cookie)).status == 401
        panel.sessions[new_cookie] = time.time() - 1
        assert (await _raw(port, "/api/layout", cookie=new_cookie)).status == 401
    finally:
        await panel.stop()


async def test_the_login_page_is_a_form_and_speaks_persian(home, tmp_path, relay_cfg):
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg))
    try:
        # the form itself needs no credentials
        raw = await _raw(port, "/login")
        assert raw.status == 200 and b"<form" in raw.body
        assert b'action="/login"' in raw.body
        assert "ورود".encode() not in raw.body     # English unless asked
        persian = await _raw(port, "/login?lang=fa")
        assert persian.status == 200 and b'dir="rtl"' in persian.body

        # a wrong password says so, in the language that was asked for
        raw = await _raw(port, "/login", method="POST",
                         body=b"user=admin&password=nope&lang=fa")
        assert raw.status == 401
        assert "کاربر یا رمز اشتباه است".encode() in raw.body

        # the right one hands out a session cookie and sends us to the panel
        raw = await _raw(port, "/login", method="POST",
                         body=f"user={USER}&password={PASSWORD}".encode())
        assert raw.status == 303 and raw.headers["location"] == "/"
        assert "simurgh_session=" in raw.headers["set-cookie"]
        assert (await _raw(port, "/api/layout",
                           auth=f"{USER}:{PASSWORD}")).status == 200
    finally:
        await panel.stop()


async def test_settings_reject_an_invalid_exit_without_corrupting_config(
        home, tmp_path, relay_cfg):
    save_relay(relay_cfg, home.relay_cfg)
    before = home.relay_cfg.read_text()
    panel, port = await _panel(home, tmp_path, node=FullNode(relay_cfg))
    auth = f"{USER}:{PASSWORD}"
    try:
        for endpoint, complaint in (
            ({"port": "not-a-port"}, "number"),
            ({"port": 70000}, "1-65535"),
            ({"carrier": "carrier-pigeon"}, "carrier"),
            ({"address": ""}, "address"),
        ):
            status, data = await _request(
                port, "/api/config", method="POST", auth=auth,
                body={"exit": endpoint})
            assert status == 400 and data["ok"] is False
            assert complaint in data["error"]
            assert home.relay_cfg.read_text() == before

        status, data = await _request(
            port, "/api/config", method="POST", auth=auth,
            body={"exit": {"carrier": "wss", "port": 9443}})
        assert status == 200 and data["ok"] is True
        changed = load_relay(home.relay_cfg)
        assert changed.exit.carrier == "wss" and changed.exit.port == 9443
    finally:
        await panel.stop()


async def test_the_wizard_previews_applies_and_rolls_back(home, tmp_path, relay_cfg):
    """The wizard is a plan, a backup, and a way back."""
    save_relay(relay_cfg, home.relay_cfg)
    original = home.relay_cfg.read_text()
    panel, port = await _panel(home, tmp_path, node=FullNode(relay_cfg))
    try:
        plan = {"carrier": "wss", "port": 8443, "host": "198.51.100.7",
                "path": "/tunnel", "split": 2, "mapping": "2053:2053"}
        status, preview = await _request(port, "/api/wizard", method="POST",
                                         auth=f"{USER}:{PASSWORD}",
                                         body={"step": "preview", "plan": plan})
        assert status == 200 and preview["ok"] is True
        assert any("carrier tls -> wss" in c for c in preview["changes"])
        assert home.relay_cfg.read_text() == original     # a preview writes nothing

        status, applied = await _request(port, "/api/wizard", method="POST",
                                         auth=f"{USER}:{PASSWORD}",
                                         body={"step": "apply", "plan": plan})
        assert status == 200 and applied["ok"] is True
        assert applied["backup"].endswith(".bak-wizard")
        assert pathlib.Path(applied["backup"]).exists()
        changed = load_relay(home.relay_cfg)
        assert changed.exit.carrier == "wss" and changed.exit.port == 8443
        assert changed.exit.split == 2 and changed.exit.path == "/tunnel"
        assert any(m.listen == 2053 for m in changed.mappings)
        # the running node is reconfigured in place, no restart needed
        assert panel.node.cfg.exit.carrier == "wss"

        status, back = await _request(port, "/api/wizard", method="POST",
                                      auth=f"{USER}:{PASSWORD}",
                                      body={"step": "rollback", "backup": applied["backup"]})
        assert status == 200 and back["ok"] is True
        assert home.relay_cfg.read_text() == original
        assert load_relay(home.relay_cfg).exit.carrier == "tls"
    finally:
        await panel.stop()


async def test_the_wizard_refuses_a_plan_it_cannot_believe(home, tmp_path, relay_cfg):
    save_relay(relay_cfg, home.relay_cfg)
    panel, port = await _panel(home, tmp_path, node=FullNode(relay_cfg))
    auth = f"{USER}:{PASSWORD}"
    try:
        for bad, complaint in (
            ({"carrier": "carrier-pigeon"}, "carrier"),
            ({"port": 99999}, "port"),
            ({"port": "eight"}, "number"),
            ({"split": "many"}, "number"),
            ({"mapping": "abc:def"}, "port"),
            ({"carrier": ""}, "address"),
        ):
            status, data = await _request(port, "/api/wizard", method="POST",
                                          body={"step": "preview", "plan": bad}, auth=auth)
            assert status == 200, (bad, status)
            assert data["ok"] is False and complaint in data["error"], (bad, data)
        # a plan that is not an object at all is refused outright
        status, data = await _request(port, "/api/wizard", method="POST", auth=auth,
                                      body={"step": "preview", "plan": "not an object"})
        assert status == 400 and "object" in data["error"]
        # an unknown step is not applied either
        status, data = await _request(port, "/api/wizard", method="POST", auth=auth,
                                      body={"step": "explode", "plan": {"carrier": "wss"}})
        assert status == 400 and data["ok"] is False
        # and a refused apply leaves the config exactly as it was
        before = home.relay_cfg.read_text()
        status, data = await _request(port, "/api/wizard", method="POST", auth=auth,
                                      body={"step": "apply", "plan": {"port": 99999}})
        assert status == 400 and home.relay_cfg.read_text() == before
    finally:
        await panel.stop()


async def test_the_profile_can_be_chosen_from_the_panel(home, tmp_path, relay_cfg):
    save_relay(relay_cfg, home.relay_cfg)
    panel, port = await _panel(home, tmp_path, node=FullNode(relay_cfg))
    try:
        status, data = await _request(port, "/api/tune", method="POST",
                                      auth=f"{USER}:{PASSWORD}",
                                      body={"profile": "speed"})
        assert status == 200 and data["ok"] is True
        assert data["profile"] == "speed"
        assert data["changes"] and "restart" in data["note"]
        after = load_relay(home.relay_cfg)
        assert after.connections == 4 and after.stream_window == 524288
        assert panel.node.cfg is after or panel.node.cfg.connections == 4

        status, data = await _request(port, "/api/tune", method="POST",
                                      auth=f"{USER}:{PASSWORD}",
                                      body={"profile": "nonsense"})
        assert status == 400 and data["ok"] is False
        # the complaint names the profiles that do exist, like the CLI does
        for profile in ("gaming", "balanced", "speed"):
            assert profile in data["error"]
    finally:
        await panel.stop()


async def test_the_update_button_asks_github_once(home, tmp_path, relay_cfg, monkeypatch):
    from simurgh import update as updater

    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg))
    try:
        monkeypatch.setattr(updater, "check", lambda: {
            "current": "2.9.1", "latest": "3.0.0", "tag": "v3.0.0",
            "notes_url": "https://example.invalid/v3", "available": True})
        status, data = await _request(port, "/api/update", auth=f"{USER}:{PASSWORD}")
        assert status == 200 and data["ok"] is True
        assert data["update_available"] is True and data["latest"] == "3.0.0"
        assert data["notes_url"].endswith("/v3")

        def boom():
            raise updater.UpdateError("github is unreachable")

        monkeypatch.setattr(updater, "check", boom)
        status, data = await _request(port, "/api/update", auth=f"{USER}:{PASSWORD}")
        assert status == 502 and data["ok"] is False
        assert "unreachable" in data["error"]
    finally:
        await panel.stop()


async def test_the_panel_can_install_an_update_then_schedules_restart(
        home, tmp_path, relay_cfg, monkeypatch):
    from simurgh import update as updater

    calls, restarts = [], []
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg),
                               on_action=lambda name: restarts.append(name))
    try:
        def install(path, verbose=True):
            calls.append((path, verbose))
            return {"current": "2.9.1", "latest": "3.0.0", "tag": "v3.0.0",
                    "available": True, "action": "source install updated",
                    "steps": ["python", "go"], "restart_required": True}

        monkeypatch.setattr(updater, "update", install)
        status, data = await _request(port, "/api/update", method="POST",
                                      auth=f"{USER}:{PASSWORD}", body={})
        assert status == 200 and data["ok"] is True
        assert data["restarting"] is True and data["steps"] == ["python", "go"]
        assert calls == [(home.path, False)]
        assert panel._restart_timer is not None
        assert restarts == []                 # the HTTP response comes first

        # Fire the callback deterministically instead of making the test sleep.
        panel._restart_timer.cancel()
        panel._restart_timer = None
        panel._restart_after_update()
        assert restarts == ["restart"]

        def boom(path, verbose=True):
            raise updater.UpdateError("download failed; install unchanged")

        monkeypatch.setattr(updater, "update", boom)
        status, data = await _request(port, "/api/update", method="POST",
                                      auth=f"{USER}:{PASSWORD}", body={})
        assert status == 502 and data["ok"] is False
        assert "unchanged" in data["error"]
    finally:
        await panel.stop()


def test_every_element_the_script_touches_exists():
    """A `$('typo')` is a dead button: check the shell against the script."""
    import re

    html = panel_ui.read_asset("index.html")[0].decode()
    script = panel_ui.read_asset("app.js")[0].decode()
    known = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', html))
    # the wizard markup and the settings fields are built by the script itself
    known |= set(re.findall(r'id="([a-zA-Z0-9_-]+)"', script))
    known |= {f"cfg_{key}" for key in ("name", "log_level", "engine", "panel_port",
                                       "keepalive", "stream_window", "max_stream_window",
                                       "chunk", "connections", "speedtest_port")}
    touched = set(re.findall(r"\$\('([a-zA-Z0-9_-]+)'\)", script))
    touched |= set(re.findall(r"getElementById\('([a-zA-Z0-9_-]+)'\)", script))
    assert touched, "no element lookups found: did the script change shape?"
    missing = sorted(touched - known)
    assert not missing, missing


def test_every_endpoint_the_script_calls_is_a_route_the_panel_answers():
    """The panel and its page are one program in two files: keep them in step."""
    import re

    script = panel_ui.read_asset("app.js")[0].decode()
    from simurgh import panel as panel_module

    source = pathlib.Path(panel_module.__file__).read_text(encoding="utf-8")
    calls = sorted(set(re.findall(r"api\('([^']+)'", script)))
    assert calls, "no API calls found in the panel's script"
    for call in calls:
        path = call.partition("?")[0]
        assert path.startswith("/api/"), call
        assert f'"{path}"' in source, f"{path} is not routed by the panel"


def test_the_panel_never_loads_anything_from_the_internet():
    """No CDN, no external font, no eval -- the CSP is not the only guard."""
    import re

    for name in ("index.html", "app.js", "app.css", "login.js"):
        text = panel_ui.read_asset(name)[0].decode()
        for word in ("http://", "https://", "//cdn.", "@import"):
            assert word not in text, (name, word)
        assert "eval(" not in text, name
        assert not re.search(r"<script[^>]+src=\"(?!\/)", text), name


# ---------------------------------------------------------------- pairing
def _unpaired_cfg() -> RelayConfig:
    """What `simurgh install --pair-later` leaves on disk."""
    cfg = RelayConfig(name="ir-tehran", unpaired=True, panel_port=8787)
    cfg.exit = ExitEndpoint(carrier="tls", address="", port=443)
    return cfg


PAIR_PAYLOAD = {
    "token": "s3cret-token", "name": "de-frankfurt",
    "exit": {"carrier": "tls", "address": "198.51.100.7", "port": 443},
    "mappings": [{"port": 443, "target": 443}],
}


async def test_an_unpaired_relay_says_so(home, tmp_path):
    """The panel must not pretend a server that waits for its exit is up."""
    cfg = _unpaired_cfg()
    save_relay(cfg, home.relay_cfg)
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(cfg))
    try:
        status, data = await _request(port, "/api/status", auth=f"{USER}:{PASSWORD}")
        assert status == 200
        assert data["panel"]["paired"] is False
        assert data["panel"]["pairing_required"] is True
        status, layout = await _request(port, "/api/layout", auth=f"{USER}:{PASSWORD}")
        assert status == 200
        assert layout["panel"]["pairing_required"] is True
    finally:
        await panel.stop()


async def test_pairing_from_the_panel_keeps_what_the_install_chose(
        home, tmp_path, monkeypatch):
    """The link brings the secret and the exit -- nothing else is replaced."""
    from simurgh import links

    cfg = _unpaired_cfg()
    cfg.engine = "python"
    cfg.mappings = [Mapping(name="mine", listen=8443, target_port=9000)]
    save_relay(cfg, home.relay_cfg)
    monkeypatch.setattr(links, "fetch_setup",
                        lambda link, timeout=15.0: PAIR_PAYLOAD)
    node = FakeRelayNode(cfg)
    panel, port = await _panel(home, tmp_path, node=node)
    try:
        status, data = await _request(
            port, "/api/pair", method="POST",
            body={"link": "simurgh://198.51.100.7:8787/setup?u=a&p=c2VjcmV0"},
            auth=f"{USER}:{PASSWORD}")
        assert status == 200 and data["ok"] is True
        assert data["applied"] == "live"
        saved = load_relay(home.relay_cfg)
        assert saved.token == "s3cret-token" and saved.unpaired is False
        assert saved.paired is True
        assert saved.exit.address == "198.51.100.7" and saved.exit.port == 443
        # the port this install added stays, the exit's port is appended
        assert [m.listen for m in saved.mappings] == [8443, 443]
        assert saved.panel_port == 8787 and saved.engine == "python"
        assert node.rebound == 1 and node.reconnected == 1
        assert data["backup"].endswith(".bak-pair")
        assert pathlib.Path(data["backup"]).is_file()
        assert data["exit"].startswith("tls://198.51.100.7")
    finally:
        await panel.stop()


async def test_pairing_replaces_an_exit_only_after_confirmation(home, tmp_path, monkeypatch):
    from simurgh import links

    cfg = RelayConfig(token="tok", name="ir",
                      exit=ExitEndpoint(carrier="tls", address="203.0.113.9",
                                        port=443))
    save_relay(cfg, home.relay_cfg)
    monkeypatch.setattr(links, "fetch_setup",
                        lambda link, timeout=15.0: PAIR_PAYLOAD)
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(cfg))
    try:
        status, data = await _request(
            port, "/api/pair", method="POST",
            body={"link": "simurgh://x:8787/setup?u=a&p=b"},
            auth=f"{USER}:{PASSWORD}")
        assert status == 409 and data["ok"] is False
        assert "already paired" in data["error"]
        assert load_relay(home.relay_cfg).exit.address == "203.0.113.9"

        status, data = await _request(
            port, "/api/pair", method="POST",
            body={"link": "simurgh://x:8787/setup?u=a&p=b", "replace": True},
            auth=f"{USER}:{PASSWORD}")
        assert status == 200 and data["ok"] is True
        replaced = load_relay(home.relay_cfg)
        assert replaced.token == "s3cret-token"
        assert replaced.exit.address == "198.51.100.7"
        assert pathlib.Path(data["backup"]).is_file()
    finally:
        await panel.stop()


async def test_pairing_rejects_a_link_for_the_exit(home, tmp_path, monkeypatch):
    """The exit's own link would make the relay dial itself: say no plainly."""
    from simurgh import links

    cfg = _unpaired_cfg()
    save_relay(cfg, home.relay_cfg)

    def refuse(link, timeout=15.0):
        raise links.LinkError("that link is for the exit server, not for the relay")

    monkeypatch.setattr(links, "fetch_setup", refuse)
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(cfg))
    try:
        status, data = await _request(
            port, "/api/pair", method="POST",
            body={"link": "simurgh://x:8787/setup?u=a&p=b"},
            auth=f"{USER}:{PASSWORD}")
        assert status == 400 and data["ok"] is False
        assert "exit server" in data["error"]
        assert load_relay(home.relay_cfg).unpaired is True
    finally:
        await panel.stop()


async def test_pairing_needs_a_link_and_a_relay(home, tmp_path):
    cfg = _unpaired_cfg()
    save_relay(cfg, home.relay_cfg)
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(cfg))
    try:
        status, data = await _request(port, "/api/pair", method="POST",
                                      body={}, auth=f"{USER}:{PASSWORD}")
        assert status == 400 and data["ok"] is False
    finally:
        await panel.stop()
    exit_cfg = ExitConfig(token="tok", name="de")
    exit_cfg.listen = [ListenSpec(carrier="tls", port=443)]
    save_exit(exit_cfg, home.exit_cfg)
    panel, port = await _panel(home, tmp_path, role="exit", node=FakeRelayNode(cfg))
    try:
        status, data = await _request(port, "/api/pair", method="POST",
                                      body={"link": "simurgh://x/setup"},
                                      auth=f"{USER}:{PASSWORD}")
        assert status == 400 and data["ok"] is False
        assert "credentials" in data["error"]
    finally:
        await panel.stop()


async def test_a_reverse_relay_is_not_paired_this_way(home, tmp_path):
    """In reverse mode the exit imports *our* link: nothing to paste here."""
    cfg = RelayConfig(token="tok", name="ir", unpaired=True, dial="exit")
    save_relay(cfg, home.relay_cfg)
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(cfg))
    try:
        status, data = await _request(port, "/api/pair", method="POST",
                                      body={"link": "simurgh://x/setup?u=a&p=b"},
                                      auth=f"{USER}:{PASSWORD}")
        assert status == 400 and data["ok"] is False
        assert "foreign exit's Pair page" in data["error"]
    finally:
        await panel.stop()


async def test_setup_link_is_created_in_the_panel_with_a_public_address(home, tmp_path):
    from simurgh.links import parse_join_link

    cfg = ExitConfig(token="tok", name="de-frankfurt")
    cfg.listen = [ListenSpec(carrier="tls", port=443)]
    save_exit(cfg, home.exit_cfg)
    panel, port = await _panel(home, tmp_path, role="exit")
    try:
        status, _ = await _request(port, "/api/setup-link", method="POST",
                                   body={"host": "203.0.113.9:9999"})
        assert status == 401
        status, data = await _request(
            port, "/api/setup-link", method="POST",
            body={"host": "203.0.113.9:9999"}, auth=f"{USER}:{PASSWORD}")
        assert status == 200 and data["ok"] is True
        assert data["target"] == "relay"
        parsed = parse_join_link(data["url"])
        assert (parsed["host"], parsed["port"]) == ("203.0.113.9", 9999)
        assert (parsed["user"], parsed["password"]) == (USER, PASSWORD)
        assert parsed["name"] == "de-frankfurt"

        status, data = await _request(
            port, "/api/setup-link", method="POST",
            body={"host": "[2001:db8::1]:8787"}, auth=f"{USER}:{PASSWORD}")
        assert status == 200 and parse_join_link(data["url"])["host"] == "2001:db8::1"
        for invalid in ("https://example.com", "bad host", "host:70000", "user@host"):
            status, data = await _request(port, "/api/setup-link", method="POST",
                                          body={"host": invalid}, auth=f"{USER}:{PASSWORD}")
            assert status == 400 and data["ok"] is False, invalid
    finally:
        await panel.stop()


async def test_only_an_exit_or_reverse_relay_offers_a_setup_link(home, tmp_path):
    direct = _unpaired_cfg()
    save_relay(direct, home.relay_cfg)
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(direct))
    try:
        status, data = await _request(port, "/api/setup-link", method="POST",
                                      body={}, auth=f"{USER}:{PASSWORD}")
        assert status == 400 and "imports a setup link" in data["error"]
    finally:
        await panel.stop()

    reverse = RelayConfig(token="tok", name="ir", dial="exit")
    save_relay(reverse, home.relay_cfg)
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(reverse))
    try:
        status, data = await _request(port, "/api/setup-link", method="POST",
                                      body={"host": "198.51.100.8"},
                                      auth=f"{USER}:{PASSWORD}")
        assert status == 200 and data["target"] == "exit"
        status, layout = await _request(port, "/api/layout", auth=f"{USER}:{PASSWORD}")
        assert layout["panel"]["setup_link"] is True
        assert layout["panel"]["reverse"] is True
    finally:
        await panel.stop()


async def test_reverse_exit_pairing_from_the_panel_preserves_local_settings(
        home, tmp_path, monkeypatch):
    from simurgh import links

    cfg = ExitConfig(token="old", name="de", engine="python", speedtest_port=9911)
    cfg.listen = [ListenSpec(carrier="tls", port=443)]
    save_exit(cfg, home.exit_cfg)
    payload = {
        "ok": True, "role": "exit", "token": "reverse-secret", "name": "ir",
        "listen": {"carrier": "wss", "address": "198.51.100.8", "port": 9443,
                   "path": "/tunnel", "fingerprint": "ab:cd"},
    }
    monkeypatch.setattr(links, "fetch_setup", lambda link, timeout=15.0: payload)
    panel, port = await _panel(home, tmp_path, role="exit")
    try:
        status, data = await _request(
            port, "/api/pair", method="POST",
            body={"link": "simurgh://198.51.100.8:8787/setup?u=a&p=c2VjcmV0"},
            auth=f"{USER}:{PASSWORD}")
        assert status == 200 and data["ok"] is True
        assert data["applied"] == "restart"
        assert pathlib.Path(data["backup"]).is_file()
        saved = load_exit(home.exit_cfg)
        assert saved.token == "reverse-secret"
        assert saved.name == "de" and saved.engine == "python"
        assert saved.speedtest_port == 9911 and saved.cert_auto is False
        assert len(saved.listen) == 1
        assert saved.listen[0].dial == "198.51.100.8"
        assert saved.listen[0].port == 9443 and saved.listen[0].carrier == "wss"
        assert saved.listen[0].fingerprint == "ab:cd"

        monkeypatch.setattr(links, "fetch_setup", lambda link, timeout=15.0: PAIR_PAYLOAD)
        status, data = await _request(
            port, "/api/pair", method="POST",
            body={"link": "simurgh://x:8787/setup?u=a&p=c2VjcmV0"},
            auth=f"{USER}:{PASSWORD}")
        assert status == 400 and "direct relay" in data["error"]
        assert load_exit(home.exit_cfg).token == "reverse-secret"

        missing_pin = {**payload, "listen": {**payload["listen"], "fingerprint": None}}
        monkeypatch.setattr(links, "fetch_setup", lambda link, timeout=15.0: missing_pin)
        status, data = await _request(
            port, "/api/pair", method="POST",
            body={"link": "simurgh://x:8787/setup?u=a&p=c2VjcmV0"},
            auth=f"{USER}:{PASSWORD}")
        assert status == 400 and "fingerprint" in data["error"]
        assert load_exit(home.exit_cfg).token == "reverse-secret"
    finally:
        await panel.stop()


async def test_every_settings_field_is_validated_and_persisted(home, tmp_path):
    cfg = _unpaired_cfg()
    cfg.engine = "python"
    save_relay(cfg, home.relay_cfg)
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(cfg))
    body = {
        "name": "ir-new", "log_level": "debug", "engine": "python",
        "dial": "exit", "tunnel_carrier": "wss", "tunnel_port": 9443,
        "tunnel_path": "/socket", "panel_port": 9999, "keepalive": 0,
        "stream_window": 524288, "max_stream_window": 4194304,
        "chunk": 32768, "connections": 4, "rate_limit": "20M",
        "quota": "100G", "quota_period": "day", "quota_action": "throttle",
        "max_conns": 250,
    }
    try:
        status, data = await _request(port, "/api/config", method="POST", body=body,
                                      auth=f"{USER}:{PASSWORD}")
        assert status == 200 and data["ok"] is True, data
        saved = load_relay(home.relay_cfg)
        assert saved.name == "ir-new" and saved.log_level == "debug"
        assert saved.dial == "exit" and saved.token and saved.unpaired is False
        assert saved.tunnel.carrier == "wss" and saved.tunnel.port == 9443
        assert saved.tunnel.path == "/socket"
        assert saved.panel_port == 9999 and saved.keepalive == 0
        assert saved.stream_window == 524288 and saved.max_stream_window == 4194304
        assert saved.chunk == 32768 and saved.connections == 4
        assert saved.rate_limit == "20M" and saved.quota == "100G"
        assert saved.quota_period == "day" and saved.quota_action == "throttle"
        assert saved.max_conns == 250

        status, data = await _request(port, "/api/config", method="POST",
                                      body={"log_level": "verbose"},
                                      auth=f"{USER}:{PASSWORD}")
        assert status == 400 and "log_level" in data["error"]
        status, data = await _request(port, "/api/config", method="POST",
                                      body={"max_stream_window": 16384},
                                      auth=f"{USER}:{PASSWORD}")
        assert status == 400 and "max_stream_window" in data["error"]
        assert load_relay(home.relay_cfg).log_level == "debug"
    finally:
        await panel.stop()


async def test_exit_settings_include_speedtest_and_limits(home, tmp_path):
    cfg = ExitConfig(token="tok", name="de", engine="python")
    cfg.listen = [ListenSpec(carrier="tls", port=443)]
    save_exit(cfg, home.exit_cfg)
    panel, port = await _panel(home, tmp_path, role="exit")
    try:
        status, data = await _request(
            port, "/api/config", method="POST",
            body={"speedtest_port": 9909, "stream_window": 524288,
                  "max_stream_window": 4194304, "chunk": 32768,
                  "rate_limit": "10M", "quota": "50G", "quota_period": "total",
                  "quota_action": "warn", "max_conns": 100},
            auth=f"{USER}:{PASSWORD}")
        assert status == 200 and data["ok"] is True, data
        saved = load_exit(home.exit_cfg)
        assert saved.speedtest_port == 9909 and saved.stream_window == 524288
        assert saved.max_stream_window == 4194304 and saved.chunk == 32768
        assert saved.rate_limit == "10M" and saved.quota == "50G"
        assert saved.quota_period == "total" and saved.quota_action == "warn"
        assert saved.max_conns == 100

        status, data = await _request(port, "/api/config", method="POST",
                                      body={"speedtest_port": 0},
                                      auth=f"{USER}:{PASSWORD}")
        assert status == 200 and load_exit(home.exit_cfg).speedtest_port == 0
        status, data = await _request(port, "/api/config", method="POST",
                                      body={"speedtest_port": -1},
                                      auth=f"{USER}:{PASSWORD}")
        assert status == 400 and "speedtest_port" in data["error"]
    finally:
        await panel.stop()


async def test_panel_port_zero_adopts_the_kernel_assigned_port(home, tmp_path):
    cfg = _unpaired_cfg()
    save_relay(cfg, home.relay_cfg)
    panel = Panel(home, "relay", node=FakeRelayNode(cfg), host="127.0.0.1",
                  port=0, user=USER, password=PASSWORD,
                  state_path=tmp_path / "state.json")
    await panel.start()
    try:
        assert panel.port > 0
        status, data = await _request(panel.port, "/healthz")
        assert status == 200 and data["ok"] is True
        assert panel.status()["panel"]["port"] == panel.port
    finally:
        await panel.stop()


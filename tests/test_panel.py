"""The web panel: authentication, status, mappings and the join payload."""

from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

from conftest import free_port
from simurgh.config import (ExitConfig, ExitEndpoint, ListenSpec, Mapping,
                            RelayConfig, load_relay, save_exit, save_relay)
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
        assert data["version"].startswith("2.")
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
        assert 'toggleLang' in text          # English / Persian switch
        assert "<canvas" in text            # live speed chart
        assert "/api/status" in text        # it polls the API
    finally:
        await panel.stop()


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
        # right key: page + cookie
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET /?k={PASSWORD} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
                     .encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), 10)
        writer.close()
        assert b"200 OK" in raw
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
    import json
    import re

    from simurgh import panel as panel_module

    html = panel_module.DASHBOARD_HTML
    # the template itself is language neutral, the strings live in the i18n table
    assert not re.search(r"[\u0600-\u06FF]", re.sub(r"فا", "", html.split("<script>")[0]))
    langs = panel_module.PANEL_I18N
    assert set(langs) == {"en", "fa"}
    assert set(langs["en"]) == set(langs["fa"])
    # every key used by the HTML/JS exists in both languages
    used = set(re.findall(r'data-i18n="([a-z_0-9]+)"', html))
    used |= set(re.findall(r"(?<![A-Za-z0-9_$.])t\('([a-z_0-9]+)'", html))
    assert used, "no i18n keys found in the dashboard"
    assert used <= set(langs["en"]), sorted(used - set(langs["en"]))
    # placeholders are substituted with both languages
    rendered = html.replace("__I18N__", json.dumps(langs, ensure_ascii=False))
    assert "__I18N__" not in rendered
    assert "سرعت لحظه‌ای" in rendered and "Live speed" in rendered
    assert "toggleLang" in rendered and "localStorage" in rendered


async def test_dashboard_served_over_http_carries_both_languages(home, tmp_path, relay_cfg):
    save_relay(relay_cfg, home.relay_cfg)
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg))
    try:
        status, raw = await _request(port, "/", auth=f"{USER}:{PASSWORD}")
        assert status == 200
        assert b'data-i18n="card_tunnel"' in raw
        assert "وضعیت تونل".encode() in raw and b"Tunnel status" in raw
        assert b"toggleLang" in raw
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

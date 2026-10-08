"""The web panel: authentication, status, mappings and the join payload."""

from __future__ import annotations

import asyncio
import base64
import json

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
        assert 'dir="rtl"' in text          # Persian-first layout
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

    async def fake_speedtest(node, seconds=6, port=8808):
        return {"seconds": seconds, "download_mbps": 42.0, "upload_mbps": 21.0,
                "download_bytes": 1000, "upload_bytes": 500}

    monkeypatch.setattr(speedtest_module, "run_speedtest", fake_speedtest)
    panel, port = await _panel(home, tmp_path, node=FakeRelayNode(relay_cfg))
    try:
        status, data = await _request(port, "/api/action", method="POST",
                                      auth=f"{USER}:{PASSWORD}",
                                      body={"name": "speedtest", "seconds": 2})
        assert status == 200
        assert data["speedtest"]["download_mbps"] == 42.0
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

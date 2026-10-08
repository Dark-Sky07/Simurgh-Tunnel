"""Reverse mode (``dial = "exit"``): the foreign exit connects to the relay.

The direct mode (relay dials the exit) is covered by ``test_e2e.py``.  Here the
roles of *who listens* are swapped: the Iranian relay accepts the tunnel and
the exit keeps the connection alive, which is what you want when the foreign
server has no reachable inbound port (NAT, blocked ports, changing IP...).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from conftest import free_port
from simurgh.config import (ExitConfig, ListenSpec, Mapping,
                            RelayConfig, TunnelSpec)
from simurgh.exit import ExitNode
from simurgh.links import (LinkError, exit_config_from_payload,
                           relay_config_from_payload, reverse_payload)
from simurgh.relay import RelayNode

pytestmark = pytest.mark.asyncio

CARRIERS = ("raw", "tls", "plain")
TOKEN = "reverse-token"


async def _http_server() -> tuple[asyncio.AbstractServer, int]:
    async def handler(reader, writer):
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except Exception:
            writer.close()
            return
        body = b"<h1>foreign panel via reverse tunnel</h1>"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                     b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body))
        writer.write(body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _reverse_stack(carrier: str, cert, target_port: int, *,
                         fingerprint: str | None = None):
    """Relay listens for the exit; exit dials the relay."""
    tunnel_port = free_port()
    user_port = free_port()

    relay_cfg = RelayConfig(
        token=TOKEN, name="ir-relay", dial="exit",
        tunnel=TunnelSpec(carrier=carrier, host="127.0.0.1", port=tunnel_port,
                          cert_file=cert[0], key_file=cert[1], cert_auto=False),
        mappings=[Mapping(name="panel", listen=user_port, target_port=target_port)],
    )
    relay = RelayNode(relay_cfg)
    await relay.start()

    exit_cfg = ExitConfig(
        token=TOKEN, name="omega-de",
        listen=[ListenSpec(carrier=carrier, port=tunnel_port, dial="127.0.0.1",
                           fingerprint=fingerprint, insecure_skip_verify=True)],
    )
    node = ExitNode(exit_cfg)
    await node.start()

    for _ in range(120):
        if relay.connected.is_set():
            break
        await asyncio.sleep(0.05)
    assert relay.connected.is_set(), f"{carrier}: the exit did not reach the relay"
    return relay, node, user_port


@pytest.mark.parametrize("carrier", CARRIERS)
async def test_reverse_mode_carries_user_traffic(carrier, certs):
    web, target_port = await _http_server()
    relay, node, user_port = await _reverse_stack(carrier, certs, target_port)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", user_port)
        writer.write(b"GET / HTTP/1.1\r\nHost: panel\r\n\r\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), 10)
        writer.close()
        assert b"200 OK" in raw
        assert b"foreign panel via reverse tunnel" in raw
    finally:
        await node.stop()
        await relay.stop()
        web.close()


async def test_the_exit_reconnects_after_the_relay_restarts(certs):
    """systemctl restart simurgh-relay must not need manual action."""
    web, target_port = await _http_server()
    relay, node, user_port = await _reverse_stack("tls", certs, target_port)
    tunnel_port = relay.cfg.tunnel.port
    await relay.stop()
    assert not relay.connected.is_set()
    try:
        relay2 = RelayNode(relay.cfg)
        await relay2.start()
        for _ in range(160):
            if relay2.connected.is_set():
                break
            await asyncio.sleep(0.05)
        assert relay2.connected.is_set(), "the exit did not come back"
        reader, writer = await asyncio.open_connection("127.0.0.1", user_port)
        writer.write(b"GET / HTTP/1.1\r\nHost: panel\r\n\r\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), 10)
        writer.close()
        assert b"foreign panel via reverse tunnel" in raw
        await relay2.stop()
    finally:
        await node.stop()
        web.close()
    assert tunnel_port > 0


async def test_a_second_exit_replaces_the_first(certs):
    """A restarting exit (old connection lingering) must not block the new one."""
    web, target_port = await _http_server()
    relay, node, _ = await _reverse_stack("plain", certs, target_port)
    try:
        extra = ExitNode(ExitConfig(
            token=TOKEN,
            listen=[ListenSpec(carrier="plain", port=relay.cfg.tunnel.port,
                               dial="127.0.0.1", insecure_skip_verify=True)],
        ))
        await extra.start()
        for _ in range(160):
            if relay.connect_count >= 2 and relay.connected.is_set():
                break
            await asyncio.sleep(0.05)
        assert relay.connected.is_set(), "the replacement exit was not accepted"
        assert relay.connect_count >= 2
        await extra.stop()
    finally:
        await node.stop()
        await relay.stop()
        web.close()


async def test_wrong_token_is_refused_in_reverse_mode(certs):
    """A prober without the token gets nothing, the exit keeps retrying."""
    tunnel_port = free_port()
    relay = RelayNode(RelayConfig(
        token=TOKEN, dial="exit", panel_port=0,
        tunnel=TunnelSpec(carrier="plain", host="127.0.0.1", port=tunnel_port,
                          cert_file=certs[0], key_file=certs[1], cert_auto=False),
    ))
    await relay.start()
    intruder = ExitNode(ExitConfig(
        token="not-the-token",
        listen=[ListenSpec(carrier="plain", port=tunnel_port, dial="127.0.0.1",
                           insecure_skip_verify=True)],
    ))
    try:
        await intruder.start()
        await asyncio.sleep(0.6)
        assert not relay.connected.is_set()
    finally:
        await intruder.stop()
        await relay.stop()


async def test_reverse_link_round_trip(certs):
    """The relay's payload tells the exit exactly where to dial."""
    relay_cfg = RelayConfig(
        token=TOKEN, name="ir-relay", dial="exit",
        tunnel=TunnelSpec(carrier="tls", host="0.0.0.0", port=8443,
                          cert_file=certs[0], key_file=certs[1]),
    )
    payload = reverse_payload(relay_cfg, "203.0.113.9", "AA:BB")
    assert payload["role"] == "exit"
    assert payload["listen"]["address"] == "203.0.113.9"
    exit_cfg = exit_config_from_payload(payload)
    assert exit_cfg.token == TOKEN
    spec = exit_cfg.listen[0]
    assert spec.reverse and spec.dial == "203.0.113.9"
    assert spec.port == 8443 and spec.carrier == "tls"
    assert spec.fingerprint == "AA:BB"


async def test_direct_payload_still_builds_a_relay(certs):
    """The old (direct) payload shape keeps working."""
    payload = {
        "token": TOKEN,
        "name": "omega-de",
        "exit": {"carrier": "tls", "address": "198.51.100.7", "port": 443,
                 "path": "/ws", "fingerprint": "AB:CD"},
        "push_ports": [443, 8443],
    }
    cfg = relay_config_from_payload(payload)
    assert cfg.dial == "relay" and not cfg.reverse
    assert cfg.exit.address == "198.51.100.7"
    assert [m.listen for m in cfg.mappings] == [443, 8443]
    assert cfg.exit.fingerprint == "AB:CD"


async def test_reverse_payload_without_an_address_is_rejected():
    with pytest.raises((ValueError, LinkError)):
        exit_config_from_payload({"token": TOKEN, "listen": {}})


def test_reverse_relay_config_round_trips(home):
    """relay.toml keeps ``dial``/``[tunnel]`` through save + load."""
    from simurgh.config import load_relay, save_relay

    cfg = RelayConfig(token=TOKEN, name="ir", dial="exit",
                      tunnel=TunnelSpec(carrier="wss", host="0.0.0.0", port=9443,
                                        path="/ws", fallback="decoy"))
    save_relay(cfg, home.relay_cfg)
    again = load_relay(home.relay_cfg)
    assert again.reverse
    assert again.tunnel.port == 9443 and again.tunnel.carrier == "wss"
    assert again.endpoints() == []            # nothing to dial out to


def test_reverse_exit_config_round_trips(home):
    from simurgh.config import load_exit, save_exit

    cfg = ExitConfig(token=TOKEN, name="omega",
                     listen=[ListenSpec(carrier="tls", port=9443,
                                        dial="203.0.113.9", fingerprint="AB:CD")])
    save_exit(cfg, home.exit_cfg)
    again = load_exit(home.exit_cfg)
    assert again.listen[0].reverse
    assert again.listen[0].dial == "203.0.113.9"
    assert again.listen[0].fingerprint == "AB:CD"


def test_reverse_relay_still_needs_a_token(home):
    """A reverse relay config is rejected when the token is missing."""
    from simurgh.config import ConfigError, load_relay, save_relay

    save_relay(RelayConfig(token="", dial="exit",
                           tunnel=TunnelSpec(carrier="plain", port=9443)),
               home.relay_cfg)
    with pytest.raises(ConfigError):
        load_relay(home.relay_cfg)


def test_panel_join_returns_the_reverse_payload(home, tmp_path):
    """``/api/join`` on a reverse relay hands the exit its dial address."""
    from pathlib import Path as _Path

    from simurgh.config import save_relay
    from simurgh.panel import Panel
    from simurgh.util import Home as _Home

    other = _Home(tmp_path / "relay-home")
    other.ensure()
    cfg = RelayConfig(token=TOKEN, name="ir-relay", dial="exit", panel_port=0,
                      tunnel=TunnelSpec(carrier="tls", port=9443))
    save_relay(cfg, other.relay_cfg)
    panel = Panel(other, "relay", node=None, user="admin", password="pw")
    raw = panel._join_payload_reverse(_FakeReq())
    payload = json.loads(raw.split(b"\r\n\r\n", 1)[1].decode())
    assert payload["ok"] and payload["role"] == "exit"
    assert payload["listen"]["port"] == 9443
    assert payload["token"] == TOKEN
    assert _Path(other.relay_cfg).exists()


class _FakeReq:
    method = "POST"
    path = "/api/join"
    query: dict = {}
    headers: dict = {"host": "203.0.113.9:8787"}

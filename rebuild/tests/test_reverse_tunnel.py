from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rebuild import reverse, tunnel


@pytest.fixture
def tls_identity(tmp_path):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("OpenSSL is required only to create the test certificate")
    cert = tmp_path / "hub.crt"
    key = tmp_path / "hub.key"
    subprocess.run([
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1",
        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
    ], check=True, capture_output=True)
    return cert, key


def run(coro):
    return asyncio.run(coro)


def test_reverse_agent_forwards_concurrent_tcp_clients_to_its_fixed_target(tls_identity):
    cert, key = tls_identity
    secret = "reverse-mode-shared-secret-0123456789"

    async def scenario():
        async def echo(reader, writer):
            data = await reader.read()
            writer.write(data[::-1])
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target = await asyncio.start_server(echo, "127.0.0.1", 0)
        hub_config = tunnel.ReverseHubConfig(
            "127.0.0.1", 0, "127.0.0.1", 0, cert, key, secret, "echo",
        )
        control, public, state = await reverse._start_reverse_hub(hub_config)
        hub_port = control.sockets[0].getsockname()[1]
        public_port = public.sockets[0].getsockname()[1]
        agent_config = tunnel.ReverseAgentConfig(
            "127.0.0.1", hub_port, "localhost", cert, secret, "echo",
            "127.0.0.1", target.sockets[0].getsockname()[1],
        )
        agent = asyncio.create_task(reverse.serve_reverse_agent(agent_config))
        try:
            await asyncio.wait_for(state.agent_ready.wait(), timeout=3)

            async def exchange(payload: bytes) -> bytes:
                reader, writer = await asyncio.open_connection("127.0.0.1", public_port)
                writer.write(payload)
                await writer.drain()
                writer.write_eof()
                result = await asyncio.wait_for(reader.read(), timeout=5)
                writer.close()
                await writer.wait_closed()
                return result

            results = await asyncio.gather(
                exchange(b"reverse-one"), exchange(b"reverse-two"),
                exchange(b"reverse-three"),
            )
            assert results == [b"eno-esrever", b"owt-esrever", b"eerht-esrever"]
            assert not state.pending
        finally:
            agent.cancel()
            await asyncio.gather(agent, return_exceptions=True)
            control.close()
            public.close()
            target.close()
            await asyncio.gather(control.wait_closed(), public.wait_closed(), target.wait_closed())

    run(scenario())


def test_reverse_public_listener_closes_when_no_agent_is_registered(tls_identity, monkeypatch):
    cert, key = tls_identity
    monkeypatch.setattr(reverse, "CONNECT_TIMEOUT", 0.05)
    config = tunnel.ReverseHubConfig(
        "127.0.0.1", 0, "127.0.0.1", 0, cert, key,
        "reverse-hub-shared-secret-0123456789", "web",
    )

    async def scenario():
        control, public, _state = await reverse._start_reverse_hub(config)
        port = public.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            assert await asyncio.wait_for(reader.read(), timeout=1) == b""
            writer.close()
            await writer.wait_closed()
        finally:
            control.close()
            public.close()
            await asyncio.gather(control.wait_closed(), public.wait_closed())

    run(scenario())


def test_reverse_hub_shutdown_closes_pending_public_connections(tls_identity, monkeypatch):
    cert, key = tls_identity
    config = tunnel.ReverseHubConfig(
        "127.0.0.1", 0, "127.0.0.1", 0, cert, key,
        "reverse-hub-shared-secret-0123456789", "web",
    )
    start_hub = reverse._start_reverse_hub
    state_holder = {}
    started = asyncio.Event()

    async def tracked_start(hub_config):
        servers = await start_hub(hub_config)
        state_holder["state"] = servers[2]
        state_holder["public"] = servers[1]
        started.set()
        return servers

    monkeypatch.setattr(reverse, "_start_reverse_hub", tracked_start)

    async def scenario():
        hub = asyncio.create_task(reverse.serve_reverse_hub(config))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            port = state_holder["public"].sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            for _ in range(100):
                if state_holder["state"].handler_tasks:
                    break
                await asyncio.sleep(0.01)
            assert state_holder["state"].handler_tasks
            hub.cancel()
            await asyncio.gather(hub, return_exceptions=True)
            assert await asyncio.wait_for(reader.read(), timeout=1) == b""
            assert not state_holder["state"].pending
            writer.close()
            await writer.wait_closed()
        finally:
            if not hub.done():
                hub.cancel()
                await asyncio.gather(hub, return_exceptions=True)

    run(scenario())


def test_reverse_hub_rejects_an_agent_with_a_wrong_secret(tls_identity):
    cert, key = tls_identity
    config = tunnel.ReverseHubConfig(
        "127.0.0.1", 0, "127.0.0.1", 0, cert, key,
        "correct-secret-012345678901234567890", "web",
    )

    async def scenario():
        control, public, state = await reverse._start_reverse_hub(config)
        port = control.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection(
            "localhost", port, ssl=tunnel._client_ssl(tunnel.ReverseAgentConfig(
                "localhost", port, "localhost", cert,
                "wrong-secret-01234567890123456789", "web", "127.0.0.1", 8080,
            )), server_hostname="localhost", limit=tunnel.MAX_HANDSHAKE,
        )
        try:
            await tunnel._send_json(writer, {
                "version": 1, "op": "register", "route": "web",
                "secret": "wrong-secret-01234567890123456789",
            })
            reply = await asyncio.wait_for(tunnel._read_json(reader), timeout=2)
            assert reply == {"ok": False, "error": "denied"}
            assert not state.agent_ready.is_set()
        finally:
            writer.close()
            control.close()
            public.close()
            await asyncio.gather(writer.wait_closed(), control.wait_closed(), public.wait_closed())

    run(scenario())


def test_reverse_configs_load_and_reject_invalid_ports(tmp_path):
    hub = {
        "role": "reverse-hub", "control_host": "0.0.0.0", "control_port": 9443,
        "public_host": "127.0.0.1", "public_port": 8080,
        "cert_file": "/tmp/hub.crt", "key_file": "/tmp/hub.key",
        "secret": "reverse-hub-secret-0123456789012345", "route": "web",
    }
    agent = {
        "role": "reverse-agent", "hub_host": "hub.example.net", "hub_port": 9443,
        "server_name": "hub.example.net", "ca_file": "/tmp/ca.crt",
        "secret": "reverse-agent-secret-0123456789012345", "route": "web",
        "target_host": "127.0.0.1", "target_port": 8080,
    }
    hub_path = tmp_path / "reverse-hub.json"
    agent_path = tmp_path / "reverse-agent.json"
    hub_path.write_text(json.dumps(hub))
    agent_path.write_text(json.dumps(agent))
    assert isinstance(tunnel.load_config(hub_path), tunnel.ReverseHubConfig)
    assert isinstance(tunnel.load_config(agent_path), tunnel.ReverseAgentConfig)
    hub["control_port"] = 0
    hub_path.write_text(json.dumps(hub))
    with pytest.raises(tunnel.ConfigError, match="control_port"):
        tunnel.load_config(hub_path)
    hub["control_port"] = hub["public_port"]
    hub_path.write_text(json.dumps(hub))
    with pytest.raises(tunnel.ConfigError, match="must differ"):
        tunnel.load_config(hub_path)

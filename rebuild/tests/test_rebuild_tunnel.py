from __future__ import annotations

import asyncio
import json
import shutil
import ssl
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rebuild import tunnel


@pytest.fixture
def server_certificate(tmp_path):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("OpenSSL is required only to create the test certificate")
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    subprocess.run([
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1",
        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
    ], check=True, capture_output=True)
    return cert, key


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("carrier", ["tls", "wss"])
def test_entry_path_check_validates_tls_and_destination(server_certificate, carrier):
    cert, key = server_certificate
    secret = "health-check-secret-" + "x" * 32

    async def scenario():
        target_connections = []

        async def target(reader, writer):
            target_connections.append(True)
            await reader.read()
            writer.close()
            await writer.wait_closed()

        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]
        exit_config = tunnel.ExitConfig(
            "127.0.0.1", 0, cert, key, secret,
            {"health": ("127.0.0.1", target_port)}, carrier,
        )
        exit_server = await asyncio.start_server(
            tunnel._exit_connection_handler(exit_config), "127.0.0.1", 0,
            ssl=tunnel._server_ssl(exit_config), limit=tunnel.MAX_HANDSHAKE,
        )
        entry_config = tunnel.EntryConfig(
            "127.0.0.1", 0, "localhost", exit_server.sockets[0].getsockname()[1],
            "localhost", cert, secret, "health", "tcp", carrier,
        )
        try:
            result = await asyncio.wait_for(
                tunnel.check_entry_path(entry_config), timeout=4)
            assert "destination accepted" in result
            await asyncio.sleep(0)
            assert target_connections == [True]
        finally:
            exit_server.close()
            target_server.close()
            await asyncio.gather(exit_server.wait_closed(), target_server.wait_closed())

    run(scenario())


@pytest.mark.parametrize("carrier", ["tls", "wss"])
def test_entry_speed_test_measures_bounded_synthetic_download(server_certificate, carrier):
    cert, key = server_certificate
    secret = "speed-test-secret-" + "x" * 32

    async def scenario():
        exit_config = tunnel.ExitConfig(
            "127.0.0.1", 0, cert, key, secret,
            {"health": ("127.0.0.1", 9)}, carrier,
        )
        exit_server = await asyncio.start_server(
            tunnel._exit_connection_handler(exit_config), "127.0.0.1", 0,
            ssl=tunnel._server_ssl(exit_config), limit=tunnel.MAX_HANDSHAKE,
        )
        entry_config = tunnel.EntryConfig(
            "127.0.0.1", 0, "localhost", exit_server.sockets[0].getsockname()[1],
            "localhost", cert, secret, "health", "tcp", carrier,
        )
        try:
            result = await asyncio.wait_for(
                tunnel.speed_test_entry(entry_config, 1), timeout=10)
            assert "Received 1 MiB" in result
            assert "Mbit/s" in result
        finally:
            exit_server.close()
            await exit_server.wait_closed()

    run(scenario())


def test_speed_test_rejects_udp_and_out_of_range_sizes():
    config = tunnel.EntryConfig(
        "127.0.0.1", 0, "localhost", 443, "localhost", Path("/unused"),
        "s" * 32, "x", "udp", "tls",
    )
    with pytest.raises(ValueError, match="TCP entry"):
        run(tunnel.speed_test_entry(config))
    config = tunnel.EntryConfig(
        "127.0.0.1", 0, "localhost", 443, "localhost", Path("/unused"),
        "s" * 32, "x", "tcp", "tls",
    )
    with pytest.raises(ValueError, match="1 to 16 MiB"):
        run(tunnel.speed_test_entry(config, 17))


@pytest.mark.parametrize("carrier", ["tls", "wss"])
def test_entry_tunnel_forwards_data_and_propagates_half_close(
        server_certificate, carrier):
    cert, key = server_certificate
    secret = "test-secret-" + "x" * 32

    async def scenario():
        async def echo(reader, writer):
            data = await reader.read()
            writer.write(data[::-1])
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target = await asyncio.start_server(echo, "127.0.0.1", 0)
        target_port = target.sockets[0].getsockname()[1]
        exit_config = tunnel.ExitConfig(
            "127.0.0.1", 0, cert, key, secret,
            {"echo": ("127.0.0.1", target_port)}, carrier,
        )

        exit_server = await asyncio.start_server(
            tunnel._exit_connection_handler(exit_config), "127.0.0.1", 0,
            ssl=tunnel._server_ssl(exit_config),
            limit=tunnel.MAX_HANDSHAKE,
        )
        exit_port = exit_server.sockets[0].getsockname()[1]
        entry_config = tunnel.EntryConfig(
            "127.0.0.1", 0, "localhost", exit_port, "localhost", cert,
            secret, "echo", "tcp", carrier,
        )
        entry_server = await asyncio.start_server(
            lambda r, w: tunnel._forward_entry(entry_config, r, w),
            "127.0.0.1", 0, limit=tunnel.MAX_HANDSHAKE,
        )
        entry_port = entry_server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", entry_port)
            writer.write(b"clean-room-tunnel")
            await writer.drain()
            writer.write_eof()
            received = await asyncio.wait_for(reader.read(), timeout=3)
            assert received == b"lennut-moor-naelc"
            writer.close()
            await writer.wait_closed()
        finally:
            entry_server.close()
            exit_server.close()
            target.close()
            await asyncio.gather(entry_server.wait_closed(), exit_server.wait_closed(),
                                 target.wait_closed())

    run(scenario())


@pytest.mark.parametrize("carrier", ["tls", "wss"])
def test_udp_entry_keeps_datagram_boundaries_and_returns_responses(server_certificate, carrier):
    cert, key = server_certificate
    secret = "udp-test-secret-" + "x" * 32

    async def scenario():
        class Echo(asyncio.DatagramProtocol):
            transport = None

            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                self.transport.sendto(data[::-1], addr)

        class Client(asyncio.DatagramProtocol):
            def __init__(self):
                self.responses = asyncio.Queue()

            def datagram_received(self, data, addr):
                self.responses.put_nowait(data)

        loop = asyncio.get_running_loop()
        echo_transport, _ = await loop.create_datagram_endpoint(
            Echo, local_addr=("127.0.0.1", 0))
        echo_port = echo_transport.get_extra_info("sockname")[1]
        exit_config = tunnel.ExitConfig(
            "127.0.0.1", 0, cert, key, secret,
            {"echo": ("127.0.0.1", echo_port)}, carrier,
        )

        exit_server = await asyncio.start_server(
            tunnel._exit_connection_handler(exit_config), "127.0.0.1", 0,
            ssl=tunnel._server_ssl(exit_config),
            limit=tunnel.MAX_HANDSHAKE,
        )
        exit_port = exit_server.sockets[0].getsockname()[1]
        entry_config = tunnel.EntryConfig(
            "127.0.0.1", 0, "localhost", exit_port, "localhost", cert,
            secret, "echo", "udp", carrier,
        )
        entry_protocol = tunnel._EntryUdpProtocol(entry_config)
        entry_transport, _ = await loop.create_datagram_endpoint(
            lambda: entry_protocol, local_addr=("127.0.0.1", 0))
        entry_port = entry_transport.get_extra_info("sockname")[1]
        client_transport, client = await loop.create_datagram_endpoint(
            Client, local_addr=("127.0.0.1", 0))
        try:
            client_transport.sendto(b"first-packet", ("127.0.0.1", entry_port))
            first = await asyncio.wait_for(client.responses.get(), timeout=3)
            assert first == b"tekcap-tsrif"
            client_transport.sendto(b"second", ("127.0.0.1", entry_port))
            second = await asyncio.wait_for(client.responses.get(), timeout=3)
            assert second == b"dnoces"
            client_transport.sendto(b"", ("127.0.0.1", entry_port))
            await asyncio.sleep(0.05)
            client_transport.sendto(b"third", ("127.0.0.1", entry_port))
            third = await asyncio.wait_for(client.responses.get(), timeout=3)
            assert third == b"driht"
            assert len(entry_protocol.sessions) == 1
        finally:
            client_transport.close()
            entry_transport.close()
            await entry_protocol.close()
            exit_server.close()
            echo_transport.close()
            await asyncio.gather(exit_server.wait_closed())
            await asyncio.sleep(0)

    run(scenario())


def test_exit_rejects_bad_secret_before_opening_a_target(server_certificate):
    cert, key = server_certificate
    secret = "test-secret-" + "x" * 32
    target_connections = []

    async def scenario():
        async def target(reader, writer):
            target_connections.append(True)
            writer.close()
            await writer.wait_closed()

        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]
        config = tunnel.ExitConfig(
            "127.0.0.1", 0, cert, key, secret,
            {"safe": ("127.0.0.1", target_port)},
        )
        server = await asyncio.start_server(
            lambda r, w: tunnel._open_exit_route(config, r, w),
            "127.0.0.1", 0, ssl=tunnel._server_ssl(config),
            limit=tunnel.MAX_HANDSHAKE,
        )
        port = server.sockets[0].getsockname()[1]
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(cert))
        try:
            reader, writer = await asyncio.open_connection(
                "localhost", port, ssl=context, server_hostname="localhost")
            await tunnel._send_json(writer, {
                "version": 1, "op": "open", "route": "safe", "secret": "wrong-secret",
            })
            response = await asyncio.wait_for(tunnel._read_json(reader), timeout=2)
            assert response == {"ok": False, "error": "denied"}
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.05)
            assert target_connections == []
        finally:
            server.close()
            target_server.close()
            await asyncio.gather(server.wait_closed(), target_server.wait_closed())

    run(scenario())


def test_exit_rejects_unknown_route_even_with_valid_secret(server_certificate):
    cert, key = server_certificate
    secret = "test-secret-" + "x" * 32

    async def scenario():
        config = tunnel.ExitConfig("127.0.0.1", 0, cert, key, secret,
                                   {"known": ("127.0.0.1", 1)})
        server = await asyncio.start_server(
            lambda r, w: tunnel._open_exit_route(config, r, w),
            "127.0.0.1", 0, ssl=tunnel._server_ssl(config),
            limit=tunnel.MAX_HANDSHAKE,
        )
        port = server.sockets[0].getsockname()[1]
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(cert))
        try:
            reader, writer = await asyncio.open_connection(
                "localhost", port, ssl=context, server_hostname="localhost")
            await tunnel._send_json(writer, {
                "version": 1, "op": "open", "route": "arbitrary.example:443",
                "secret": secret,
            })
            assert await tunnel._read_json(reader) == {"ok": False, "error": "denied"}
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

    run(scenario())


def test_entry_requires_certificate_hostname_verification(server_certificate):
    cert, key = server_certificate
    secret = "test-secret-" + "x" * 32

    async def scenario():
        config = tunnel.ExitConfig("127.0.0.1", 0, cert, key, secret,
                                   {"x": ("127.0.0.1", 1)})
        server = await asyncio.start_server(
            lambda r, w: tunnel._open_exit_route(config, r, w),
            "127.0.0.1", 0, ssl=tunnel._server_ssl(config),
            limit=tunnel.MAX_HANDSHAKE,
        )
        port = server.sockets[0].getsockname()[1]
        entry = tunnel.EntryConfig("127.0.0.1", 0, "127.0.0.1", port,
                                   "not-localhost", cert, secret, "x")
        context = tunnel._client_ssl(entry)
        try:
            with pytest.raises(ssl.SSLCertVerificationError):
                await asyncio.open_connection("127.0.0.1", port, ssl=context,
                                               server_hostname=entry.server_name)
        finally:
            server.close()
            await server.wait_closed()

    run(scenario())


def test_config_rejects_short_shared_secret(tmp_path):
    path = tmp_path / "entry.json"
    path.write_text(json.dumps({
        "role": "entry", "listen_host": "127.0.0.1", "listen_port": 1,
        "exit_host": "localhost", "exit_port": 2, "server_name": "localhost",
        "ca_file": "/tmp/ca.crt", "secret": "too-short", "route": "web",
    }))
    with pytest.raises(tunnel.ConfigError, match="32 UTF-8 bytes"):
        tunnel.load_config(path)


def test_config_defaults_carrier_and_rejects_unknown_carrier(tmp_path):
    path = tmp_path / "entry.json"
    base = {
        "role": "entry", "listen_host": "127.0.0.1", "listen_port": 1,
        "exit_host": "localhost", "exit_port": 2, "server_name": "localhost",
        "ca_file": "/tmp/ca.crt", "secret": "s" * 32, "route": "web",
    }
    path.write_text(json.dumps(base))
    assert tunnel.load_config(path).carrier == "tls"
    base["carrier"] = "plain"
    path.write_text(json.dumps(base))
    with pytest.raises(tunnel.ConfigError, match="carrier must"):
        tunnel.load_config(path)


def test_config_normalizes_idn_and_rejects_websocket_host_injection(tmp_path):
    path = tmp_path / "entry.json"
    config = {
        "role": "entry", "listen_host": "127.0.0.1", "listen_port": 1,
        "exit_host": "localhost", "exit_port": 2,
        "server_name": "münich.example", "ca_file": "/tmp/ca.crt",
        "secret": "s" * 32, "route": "web",
    }
    path.write_text(json.dumps(config))
    assert tunnel.load_config(path).server_name == "xn--mnich-kva.example"
    config["server_name"] = "localhost\r\nX-Injected: yes"
    path.write_text(json.dumps(config))
    with pytest.raises(tunnel.ConfigError, match="without header characters"):
        tunnel.load_config(path)


def test_config_rejects_arbitrary_exit_target_shape(tmp_path):
    path = tmp_path / "exit.json"
    path.write_text(json.dumps({
        "role": "exit", "listen_host": "0.0.0.0", "listen_port": 8443,
        "cert_file": "/tmp/server.crt", "key_file": "/tmp/server.key",
        "secret": "s" * 32, "routes": ["host:port"],
    }))
    with pytest.raises(tunnel.ConfigError, match="non-empty object"):
        tunnel.load_config(path)


def test_websocket_server_rejects_unmasked_client_frame():
    async def scenario():
        rejected = asyncio.Event()

        async def accept(reader, writer):
            stream = tunnel._WebSocketStream(reader, writer, client=False)
            try:
                await stream.readexactly(1)
            except tunnel.ProtocolError as exc:
                assert "masking direction" in str(exc)
                rejected.set()
            finally:
                stream.close()
                await stream.wait_closed()

        server = await asyncio.start_server(accept, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"\x82\x01x")
            await writer.drain()
            await asyncio.wait_for(rejected.wait(), timeout=2)
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

    run(scenario())


def test_websocket_upgrade_rejects_wrong_path():
    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(
            b"GET /wrong HTTP/1.1\r\nHost: exit\r\n\r\n")
        reader.feed_eof()
        with pytest.raises(tunnel.ProtocolError, match="request path"):
            await tunnel._server_websocket(reader, None)

    run(scenario())


def test_unknown_frames_are_rejected():
    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x09\x00\x00\x00\x00")
        reader.feed_eof()
        with pytest.raises(tunnel.ProtocolError, match="unknown frame"):
            await tunnel._read_frame(reader)

    run(scenario())


def test_connection_gate_enforces_per_peer_limit():
    async def scenario():
        active = [0]
        entered = []
        release = asyncio.Event()

        class Writer:
            def __init__(self, peer):
                self.peer = peer
                self.closed = False

            def get_extra_info(self, name):
                return (self.peer, 1234) if name == "peername" else None

            def close(self):
                self.closed = True

            async def wait_closed(self):
                return None

        async def handler(_reader, writer):
            entered.append(writer.peer)
            await release.wait()

        gate = tunnel._limited_handler(handler, active, 10, per_peer_limit=1)
        first = Writer("192.0.2.1")
        same_peer = Writer("192.0.2.1")
        other_peer = Writer("192.0.2.2")
        first_task = asyncio.create_task(gate(None, first))
        while not entered:
            await asyncio.sleep(0)
        await gate(None, same_peer)
        other_task = asyncio.create_task(gate(None, other_peer))
        while len(entered) < 2:
            await asyncio.sleep(0)
        assert same_peer.closed
        assert entered == ["192.0.2.1", "192.0.2.2"]
        assert active == [2]
        release.set()
        await asyncio.gather(first_task, other_task)
        assert active == [0]

    run(scenario())


def test_connection_gate_rejects_excess_session_without_leaking_counter():
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        active = [0]

        class Writer:
            closed = False

            def close(self):
                self.closed = True

            def get_extra_info(self, name):
                return ("127.0.0.1", 1000) if name == "peername" else None

            async def wait_closed(self):
                return None

        async def handler(_reader, _writer):
            started.set()
            await release.wait()

        gate = tunnel._limited_handler(handler, active, 1)
        first_writer, excess_writer = Writer(), Writer()
        first = asyncio.create_task(gate(None, first_writer))
        await started.wait()
        await gate(None, excess_writer)
        assert excess_writer.closed and active == [1]
        release.set()
        await first
        assert active == [0]

    run(scenario())

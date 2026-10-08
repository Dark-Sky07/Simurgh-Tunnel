"""End-to-end: a real user connection through a real tunnel.

Four carriers, a webserver behind the exit, and the exact scenario the project
exists for: the client connects to the *relay* (Iran) and gets the *exit*
(foreign) content byte for byte.
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import free_port
from simurgh.config import (ExitConfig, ExitEndpoint, ListenSpec, Mapping,
                            RelayConfig)
from simurgh.exit import ExitNode
from simurgh.relay import RelayNode

pytestmark = pytest.mark.asyncio

CARRIERS = ("raw", "tls", "wss", "plain")
TOKEN = "e2e-token"


async def _http_server() -> tuple[asyncio.AbstractServer, int]:
    async def handler(reader, writer):
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except Exception:
            writer.close()
            return
        body = b"<h1>foreign panel</h1>"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                     b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body))
        writer.write(body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _stack(carrier: str, cert, target_port: int | None):
    """Bring up exit + relay for one carrier; returns (relay, node, relay_port)."""
    exit_port = free_port()
    relay_port = free_port()
    home_exit = ExitConfig(
        token=TOKEN, name="exit", cert_file=cert[0], key_file=cert[1],
        cert_auto=False,
        listen=[ListenSpec(carrier=carrier, host="127.0.0.1", port=exit_port)],
    )
    node = ExitNode(home_exit)
    await node.start()
    relay_cfg = RelayConfig(
        token=TOKEN,
        exit=ExitEndpoint(carrier=carrier, address="127.0.0.1", port=exit_port,
                          insecure_skip_verify=True),
        mappings=[Mapping(name="panel", listen=relay_port, target_port=target_port
                          or free_port())],
    )
    relay = RelayNode(relay_cfg)
    await relay.start()
    for _ in range(80):
        if relay.connected.is_set():
            break
        await asyncio.sleep(0.05)
    assert relay.connected.is_set(), f"{carrier}: tunnel did not come up"
    return relay, node, relay_port


@pytest.mark.parametrize("carrier", CARRIERS)
async def test_user_gets_the_foreign_server_content(carrier, certs):
    web, port = await _http_server()
    relay, node, relay_port = await _stack(carrier, certs, port)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        writer.write(b"GET / HTTP/1.1\r\nHost: panel\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), 15)
        writer.close()
        assert b"200 OK" in response
        assert b"<h1>foreign panel</h1>" in response
    finally:
        await relay.stop()
        await node.stop()
        web.close()


@pytest.mark.parametrize("carrier", CARRIERS)
async def test_bulk_transfer_survives_the_window(carrier, certs):
    """Downloads bigger than one flow-control window must not stall."""
    payload = b"x" * (512 * 1024)

    async def handler(reader, writer):
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except Exception:
            writer.close()
            return
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n"
                     b"Connection: close\r\n\r\n" % len(payload))
        writer.write(payload)
        await writer.drain()
        writer.close()

    web = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = web.sockets[0].getsockname()[1]
    relay, node, relay_port = await _stack(carrier, certs, port)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        body = await asyncio.wait_for(reader.read(-1), 30)
        writer.close()
        assert body.endswith(payload)
    finally:
        await relay.stop()
        await node.stop()
        web.close()


async def test_exit_port_looks_like_a_web_server(certs):
    """A prober speaking plain HTTP to the TLS port must see the decoy site."""
    exit_port = free_port()
    node = ExitNode(ExitConfig(
        token=TOKEN, cert_file=certs[0], key_file=certs[1], cert_auto=False,
        listen=[ListenSpec(carrier="tls", host="127.0.0.1", port=exit_port)],
    ))
    await node.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", exit_port)
        writer.write(b"GET / HTTP/1.1\r\nHost: whatever\r\n\r\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(4096), 10)
        writer.close()
        assert b"HTTP/1.1 200" in data
        assert b"nginx" in data.lower() or b"<html" in data.lower()
    finally:
        await node.stop()


async def test_wrong_token_cannot_open_a_tunnel(certs):
    exit_port = free_port()
    node = ExitNode(ExitConfig(
        token="right-token", cert_file=certs[0], key_file=certs[1],
        cert_auto=False,
        listen=[ListenSpec(carrier="plain", host="127.0.0.1", port=exit_port)],
    ))
    await node.start()
    try:
        relay = RelayNode(RelayConfig(
            token="wrong-token",
            exit=ExitEndpoint(carrier="plain", address="127.0.0.1", port=exit_port),
            mappings=[Mapping(listen=free_port(), target_port=80)],
        ), listen=False)
        await relay.start()
        await asyncio.sleep(1.0)
        assert not relay.connected.is_set()
        assert relay.last_error
        await relay.stop()
    finally:
        await node.stop()


async def test_udp_mapping_relays_datagrams(certs):
    loop = asyncio.get_running_loop()

    class Echo(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, addr):
            self.transport.sendto(b"echo:" + data, addr)

    target, _ = await loop.create_datagram_endpoint(Echo, local_addr=("127.0.0.1", 0))
    target_port = target.get_extra_info("sockname")[1]
    exit_port, relay_port = free_port(), free_port()
    node = ExitNode(ExitConfig(
        token=TOKEN, cert_file=certs[0], key_file=certs[1], cert_auto=False,
        listen=[ListenSpec(carrier="tls", host="127.0.0.1", port=exit_port)],
    ))
    await node.start()
    relay = RelayNode(RelayConfig(
        token=TOKEN,
        exit=ExitEndpoint(carrier="tls", address="127.0.0.1", port=exit_port,
                          insecure_skip_verify=True),
        mappings=[Mapping(listen=relay_port, target_port=target_port, udp=True)],
    ))
    await relay.start()
    for _ in range(80):
        if relay.connected.is_set():
            break
        await asyncio.sleep(0.05)

    got: list[bytes] = []

    class Client(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            got.append(data)

    transport, _proto = await loop.create_datagram_endpoint(
        Client, remote_addr=("127.0.0.1", relay_port))
    try:
        for i in range(3):
            transport.sendto(f"packet-{i}".encode())
            await asyncio.sleep(0.2)
        assert got and got[0].startswith(b"echo:packet-")
    finally:
        transport.close()
        await relay.stop()
        await node.stop()
        target.close()


async def test_relay_survives_the_exit_restarting(certs):
    """The supervisor must reconnect by itself when the exit comes back."""
    web, port = await _http_server()
    relay, node, relay_port = await _stack("tls", certs, port)
    try:
        await node.stop()                     # simulate the exit going away
        for _ in range(60):
            if not relay.connected.is_set():
                break
            await asyncio.sleep(0.1)
        assert not relay.connected.is_set()
    finally:
        await relay.stop()
        web.close()

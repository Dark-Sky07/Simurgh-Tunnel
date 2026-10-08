"""The tunnel pool: ``connections`` in the config, several tunnels at once.

One TCP connection between Iran and abroad is one flow: carriers, middleboxes
and single-core handling can each cap it, and if it drops *everything* stops
for a moment.  With ``connections = N`` the relay keeps N tunnels and hands
each user connection to the least busy one, so neither a cap nor a single
broken tunnel takes every user down.
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import free_port
from simurgh.config import (ExitConfig, ExitEndpoint, ListenSpec, Mapping,
                            RelayConfig, TunnelSpec)
from simurgh.exit import ExitNode
from simurgh.protocol import MODE_TCP, encode_addr
from simurgh.relay import RelayNode

pytestmark = pytest.mark.asyncio

TOKEN = "pool-token"


async def _echo_server():
    async def handler(reader, writer):
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _wait(predicate, timeout: float = 6.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


async def _open_many(relay, port: int, count: int):
    target = encode_addr("127.0.0.1", port)
    tasks = [asyncio.ensure_future(relay.open_stream(target, MODE_TCP))
             for _ in range(count)]
    streams = await asyncio.gather(*tasks)
    await asyncio.sleep(0.05)
    return streams


async def test_direct_mode_keeps_a_pool_and_spreads_the_load():
    web, target_port = await _echo_server()
    exit_port = free_port()
    relay_port = free_port()
    node = ExitNode(ExitConfig(
        token=TOKEN, name="exit", cert_auto=False,
        listen=[ListenSpec(carrier="plain", host="127.0.0.1", port=exit_port)]))
    await node.start()
    relay = RelayNode(RelayConfig(
        token=TOKEN, name="relay", connections=3,
        exit=ExitEndpoint(carrier="plain", address="127.0.0.1", port=exit_port,
                          insecure_skip_verify=True),
        mappings=[Mapping(name="web", listen=relay_port, target_port=target_port)]))
    await relay.start()
    try:
        assert await _wait(lambda: len(relay.muxes) == 3), "the pool did not fill"
        assert await _wait(lambda: len(node.tunnels) == 3), "the exit missed tunnels"
        assert relay.status()["connections"] == 3
        streams = await _open_many(relay, target_port, 6)
        spread = sorted(len(m.streams) for m in relay.muxes)
        assert spread == [2, 2, 2], f"load was not spread: {spread}"
        for s in streams:
            s.close(rst=True)
    finally:
        await relay.stop()
        await node.stop()
        web.close()


async def test_reverse_mode_keeps_a_pool_too():
    web, target_port = await _echo_server()
    tunnel_port = free_port()
    user_port = free_port()
    relay = RelayNode(RelayConfig(
        token=TOKEN, name="ir-relay", dial="exit", connections=2,
        tunnel=TunnelSpec(carrier="plain", host="127.0.0.1", port=tunnel_port,
                          cert_auto=False),
        mappings=[Mapping(name="web", listen=user_port, target_port=target_port)]))
    await relay.start()
    node = ExitNode(ExitConfig(
        token=TOKEN, name="omega-de", connections=2,
        listen=[ListenSpec(carrier="plain", port=tunnel_port, dial="127.0.0.1",
                           insecure_skip_verify=True)]))
    await node.start()
    try:
        assert await _wait(lambda: len(relay.muxes) == 2), "the exit dialled once"
        assert relay.status()["dial"] == "exit"
        streams = await _open_many(relay, target_port, 4)
        spread = sorted(len(m.streams) for m in relay.muxes)
        assert spread == [2, 2], f"load was not spread: {spread}"
        for s in streams:
            s.close(rst=True)
    finally:
        await node.stop()
        await relay.stop()
        web.close()


async def test_a_lost_tunnel_does_not_stop_new_connections():
    web, target_port = await _echo_server()
    exit_port = free_port()
    node = ExitNode(ExitConfig(
        token=TOKEN, name="exit", cert_auto=False,
        listen=[ListenSpec(carrier="plain", host="127.0.0.1", port=exit_port)]))
    await node.start()
    relay = RelayNode(RelayConfig(
        token=TOKEN, name="relay", connections=2,
        exit=ExitEndpoint(carrier="plain", address="127.0.0.1", port=exit_port,
                          insecure_skip_verify=True)))
    await relay.start()
    try:
        assert await _wait(lambda: len(relay.muxes) == 2)
        relay.muxes[0].close()                     # one tunnel dies
        await asyncio.sleep(0.2)
        assert len(relay.muxes) >= 1
        streams = await _open_many(relay, target_port, 2)
        assert all(not s.closed for s in streams)  # traffic keeps flowing
        for s in streams:
            s.close(rst=True)
    finally:
        await relay.stop()
        await node.stop()
        web.close()

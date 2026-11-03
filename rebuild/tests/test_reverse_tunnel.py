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


@pytest.mark.parametrize("carrier", ["tls", "wss", "h2"])
def test_reverse_agent_forwards_concurrent_tcp_clients_to_its_fixed_target(
        tls_identity, carrier):
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
            carrier=carrier,
        )
        control, public, state = await reverse._start_reverse_hub(hub_config)
        hub_port = control.sockets[0].getsockname()[1]
        public_port = public.sockets[0].getsockname()[1]
        agent_config = tunnel.ReverseAgentConfig(
            "127.0.0.1", hub_port, "localhost", cert, secret, "echo",
            "127.0.0.1", target.sockets[0].getsockname()[1], carrier=carrier,
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


@pytest.mark.parametrize("carrier", ["tls", "wss", "h2"])
def test_reverse_hub_serves_multiple_routes_on_one_control_listener(tls_identity, carrier):
    cert, key = tls_identity
    secret = "reverse-multi-route-shared-secret-012345"
    route_secrets = {"web": secret, "admin": "reverse-admin-route-secret-0123456789"}

    async def scenario():
        async def make_target(prefix: bytes):
            async def echo(reader, writer):
                payload = await reader.read()
                writer.write(prefix + payload)
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            return await asyncio.start_server(echo, "127.0.0.1", 0)

        targets = {
            "web": await make_target(b"web:"),
            "admin": await make_target(b"admin:"),
        }
        routes = tuple(tunnel.ReverseHubRoute(
            route, "127.0.0.1", 0, route_secrets[route] if route != "web" else None,
        ) for route in targets)
        config = tunnel.ReverseHubConfig(
            "127.0.0.1", 0, "127.0.0.1", 0, cert, key, secret, "web",
            carrier=carrier, routes=routes,
        )
        control, publics, states = await reverse._start_reverse_hub(config)
        control_port = control.sockets[0].getsockname()[1]
        agents = []
        try:
            for route, target in targets.items():
                agent_config = tunnel.ReverseAgentConfig(
                    "127.0.0.1", control_port, "localhost", cert, route_secrets[route], route,
                    "127.0.0.1", target.sockets[0].getsockname()[1], carrier=carrier,
                )
                agents.append(asyncio.create_task(reverse.serve_reverse_agent(agent_config)))
            await asyncio.gather(*(asyncio.wait_for(state.agent_ready.wait(), 3)
                                   for state in states.values()))
            unknown_route = tunnel.ReverseAgentConfig(
                "127.0.0.1", control_port, "localhost", cert, secret, "unknown",
                "127.0.0.1", next(iter(targets.values())).sockets[0].getsockname()[1],
                carrier=carrier,
            )
            with pytest.raises(ConnectionError, match="hub rejected"):
                await tunnel.check_reverse_agent_hub(unknown_route)
            wrong_admin_secret = tunnel.ReverseAgentConfig(
                "127.0.0.1", control_port, "localhost", cert, secret, "admin",
                "127.0.0.1", targets["admin"].sockets[0].getsockname()[1],
                carrier=carrier,
            )
            with pytest.raises(ConnectionError, match="hub rejected"):
                await tunnel.check_reverse_agent_hub(wrong_admin_secret)
            malformed_reader, malformed_writer = await reverse._open_agent_hub_connection(
                unknown_route)
            try:
                await tunnel._send_json(malformed_writer, {
                    "version": 1, "op": "probe", "route": [], "secret": secret,
                })
                assert (await tunnel._read_json(malformed_reader)).get("ok") is False
            finally:
                malformed_writer.close()
                await malformed_writer.wait_closed()

            async def exchange(route: str, payload: bytes) -> bytes:
                listener = publics[route]
                port = listener.sockets[0].getsockname()[1]
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(payload)
                await writer.drain()
                writer.write_eof()
                result = await asyncio.wait_for(reader.read(), timeout=4)
                writer.close()
                await writer.wait_closed()
                return result

            assert await asyncio.gather(
                exchange("web", b"one"), exchange("admin", b"two"),
            ) == [b"web:one", b"admin:two"]
            assert not states["web"].pending
            assert not states["admin"].pending
        finally:
            for agent in agents:
                agent.cancel()
            await asyncio.gather(*agents, return_exceptions=True)
            control.close()
            for listener in publics.values():
                listener.close()
            for target in targets.values():
                target.close()
            await asyncio.gather(control.wait_closed(),
                                 *(listener.wait_closed() for listener in publics.values()),
                                 *(target.wait_closed() for target in targets.values()))
            for state in states.values():
                handlers = tuple(state.handler_tasks)
                for task in handlers:
                    task.cancel()
                if handlers:
                    await asyncio.gather(*handlers, return_exceptions=True)

    run(scenario())


@pytest.mark.parametrize("carrier", ["tls", "wss", "h2"])
def test_reverse_tcp_flow_outlives_channel_open_deadline(tls_identity, carrier, monkeypatch):
    cert, key = tls_identity
    secret = "reverse-long-tcp-flow-secret-0123456789"
    monkeypatch.setattr(reverse, "_CHANNEL_OPEN_TIMEOUT", 0.05)

    async def scenario():
        async def echo(reader, writer):
            try:
                payload = await reader.readexactly(4)
                writer.write(payload)
                await writer.drain()
                await reader.read()
            except asyncio.IncompleteReadError:
                pass
            finally:
                writer.close()
                await writer.wait_closed()

        target = await asyncio.start_server(echo, "127.0.0.1", 0)
        hub_config = tunnel.ReverseHubConfig(
            "127.0.0.1", 0, "127.0.0.1", 0, cert, key, secret, "web",
            carrier=carrier,
        )
        control, public, state = await reverse._start_reverse_hub(hub_config)
        agent = asyncio.create_task(reverse.serve_reverse_agent(
            tunnel.ReverseAgentConfig(
                "127.0.0.1", control.sockets[0].getsockname()[1], "localhost",
                cert, secret, "web", "127.0.0.1",
                target.sockets[0].getsockname()[1], carrier=carrier,
            )))
        client_writer = None
        try:
            await asyncio.wait_for(state.agent_ready.wait(), timeout=3)
            public_port = public.sockets[0].getsockname()[1]
            reader, client_writer = await asyncio.open_connection("127.0.0.1", public_port)
            for _ in range(100):
                if state.pending and next(iter(state.pending.values())).opened.is_set():
                    break
                await asyncio.sleep(0.005)
            assert state.pending
            assert next(iter(state.pending.values())).opened.is_set()
            await asyncio.sleep(0.1)
            client_writer.write(b"ping")
            await client_writer.drain()
            assert await asyncio.wait_for(reader.readexactly(4), timeout=2) == b"ping"
        finally:
            if client_writer is not None:
                client_writer.close()
                await client_writer.wait_closed()
            agent.cancel()
            await asyncio.gather(agent, return_exceptions=True)
            control.close()
            public.close()
            target.close()
            await asyncio.gather(control.wait_closed(), public.wait_closed(),
                                 target.wait_closed())
            handlers = tuple(state.handler_tasks)
            for task in handlers:
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)

    run(scenario())


@pytest.mark.parametrize("carrier", ["tls", "wss", "h2"])
def test_reverse_tcp_reassigns_preopen_failure_to_another_agent(tls_identity, carrier):
    cert, key = tls_identity
    secret = "reverse-tcp-failover-secret-0123456789"

    async def scenario():
        async def echo(reader, writer):
            data = await reader.read()
            writer.write(b"agent-b:" + data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target = await asyncio.start_server(echo, "127.0.0.1", 0)
        closed = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
        failed_port = closed.sockets[0].getsockname()[1]
        closed.close()
        await closed.wait_closed()
        hub_config = tunnel.ReverseHubConfig(
            "127.0.0.1", 0, "127.0.0.1", 0, cert, key, secret, "web",
            carrier=carrier,
        )
        control, public, state = await reverse._start_reverse_hub(hub_config)
        hub_port = control.sockets[0].getsockname()[1]
        agents = [asyncio.create_task(reverse.serve_reverse_agent(
            tunnel.ReverseAgentConfig(
                "127.0.0.1", hub_port, "localhost", cert, secret, "web",
                "127.0.0.1", failed_port, carrier=carrier,
            )))]
        try:
            for _ in range(100):
                if len(state.agents) == 1:
                    break
                await asyncio.sleep(0.01)
            assert len(state.agents) == 1
            agents.append(asyncio.create_task(reverse.serve_reverse_agent(
                tunnel.ReverseAgentConfig(
                    "127.0.0.1", hub_port, "localhost", cert, secret, "web",
                    "127.0.0.1", target.sockets[0].getsockname()[1], carrier=carrier,
                ))))
            for _ in range(100):
                if len(state.agents) == 2:
                    break
                await asyncio.sleep(0.01)
            assert len(state.agents) == 2
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", public.sockets[0].getsockname()[1])
            writer.write(b"preserved-buffer")
            await writer.drain()
            writer.write_eof()
            result = await asyncio.wait_for(reader.read(), timeout=5)
            assert result == b"agent-b:preserved-buffer"
            writer.close()
            await writer.wait_closed()
        finally:
            for agent in agents:
                agent.cancel()
            await asyncio.gather(*agents, return_exceptions=True)
            control.close()
            public.close()
            target.close()
            await asyncio.gather(control.wait_closed(), public.wait_closed(),
                                 target.wait_closed())
            handlers = tuple(state.handler_tasks)
            for task in handlers:
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)

    run(scenario())


@pytest.mark.parametrize("carrier", ["tls", "wss", "h2"])
def test_reverse_hub_round_robins_tcp_clients_across_agents(tls_identity, carrier):
    cert, key = tls_identity
    secret = "reverse-multi-agent-secret-0123456789"

    async def scenario():
        async def echo(label, reader, writer):
            data = await reader.read()
            writer.write(label + b":" + data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        targets = [
            await asyncio.start_server(
                lambda r, w, label=label: echo(label, r, w), "127.0.0.1", 0)
            for label in (b"agent-a", b"agent-b")
        ]
        hub_config = tunnel.ReverseHubConfig(
            "127.0.0.1", 0, "127.0.0.1", 0, cert, key, secret, "web",
            carrier=carrier,
        )
        control, public, state = await reverse._start_reverse_hub(hub_config)
        hub_port = control.sockets[0].getsockname()[1]
        agents = [
            asyncio.create_task(reverse.serve_reverse_agent(
                tunnel.ReverseAgentConfig(
                    "127.0.0.1", hub_port, "localhost", cert, secret, "web",
                    "127.0.0.1", target.sockets[0].getsockname()[1], carrier=carrier,
                )))
            for target in targets
        ]
        try:
            for _ in range(300):
                if len(state.agents) == 2:
                    break
                await asyncio.sleep(0.01)
            assert len(state.agents) == 2
            public_port = public.sockets[0].getsockname()[1]

            async def exchange(index):
                reader, writer = await asyncio.open_connection("127.0.0.1", public_port)
                payload = f"request-{index}".encode()
                writer.write(payload)
                await writer.drain()
                writer.write_eof()
                response = await asyncio.wait_for(reader.read(), timeout=5)
                writer.close()
                await writer.wait_closed()
                return response

            responses = [await exchange(index) for index in range(6)]
            assert {response.rsplit(b":", 1)[1] for response in responses} == {
                f"request-{index}".encode() for index in range(6)
            }
            assert {response.split(b":", 1)[0] for response in responses} == {
                b"agent-a", b"agent-b",
            }
            assert sum(response.startswith(b"agent-a:") for response in responses) == 3
            assert sum(response.startswith(b"agent-b:") for response in responses) == 3
            agents[0].cancel()
            await asyncio.gather(agents[0], return_exceptions=True)
            for _ in range(100):
                if len(state.agents) == 1:
                    break
                await asyncio.sleep(0.01)
            assert len(state.agents) == 1
            assert await exchange(6) in {
                b"agent-a:request-6", b"agent-b:request-6",
            }
        finally:
            for agent in agents:
                agent.cancel()
            await asyncio.gather(*agents, return_exceptions=True)
            control.close()
            public.close()
            await asyncio.gather(control.wait_closed(), public.wait_closed())
            for target in targets:
                target.close()
            await asyncio.gather(*(target.wait_closed() for target in targets))
            handlers = tuple(state.handler_tasks)
            for task in handlers:
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)

    run(scenario())


@pytest.mark.parametrize("carrier", ["tls", "wss", "h2"])
def test_reverse_hub_serves_multiple_udp_routes_on_one_control_listener(tls_identity, carrier):
    cert, key = tls_identity
    secret = "reverse-multi-route-udp-secret-012345"
    route_secrets = {"dns": secret, "metrics": "reverse-metrics-secret-0123456789"}

    async def scenario():
        class Echo(asyncio.DatagramProtocol):
            def __init__(self, prefix):
                self.prefix = prefix

            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                self.transport.sendto(self.prefix + data, addr)

        class Client(asyncio.DatagramProtocol):
            def __init__(self):
                self.responses = asyncio.Queue()

            def datagram_received(self, data, addr):
                self.responses.put_nowait(data)

        loop = asyncio.get_running_loop()
        targets = {}
        for route, prefix in (("dns", b"dns:"), ("metrics", b"metrics:")):
            transport, _ = await loop.create_datagram_endpoint(
                lambda prefix=prefix: Echo(prefix), local_addr=("127.0.0.1", 0))
            targets[route] = transport
        routes = tuple(tunnel.ReverseHubRoute(
            route, "127.0.0.1", 0, route_secrets[route] if route != "dns" else None,
        ) for route in targets)
        config = tunnel.ReverseHubConfig(
            "127.0.0.1", 0, "127.0.0.1", 0, cert, key, secret, "dns",
            carrier=carrier, transport="udp", routes=routes,
        )
        control, publics, states = await reverse._start_reverse_hub(config)
        control_port = control.sockets[0].getsockname()[1]
        agents = []
        clients = {}
        try:
            for route, target in targets.items():
                target_port = target.get_extra_info("sockname")[1]
                agent_config = tunnel.ReverseAgentConfig(
                    "127.0.0.1", control_port, "localhost", cert, route_secrets[route], route,
                    "127.0.0.1", target_port, carrier=carrier, transport="udp",
                )
                agents.append(asyncio.create_task(reverse.serve_reverse_agent(agent_config)))
            await asyncio.gather(*(asyncio.wait_for(state.agent_ready.wait(), 3)
                                   for state in states.values()))
            for route in targets:
                transport, protocol = await loop.create_datagram_endpoint(
                    Client, local_addr=("127.0.0.1", 0))
                clients[route] = (transport, protocol)
                port = publics[route].get_extra_info("sockname")[1]
                transport.sendto(route.encode(), ("127.0.0.1", port))
            responses = await asyncio.gather(*(
                asyncio.wait_for(protocol.responses.get(), 4)
                for _transport, protocol in clients.values()
            ))
            assert responses == [b"dns:dns", b"metrics:metrics"]
        finally:
            for agent in agents:
                agent.cancel()
            await asyncio.gather(*agents, return_exceptions=True)
            for transport, _protocol in clients.values():
                transport.close()
            for listener in publics.values():
                listener.close()
            for target in targets.values():
                target.close()
            for state in states.values():
                if state.public_udp is not None:
                    await state.public_udp.close()
                handlers = tuple(state.handler_tasks)
                for task in handlers:
                    task.cancel()
                if handlers:
                    await asyncio.gather(*handlers, return_exceptions=True)
            control.close()
            await control.wait_closed()

    run(scenario())


@pytest.mark.parametrize("carrier", ["tls", "wss", "h2"])
def test_reverse_hub_can_mix_tcp_and_udp_routes(tls_identity, carrier):
    cert, key = tls_identity
    secret = "reverse-mixed-route-shared-secret-0123"
    udp_secret = "reverse-mixed-route-udp-secret-012345"

    async def scenario():
        class UdpEcho(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                self.transport.sendto(b"udp:" + data, addr)

        class UdpClient(asyncio.DatagramProtocol):
            def __init__(self):
                self.responses = asyncio.Queue()

            def datagram_received(self, data, addr):
                self.responses.put_nowait(data)

        async def tcp_echo(reader, writer):
            payload = await reader.read()
            writer.write(b"tcp:" + payload)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        tcp_target = await asyncio.start_server(tcp_echo, "127.0.0.1", 0)
        udp_target, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            UdpEcho, local_addr=("127.0.0.1", 0))
        tcp_route = tunnel.ReverseHubRoute("stream", "127.0.0.1", 0, transport="tcp")
        udp_route = tunnel.ReverseHubRoute(
            "datagram", "127.0.0.1", 0, udp_secret, transport="udp")
        config = tunnel.ReverseHubConfig(
            "127.0.0.1", 0, "127.0.0.1", 0, cert, key, secret, "stream",
            carrier=carrier, transport="tcp", routes=(tcp_route, udp_route),
        )
        control, publics, states = await reverse._start_reverse_hub(config)
        control_port = control.sockets[0].getsockname()[1]
        agents = [
            asyncio.create_task(reverse.serve_reverse_agent(tunnel.ReverseAgentConfig(
                "127.0.0.1", control_port, "localhost", cert, secret, "stream",
                "127.0.0.1", tcp_target.sockets[0].getsockname()[1],
                carrier=carrier, transport="tcp",
            ))),
            asyncio.create_task(reverse.serve_reverse_agent(tunnel.ReverseAgentConfig(
                "127.0.0.1", control_port, "localhost", cert, udp_secret, "datagram",
                "127.0.0.1", udp_target.get_extra_info("sockname")[1],
                carrier=carrier, transport="udp",
            ))),
        ]
        client_udp = None
        try:
            await asyncio.gather(*(asyncio.wait_for(state.agent_ready.wait(), 3)
                                   for state in states.values()))
            tcp_reader, tcp_writer = await asyncio.open_connection(
                "127.0.0.1", publics["stream"].sockets[0].getsockname()[1])
            tcp_writer.write(b"payload")
            await tcp_writer.drain()
            tcp_writer.write_eof()
            tcp_response = await asyncio.wait_for(tcp_reader.read(), 4)
            tcp_writer.close()
            await tcp_writer.wait_closed()

            client_udp, udp_client = await asyncio.get_running_loop().create_datagram_endpoint(
                UdpClient, local_addr=("127.0.0.1", 0))
            udp_port = publics["datagram"].get_extra_info("sockname")[1]
            client_udp.sendto(b"payload", ("127.0.0.1", udp_port))
            udp_response = await asyncio.wait_for(udp_client.responses.get(), 4)
            assert tcp_response == b"tcp:payload"
            assert udp_response == b"udp:payload"
            assert states["stream"].config.transport == "tcp"
            assert states["datagram"].config.transport == "udp"
        finally:
            if client_udp is not None:
                client_udp.close()
            for agent in agents:
                agent.cancel()
            await asyncio.gather(*agents, return_exceptions=True)
            for listener in publics.values():
                listener.close()
            tcp_target.close()
            udp_target.close()
            await tcp_target.wait_closed()
            for state in states.values():
                if state.public_udp is not None:
                    await state.public_udp.close()
                handlers = tuple(state.handler_tasks)
                for task in handlers:
                    task.cancel()
                if handlers:
                    await asyncio.gather(*handlers, return_exceptions=True)
            control.close()
            await control.wait_closed()

    run(scenario())


@pytest.mark.parametrize("carrier", ["tls", "wss", "h2"])
def test_reverse_agent_forwards_udp_clients_to_its_fixed_target(tls_identity, carrier):
    cert, key = tls_identity
    secret = "reverse-udp-shared-secret-0123456789"

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
        target_transport, _ = await loop.create_datagram_endpoint(
            Echo, local_addr=("127.0.0.1", 0))
        target_port = target_transport.get_extra_info("sockname")[1]
        hub_config = tunnel.ReverseHubConfig(
            "127.0.0.1", 0, "127.0.0.1", 0, cert, key, secret, "dns",
            transport="udp", carrier=carrier,
        )
        control, public, state = await reverse._start_reverse_hub(hub_config)
        hub_port = control.sockets[0].getsockname()[1]
        public_port = public.get_extra_info("sockname")[1]
        agent_config = tunnel.ReverseAgentConfig(
            "127.0.0.1", hub_port, "localhost", cert, secret, "dns", "127.0.0.1",
            target_port, transport="udp", carrier=carrier,
        )
        agent = asyncio.create_task(reverse.serve_reverse_agent(agent_config))
        client_transport, client = await loop.create_datagram_endpoint(
            Client, local_addr=("127.0.0.1", 0))
        try:
            await asyncio.wait_for(state.agent_ready.wait(), timeout=3)
            client_transport.sendto(b"reverse-udp", ("127.0.0.1", public_port))
            response = await asyncio.wait_for(client.responses.get(), timeout=5)
            assert response == b"pdu-esrever"
            assert state.public_udp is not None
            assert len(state.pending_udp) == 1
        finally:
            client_transport.close()
            agent.cancel()
            await asyncio.gather(agent, return_exceptions=True)
            public.close()
            await state.public_udp.close()
            control.close()
            target_transport.close()
            await asyncio.gather(control.wait_closed())
            handlers = tuple(state.handler_tasks)
            for task in handlers:
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)

    run(scenario())


@pytest.mark.parametrize("carrier", ["tls", "wss", "h2"])
def test_reverse_hub_round_robins_udp_associations_across_agents(tls_identity, carrier):
    cert, key = tls_identity
    secret = "reverse-multi-agent-udp-secret-012345"

    async def scenario():
        class Echo(asyncio.DatagramProtocol):
            def __init__(self, label):
                self.label = label

            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                self.transport.sendto(self.label + b":" + data, addr)

        class Client(asyncio.DatagramProtocol):
            def __init__(self):
                self.responses = asyncio.Queue()

            def datagram_received(self, data, addr):
                self.responses.put_nowait(data)

        loop = asyncio.get_running_loop()
        target_transports = []
        target_ports = []
        for label in (b"agent-a", b"agent-b"):
            transport, _ = await loop.create_datagram_endpoint(
                lambda label=label: Echo(label), local_addr=("127.0.0.1", 0))
            target_transports.append(transport)
            target_ports.append(transport.get_extra_info("sockname")[1])
        hub_config = tunnel.ReverseHubConfig(
            "127.0.0.1", 0, "127.0.0.1", 0, cert, key, secret, "dns",
            transport="udp", carrier=carrier,
        )
        control, public, state = await reverse._start_reverse_hub(hub_config)
        hub_port = control.sockets[0].getsockname()[1]
        agents = [
            asyncio.create_task(reverse.serve_reverse_agent(
                tunnel.ReverseAgentConfig(
                    "127.0.0.1", hub_port, "localhost", cert, secret, "dns",
                    "127.0.0.1", target_port, transport="udp", carrier=carrier,
                )))
            for target_port in target_ports
        ]
        clients = []
        client_protocols = []
        client_addresses = []
        try:
            for _ in range(300):
                if len(state.agents) == 2:
                    break
                await asyncio.sleep(0.01)
            assert len(state.agents) == 2
            public_port = public.get_extra_info("sockname")[1]
            responses = []
            for payload in (b"one", b"two"):
                transport, client = await loop.create_datagram_endpoint(
                    Client, local_addr=("127.0.0.1", 0))
                clients.append(transport)
                client_protocols.append(client)
                client_addresses.append(transport.get_extra_info("sockname"))
                transport.sendto(payload, ("127.0.0.1", public_port))
                responses.append(await asyncio.wait_for(client.responses.get(), timeout=5))
            assert {item.split(b":", 1)[0] for item in responses} == {
                b"agent-a", b"agent-b",
            }
            assert {item.split(b":", 1)[1] for item in responses} == {b"one", b"two"}

            first_label = responses[0].split(b":", 1)[0]
            failed_agent_index = 0 if first_label == b"agent-a" else 1
            surviving_label = b"agent-b" if failed_agent_index == 0 else b"agent-a"
            first_address = client_addresses[0]
            first_session = state.public_udp.sessions[first_address]
            agents[failed_agent_index].cancel()
            await asyncio.gather(agents[failed_agent_index], return_exceptions=True)
            for _ in range(100):
                if len(state.agents) == 1:
                    break
                await asyncio.sleep(0.01)
            assert len(state.agents) == 1
            clients[0].sendto(b"after-disconnect", ("127.0.0.1", public_port))
            recovered = await asyncio.wait_for(
                client_protocols[0].responses.get(), timeout=5)
            assert recovered == surviving_label + b":after-disconnect"
            assert state.public_udp.sessions[first_address] is first_session
        finally:
            for client_transport in clients:
                client_transport.close()
            for agent in agents:
                agent.cancel()
            await asyncio.gather(*agents, return_exceptions=True)
            public.close()
            await state.public_udp.close()
            control.close()
            await asyncio.gather(control.wait_closed())
            for target_transport in target_transports:
                target_transport.close()
            handlers = tuple(state.handler_tasks)
            for task in handlers:
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)

    run(scenario())


def test_reverse_hub_bounds_registered_agents_at_configured_ceiling():
    class Writer:
        def __init__(self):
            self.data = bytearray()
            self.closed = False

        def write(self, data):
            self.data.extend(data)

        async def drain(self):
            pass

        def close(self):
            self.closed = True

        async def wait_closed(self):
            pass

    config = tunnel.ReverseHubConfig(
        "127.0.0.1", 1, "127.0.0.1", 2, Path("/unused.crt"),
        Path("/unused.key"), "reverse-agent-identity-secret-012345", "web",
    )
    state = reverse._HubState(config)
    state.agents = {
        f"agent-{index}": reverse._HubAgent(f"agent-{index}", Writer())
        for index in range(reverse.MAX_REVERSE_AGENTS)
    }
    writer = Writer()

    async def scenario():
        await reverse._hub_control(state, None, writer, {
            "op": "register", "version": 1, "route": "web",
            "secret": config.secret, "transport": "tcp",
        })
        assert json.loads(writer.data) == {"ok": False, "error": "agent_limit"}
        assert writer.closed
        assert len(state.agents) == reverse.MAX_REVERSE_AGENTS

    run(scenario())


def test_reverse_callbacks_require_agent_identity_when_route_has_multiple_agents():
    config = tunnel.ReverseHubConfig(
        "127.0.0.1", 1, "127.0.0.1", 2, Path("/unused.crt"),
        Path("/unused.key"), "reverse-agent-identity-secret-012345", "web",
    )
    state = reverse._HubState(config)
    state.agents["agent-a"] = reverse._HubAgent("agent-a", object())
    assert reverse._callback_agent_matches(state, "agent-a", {})
    assert reverse._callback_agent_matches(state, "agent-a", {"agent_id": "agent-a"})
    assert not reverse._callback_agent_matches(state, "agent-a", {"agent_id": None})
    state.agents["agent-b"] = reverse._HubAgent("agent-b", object())
    assert not reverse._callback_agent_matches(state, "agent-a", {})
    assert reverse._callback_agent_matches(state, "agent-a", {"agent_id": "agent-a"})
    assert not reverse._callback_agent_matches(state, "agent-a", {"agent_id": "agent-b"})


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


def test_reverse_udp_listener_rate_limits_and_expires_without_agent(tls_identity, monkeypatch):
    cert, key = tls_identity
    monkeypatch.setattr(reverse, "CONNECT_TIMEOUT", 0.05)
    monkeypatch.setattr(reverse, "UDP_RECONNECT_ATTEMPTS", 3)
    monkeypatch.setattr(reverse, "UDP_RECONNECT_BASE_DELAY", 0)
    config = tunnel.ReverseHubConfig(
        "127.0.0.1", 0, "127.0.0.1", 0, cert, key,
        "reverse-hub-shared-secret-0123456789", "dns", transport="udp",
        udp_rate_limit_pps=1,
    )

    async def scenario():
        control, public, state = await reverse._start_reverse_hub(config)
        client_transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=public.get_extra_info("sockname"))
        try:
            client_transport.sendto(b"first")
            client_transport.sendto(b"second")
            client_transport.sendto(b"third")
            for _ in range(100):
                if state.public_udp.sessions:
                    break
                await asyncio.sleep(0.005)
            assert len(state.public_udp.sessions) == 1
            session = next(iter(state.public_udp.sessions.values()))
            assert session.queue.qsize() == 2
            await asyncio.wait_for(session.task, timeout=1)
            assert not state.public_udp.sessions
            assert not state.pending_udp
        finally:
            client_transport.close()
            public.close()
            await state.public_udp.close()
            control.close()
            await control.wait_closed()

    run(scenario())


def test_reverse_hub_shutdown_closes_pending_public_connections(tls_identity, monkeypatch):
    cert, key = tls_identity
    config = tunnel.ReverseHubConfig(
        "127.0.0.1", 0, "127.0.0.1", 0, cert, key,
        "reverse-hub-shared-secret-0123456789", "web",
        routes=(
            tunnel.ReverseHubRoute("web", "127.0.0.1", 0),
            tunnel.ReverseHubRoute("admin", "127.0.0.1", 0),
        ),
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
            publics = state_holder["public"]
            states = state_holder["state"]
            port = publics["web"].sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            for _ in range(100):
                if states["web"].handler_tasks:
                    break
                await asyncio.sleep(0.01)
            assert states["web"].handler_tasks
            hub.cancel()
            await asyncio.gather(hub, return_exceptions=True)
            assert await asyncio.wait_for(reader.read(), timeout=1) == b""
            assert not states["web"].pending
            assert all(not listener.is_serving() for listener in publics.values())
            writer.close()
            await writer.wait_closed()
        finally:
            if not hub.done():
                hub.cancel()
                await asyncio.gather(hub, return_exceptions=True)

    run(scenario())


def test_reverse_agent_target_check_only_connects_to_the_fixed_local_target():
    async def scenario():
        connections = []

        async def target(reader, writer):
            connections.append(True)
            assert await reader.read() == b""
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(target, "127.0.0.1", 0)
        config = tunnel.ReverseAgentConfig(
            "unreachable-hub.invalid", 1, "unreachable-hub.invalid",
            Path("/not-used-by-local-target-check.pem"),
            "reverse-agent-check-secret-0123456789", "web", "127.0.0.1",
            server.sockets[0].getsockname()[1],
        )
        try:
            result = await tunnel.check_reverse_agent_target(config)
            assert "accepted TCP" in result
            assert connections == [True]
        finally:
            server.close()
            await server.wait_closed()

    run(scenario())


@pytest.mark.parametrize("carrier", ["tls", "wss", "h2"])
def test_reverse_agent_check_verifies_hub_without_registering_and_checks_target(
        tls_identity, carrier):
    cert, key = tls_identity
    secret = "reverse-agent-check-secret-0123456789"

    async def scenario():
        target_connections = []

        async def target(reader, writer):
            target_connections.append(True)
            assert await reader.read() == b""
            writer.close()
            await writer.wait_closed()

        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        hub_config = tunnel.ReverseHubConfig(
            "127.0.0.1", 0, "127.0.0.1", 0, cert, key, secret, "web",
            carrier=carrier,
        )
        control, public, state = await reverse._start_reverse_hub(hub_config)
        config = tunnel.ReverseAgentConfig(
            "127.0.0.1", control.sockets[0].getsockname()[1], "localhost", cert,
            secret, "web", "127.0.0.1", target_server.sockets[0].getsockname()[1],
            carrier=carrier,
        )
        try:
            result = await tunnel.check_reverse_agent(config)
            assert "Hub TLS and route authentication succeeded" in result
            assert "accepted TCP" in result
            assert "no application payload was sent" in result
            assert not state.agent_ready.is_set()
            assert target_connections == [True]

            wrong_secret = tunnel.ReverseAgentConfig(
                config.hub_host, config.hub_port, config.server_name, config.ca_file,
                "incorrect-reverse-agent-secret-012345", config.route,
                config.target_host, config.target_port, carrier=carrier,
            )
            with pytest.raises(ConnectionError, match="hub check failed"):
                await tunnel.check_reverse_agent(wrong_secret)
            assert not state.agent_ready.is_set()
            assert target_connections == [True]
        finally:
            control.close()
            public.close()
            target_server.close()
            await asyncio.gather(control.wait_closed(), public.wait_closed(),
                                 target_server.wait_closed())

    run(scenario())


def test_reverse_agent_udp_target_check_sends_no_payload():
    async def scenario():
        received = []

        class Observer(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                received.append(data)

        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            Observer, local_addr=("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        config = tunnel.ReverseAgentConfig(
            "unreachable-hub.invalid", 1, "unreachable-hub.invalid",
            Path("/unused-ca.crt"), "reverse-agent-udp-secret-0123456789",
            "dns", "127.0.0.1", port, transport="udp",
        )
        try:
            result = await tunnel.check_reverse_agent_target(config)
            assert "no datagram was sent" in result
            assert "reachability is unverified" in result
            await asyncio.sleep(0.05)
            assert received == []
        finally:
            transport.close()

    run(scenario())


def test_reverse_public_listener_applies_configured_source_ip_cap(tls_identity):
    cert, key = tls_identity
    config = tunnel.ReverseHubConfig(
        "127.0.0.1", 0, "127.0.0.1", 0, cert, key,
        "reverse-hub-shared-secret-0123456789", "web", client_session_limit=1,
    )

    async def scenario():
        control, public, state = await reverse._start_reverse_hub(config)
        port = public.sockets[0].getsockname()[1]
        first_reader, first_writer = await asyncio.open_connection("127.0.0.1", port)
        second_writer = None
        try:
            for _ in range(100):
                if state.pending:
                    break
                await asyncio.sleep(0.01)
            assert len(state.pending) == 1
            second_reader, second_writer = await asyncio.open_connection("127.0.0.1", port)
            assert await asyncio.wait_for(second_reader.read(), timeout=1) == b""
            assert len(state.pending) == 1
        finally:
            handlers = tuple(state.handler_tasks)
            for task in handlers:
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
            for writer in (first_writer, second_writer):
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()
            control.close()
            public.close()
            await asyncio.gather(control.wait_closed(), public.wait_closed())
        assert await asyncio.wait_for(first_reader.read(), timeout=1) == b""
        assert not state.pending

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


def test_entry_tcp_enforces_configured_client_session_limit(monkeypatch):
    config = tunnel.EntryConfig(
        "127.0.0.1", 0, "exit.example.net", 8443, "exit.example.net",
        Path("/tmp/unused-ca.crt"), "entry-session-limit-secret-012345", "web",
        client_session_limit=1,
    )
    original_start_server = asyncio.start_server
    capture = {}

    async def captured_start_server(handler, *args, **kwargs):
        server = await original_start_server(handler, *args, **kwargs)
        capture["server"] = server
        capture["started"].set()
        return server

    monkeypatch.setattr(tunnel.asyncio, "start_server", captured_start_server)

    async def scenario():
        capture["started"] = asyncio.Event()
        handler_started = asyncio.Event()
        release_handler = asyncio.Event()

        async def hold_entry(_config, _reader, writer, _meter):
            handler_started.set()
            try:
                await release_handler.wait()
            finally:
                writer.close()
                await writer.wait_closed()

        monkeypatch.setattr(tunnel, "_forward_entry", hold_entry)
        service = asyncio.create_task(tunnel.serve_entry(config))
        first_writer = second_writer = None
        try:
            await asyncio.wait_for(capture["started"].wait(), timeout=2)
            port = capture["server"].sockets[0].getsockname()[1]
            first_reader, first_writer = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.wait_for(handler_started.wait(), timeout=2)
            second_reader, second_writer = await asyncio.open_connection("127.0.0.1", port)
            assert await asyncio.wait_for(second_reader.read(), timeout=1) == b""
            release_handler.set()
            assert await asyncio.wait_for(first_reader.read(), timeout=1) == b""
        finally:
            release_handler.set()
            for writer in (first_writer, second_writer):
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()
            service.cancel()
            await asyncio.gather(service, return_exceptions=True)

    run(scenario())


def test_udp_entry_counts_sessions_per_source_ip_and_cleans_up():
    config = tunnel.EntryConfig(
        "127.0.0.1", 0, "exit.example.net", 8443, "exit.example.net",
        Path("/tmp/unused-ca.crt"), "entry-session-limit-secret-012345", "web",
        transport="udp", client_session_limit=1,
    )
    protocol = tunnel._EntryUdpProtocol(config)
    first = ("192.0.2.44", 3000)
    second = ("192.0.2.44", 3001)

    async def scenario():
        protocol.datagram_received(b"one", first)
        protocol.datagram_received(b"two", second)
        assert set(protocol.sessions) == {first}
        assert protocol.peer_sessions == {"192.0.2.44": 1}
        await protocol.close()
        assert not protocol.sessions
        assert not protocol.peer_sessions

    run(scenario())


def test_entry_client_session_limit_is_bounded_and_backward_compatible(tmp_path):
    entry = {
        "role": "entry", "listen_host": "127.0.0.1", "listen_port": 18080,
        "exit_host": "exit.example.net", "exit_port": 8443,
        "server_name": "exit.example.net", "ca_file": "/tmp/ca.crt",
        "secret": "entry-session-limit-secret-012345", "route": "web",
    }
    path = tmp_path / "entry.json"
    path.write_text(json.dumps(entry))
    assert tunnel.load_config(path).client_session_limit == 128
    for value in (1, 512):
        entry["client_session_limit"] = value
        path.write_text(json.dumps(entry))
        assert tunnel.load_config(path).client_session_limit == value
    for value in (0, 513, True, 1.5):
        entry["client_session_limit"] = value
        path.write_text(json.dumps(entry))
        with pytest.raises(tunnel.ConfigError, match="client_session_limit"):
            tunnel.load_config(path)


def test_reverse_configs_load_and_reject_invalid_ports(tmp_path):
    hub = {
        "role": "reverse-hub", "control_host": "0.0.0.0", "control_port": 9443,
        "public_host": "127.0.0.1", "public_port": 8080,
        "cert_file": "/tmp/hub.crt", "key_file": "/tmp/hub.key",
        "secret": "reverse-hub-secret-0123456789012345", "route": "web",
        "client_session_limit": 73,
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
    loaded_hub = tunnel.load_config(hub_path)
    assert isinstance(loaded_hub, tunnel.ReverseHubConfig)
    assert loaded_hub.client_session_limit == 73
    loaded_agent = tunnel.load_config(agent_path)
    assert isinstance(loaded_agent, tunnel.ReverseAgentConfig)
    assert loaded_hub.transport == loaded_agent.transport == "tcp"
    hub["transport"] = "udp"
    hub["udp_rate_limit_pps"] = 2400
    agent["transport"] = "udp"
    hub_path.write_text(json.dumps(hub))
    agent_path.write_text(json.dumps(agent))
    loaded_hub = tunnel.load_config(hub_path)
    loaded_agent = tunnel.load_config(agent_path)
    assert loaded_hub.transport == loaded_agent.transport == "udp"
    assert loaded_hub.udp_rate_limit_pps == 2400
    hub["transport"] = "icmp"
    hub_path.write_text(json.dumps(hub))
    with pytest.raises(tunnel.ConfigError, match="reverse transport"):
        tunnel.load_config(hub_path)
    hub["transport"] = "udp"
    hub["control_port"] = 0
    hub_path.write_text(json.dumps(hub))
    with pytest.raises(tunnel.ConfigError, match="control_port"):
        tunnel.load_config(hub_path)
    hub["control_port"] = hub["public_port"]
    hub_path.write_text(json.dumps(hub))
    with pytest.raises(tunnel.ConfigError, match="must differ"):
        tunnel.load_config(hub_path)

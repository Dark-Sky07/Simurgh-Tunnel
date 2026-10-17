"""Dialing the exit *through* an upstream proxy, proved against real proxies.

A tiny SOCKS5 server and a tiny HTTP CONNECT server live in this file: the
tunnel then really has to speak the protocol (greeting, choice, request,
reply, CONNECT) and the bytes that come out the other end are compared.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from conftest import free_port
from simurgh import carriers
from simurgh.proxyout import ProxyError, open_through_proxy, parse_proxy

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------- the proxies


class Socks5Server:
    """Just enough SOCKS5 for a CONNECT: no auth, or user/pass."""

    def __init__(self, user: str = "", password: str = ""):
        self.user, self.password = user, password
        self.seen: list[tuple] = []
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            version, count = struct.unpack(">BB", await reader.readexactly(2))
            assert version == 5
            methods = set(await reader.readexactly(count))
            if self.user:
                if 2 not in methods:
                    writer.write(b"\x05\xff")
                    await writer.drain()
                    return
                writer.write(b"\x05\x02")
                await writer.drain()
                ver = await reader.readexactly(1)
                assert ver == b"\x01"
                ulen = (await reader.readexactly(1))[0]
                user = (await reader.readexactly(ulen)).decode()
                plen = (await reader.readexactly(1))[0]
                password = (await reader.readexactly(plen)).decode()
                self.seen.append(("auth", user, password))
                if (user, password) != (self.user, self.password):
                    writer.write(b"\x01\x01")
                    await writer.drain()
                    return
                writer.write(b"\x01\x00")
            else:
                writer.write(b"\x05\x00")
            await writer.drain()

            head = await reader.readexactly(4)
            ver, cmd, _rsv, atyp = head
            assert ver == 5 and cmd == 1
            if atyp == 1:
                host = ".".join(str(b) for b in await reader.readexactly(4))
            elif atyp == 3:
                length = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(length)).decode()
            else:
                host = "::" + ":".join(
                    "%x" % part for part in struct.unpack(">8H", await reader.readexactly(16)))
            port = struct.unpack(">H", await reader.readexactly(2))[0]
            self.seen.append(("connect", host, port))
            target = await asyncio.open_connection(host, port)
            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            await writer.drain()
            await _pump(reader, writer, target)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            writer.close()


class HttpProxyServer:
    """An HTTP CONNECT proxy, with optional Basic auth."""

    def __init__(self, user: str = "", password: str = ""):
        self.user, self.password = user, password
        self.seen: list[tuple] = []
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _version = lines[0].split(" ", 2)
            assert method == "CONNECT"
            if self.user:
                import base64

                want = base64.b64encode(
                    f"{self.user}:{self.password}".encode()).decode()
                got = ""
                for line in lines[1:]:
                    if line.lower().startswith("proxy-authorization:"):
                        got = line.split(" ", 1)[1].strip()
                self.seen.append(("auth", got))
                if got != f"Basic {want}":
                    writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                                 b"Proxy-Authenticate: Basic\r\n\r\n")
                    await writer.drain()
                    return
            host, _, port = target.rpartition(":")
            self.seen.append(("connect", host, int(port)))
            stream = await asyncio.open_connection(host, int(port))
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await _pump(reader, writer, stream)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError):
            pass
        finally:
            writer.close()


async def _pump(reader, writer, target) -> None:
    """Two-way copy between the client and the real destination."""
    far_reader, far_writer = target

    async def one_way(src, dst):
        try:
            while True:
                data = await src.read(65536)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                dst.write_eof()
            except Exception:
                pass

    await asyncio.gather(one_way(reader, far_writer), one_way(far_reader, writer))


async def _echo_server():
    async def handler(reader, writer):
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(b"echo:" + data)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


# ------------------------------------------------------------------- parsing


def test_proxy_urls_are_understood():
    info = parse_proxy("socks5://user:p%40ss@127.0.0.1:1080")
    assert info["scheme"] == "socks5"
    assert info["host"] == "127.0.0.1" and info["port"] == 1080
    assert info["user"] == "user" and info["password"] == "p@ss"
    http = parse_proxy("http://proxy.local:3128")
    assert http["scheme"] == "http" and http["port"] == 3128
    # the default port comes from the scheme, and a bare host:port is socks5
    assert parse_proxy("socks5://127.0.0.1")["port"] == 1080
    assert parse_proxy("127.0.0.1:1081")["scheme"] == "socks5" 


@pytest.mark.parametrize("bad", ["", "ftp://127.0.0.1:21", "socks5://:1080", "://x"])
def test_a_broken_proxy_url_is_rejected(bad):
    with pytest.raises(ProxyError):
        parse_proxy(bad)


# ------------------------------------------------------------------- dialing


async def test_socks5_carries_a_connection():
    echo, port = await _echo_server()
    proxy = Socks5Server()
    await proxy.start()
    try:
        conn = await open_through_proxy(
            f"socks5://127.0.0.1:{proxy.port}", "127.0.0.1", port, timeout=5)
        conn.write(b"hello")
        await conn.drain()
        assert await conn.read_exactly(10) == b"echo:hello"
        assert ("connect", "127.0.0.1", port) in proxy.seen
        conn.close()
    finally:
        await proxy.stop()
        echo.close()


async def test_socks5_with_credentials():
    echo, port = await _echo_server()
    proxy = Socks5Server(user="mehdi", password="s3cret")
    await proxy.start()
    try:
        conn = await open_through_proxy(
            f"socks5://mehdi:s3cret@127.0.0.1:{proxy.port}", "127.0.0.1", port,
            timeout=5)
        conn.write(b"x")
        await conn.drain()
        assert await conn.read_exactly(6) == b"echo:x"
        assert ("auth", "mehdi", "s3cret") in proxy.seen
        conn.close()
        with pytest.raises(ConnectionError):
            await open_through_proxy(
                f"socks5://mehdi:wrong@127.0.0.1:{proxy.port}", "127.0.0.1", port,
                timeout=5)
    finally:
        await proxy.stop()
        echo.close()


async def test_http_connect_carries_a_connection():
    echo, port = await _echo_server()
    proxy = HttpProxyServer(user="mehdi", password="s3cret")
    await proxy.start()
    try:
        conn = await open_through_proxy(
            f"http://mehdi:s3cret@127.0.0.1:{proxy.port}", "127.0.0.1", port,
            timeout=5)
        conn.write(b"hi")
        await conn.drain()
        assert await conn.read_exactly(7) == b"echo:hi"
        assert ("connect", "127.0.0.1", port) in proxy.seen
        conn.close()
    finally:
        await proxy.stop()
        echo.close()


async def test_a_real_carrier_handshake_goes_through_the_proxy():
    """A whole `plain` tunnel handshake, dialled through the SOCKS5 proxy."""
    from simurgh.carriers import ServerCarrier, create_tcp_server

    accepted = asyncio.Event()

    async def on_channel(channel):
        accepted.set()

    carrier = ServerCarrier("plain", "token")
    loop = asyncio.get_running_loop()
    server = await create_tcp_server(loop, carrier.protocol_factory(on_channel),
                                     "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    proxy = Socks5Server()
    await proxy.start()
    try:
        channel = await carriers.client_connect(
            "plain", "127.0.0.1", port, "token", connect_timeout=5,
            proxy=f"socks5://127.0.0.1:{proxy.port}",
            net={"nodelay": True, "keepalive": 0, "so_mark": 0, "prefer": "ipv4"},
        )
        assert channel is not None
        assert ("connect", "127.0.0.1", port) in proxy.seen
        await asyncio.wait_for(accepted.wait(), 5)
        channel.write_frame(b"ping")
        await channel.drain()
        await channel.close()
    finally:
        await proxy.stop()
        server.close()


async def test_a_proxy_that_says_no_fails_the_dial():
    proxy = Socks5Server(user="mehdi", password="right")
    await proxy.start()
    try:
        with pytest.raises((ConnectionError, asyncio.TimeoutError)):
            await open_through_proxy(
                f"socks5://mehdi:wrong@127.0.0.1:{proxy.port}", "127.0.0.1",
                free_port(), timeout=3)
    finally:
        await proxy.stop()

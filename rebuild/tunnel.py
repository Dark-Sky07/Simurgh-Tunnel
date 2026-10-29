"""Independent Simurgh Tunnel rebuild, milestone 1: authenticated TLS TCP forwarding."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import ssl
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOG = logging.getLogger("simurgh.rebuild")
VERSION = "0.1.0"
MAX_HANDSHAKE = 8192
MAX_FRAME = 64 * 1024
HANDSHAKE_TIMEOUT = 5.0
TLS_HANDSHAKE_TIMEOUT = 10.0
CONNECT_TIMEOUT = 10.0
MAX_TCP_SESSIONS = 1024
MAX_UDP_ASSOCIATIONS = 1024
_FRAME_HEADER = struct.Struct("!BI")
_DATA = 1
_FIN = 2
_UDP_DATA = 3


class ConfigError(ValueError):
    """A configuration is missing a required value or has an invalid type."""


class ProtocolError(ConnectionError):
    """A peer sent a malformed or unsupported frame."""


@dataclass(frozen=True)
class ExitConfig:
    host: str
    port: int
    cert_file: Path
    key_file: Path
    secret: str
    routes: dict[str, tuple[str, int]]


@dataclass(frozen=True)
class EntryConfig:
    host: str
    port: int
    exit_host: str
    exit_port: int
    server_name: str
    ca_file: Path
    secret: str
    route: str
    transport: str = "tcp"


def _text(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string")
    return value.strip()


def _port(obj: dict[str, Any], key: str) -> int:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConfigError(f"{key} must be an integer from 1 to 65535")
    return value


def load_config(path: Path) -> ExitConfig | EntryConfig:
    """Read a JSON config. It is read-only; this function never edits it."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read JSON configuration {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("the top-level configuration must be a JSON object")
    role = raw.get("role")
    if role not in ("entry", "exit"):
        raise ConfigError("role must be 'entry' or 'exit'")
    host = _text(raw, "listen_host")
    port = _port(raw, "listen_port")
    secret = _text(raw, "secret")
    if len(secret.encode("utf-8")) < 32:
        raise ConfigError("secret must contain at least 32 UTF-8 bytes")

    if role == "exit":
        raw_routes = raw.get("routes")
        if not isinstance(raw_routes, dict) or not raw_routes:
            raise ConfigError("exit routes must be a non-empty object")
        routes: dict[str, tuple[str, int]] = {}
        for name, target in raw_routes.items():
            if (not isinstance(name, str) or not name or len(name) > 64
                    or not isinstance(target, dict)):
                raise ConfigError("each exit route needs a short name and host/port object")
            routes[name] = (_text(target, "host"), _port(target, "port"))
        return ExitConfig(
            host=host,
            port=port,
            cert_file=Path(_text(raw, "cert_file")),
            key_file=Path(_text(raw, "key_file")),
            secret=secret,
            routes=routes,
        )

    transport = raw.get("transport", "tcp")
    if transport not in ("tcp", "udp"):
        raise ConfigError("entry transport must be 'tcp' or 'udp'")
    return EntryConfig(
        host=host,
        port=port,
        exit_host=_text(raw, "exit_host"),
        exit_port=_port(raw, "exit_port"),
        server_name=_text(raw, "server_name"),
        ca_file=Path(_text(raw, "ca_file")),
        secret=secret,
        route=_text(raw, "route"),
        transport=transport,
    )


def _server_ssl(config: ExitConfig) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(config.cert_file), str(config.key_file))
    return context


def _client_ssl(config: EntryConfig) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(config.ca_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


async def _send_json(writer: asyncio.StreamWriter, value: dict[str, Any]) -> None:
    writer.write(json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n")
    await writer.drain()


async def _read_json(reader: asyncio.StreamReader) -> dict[str, Any]:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=HANDSHAKE_TIMEOUT)
    except (TimeoutError, asyncio.LimitOverrunError, ValueError) as exc:
        raise ProtocolError("handshake timed out or exceeded the stream limit") from exc
    if not line or len(line) > MAX_HANDSHAKE or not line.endswith(b"\n"):
        raise ProtocolError("missing or oversized handshake")
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError("handshake is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ProtocolError("handshake must be a JSON object")
    return data


async def _write_frame(writer: asyncio.StreamWriter, kind: int, payload: bytes = b"") -> None:
    if kind == _DATA:
        if not payload or len(payload) > MAX_FRAME:
            raise ProtocolError("invalid TCP data frame length")
    elif kind == _UDP_DATA:
        if not payload or len(payload) > MAX_UDP_DATAGRAM:
            raise ProtocolError("invalid UDP datagram frame length")
    elif kind == _FIN:
        if payload:
            raise ProtocolError("FIN frame must be empty")
    else:
        raise ProtocolError("unknown local frame type")
    writer.write(_FRAME_HEADER.pack(kind, len(payload)) + payload)
    await writer.drain()


async def _read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    try:
        header = await reader.readexactly(_FRAME_HEADER.size)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            raise EOFError from exc
        raise ProtocolError("truncated frame header") from exc
    kind, size = _FRAME_HEADER.unpack(header)
    if kind == _DATA and not 1 <= size <= MAX_FRAME:
        raise ProtocolError("invalid TCP data frame length")
    if kind == _UDP_DATA and not 1 <= size <= MAX_UDP_DATAGRAM:
        raise ProtocolError("invalid UDP datagram frame length")
    if kind in (_DATA, _UDP_DATA):
        try:
            return kind, await reader.readexactly(size)
        except asyncio.IncompleteReadError as exc:
            raise ProtocolError("truncated data frame") from exc
    if kind == _FIN and size == 0:
        return kind, b""
    raise ProtocolError("unknown frame type or invalid frame length")


async def _copy_to_tunnel(reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
    while True:
        chunk = await reader.read(MAX_FRAME)
        if not chunk:
            await _write_frame(writer, _FIN)
            return
        await _write_frame(writer, _DATA, chunk)


async def _copy_from_tunnel(reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
    while True:
        kind, payload = await _read_frame(reader)
        if kind == _FIN:
            if writer.can_write_eof():
                writer.write_eof()
                await writer.drain()
            return
        if kind != _DATA:
            raise ProtocolError("UDP frame received on a TCP flow")
        writer.write(payload)
        await writer.drain()


async def _wait_for_tasks(coroutines, *, first_completed: bool) -> None:
    tasks = [asyncio.create_task(coro) for coro in coroutines]
    try:
        mode = asyncio.FIRST_COMPLETED if first_completed else asyncio.FIRST_EXCEPTION
        done, pending = await asyncio.wait(tasks, return_when=mode)
        failed = any(task.exception() is not None for task in done if not task.cancelled())
        if first_completed or failed:
            for task in pending:
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failure = next((result for result in results
                        if isinstance(result, BaseException)
                        and not isinstance(result, asyncio.CancelledError)), None)
        if failure is not None:
            raise failure
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _join_directions(*coroutines) -> None:
    await _wait_for_tasks(coroutines, first_completed=False)


async def _stop_when_either_finishes(*coroutines) -> None:
    """Run UDP directions until either side ends, then cancel its peer."""
    await _wait_for_tasks(coroutines, first_completed=True)


class _RemoteDatagrams(asyncio.DatagramProtocol):
    def __init__(self, limit: int = 128):
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=limit)

    def datagram_received(self, data: bytes, addr) -> None:
        if not data:
            LOG.debug("dropping empty UDP response")
            return
        if self.queue.full():
            LOG.debug("dropping UDP response because its receive queue is full")
            return
        self.queue.put_nowait(data)

    def error_received(self, exc: OSError) -> None:
        LOG.debug("remote UDP socket error: %s", exc)


async def _remote_udp_session(reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter,
                              target: tuple[str, int]) -> None:
    remote_protocol = _RemoteDatagrams()
    transport = None
    try:
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: remote_protocol, remote_addr=target)
        await _send_json(writer, {"ok": True})

        async def tunnel_to_udp() -> None:
            while True:
                kind, payload = await _read_frame(reader)
                if kind == _FIN:
                    return
                if kind != _UDP_DATA:
                    raise ProtocolError("TCP frame received on a UDP association")
                transport.sendto(payload)

        async def udp_to_tunnel() -> None:
            while True:
                payload = await remote_protocol.queue.get()
                await _write_frame(writer, _UDP_DATA, payload)

        await _stop_when_either_finishes(tunnel_to_udp(), udp_to_tunnel())
    finally:
        if transport is not None:
            transport.close()


async def _open_exit_route(config: ExitConfig, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter) -> None:
    remote_writer: asyncio.StreamWriter | None = None
    try:
        hello = await _read_json(reader)
        route_name = hello.get("route")
        peer_secret = hello.get("secret")
        operation = hello.get("op")
        valid = (type(hello.get("version")) is int and hello.get("version") == 1
                 and operation in ("open", "udp") and isinstance(peer_secret, str)
                 and hmac.compare_digest(peer_secret.encode("utf-8"),
                                         config.secret.encode("utf-8"))
                 and isinstance(route_name, str) and route_name in config.routes)
        if not valid:
            await _send_json(writer, {"ok": False, "error": "denied"})
            return
        target_host, target_port = config.routes[route_name]
        if operation == "udp":
            await _remote_udp_session(reader, writer, (target_host, target_port))
            return
        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(target_host, target_port), timeout=CONNECT_TIMEOUT)
        except (TimeoutError, OSError):
            await _send_json(writer, {"ok": False, "error": "target_unavailable"})
            return
        await _send_json(writer, {"ok": True})
        await _join_directions(
            _copy_from_tunnel(reader, remote_writer),
            _copy_to_tunnel(remote_reader, writer),
        )
    except (TimeoutError, ProtocolError, EOFError, OSError, ssl.SSLError) as exc:
        LOG.debug("closing invalid or interrupted tunnel session: %s", exc)
    finally:
        if remote_writer is not None:
            remote_writer.close()
            try:
                await remote_writer.wait_closed()
            except OSError:
                pass
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def _forward_entry(config: EntryConfig, local_reader: asyncio.StreamReader,
                         local_writer: asyncio.StreamWriter) -> None:
    remote_writer: asyncio.StreamWriter | None = None
    try:
        remote_reader, remote_writer = await asyncio.wait_for(
            asyncio.open_connection(
                config.exit_host,
                config.exit_port,
                ssl=_client_ssl(config),
                server_hostname=config.server_name,
            ),
            timeout=CONNECT_TIMEOUT,
        )
        await _send_json(remote_writer, {
            "version": 1,
            "op": "open",
            "route": config.route,
            "secret": config.secret,
        })
        response = await _read_json(remote_reader)
        if response.get("ok") is not True:
            LOG.warning("exit declined route %r (%s)", config.route,
                        response.get("error", "unknown error"))
            return
        await _join_directions(
            _copy_to_tunnel(local_reader, remote_writer),
            _copy_from_tunnel(remote_reader, local_writer),
        )
    except (TimeoutError, OSError, ssl.SSLError, ProtocolError, EOFError) as exc:
        LOG.warning("tunnel flow to %s:%s failed: %s", config.exit_host,
                    config.exit_port, exc)
    finally:
        local_writer.close()
        try:
            await local_writer.wait_closed()
        except OSError:
            pass
        if remote_writer is not None:
            remote_writer.close()
            try:
                await remote_writer.wait_closed()
            except OSError:
                pass


UDP_IDLE_TIMEOUT = 45.0
UDP_QUEUE_LIMIT = 128
MAX_UDP_DATAGRAM = 65507


class _EntryUdpSession:
    def __init__(self, owner: _EntryUdpProtocol, client):
        self.owner = owner
        self.client = client
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=UDP_QUEUE_LIMIT)
        self.task = asyncio.create_task(self.run())

    def enqueue(self, payload: bytes) -> None:
        if not payload:
            LOG.debug("dropping empty UDP datagram from %r", self.client)
            return
        if len(payload) > MAX_UDP_DATAGRAM:
            LOG.debug("dropping oversized UDP datagram from %r", self.client)
            return
        if self.queue.full():
            LOG.debug("dropping UDP datagram from %r because its send queue is full",
                      self.client)
            return
        self.queue.put_nowait(payload)

    async def run(self) -> None:
        remote_writer: asyncio.StreamWriter | None = None
        try:
            config = self.owner.config
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    config.exit_host,
                    config.exit_port,
                    ssl=_client_ssl(config),
                    server_hostname=config.server_name,
                ),
                timeout=CONNECT_TIMEOUT,
            )
            await _send_json(remote_writer, {
                "version": 1, "op": "udp", "route": config.route,
                "secret": config.secret,
            })
            response = await _read_json(remote_reader)
            if response.get("ok") is not True:
                LOG.warning("exit declined UDP route %r (%s)", config.route,
                            response.get("error", "unknown error"))
                return

            async def send_datagrams() -> None:
                while True:
                    try:
                        payload = await asyncio.wait_for(
                            self.queue.get(), timeout=UDP_IDLE_TIMEOUT)
                    except TimeoutError:
                        await _write_frame(remote_writer, _FIN)
                        return
                    await _write_frame(remote_writer, _UDP_DATA, payload)

            async def receive_datagrams() -> None:
                while True:
                    kind, payload = await _read_frame(remote_reader)
                    if kind == _FIN:
                        return
                    if kind != _UDP_DATA:
                        raise ProtocolError("TCP frame received on a UDP association")
                    transport = self.owner.transport
                    if transport is not None:
                        transport.sendto(payload, self.client)

            await _stop_when_either_finishes(send_datagrams(), receive_datagrams())
        except (TimeoutError, OSError, ssl.SSLError, ProtocolError, EOFError) as exc:
            LOG.debug("UDP tunnel for %r ended: %s", self.client, exc)
        finally:
            if remote_writer is not None:
                remote_writer.close()
                try:
                    await remote_writer.wait_closed()
                except OSError:
                    pass
            if self.owner.sessions.get(self.client) is self:
                del self.owner.sessions[self.client]


class _EntryUdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, config: EntryConfig):
        self.config = config
        self.transport: asyncio.DatagramTransport | None = None
        self.sessions: dict[tuple, _EntryUdpSession] = {}

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        session = self.sessions.get(addr)
        if session is None:
            if len(self.sessions) >= MAX_UDP_ASSOCIATIONS:
                LOG.warning("dropping UDP client %r: association limit reached", addr)
                return
            session = _EntryUdpSession(self, addr)
            self.sessions[addr] = session
        session.enqueue(data)

    def error_received(self, exc: OSError) -> None:
        LOG.warning("local UDP listener error: %s", exc)

    async def close(self) -> None:
        sessions = list(self.sessions.values())
        for session in sessions:
            session.task.cancel()
        if sessions:
            await asyncio.gather(*(session.task for session in sessions),
                                 return_exceptions=True)
        self.sessions.clear()


def _limited_handler(handler, active: list[int], limit: int):
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if active[0] >= limit:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return
        active[0] += 1
        try:
            await handler(reader, writer)
        finally:
            active[0] -= 1
    return serve


async def serve_exit(config: ExitConfig) -> None:
    active = [0]
    server = await asyncio.start_server(
        _limited_handler(lambda reader, writer: _open_exit_route(config, reader, writer),
                         active, MAX_TCP_SESSIONS),
        config.host,
        config.port,
        ssl=_server_ssl(config),
        ssl_handshake_timeout=TLS_HANDSHAKE_TIMEOUT,
        limit=MAX_HANDSHAKE,
    )
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or ())
    LOG.info("exit listening on %s; %d fixed route(s)", addresses, len(config.routes))
    async with server:
        await server.serve_forever()


async def serve_entry(config: EntryConfig) -> None:
    if config.transport == "udp":
        protocol = _EntryUdpProtocol(config)
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: protocol, local_addr=(config.host, config.port))
        addresses = str(transport.get_extra_info("sockname") or (config.host, config.port))
        LOG.info("UDP entry listening on %s -> %s:%s (%s)", addresses,
                 config.exit_host, config.exit_port, config.route)
        try:
            await asyncio.Future()
        finally:
            transport.close()
            await protocol.close()
        return

    active = [0]
    server = await asyncio.start_server(
        _limited_handler(lambda reader, writer: _forward_entry(config, reader, writer),
                         active, MAX_TCP_SESSIONS),
        config.host,
        config.port,
        limit=MAX_HANDSHAKE,
    )
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or ())
    LOG.info("TCP entry listening on %s -> %s:%s (%s)", addresses,
             config.exit_host, config.exit_port, config.route)
    async with server:
        await server.serve_forever()


def _check_permissions(path: Path) -> None:
    if os.name == "posix":
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            return
        if mode & 0o077:
            LOG.warning("%s is readable by group/other; protect secrets with chmod 600", path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent Simurgh TCP tunnel (rebuild milestone 1)")
    parser.add_argument("--version", action="version", version=f"Simurgh rebuild {VERSION}")
    parser.add_argument("--config", type=Path, required=True, help="path to a JSON node config")
    parser.add_argument("--log-level", choices=("debug", "info", "warning", "error"), default="info")
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper()),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config(args.config)
        _check_permissions(args.config)
        asyncio.run(serve_exit(config) if isinstance(config, ExitConfig)
                    else serve_entry(config))
    except (ConfigError, OSError, ssl.SSLError, KeyboardInterrupt) as exc:
        if not isinstance(exc, KeyboardInterrupt):
            LOG.error("startup failed: %s", exc)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

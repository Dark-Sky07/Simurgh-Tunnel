"""Independent Simurgh Tunnel protocol with TLS/WSS carriers and TCP/UDP forwarding."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import ssl
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOG = logging.getLogger("simurgh.rebuild")
VERSION = "0.2.0"
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
    carrier: str = "tls"


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
    carrier: str = "tls"


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
        carrier = raw.get("carrier", "tls")
        if carrier not in ("tls", "wss"):
            raise ConfigError("carrier must be 'tls' or 'wss'")
        return ExitConfig(
            host=host,
            port=port,
            cert_file=Path(_text(raw, "cert_file")),
            key_file=Path(_text(raw, "key_file")),
            secret=secret,
            routes=routes,
            carrier=carrier,
        )

    transport = raw.get("transport", "tcp")
    if transport not in ("tcp", "udp"):
        raise ConfigError("entry transport must be 'tcp' or 'udp'")
    carrier = raw.get("carrier", "tls")
    if carrier not in ("tls", "wss"):
        raise ConfigError("carrier must be 'tls' or 'wss'")
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
        carrier=carrier,
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


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WS_PATH = "/simurgh-tunnel"
_WS_MAX_FRAME = MAX_FRAME + _FRAME_HEADER.size
MAX_WS_BUFFER = 256 * 1024
_WS_HEADER_LIMIT = 8192


def _ws_accept(key: str) -> str:
    digest = hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


async def _read_http_headers(reader: asyncio.StreamReader) -> tuple[str, dict[str, str]]:
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=HANDSHAKE_TIMEOUT)
    except (TimeoutError, asyncio.LimitOverrunError, asyncio.IncompleteReadError) as exc:
        raise ProtocolError("WebSocket HTTP handshake timed out or exceeded its header limit") from exc
    if len(raw) > _WS_HEADER_LIMIT:
        raise ProtocolError("WebSocket HTTP headers are oversized")
    try:
        lines = raw[:-4].decode("ascii").split("\r\n")
    except UnicodeDecodeError as exc:
        raise ProtocolError("WebSocket HTTP headers must be ASCII") from exc
    start = lines[0] if lines else ""
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if not line or line[0] in " \t" or ":" not in line:
            raise ProtocolError("malformed WebSocket HTTP header")
        name, value = line.split(":", 1)
        key = name.strip().lower()
        if not key or key in fields:
            raise ProtocolError("duplicate or empty WebSocket HTTP header")
        fields[key] = value.strip()
    return start, fields


async def _server_websocket(reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> _WebSocketStream:
    request, fields = await _read_http_headers(reader)
    LOG.debug("WSS server request: %s", request)
    if request != f"GET {_WS_PATH} HTTP/1.1":
        raise ProtocolError("unsupported WebSocket request path")
    if not fields.get("host"):
        raise ProtocolError("missing WebSocket Host header")
    if fields.get("content-length", "0") != "0" or "transfer-encoding" in fields:
        raise ProtocolError("WebSocket upgrade request must not have a body")
    if fields.get("upgrade", "").lower() != "websocket":
        raise ProtocolError("missing WebSocket upgrade")
    if "upgrade" not in {part.strip().lower() for part in fields.get("connection", "").split(",")}:
        raise ProtocolError("missing Connection: Upgrade")
    if fields.get("sec-websocket-version") != "13":
        raise ProtocolError("unsupported WebSocket version")
    key = fields.get("sec-websocket-key", "")
    try:
        decoded_key = base64.b64decode(key, validate=True)
    except (ValueError, TypeError) as exc:
        raise ProtocolError("invalid WebSocket key") from exc
    if len(decoded_key) != 16:
        raise ProtocolError("invalid WebSocket key length")
    response = ("HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {_ws_accept(key)}\r\n\r\n")
    writer.write(response.encode("ascii"))
    await writer.drain()
    return _WebSocketStream(reader, writer, client=False)


async def _client_websocket(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                            host: str, port: int) -> _WebSocketStream:
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    host_header = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    request = (f"GET {_WS_PATH} HTTP/1.1\r\nHost: {host_header}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
    writer.write(request.encode("ascii"))
    await writer.drain()
    response, fields = await _read_http_headers(reader)
    LOG.debug("WSS client response: %s", response)
    if response != "HTTP/1.1 101 Switching Protocols":
        raise ProtocolError("exit refused the WebSocket upgrade")
    if fields.get("upgrade", "").lower() != "websocket":
        raise ProtocolError("exit returned an invalid WebSocket upgrade")
    if "upgrade" not in {part.strip().lower() for part in fields.get("connection", "").split(",")}:
        raise ProtocolError("exit returned an invalid Connection header")
    if not hmac.compare_digest(fields.get("sec-websocket-accept", ""), _ws_accept(key)):
        raise ProtocolError("exit returned an invalid WebSocket accept value")
    return _WebSocketStream(reader, writer, client=True)


class _WebSocketStream:
    """Expose ordered WebSocket binary-message payloads as an async byte stream."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 *, client: bool):
        self._base_reader = reader
        self._base_writer = writer
        self._client = client
        self._read_buffer = bytearray()
        self._read_condition = asyncio.Condition()
        self._read_eof = False
        self._read_error: BaseException | None = None
        self._closed = False
        self._write_lock = asyncio.Lock()
        self._write_buffer = bytearray()
        self._receiver = asyncio.create_task(self._receive())

    async def _send(self, opcode: int, payload: bytes) -> None:
        if len(payload) > _WS_MAX_FRAME:
            raise ProtocolError("WebSocket output frame exceeds the size limit")
        mask = self._client
        first = 0x80 | opcode
        size = len(payload)
        if size < 126:
            header = bytes((first, (0x80 if mask else 0) | size))
        elif size <= 0xFFFF:
            header = bytes((first, (0x80 if mask else 0) | 126)) + struct.pack("!H", size)
        else:
            header = bytes((first, (0x80 if mask else 0) | 127)) + struct.pack("!Q", size)
        if mask:
            mask_key = secrets.token_bytes(4)
            payload = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
            header += mask_key
        self._base_writer.write(header + payload)
        LOG.debug("WSS send frame: %d bytes opcode=%d client=%s", size, opcode, self._client)
        await self._base_writer.drain()

    async def _receive(self) -> None:
        try:
            while True:
                first, second = await self._base_reader.readexactly(2)
                fin = bool(first & 0x80)
                if first & 0x70:
                    raise ProtocolError("WebSocket extensions are not negotiated")
                opcode = first & 0x0F
                masked = bool(second & 0x80)
                if masked != (not self._client):
                    raise ProtocolError("invalid WebSocket masking direction")
                size = second & 0x7F
                if size == 126:
                    size = struct.unpack("!H", await self._base_reader.readexactly(2))[0]
                    if size < 126:
                        raise ProtocolError("non-canonical WebSocket frame length")
                elif size == 127:
                    size = struct.unpack("!Q", await self._base_reader.readexactly(8))[0]
                    if size < 65536 or size >> 63:
                        raise ProtocolError("invalid WebSocket frame length")
                if size > _WS_MAX_FRAME:
                    raise ProtocolError("WebSocket frame exceeds the size limit")
                if opcode >= 0x8 and (not fin or size > 125):
                    raise ProtocolError("invalid WebSocket control frame")
                mask_key = await self._base_reader.readexactly(4) if masked else b""
                payload = await self._base_reader.readexactly(size)
                if masked:
                    payload = bytes(byte ^ mask_key[index % 4]
                                    for index, byte in enumerate(payload))
                if opcode == 0x8:
                    if not self._closed:
                        await self._send(0x8, payload[:125])
                    return
                if opcode == 0x9:
                    await self._send(0xA, payload)
                    continue
                if opcode == 0xA:
                    continue
                if opcode != 0x2 or not fin:
                    raise ProtocolError("only complete binary WebSocket messages are supported")
                LOG.debug("WSS received binary message: %d bytes (client=%s)", size, self._client)
                async with self._read_condition:
                    payload_size = len(payload)
                    await self._read_condition.wait_for(
                        lambda size=payload_size: len(self._read_buffer) + size <= MAX_WS_BUFFER
                        or self._closed)
                    if self._closed:
                        return
                    self._read_buffer.extend(payload)
                    self._read_condition.notify_all()
        except asyncio.IncompleteReadError:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - propagate receiver failures to stream readers
            LOG.debug("WSS receiver error: %s", exc)
            self._read_error = exc
        finally:
            async with self._read_condition:
                self._read_eof = True
                self._read_condition.notify_all()

    async def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        async with self._read_condition:
            if size < 0:
                await self._read_condition.wait_for(lambda: self._read_eof)
                size = len(self._read_buffer)
            else:
                await self._read_condition.wait_for(
                    lambda: bool(self._read_buffer) or self._read_eof)
            if not self._read_buffer and self._read_error is not None:
                raise self._read_error
            count = min(size, len(self._read_buffer))
            data = bytes(self._read_buffer[:count])
            del self._read_buffer[:count]
            self._read_condition.notify_all()
            return data

    async def readexactly(self, size: int) -> bytes:
        async with self._read_condition:
            await self._read_condition.wait_for(
                lambda: len(self._read_buffer) >= size or self._read_eof)
            if len(self._read_buffer) < size:
                partial = bytes(self._read_buffer)
                self._read_buffer.clear()
                if self._read_error is not None:
                    raise self._read_error
                raise asyncio.IncompleteReadError(partial, size)
            data = bytes(self._read_buffer[:size])
            del self._read_buffer[:size]
            self._read_condition.notify_all()
            return data

    async def readline(self) -> bytes:
        async with self._read_condition:
            await self._read_condition.wait_for(
                lambda: b"\n" in self._read_buffer or self._read_eof)
            newline = self._read_buffer.find(b"\n")
            if newline < 0:
                if self._read_error is not None:
                    raise self._read_error
                data = bytes(self._read_buffer)
                self._read_buffer.clear()
            else:
                data = bytes(self._read_buffer[:newline + 1])
                del self._read_buffer[:newline + 1]
            if len(data) > MAX_HANDSHAKE:
                raise ValueError("WebSocket stream line exceeds limit")
            self._read_condition.notify_all()
            return data

    def write(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("WebSocket stream is closed")
        if len(self._write_buffer) + len(data) > _WS_MAX_FRAME:
            raise ProtocolError("WebSocket output buffer exceeds the frame limit")
        self._write_buffer.extend(data)

    async def drain(self) -> None:
        if self._write_buffer:
            data = bytes(self._write_buffer)
            self._write_buffer.clear()
            async with self._write_lock:
                await self._send(0x2, data)
        await self._base_writer.drain()

    def can_write_eof(self) -> bool:
        return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._base_writer.close()
        if not self._receiver.done():
            self._receiver.cancel()

    async def wait_closed(self) -> None:
        try:
            await self._base_writer.wait_closed()
        except OSError:
            pass
        if not self._receiver.done():
            self._receiver.cancel()
        await asyncio.gather(self._receiver, return_exceptions=True)


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
            else:
                writer.close()
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


async def _open_entry_connection(config: EntryConfig):
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(
            config.exit_host,
            config.exit_port,
            ssl=_client_ssl(config),
            server_hostname=config.server_name,
            limit=MAX_HANDSHAKE,
        ),
        timeout=CONNECT_TIMEOUT,
    )
    if config.carrier == "tls":
        return reader, writer
    try:
        stream = await _client_websocket(reader, writer, config.server_name, config.exit_port)
    except BaseException:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        raise
    return stream, stream


async def _forward_entry(config: EntryConfig, local_reader: asyncio.StreamReader,
                         local_writer: asyncio.StreamWriter) -> None:
    remote_writer = None
    try:
        remote_reader, remote_writer = await _open_entry_connection(config)
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
        remote_writer = None
        try:
            config = self.owner.config
            remote_reader, remote_writer = await _open_entry_connection(config)
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


def _exit_connection_handler(config: ExitConfig):
    async def handle_connection(reader: asyncio.StreamReader,
                               writer: asyncio.StreamWriter) -> None:
        if config.carrier == "tls":
            await _open_exit_route(config, reader, writer)
            return
        try:
            stream = await _server_websocket(reader, writer)
        except (TimeoutError, ProtocolError, OSError) as exc:
            LOG.debug("closing invalid WebSocket upgrade: %s", exc)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return
        await _open_exit_route(config, stream, stream)
    return handle_connection


async def serve_exit(config: ExitConfig) -> None:
    active = [0]
    server = await asyncio.start_server(
        _limited_handler(_exit_connection_handler(config), active, MAX_TCP_SESSIONS),
        config.host,
        config.port,
        ssl=_server_ssl(config),
        ssl_handshake_timeout=TLS_HANDSHAKE_TIMEOUT,
        limit=MAX_HANDSHAKE,
    )
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or ())
    LOG.info("exit listening on %s via %s; %d fixed route(s)",
             addresses, config.carrier.upper(), len(config.routes))
    async with server:
        await server.serve_forever()


async def serve_entry(config: EntryConfig) -> None:
    if config.transport == "udp":
        protocol = _EntryUdpProtocol(config)
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: protocol, local_addr=(config.host, config.port))
        addresses = str(transport.get_extra_info("sockname") or (config.host, config.port))
        LOG.info("UDP entry listening on %s -> %s:%s via %s (%s)", addresses,
                 config.exit_host, config.exit_port, config.carrier.upper(), config.route)
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
    LOG.info("TCP entry listening on %s -> %s:%s via %s (%s)", addresses,
             config.exit_host, config.exit_port, config.carrier.upper(), config.route)
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
    parser = argparse.ArgumentParser(description="Independent Simurgh TCP/UDP tunnel")
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

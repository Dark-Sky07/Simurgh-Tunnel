"""Independent Simurgh Tunnel protocol with TLS, WSS and HTTP/2 TCP/UDP carriers."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import ssl
import stat
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .usage import QuotaReached, UsageMeter

LOG = logging.getLogger("simurgh.rebuild")
VERSION = "0.2.14"
MAX_HANDSHAKE = 8192
MAX_FRAME = 64 * 1024
HANDSHAKE_TIMEOUT = 5.0
TLS_HANDSHAKE_TIMEOUT = 10.0
CONNECT_TIMEOUT = 10.0
TCP_HALF_CLOSE_TIMEOUT = 120.0
TCP_KEEPALIVE_IDLE = 60
TCP_KEEPALIVE_INTERVAL = 15
TCP_KEEPALIVE_COUNT = 4
MAX_TCP_SESSIONS = 1024
MAX_TCP_SESSIONS_PER_PEER = 128
MAX_UDP_ASSOCIATIONS = 1024
DEFAULT_CLIENT_SESSION_LIMIT = 128
MAX_CLIENT_SESSION_LIMIT = 512
MAX_SPEED_TEST_BYTES = 16 * 1024 * 1024
DEFAULT_SPEED_TEST_MIB = 4
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
    monthly_quota_mib: int | None = None
    client_session_limit: int = DEFAULT_CLIENT_SESSION_LIMIT


@dataclass(frozen=True)
class ReverseHubConfig:
    control_host: str
    control_port: int
    public_host: str
    public_port: int
    cert_file: Path
    key_file: Path
    secret: str
    route: str
    carrier: str = "tls"
    client_session_limit: int = DEFAULT_CLIENT_SESSION_LIMIT


@dataclass(frozen=True)
class ReverseAgentConfig:
    hub_host: str
    hub_port: int
    server_name: str
    ca_file: Path
    secret: str
    route: str
    target_host: str
    target_port: int
    carrier: str = "tls"


NODE_CONFIG_TYPES = {
    "entry": EntryConfig,
    "exit": ExitConfig,
    "reverse-hub": ReverseHubConfig,
    "reverse-agent": ReverseAgentConfig,
}
NODE_ROLES = tuple(NODE_CONFIG_TYPES)


def _text(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string")
    return value.strip()


def _server_name(value: str) -> str:
    if (not value or len(value) > 253 or "%" in value
            or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in value)
            or value.endswith("..") or any(char in value for char in "/\\?#@[]")):
        raise ConfigError("server_name must be a DNS name or IP address without header characters")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        try:
            ascii_name = value.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ConfigError("server_name is not a valid DNS name") from exc
        labels = ascii_name.rstrip(".").split(".")
        if (not ascii_name or len(ascii_name.rstrip(".")) > 253
                or any(not label or len(label) > 63
                       or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
                       for label in labels)):
            raise ConfigError("server_name is not a valid DNS name")
        return ascii_name


def _port(obj: dict[str, Any], key: str) -> int:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConfigError(f"{key} must be an integer from 1 to 65535")
    return value


def _client_session_limit(obj: dict[str, Any]) -> int:
    value = obj.get("client_session_limit", DEFAULT_CLIENT_SESSION_LIMIT)
    if (isinstance(value, bool) or not isinstance(value, int)
            or not 1 <= value <= MAX_CLIENT_SESSION_LIMIT):
        raise ConfigError(
            f"client_session_limit must be an integer from 1 to {MAX_CLIENT_SESSION_LIMIT}")
    return value


def load_config(path: Path) -> ExitConfig | EntryConfig | ReverseHubConfig | ReverseAgentConfig:
    """Read a JSON config. It is read-only; this function never edits it."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read JSON configuration {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("the top-level configuration must be a JSON object")
    role = raw.get("role")
    if role not in ("entry", "exit", "reverse-hub", "reverse-agent"):
        raise ConfigError("role must be entry, exit, reverse-hub or reverse-agent")
    secret = _text(raw, "secret")
    if len(secret.encode("utf-8")) < 32:
        raise ConfigError("secret must contain at least 32 UTF-8 bytes")

    if role == "reverse-hub":
        route = _text(raw, "route")
        if len(route) > 64:
            raise ConfigError("route must contain at most 64 characters")
        control_port = _port(raw, "control_port")
        public_port = _port(raw, "public_port")
        if control_port == public_port:
            raise ConfigError("reverse hub control_port and public_port must differ")
        return ReverseHubConfig(
            control_host=_text(raw, "control_host"),
            control_port=control_port,
            public_host=_text(raw, "public_host"),
            public_port=public_port,
            cert_file=Path(_text(raw, "cert_file")),
            key_file=Path(_text(raw, "key_file")),
            secret=secret,
            route=route,
            client_session_limit=_client_session_limit(raw),
        )

    if role == "reverse-agent":
        route = _text(raw, "route")
        if len(route) > 64:
            raise ConfigError("route must contain at most 64 characters")
        return ReverseAgentConfig(
            hub_host=_text(raw, "hub_host"),
            hub_port=_port(raw, "hub_port"),
            server_name=_server_name(_text(raw, "server_name")),
            ca_file=Path(_text(raw, "ca_file")),
            secret=secret,
            route=route,
            target_host=_text(raw, "target_host"),
            target_port=_port(raw, "target_port"),
        )

    host = _text(raw, "listen_host")
    port = _port(raw, "listen_port")
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
        if carrier not in ("tls", "wss", "h2"):
            raise ConfigError("carrier must be 'tls', 'wss' or 'h2'")
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
    if carrier not in ("tls", "wss", "h2"):
        raise ConfigError("carrier must be 'tls', 'wss' or 'h2'")
    monthly_quota_mib = raw.get("monthly_quota_mib")
    if monthly_quota_mib is not None and (
            isinstance(monthly_quota_mib, bool) or not isinstance(monthly_quota_mib, int)
            or not 1 <= monthly_quota_mib <= 1_048_576):
        raise ConfigError("monthly_quota_mib must be an integer from 1 to 1048576 when set")
    return EntryConfig(
        host=host,
        port=port,
        exit_host=_text(raw, "exit_host"),
        exit_port=_port(raw, "exit_port"),
        server_name=_server_name(_text(raw, "server_name")),
        ca_file=Path(_text(raw, "ca_file")),
        secret=secret,
        route=_text(raw, "route"),
        transport=transport,
        carrier=carrier,
        monthly_quota_mib=monthly_quota_mib,
        client_session_limit=_client_session_limit(raw),
    )


def _server_ssl(config: ExitConfig) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if config.carrier == "h2":
        context.set_alpn_protocols(["h2"])
    context.load_cert_chain(str(config.cert_file), str(config.key_file))
    return context


def _client_ssl(config: EntryConfig) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(config.ca_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if config.carrier == "h2":
        context.set_alpn_protocols(["h2"])
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WS_PATH = "/simurgh-tunnel"
_WS_MAX_FRAME = MAX_FRAME + _FRAME_HEADER.size
MAX_WS_BUFFER = 256 * 1024
_WS_HEADER_LIMIT = 8192


def _ws_accept(key: str) -> str:
    digest = hashlib.sha1((key + _WS_GUID).encode("ascii"), usedforsecurity=False).digest()
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

    def get_extra_info(self, name: str, default=None):
        return self._base_writer.get_extra_info(name, default)

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


def _enable_tcp_keepalive(writer: asyncio.StreamWriter) -> None:
    """Best-effort keepalive probes for a forwarded TCP stream's underlying socket."""
    try:
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        idle_option = getattr(socket, "TCP_KEEPIDLE", getattr(socket, "TCP_KEEPALIVE", None))
        if idle_option is not None:
            sock.setsockopt(socket.IPPROTO_TCP, idle_option, TCP_KEEPALIVE_IDLE)
        interval_option = getattr(socket, "TCP_KEEPINTVL", None)
        if interval_option is not None:
            sock.setsockopt(socket.IPPROTO_TCP, interval_option, TCP_KEEPALIVE_INTERVAL)
        count_option = getattr(socket, "TCP_KEEPCNT", None)
        if count_option is not None:
            sock.setsockopt(socket.IPPROTO_TCP, count_option, TCP_KEEPALIVE_COUNT)
    except (AttributeError, OSError) as exc:
        LOG.debug("could not configure TCP keepalive on a forwarded stream: %s", exc)


async def _copy_to_tunnel(reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter,
                          meter: UsageMeter | None = None,
                          activity: asyncio.Event | None = None) -> None:
    while True:
        chunk = await reader.read(MAX_FRAME)
        if not chunk:
            await _write_frame(writer, _FIN)
            return
        allowed = meter.consume("upload", len(chunk)) if meter else len(chunk)
        if allowed:
            await _write_frame(writer, _DATA, chunk[:allowed])
            if activity is not None:
                activity.set()
        if allowed < len(chunk) or (meter and meter.exhausted):
            raise QuotaReached("monthly transfer quota reached")


async def _copy_from_tunnel(reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter,
                            meter: UsageMeter | None = None,
                            activity: asyncio.Event | None = None) -> None:
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
        allowed = meter.consume("download", len(payload)) if meter else len(payload)
        if allowed:
            writer.write(payload[:allowed])
            await writer.drain()
            if activity is not None:
                activity.set()
        if allowed < len(payload) or (meter and meter.exhausted):
            raise QuotaReached("monthly transfer quota reached")


async def _wait_for_tasks(coroutines, *, first_completed: bool,
                          completion_timeout: float | None = None,
                          activity: asyncio.Event | None = None) -> None:
    tasks = [asyncio.create_task(coro) for coro in coroutines]
    pending = set(tasks)
    deadline = None
    activity_waiter = None
    loop = asyncio.get_running_loop()
    try:
        while pending:
            timeout = None if deadline is None else max(0.0, deadline - loop.time())
            waiters = set(pending)
            if deadline is not None and activity is not None:
                activity_waiter = asyncio.create_task(activity.wait())
                waiters.add(activity_waiter)
            done, _ = await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            activity_woken = False
            if activity_waiter is not None:
                if activity_waiter in done:
                    activity.clear()
                    deadline = loop.time() + completion_timeout
                    activity_woken = True
                    done.discard(activity_waiter)
                else:
                    activity_waiter.cancel()
                    await asyncio.gather(activity_waiter, return_exceptions=True)
                activity_waiter = None

            flow_done = done.intersection(pending)
            if not flow_done and activity_woken:
                continue
            if not flow_done:
                LOG.debug("closing half-closed TCP flow after its idle grace period")
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                return
            pending.difference_update(flow_done)

            failure = next((task.exception() for task in flow_done
                            if not task.cancelled() and task.exception() is not None), None)
            if failure is not None:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise failure

            if first_completed:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                return

            if pending and deadline is None and completion_timeout is not None:
                deadline = loop.time() + completion_timeout
    except BaseException:
        if activity_waiter is not None and not activity_waiter.done():
            activity_waiter.cancel()
            await asyncio.gather(activity_waiter, return_exceptions=True)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _join_directions(*coroutines, activity: asyncio.Event | None = None) -> None:
    await _wait_for_tasks(coroutines, first_completed=False,
                          completion_timeout=TCP_HALF_CLOSE_TIMEOUT,
                          activity=activity)


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


async def _serve_speed_test(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                           byte_count: int) -> None:
    await _send_json(writer, {"ok": True, "bytes": byte_count})
    block = b"\x00" * MAX_FRAME
    remaining = byte_count
    while remaining:
        payload = block[:min(remaining, MAX_FRAME)]
        await _write_frame(writer, _DATA, payload)
        remaining -= len(payload)
    await _write_frame(writer, _FIN)
    kind, payload = await asyncio.wait_for(_read_frame(reader), timeout=HANDSHAKE_TIMEOUT)
    if kind != _FIN or payload:
        raise ProtocolError("speed test completion acknowledgement is invalid")


async def _open_exit_route(config: ExitConfig, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter) -> None:
    remote_writer: asyncio.StreamWriter | None = None
    try:
        hello = await _read_json(reader)
        route_name = hello.get("route")
        peer_secret = hello.get("secret")
        operation = hello.get("op")
        valid = (type(hello.get("version")) is int and hello.get("version") == 1
                 and operation in ("open", "udp", "speed") and isinstance(peer_secret, str)
                 and hmac.compare_digest(peer_secret.encode("utf-8"),
                                         config.secret.encode("utf-8"))
                 and isinstance(route_name, str) and route_name in config.routes)
        if not valid:
            await _send_json(writer, {"ok": False, "error": "denied"})
            return
        if operation == "speed":
            byte_count = hello.get("bytes")
            if (isinstance(byte_count, bool) or not isinstance(byte_count, int)
                    or not 1 <= byte_count <= MAX_SPEED_TEST_BYTES):
                await _send_json(writer, {"ok": False, "error": "invalid_test_size"})
                return
            await _serve_speed_test(reader, writer, byte_count)
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
        _enable_tcp_keepalive(writer)
        _enable_tcp_keepalive(remote_writer)
        await _send_json(writer, {"ok": True})
        activity = asyncio.Event()
        await _join_directions(
            _copy_from_tunnel(reader, remote_writer, activity=activity),
            _copy_to_tunnel(remote_reader, writer, activity=activity),
            activity=activity,
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
        if config.carrier == "wss":
            stream = await _client_websocket(reader, writer, config.server_name, config.exit_port)
        elif config.carrier == "h2":
            ssl_object = writer.get_extra_info("ssl_object")
            if ssl_object is None or ssl_object.selected_alpn_protocol() != "h2":
                raise ProtocolError("exit did not negotiate the HTTP/2 TLS protocol")
            from .h2stream import H2TunnelStream

            authority_host = (f"[{config.server_name}]" if ":" in config.server_name
                              else config.server_name)
            stream = await H2TunnelStream(reader, writer, client=True).start_client(
                f"{authority_host}:{config.exit_port}")
        else:
            raise ConfigError(f"unsupported carrier: {config.carrier}")
    except BaseException:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        raise
    return stream, stream


async def check_entry_path(config: EntryConfig) -> str:
    """Verify the authenticated tunnel and destination setup without forwarding user data."""
    reader = writer = None
    operation = "udp" if config.transport == "udp" else "open"
    try:
        reader, writer = await _open_entry_connection(config)
        await _send_json(writer, {
            "version": 1,
            "op": operation,
            "route": config.route,
            "secret": config.secret,
        })
        response = await asyncio.wait_for(_read_json(reader), timeout=HANDSHAKE_TIMEOUT)
        if response.get("ok") is not True:
            reason = response.get("error", "route was rejected")
            raise ConnectionError(f"exit declined the route ({reason})")
        if config.transport == "udp":
            return (f"{config.carrier.upper()} path and UDP association are available; the UDP "
                    "destination did not receive a test datagram, so application reachability "
                    "is unverified.")
        return (f"{config.carrier.upper()} path authenticated and the fixed TCP destination "
                "accepted a connection.")
    except (TimeoutError, OSError, ssl.SSLError, ProtocolError, EOFError) as exc:
        raise ConnectionError(f"path check failed: {exc}") from exc
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=2)
            except (TimeoutError, OSError):
                pass


async def speed_test_entry(config: EntryConfig,
                            mebibytes: int = DEFAULT_SPEED_TEST_MIB) -> str:
    """Measure authenticated tunnel download throughput with a bounded synthetic payload."""
    if config.transport != "tcp":
        raise ValueError("speed tests require a TCP entry; no UDP traffic is sent")
    if (isinstance(mebibytes, bool) or not isinstance(mebibytes, int)
            or not 1 <= mebibytes <= MAX_SPEED_TEST_BYTES // (1024 * 1024)):
        raise ValueError("speed test size must be an integer from 1 to 16 MiB")
    byte_count = mebibytes * 1024 * 1024
    reader = writer = None
    try:
        reader, writer = await _open_entry_connection(config)
        started = time.perf_counter()
        await _send_json(writer, {
            "version": 1,
            "op": "speed",
            "route": config.route,
            "secret": config.secret,
            "bytes": byte_count,
        })
        response = await asyncio.wait_for(_read_json(reader), timeout=HANDSHAKE_TIMEOUT)
        if response.get("ok") is not True or response.get("bytes") != byte_count:
            raise ProtocolError(f"exit refused the speed test ({response.get('error', 'bad response')})")
        received = 0
        while received < byte_count:
            kind, payload = await asyncio.wait_for(_read_frame(reader), timeout=60)
            if kind != _DATA or received + len(payload) > byte_count:
                raise ProtocolError("speed test returned invalid data frames")
            received += len(payload)
        kind, payload = await asyncio.wait_for(_read_frame(reader), timeout=60)
        if kind != _FIN or payload:
            raise ProtocolError("speed test did not finish at the declared byte count")
        await _write_frame(writer, _FIN)
        elapsed = max(time.perf_counter() - started, 1e-6)
        rate_mbps = byte_count * 8 / elapsed / 1_000_000
        return (f"Received {mebibytes} MiB over {config.carrier.upper()} in {elapsed:.2f} s "
                f"({rate_mbps:.2f} Mbit/s). This measures tunnel download capacity, "
                "not end-to-end application performance.")
    except (TimeoutError, OSError, ssl.SSLError, ProtocolError, EOFError) as exc:
        raise ConnectionError(f"speed test failed: {exc}") from exc
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=2)
            except (TimeoutError, OSError):
                pass


async def _forward_entry(config: EntryConfig, local_reader: asyncio.StreamReader,
                         local_writer: asyncio.StreamWriter,
                         meter: UsageMeter | None = None) -> None:
    remote_writer = None
    try:
        if meter and meter.exhausted:
            LOG.warning("refusing new flow: monthly transfer quota reached")
            return
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
        _enable_tcp_keepalive(local_writer)
        _enable_tcp_keepalive(remote_writer)
        activity = asyncio.Event()
        await _join_directions(
            _copy_to_tunnel(local_reader, remote_writer, meter, activity),
            _copy_from_tunnel(remote_reader, local_writer, meter, activity),
            activity=activity,
        )
    except QuotaReached as exc:
        LOG.info("closing entry flow: %s", exc)
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
        if (self.owner.meter
                and not self.owner.meter.allow_datagram("upload", len(payload))):
            LOG.debug("dropping UDP datagram from %r: monthly quota reached", self.client)
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
                    meter = self.owner.meter
                    if meter and not meter.allow_datagram("download", len(payload)):
                        raise QuotaReached("monthly transfer quota reached")
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
            self.owner.remove_session(self.client, self)


class _EntryUdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, config: EntryConfig, meter: UsageMeter | None = None):
        self.config = config
        self.meter = meter
        self.transport: asyncio.DatagramTransport | None = None
        self.sessions: dict[tuple, _EntryUdpSession] = {}
        self.peer_sessions: dict[str, int] = {}

    @staticmethod
    def peer_key(addr) -> str:
        return str(addr[0]) if isinstance(addr, tuple) and addr else str(addr)

    def remove_session(self, addr, session: _EntryUdpSession) -> None:
        if self.sessions.get(addr) is not session:
            return
        del self.sessions[addr]
        peer = self.peer_key(addr)
        remaining = self.peer_sessions[peer] - 1
        if remaining:
            self.peer_sessions[peer] = remaining
        else:
            del self.peer_sessions[peer]

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if self.meter and self.meter.exhausted:
            LOG.debug("dropping UDP client %r: monthly transfer quota reached", addr)
            return
        session = self.sessions.get(addr)
        if session is None:
            if len(self.sessions) >= MAX_UDP_ASSOCIATIONS:
                LOG.warning("dropping UDP client %r: association limit reached", addr)
                return
            peer = self.peer_key(addr)
            if self.peer_sessions.get(peer, 0) >= self.config.client_session_limit:
                LOG.debug("dropping UDP client %r: per-address session limit reached", addr)
                return
            session = _EntryUdpSession(self, addr)
            self.sessions[addr] = session
            self.peer_sessions[peer] = self.peer_sessions.get(peer, 0) + 1
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
        self.peer_sessions.clear()


def _limited_handler(handler, active: list[int], limit: int,
                     per_peer_limit: int = MAX_TCP_SESSIONS_PER_PEER):
    peer_active: dict[str, int] = {}

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_key = str(peer[0]) if isinstance(peer, tuple) and peer else str(peer)
        if active[0] >= limit or peer_active.get(peer_key, 0) >= per_peer_limit:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return
        active[0] += 1
        peer_active[peer_key] = peer_active.get(peer_key, 0) + 1
        try:
            await handler(reader, writer)
        finally:
            active[0] -= 1
            remaining = peer_active[peer_key] - 1
            if remaining:
                peer_active[peer_key] = remaining
            else:
                del peer_active[peer_key]
    return serve


def _exit_connection_handler(config: ExitConfig):
    async def handle_connection(reader: asyncio.StreamReader,
                               writer: asyncio.StreamWriter) -> None:
        if config.carrier == "tls":
            await _open_exit_route(config, reader, writer)
            return
        try:
            if config.carrier == "wss":
                stream = await _server_websocket(reader, writer)
            elif config.carrier == "h2":
                ssl_object = writer.get_extra_info("ssl_object")
                if ssl_object is None or ssl_object.selected_alpn_protocol() != "h2":
                    raise ProtocolError("client did not negotiate the HTTP/2 TLS protocol")
                from .h2stream import H2TunnelStream

                stream = await H2TunnelStream(reader, writer, client=False).start_server()
            else:
                raise ConfigError(f"unsupported carrier: {config.carrier}")
        except (TimeoutError, ProtocolError, OSError) as exc:
            LOG.debug("closing invalid %s tunnel upgrade: %s", config.carrier, exc)
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


async def serve_entry(config: EntryConfig, usage_path: Path | None = None) -> None:
    meter = (UsageMeter(usage_path, config.monthly_quota_mib)
             if usage_path is not None else None)
    try:
        if config.transport == "udp":
            protocol = _EntryUdpProtocol(config, meter)
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
            _limited_handler(lambda reader, writer: _forward_entry(config, reader, writer, meter),
                             active, MAX_TCP_SESSIONS,
                             per_peer_limit=config.client_session_limit),
            config.host,
            config.port,
            limit=MAX_HANDSHAKE,
        )
        addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or ())
        LOG.info("TCP entry listening on %s -> %s:%s via %s (%s)", addresses,
                 config.exit_host, config.exit_port, config.carrier.upper(), config.route)
        async with server:
            await server.serve_forever()
    finally:
        if meter:
            meter.flush()


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
    diagnostics = parser.add_mutually_exclusive_group()
    diagnostics.add_argument("--check", action="store_true",
                             help="test an entry's authenticated path and configured destination")
    diagnostics.add_argument("--speed-test-mib", type=int, metavar="MIB",
                             help="measure tunnel download speed (1–16 MiB, TCP entry only)")
    parser.add_argument("--log-level", choices=("debug", "info", "warning", "error"), default="info")
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper()),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config(args.config)
        _check_permissions(args.config)
        if args.check or args.speed_test_mib is not None:
            if not isinstance(config, EntryConfig):
                LOG.error("diagnostics require an entry config; run them from the entry server")
                return 2
            try:
                if args.check:
                    print(asyncio.run(check_entry_path(config)))
                else:
                    print(asyncio.run(speed_test_entry(config, args.speed_test_mib)))
            except (ConnectionError, OSError, TimeoutError, ValueError) as exc:
                LOG.error("%s", exc)
                return 1
            return 0
        if isinstance(config, ExitConfig):
            asyncio.run(serve_exit(config))
        elif isinstance(config, EntryConfig):
            asyncio.run(serve_entry(config, args.config.parent / "entry-usage.json"))
        else:
            from .reverse import serve_reverse_agent, serve_reverse_hub

            if isinstance(config, ReverseHubConfig):
                asyncio.run(serve_reverse_hub(config))
            else:
                asyncio.run(serve_reverse_agent(config))
    except (ConfigError, OSError, ssl.SSLError, KeyboardInterrupt) as exc:
        if not isinstance(exc, KeyboardInterrupt):
            LOG.error("startup failed: %s", exc)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

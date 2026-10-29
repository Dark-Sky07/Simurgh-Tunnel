"""One-request HTTP/2 byte stream adapter for the independent tunnel protocol."""

from __future__ import annotations

import asyncio

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import (
    ConnectionTerminated,
    DataReceived,
    RequestReceived,
    ResponseReceived,
    StreamEnded,
    StreamReset,
    WindowUpdated,
)
from h2.exceptions import H2Error

from .tunnel import MAX_HANDSHAKE, ProtocolError

_H2_PATH = "/simurgh-tunnel"
_BUFFER_LIMIT = 256 * 1024
_READ_CHUNK = 64 * 1024


class H2TunnelStream:
    """Expose a single HTTP/2 stream as the reader/writer used by tunnel handlers."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 *, client: bool):
        self._raw_reader = reader
        self._raw_writer = writer
        self._client = client
        self._connection = H2Connection(config=H2Configuration(
            client_side=client, header_encoding="utf-8"))
        self._connection.decoder.max_header_list_size = MAX_HANDSHAKE
        self._connection.local_settings.max_header_list_size = MAX_HANDSHAKE
        self._connection.local_settings.max_concurrent_streams = 1
        self._connection_lock = asyncio.Lock()
        self._wire_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._window_changed = asyncio.Event()
        self._read_changed = asyncio.Condition()
        self._read_buffer = bytearray()
        self._write_buffer = bytearray()
        self._read_eof = False
        self._read_error: BaseException | None = None
        self._closed = False
        self._close_task: asyncio.Task | None = None
        self._request_event = asyncio.Event()
        self._response_event = asyncio.Event()
        self._headers: list[tuple[str, str]] = []
        self._stream_id: int | None = None
        self._receiver: asyncio.Task | None = None

    async def start_client(self, authority: str) -> H2TunnelStream:
        if not self._client:
            raise RuntimeError("client startup used on a server HTTP/2 stream")
        self._connection.initiate_connection()
        self._stream_id = self._connection.get_next_available_stream_id()
        self._connection.send_headers(self._stream_id, [
            (":method", "POST"),
            (":scheme", "https"),
            (":authority", authority),
            (":path", _H2_PATH),
            ("content-type", "application/octet-stream"),
        ])
        self._raw_writer.write(self._connection.data_to_send())
        await self._raw_writer.drain()
        self._receiver = asyncio.create_task(self._receive_loop())
        try:
            await asyncio.wait_for(self._response_event.wait(), timeout=5.0)
        except TimeoutError as exc:
            raise ProtocolError("HTTP/2 response timed out") from exc
        if self._read_error is not None:
            raise ProtocolError(f"HTTP/2 connection failed: {self._read_error}")
        status = dict(self._headers).get(":status")
        if status != "200":
            raise ProtocolError(f"HTTP/2 tunnel request returned status {status!r}")
        return self

    async def start_server(self) -> H2TunnelStream:
        if self._client:
            raise RuntimeError("server startup used on a client HTTP/2 stream")
        self._connection.initiate_connection()
        self._raw_writer.write(self._connection.data_to_send())
        await self._raw_writer.drain()
        self._receiver = asyncio.create_task(self._receive_loop())
        try:
            await asyncio.wait_for(self._request_event.wait(), timeout=5.0)
        except TimeoutError as exc:
            raise ProtocolError("HTTP/2 request timed out") from exc
        headers = dict(self._headers)
        if (headers.get(":method") != "POST" or headers.get(":path") != _H2_PATH
                or headers.get(":scheme") != "https" or not headers.get(":authority")
                or headers.get("content-type") != "application/octet-stream"):
            await self._respond(400)
            raise ProtocolError("unsupported HTTP/2 tunnel request")
        await self._respond(200)
        return self

    async def _respond(self, status: int) -> None:
        if self._stream_id is None:
            raise ProtocolError("HTTP/2 stream was not initialized")
        async with self._wire_lock:
            async with self._connection_lock:
                self._connection.send_headers(self._stream_id, [
                    (":status", str(status)),
                    ("content-type", "application/octet-stream"),
                ])
                data = self._connection.data_to_send()
            self._raw_writer.write(data)
            await self._raw_writer.drain()

    async def _send_pending(self) -> None:
        async with self._wire_lock:
            async with self._connection_lock:
                data = self._connection.data_to_send()
            if data:
                self._raw_writer.write(data)
                await self._raw_writer.drain()

    async def _receive_loop(self) -> None:
        try:
            while True:
                data = await self._raw_reader.read(_READ_CHUNK)
                if not data:
                    break
                async with self._connection_lock:
                    events = self._connection.receive_data(data)
                for event in events:
                    if isinstance(event, RequestReceived) and not self._client:
                        if self._stream_id not in (None, event.stream_id):
                            raise ProtocolError("only one HTTP/2 tunnel stream is supported")
                        self._stream_id = event.stream_id
                        self._headers = list(event.headers)
                        self._request_event.set()
                    elif isinstance(event, ResponseReceived) and self._client:
                        if self._stream_id not in (None, event.stream_id):
                            raise ProtocolError("unexpected HTTP/2 response stream")
                        self._stream_id = event.stream_id
                        self._headers = list(event.headers)
                        self._response_event.set()
                    elif isinstance(event, DataReceived):
                        if event.stream_id != self._stream_id:
                            raise ProtocolError("unexpected HTTP/2 data stream")
                        async with self._read_changed:
                            payload_size = len(event.data)
                            await self._read_changed.wait_for(
                                lambda size=payload_size:
                                len(self._read_buffer) + size <= _BUFFER_LIMIT or self._closed)
                            if self._closed:
                                return
                            self._read_buffer.extend(event.data)
                            self._read_changed.notify_all()
                        async with self._connection_lock:
                            self._connection.acknowledge_received_data(
                                event.flow_controlled_length, event.stream_id)
                    elif isinstance(event, WindowUpdated):
                        self._window_changed.set()
                    elif isinstance(event, StreamEnded) and event.stream_id == self._stream_id:
                        async with self._read_changed:
                            self._read_eof = True
                            self._read_changed.notify_all()
                    elif isinstance(event, StreamReset) and event.stream_id == self._stream_id:
                        self._read_error = ProtocolError("HTTP/2 tunnel stream was reset")
                        self._response_event.set()
                        self._request_event.set()
                        async with self._read_changed:
                            self._read_eof = True
                            self._read_changed.notify_all()
                    elif isinstance(event, ConnectionTerminated):
                        self._read_error = ProtocolError("HTTP/2 connection was terminated")
                        self._response_event.set()
                        self._request_event.set()
                        async with self._read_changed:
                            self._read_eof = True
                            self._read_changed.notify_all()
                await self._send_pending()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - make protocol errors visible to stream readers
            self._read_error = (ProtocolError(f"invalid HTTP/2 frame: {exc}")
                                if isinstance(exc, H2Error) else exc)
            self._response_event.set()
            self._request_event.set()
        finally:
            async with self._read_changed:
                self._read_eof = True
                self._read_changed.notify_all()
            self._response_event.set()
            self._request_event.set()

    async def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        async with self._read_changed:
            if size < 0:
                await self._read_changed.wait_for(lambda: self._read_eof)
                size = len(self._read_buffer)
            else:
                await self._read_changed.wait_for(
                    lambda: bool(self._read_buffer) or self._read_eof)
            if not self._read_buffer and self._read_error is not None:
                raise self._read_error
            count = min(size, len(self._read_buffer))
            data = bytes(self._read_buffer[:count])
            del self._read_buffer[:count]
            self._read_changed.notify_all()
            return data

    async def readexactly(self, size: int) -> bytes:
        async with self._read_changed:
            await self._read_changed.wait_for(
                lambda: len(self._read_buffer) >= size or self._read_eof)
            if len(self._read_buffer) < size:
                partial = bytes(self._read_buffer)
                self._read_buffer.clear()
                if self._read_error is not None:
                    raise self._read_error
                raise asyncio.IncompleteReadError(partial, size)
            data = bytes(self._read_buffer[:size])
            del self._read_buffer[:size]
            self._read_changed.notify_all()
            return data

    async def readline(self) -> bytes:
        async with self._read_changed:
            await self._read_changed.wait_for(
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
                raise ValueError("HTTP/2 stream line exceeds limit")
            self._read_changed.notify_all()
            return data

    def write(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("HTTP/2 tunnel stream is closed")
        if len(self._write_buffer) + len(data) > _BUFFER_LIMIT:
            raise ProtocolError("HTTP/2 output buffer exceeds its limit")
        self._write_buffer.extend(data)

    async def drain(self) -> None:
        async with self._write_lock:
            while self._write_buffer:
                if self._stream_id is None:
                    raise ProtocolError("HTTP/2 stream is not ready")
                async with self._wire_lock:
                    async with self._connection_lock:
                        window = self._connection.local_flow_control_window(self._stream_id)
                        if window <= 0:
                            self._window_changed.clear()
                            data = b""
                        else:
                            count = min(window, self._connection.max_outbound_frame_size,
                                        len(self._write_buffer))
                            chunk = bytes(self._write_buffer[:count])
                            del self._write_buffer[:count]
                            self._connection.send_data(self._stream_id, chunk)
                            data = self._connection.data_to_send()
                    if data:
                        self._raw_writer.write(data)
                        await self._raw_writer.drain()
                if not data:
                    await self._window_changed.wait()
        await self._send_pending()

    def can_write_eof(self) -> bool:
        return False

    def write_eof(self) -> None:
        raise OSError("HTTP/2 tunnel stream does not expose half-close to its byte stream")

    def get_extra_info(self, name: str, default=None):
        return self._raw_writer.get_extra_info(name, default)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_task = asyncio.create_task(self._close_stream())

    async def _close_stream(self) -> None:
        try:
            await self.drain()
            if self._stream_id is not None:
                async with self._wire_lock:
                    async with self._connection_lock:
                        self._connection.end_stream(self._stream_id)
                        data = self._connection.data_to_send()
                    if data:
                        self._raw_writer.write(data)
                        await self._raw_writer.drain()
        except (H2Error, OSError, ProtocolError):
            pass
        self._raw_writer.close()

    async def wait_closed(self) -> None:
        if self._close_task is not None:
            await self._close_task
        await self._raw_writer.wait_closed()
        if self._receiver is not None and not self._receiver.done():
            self._receiver.cancel()
        if self._receiver is not None:
            await asyncio.gather(self._receiver, return_exceptions=True)

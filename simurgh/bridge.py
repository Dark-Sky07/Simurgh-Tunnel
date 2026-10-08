"""The pipe between a real socket and a tunnel stream.

Both directions of the tunnel use exactly this class:

* on the **Iranian relay** a user connection is bridged into a tunnel stream;
* on the **exit** a tunnel stream is bridged into the panel's socket.

Flow control is end to end:

* data coming *from* the socket is only written into the stream while the peer
  has granted credit; when credit runs out we simply stop reading the socket
  (the kernel's receive window closes -- no memory growth).
* data coming *from* the stream is only credited once it has really been handed
  to the socket, so a slow user cannot make the far side buffer for it.

If the destination socket is congested, at most one flow-control window is
buffered (256 KiB by default), then the peer stops.
"""

from __future__ import annotations

import asyncio

from .util import get_logger

log = get_logger("simurgh.bridge")

CHUNK = 65536
MAX_PREOPEN = 256 * 1024


class Bridge(asyncio.Protocol):
    """asyncio transport <-> tunnel stream."""

    def __init__(self, *, stream=None, opener=None, proxy_header: bytes | None = None,
                 chunk: int = CHUNK, on_open_error=None, on_open=None,
                 on_close=None, counter=None):
        self.stream = stream
        self.opener = opener
        self.proxy_header = proxy_header
        self.chunk = chunk
        self.on_open_error = on_open_error
        self.on_open = on_open
        self.on_close = on_close
        self.counter = counter
        self.transport: asyncio.Transport | None = None
        self.buf = bytearray()          # socket -> stream (waiting for credit)
        self.out = bytearray()          # stream -> socket (socket congested)
        self.sock_paused = False
        self.closed = False
        self._opening = False
        self.peer = ""

    # ---------------------------------------------------------------- set up
    def connection_made(self, transport) -> None:
        self.transport = transport
        self.peer = _fmt(transport.get_extra_info("peername"))
        try:
            sock = transport.get_extra_info("socket")
            if sock is not None:
                import socket as _s

                sock.setsockopt(_s.IPPROTO_TCP, _s.TCP_NODELAY, 1)
        except Exception:
            pass
        if self.stream is not None:
            self._wire(self.stream)
        elif self.opener is not None:
            self._opening = True
            task = asyncio.ensure_future(self._open())
            task.add_done_callback(self._open_done)
        if self.counter is not None:
            self.counter.conns += 1
            self.counter.active += 1

    def _open_done(self, task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if self.counter is not None:
                self.counter.errors += 1
                self.counter.last_error = str(exc)[:120]
            log.debug("open through tunnel failed for %s: %s", self.peer, exc)
            if self.on_open_error is not None:
                try:
                    self.on_open_error(exc)
                except Exception:
                    pass
            self.close_socket()

    async def _open(self) -> None:
        stream = await self.opener()
        self._wire(stream)

    def _wire(self, stream) -> None:
        self.stream = stream
        self._opening = False
        stream.on_data = self._from_stream
        stream.on_eof = self._stream_eof
        stream.on_credit = self._flush_into_stream
        if self.on_open is not None:
            try:
                self.on_open(stream)
            except Exception:
                pass
        if self.proxy_header:
            try:
                stream.write_now(self.proxy_header)
            except Exception:
                pass
        if self.buf:
            self._flush_into_stream()
        if stream.eof:
            # the far side finished before we even attached
            self._stream_eof()

    # --------------------------------------------------------- socket -> tun
    def data_received(self, data: bytes) -> None:
        if self.stream is None:
            self.buf += data
            if len(self.buf) > MAX_PREOPEN:
                try:
                    self.transport.pause_reading()
                except Exception:
                    pass
            return
        if self.buf:
            self.buf += data
        else:
            self.buf = bytearray(data)
        self._flush_into_stream()

    def _flush_into_stream(self) -> None:
        s = self.stream
        if s is None or self.closed:
            return
        while self.buf:
            n = s.can_send(self.chunk if len(self.buf) > self.chunk else len(self.buf))
            if n <= 0:
                try:
                    if self.transport is not None:
                        self.transport.pause_reading()
                except Exception:
                    pass
                return
            chunk = bytes(self.buf[:n])
            del self.buf[:n]
            s.write_now(chunk)
        try:
            if self.transport is not None:
                self.transport.resume_reading()
        except Exception:
            pass

    # --------------------------------------------------------- tun -> socket
    def _from_stream(self, data: bytes) -> None:
        if self.closed or self.transport is None:
            return
        if self.sock_paused:
            # The socket asked us to stop; park the bytes (bounded by the
            # peer's flow-control window) and credit them once they are out.
            self.out += data
            return
        self.transport.write(data)
        self.stream.grant(len(data))
        self.stream.flush_credit()

    def pause_writing(self) -> None:
        self.sock_paused = True

    def resume_writing(self) -> None:
        self.sock_paused = False
        if self.out and self.transport is not None:
            pending = bytes(self.out)
            self.out.clear()
            self.transport.write(pending)
            self.stream.grant(len(pending))
            self.stream.flush_credit()

    def eof_received(self) -> bool:
        # the local socket is done sending: tell the peer, keep receiving
        if self.stream is not None:
            self.stream.close()
        return True

    def _stream_eof(self) -> None:
        if self.closed:
            return
        if self.second_half_done():
            self.close_socket()
            return
        try:
            if self.transport is not None:
                self.transport.write_eof()
        except Exception:
            self.close_socket()

    def second_half_done(self) -> bool:
        return bool(self.stream is not None and self.stream.closed)

    # ------------------------------------------------------------- teardown
    def connection_lost(self, exc) -> None:
        self.closed = True
        if self.counter is not None:
            self.counter.active = max(0, self.counter.active - 1)
        s, self.stream = self.stream, None
        if s is not None:
            try:
                s.on_data = None
                s.on_eof = None
                s.close()
            except Exception:
                pass
        if self.on_close is not None:
            try:
                self.on_close()
            except Exception:
                pass

    def close_socket(self) -> None:
        self.closed = True
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass


def _fmt(info) -> str:
    if isinstance(info, tuple) and len(info) >= 2:
        return f"{info[0]}:{info[1]}"
    return str(info or "")


def proxy_v1_header(client_ip: str, client_port: int, target_ip: str,
                    target_port: int) -> bytes:
    """PROXY protocol v1 -- the panel sees the real user IP.  Real bonus: x-ui
    logs and per-user limits stay correct through the tunnel."""
    return (f"PROXY TCP4 {client_ip} {target_ip} {client_port} {target_port}\r\n"
            ).encode("ascii", "replace")

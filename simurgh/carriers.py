"""Carriers: the different "lives" the tunnel lives on the wire.

``plain``  plain TCP + a shared-secret header.  No crypto at all -- only for
           links you control completely (Iran <-> Iran, lab, ...).
``raw``    looks like random noise in both directions: X25519 handshake, HMAC
           authentication and ChaCha20-Poly1305 frames.  No TLS certificate
           needed, works on any port; the cheapest carrier in CPU terms after
           ``plain``.
``tls``    a *real* certificate, a *real* TLS handshake and -- for anybody who
           does not know the token -- a *real* decoy website.  This is what
           makes active probing pointless.
``wss``    the same, wrapped in a WebSocket upgrade so the tunnel can sit
           behind Cloudflare / any CDN and blend with ordinary HTTPS traffic.

Everything above (multiplexer, control plane, user traffic) is identical
whatever the carrier is, so switching carriers never changes behaviour.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import struct
import time

from .decoy import serve_decoy
from .protocol import DEFAULT_CHUNK
from .tlsio import TlsLayer, client_context, server_context
from .util import get_logger

log = get_logger("simurgh.carrier")

VERSION = 1
AUTH_WINDOW = 180.0          # tolerated clock skew, seconds
HEADER_LEN_FIELD = struct.Struct(">I")
MAX_FRAME = 16 * 1024 * 1024

# --------------------------------------------------------------------------
# authentication header (tls / wss / plain carriers)
# --------------------------------------------------------------------------


def auth_key(token: str) -> bytes:
    return hashlib.sha256(b"simurgh/v2/token|" + token.encode("utf-8")).digest()


def client_header(token: str, ts: int | None = None) -> bytes:
    ts = int(time.time()) if ts is None else ts
    key = auth_key(token)
    mac = hmac.new(key, b"simurgh/v2/c" + struct.pack(">Q", ts), hashlib.sha256).digest()[:16]
    return struct.pack(">BQ", VERSION, ts) + mac          # 25 bytes


def check_client_header(header: bytes, token: str, replay=None) -> int | None:
    if len(header) < 25 or header[0] != VERSION:
        return None
    (ts,) = struct.unpack_from(">Q", header, 1)
    key = auth_key(token)
    expect = hmac.new(key, b"simurgh/v2/c" + struct.pack(">Q", ts), hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(header[9:25], expect):
        return None
    if abs(time.time() - ts) > AUTH_WINDOW:
        return None
    if replay is not None and not replay.add(header[9:25]):
        return None
    return ts


def server_header(token: str, client_ts: int, ts: int | None = None) -> bytes:
    ts = int(time.time()) if ts is None else ts
    key = auth_key(token)
    mac = hmac.new(
        key, b"simurgh/v2/s" + struct.pack(">QQ", client_ts, ts), hashlib.sha256
    ).digest()[:16]
    return struct.pack(">BQ", VERSION, ts) + mac


def check_server_header(header: bytes, token: str, client_ts: int) -> bool:
    if len(header) < 25 or header[0] != VERSION:
        return False
    (ts,) = struct.unpack_from(">Q", header, 1)
    key = auth_key(token)
    expect = hmac.new(
        key, b"simurgh/v2/s" + struct.pack(">QQ", client_ts, ts), hashlib.sha256
    ).digest()[:16]
    if not hmac.compare_digest(header[9:25], expect):
        return False
    return abs(time.time() - ts) <= AUTH_WINDOW


class ReplayCache:
    """Small set of recently seen authentication tags."""

    def __init__(self, ttl: float = 300.0, size: int = 20000):
        self.ttl = ttl
        self.size = size
        self._seen: dict[bytes, float] = {}

    def add(self, tag: bytes) -> bool:
        now = time.time()
        if len(self._seen) >= self.size:
            cutoff = now - self.ttl
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        if tag in self._seen:
            return False
        self._seen[tag] = now + self.ttl
        return True


# --------------------------------------------------------------------------
# raw connection: one socket + asyncio transport
# --------------------------------------------------------------------------


class RawConn(asyncio.Protocol):
    """A socket that hands bytes to a callback (no reader task)."""

    def __init__(self, peer: str = ""):
        self.peer = peer
        self.transport: asyncio.Transport | None = None
        self.buf = bytearray()
        self.on_data = None          # callable(bytes)
        self.on_lost = None          # callable(Exception | None)
        self.eof = False
        self._read_waiters: list[asyncio.Future] = []
        self._drain_waiters: list[asyncio.Future] = []
        self._paused = False

    # -- asyncio protocol ---------------------------------------------------
    def connection_made(self, transport):
        self.transport = transport
        try:
            sock = transport.get_extra_info("socket")
            if sock is not None:
                import socket as _s

                sock.setsockopt(_s.IPPROTO_TCP, _s.TCP_NODELAY, 1)
                self.peer = _fmt_sock(transport.get_extra_info("peername"))
        except Exception:
            pass

    def data_received(self, data: bytes) -> None:
        cb = self.on_data
        if cb is not None:
            cb(data)
        else:
            self.buf += data
            self._wake_readers()

    def eof_received(self) -> bool:
        self.eof = True
        self._wake_readers()
        if self.on_lost is not None:
            self.on_lost(None)
        return True  # keep the write side open (half close)

    def connection_lost(self, exc) -> None:
        self.eof = True
        self._wake_readers()
        self._wake_drain()
        if self.on_lost is not None:
            self.on_lost(exc)

    def pause_writing(self) -> None:
        self._paused = True

    def resume_writing(self) -> None:
        self._paused = False
        self._wake_drain()

    # -- plumbing -----------------------------------------------------------
    def _wake_readers(self) -> None:
        for w in self._read_waiters:
            if not w.done():
                w.set_result(None)
        self._read_waiters.clear()

    def _wake_drain(self) -> None:
        for w in self._drain_waiters:
            if not w.done():
                w.set_result(None)
        self._drain_waiters.clear()

    def set_on_data(self, cb) -> None:
        self.on_data = cb
        if self.buf:
            data = bytes(self.buf)
            self.buf.clear()
            cb(data)

    # -- byte stream interface ---------------------------------------------
    def pushback(self, data: bytes) -> None:
        if not data:
            return
        if self.on_data is not None:
            # A TLS layer owns the bytes; it has its own buffer.
            self.on_data(data)
            return
        self.buf[:0] = data

    async def read_some(self, n: int = 262144) -> bytes:
        while not self.buf:
            if self.eof:
                return b""
            loop = asyncio.get_running_loop()
            waiter = loop.create_future()
            self._read_waiters.append(waiter)
            try:
                await waiter
            finally:
                try:
                    self._read_waiters.remove(waiter)
                except ValueError:
                    pass
        if n >= len(self.buf):
            out = bytes(self.buf)
            self.buf.clear()
        else:
            out = bytes(self.buf[:n])
            del self.buf[:n]
        return out

    async def read_exactly(self, n: int) -> bytes:
        while len(self.buf) < n:
            if self.eof:
                data = bytes(self.buf)
                self.buf.clear()
                raise asyncio.IncompleteReadError(data, n)
            loop = asyncio.get_running_loop()
            waiter = loop.create_future()
            self._read_waiters.append(waiter)
            try:
                await waiter
            finally:
                try:
                    self._read_waiters.remove(waiter)
                except ValueError:
                    pass
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def write(self, data) -> None:
        if self.transport is None:
            raise ConnectionError("connection closed")
        self.transport.write(data)

    async def drain(self) -> None:
        if not self._paused or self.transport is None:
            return
        if self.transport.get_write_buffer_size() < 65536:
            return
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        self._drain_waiters.append(waiter)
        try:
            await waiter
        finally:
            try:
                self._drain_waiters.remove(waiter)
            except ValueError:
                pass

    def paused(self) -> bool:
        return bool(self.transport is not None and self.transport.get_write_buffer_size() > 512 * 1024)

    async def wait_writable(self) -> None:
        await self.drain()

    def pause_reading(self) -> None:
        if self.transport is not None:
            try:
                self.transport.pause_reading()
            except Exception:
                pass

    def resume_reading(self) -> None:
        if self.transport is not None:
            try:
                self.transport.resume_reading()
            except Exception:
                pass

    def close(self) -> None:
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass


def _fmt_sock(info) -> str:
    if not info:
        return ""
    if isinstance(info, tuple) and len(info) >= 2:
        return f"{info[0]}:{info[1]}"
    return str(info)


async def tcp_connect(host: str, port: int, timeout: float = 10.0) -> RawConn:
    loop = asyncio.get_running_loop()
    conn = RawConn()
    await asyncio.wait_for(
        loop.create_connection(lambda: conn, host, port), timeout
    )
    return conn


# --------------------------------------------------------------------------
# framing channels
# --------------------------------------------------------------------------


class BaseChannel:
    """Delimits frames on a byte stream and exposes them to the multiplexer."""

    name = "base"

    def __init__(self, peer: str = ""):
        self.peer = peer
        self.closed = False

    async def read_frame(self) -> bytes:
        raise NotImplementedError

    def write_frame(self, frame: bytes) -> None:
        raise NotImplementedError

    async def drain(self) -> None:
        pass

    def paused(self) -> bool:
        return False

    async def wait_writable(self) -> None:
        pass

    async def close(self) -> None:
        """Close the channel *and* the socket underneath it.

        Anything less and a half-dead peer keeps a file descriptor (and the
        relay keeps believing the tunnel is up).
        """
        self.closed = True
        stream = getattr(self, "stream", None)
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


class ByteFrameChannel(BaseChannel):
    """``[4 byte length][frame]`` over any byte stream."""

    def __init__(self, stream, peer: str = "", name: str = "byte"):
        super().__init__(peer)
        self.stream = stream
        self.name = name
        self._buf = bytearray()

    async def _need(self, n: int) -> None:
        while len(self._buf) < n:
            chunk = await self.stream.read_some(262144)
            if not chunk:
                if self._buf:
                    raise asyncio.IncompleteReadError(bytes(self._buf), n)
                raise asyncio.IncompleteReadError(b"", n)
            self._buf += chunk

    async def read_frame(self) -> bytes:
        await self._need(4)
        (n,) = HEADER_LEN_FIELD.unpack_from(self._buf, 0)
        if n > MAX_FRAME:
            raise ConnectionError(f"frame too large: {n}")
        await self._need(4 + n)
        frame = bytes(self._buf[4:4 + n])
        del self._buf[:4 + n]
        return frame

    def write_frame(self, frame: bytes) -> None:
        self.stream.write(HEADER_LEN_FIELD.pack(len(frame)) + frame)

    async def drain(self) -> None:
        await self.stream.drain()

    def paused(self) -> bool:
        return self.stream.paused()

    async def wait_writable(self) -> None:
        await self.stream.wait_writable()

    async def close(self) -> None:
        self.closed = True
        try:
            self.stream.close()
        except Exception:
            pass


class AeadFrameChannel(BaseChannel):
    """Encrypts every frame; optional padding hides exact sizes."""

    BUCKETS = (128, 256, 512, 1024, 2048, 4096, 8192, 16384)

    def __init__(self, inner: BaseChannel, send_key: bytes, recv_key: bytes,
                 pad: bool = True):
        super().__init__(getattr(inner, "peer", ""))
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

        self.inner = inner
        self.name = f"{inner.name}+aead"
        self._send = ChaCha20Poly1305(send_key)
        self._recv = ChaCha20Poly1305(recv_key)
        self.pad = pad
        self._sctr = 0
        self._rctr = 0

    @staticmethod
    def _nonce(ctr: int) -> bytes:
        return b"\x00\x00\x00\x00" + struct.pack(">Q", ctr)

    def _padded(self, frame: bytes) -> bytes:
        if not self.pad:
            return frame + struct.pack(">H", 0)
        target = None
        need = len(frame) + 2
        for b in self.BUCKETS:
            if need <= b:
                target = b
                break
        if target is None:
            target = need + (64 - need % 64)
        padlen = target - need
        return frame + os.urandom(padlen) + struct.pack(">H", padlen)

    async def read_frame(self) -> bytes:
        ct = await self.inner.read_frame()
        try:
            plain = self._recv.decrypt(self._nonce(self._rctr), ct, None)
        except Exception as exc:
            raise ConnectionError("frame authentication failed") from exc
        self._rctr += 1
        if len(plain) < 2:
            raise ConnectionError("short frame")
        (padlen,) = struct.unpack_from(">H", plain, len(plain) - 2)
        if padlen + 2 > len(plain):
            raise ConnectionError("bad padding")
        return plain[: len(plain) - 2 - padlen]

    def write_frame(self, frame: bytes) -> None:
        ct = self._send.encrypt(self._nonce(self._sctr), self._padded(frame), None)
        self._sctr += 1
        self.inner.write_frame(ct)

    async def drain(self) -> None:
        await self.inner.drain()

    def paused(self) -> bool:
        return self.inner.paused()

    async def wait_writable(self) -> None:
        await self.inner.wait_writable()

    async def close(self) -> None:
        self.closed = True
        await self.inner.close()


# --------------------------------------------------------------------------
# WebSocket framing
# --------------------------------------------------------------------------

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WsChannel(BaseChannel):
    """One WebSocket message == one tunnel frame."""

    def __init__(self, stream, peer: str = "", mask: bool = False,
                 name: str = "ws"):
        super().__init__(peer)
        self.stream = stream
        self.name = name
        self.mask = mask
        self._buf = bytearray()

    # -- reading ------------------------------------------------------------
    async def _need(self, n: int) -> None:
        while len(self._buf) < n:
            chunk = await self.stream.read_some(262144)
            if not chunk:
                raise asyncio.IncompleteReadError(bytes(self._buf), n)
            self._buf += chunk

    async def read_frame(self) -> bytes:
        while True:
            await self._need(2)
            b0, b1 = self._buf[0], self._buf[1]
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            offset = 2
            if length == 126:
                await self._need(4)
                (length,) = struct.unpack_from(">H", self._buf, 2)
                offset = 4
            elif length == 127:
                await self._need(10)
                (length,) = struct.unpack_from(">Q", self._buf, 2)
                offset = 10
            if length > MAX_FRAME:
                raise ConnectionError(f"websocket frame too large: {length}")
            if masked:
                await self._need(offset + 4)
                mask = bytes(self._buf[offset:offset + 4])
                offset += 4
            else:
                mask = b""
            await self._need(offset + length)
            payload = bytearray(self._buf[offset:offset + length])
            del self._buf[:offset + length]
            if mask:
                for i in range(len(payload)):
                    payload[i] ^= mask[i & 3]
            if opcode == OP_CLOSE:
                raise asyncio.IncompleteReadError(b"", 0)
            if opcode == OP_PING:
                self._send_pong(bytes(payload))
                continue
            if opcode == OP_PONG:
                continue
            if opcode in (0x0, 0x1, OP_BIN):
                if fin:
                    return bytes(payload)
                # continuation frames: collect until FIN
                data = bytearray(payload)
                while True:
                    await self._need(2)
                    b0, b1 = self._buf[0], self._buf[1]
                    fin = b0 & 0x80
                    length = b1 & 0x7F
                    offset = 2
                    if length == 126:
                        await self._need(4)
                        (length,) = struct.unpack_from(">H", self._buf, 2)
                        offset = 4
                    elif length == 127:
                        await self._need(10)
                        (length,) = struct.unpack_from(">Q", self._buf, 2)
                        offset = 10
                    if b1 & 0x80:
                        await self._need(offset + 4)
                        mask = bytes(self._buf[offset:offset + 4])
                        offset += 4
                    else:
                        mask = b""
                    await self._need(offset + length)
                    chunk = bytearray(self._buf[offset:offset + length])
                    del self._buf[:offset + length]
                    if mask:
                        for i in range(len(chunk)):
                            chunk[i] ^= mask[i & 3]
                    data += chunk
                    if fin:
                        return bytes(data)
                continue
            continue

    # -- writing ------------------------------------------------------------
    def _send_pong(self, payload: bytes) -> None:
        self.stream.write(self._encode(payload, OP_PONG))

    def _encode(self, payload: bytes, opcode: int = OP_BIN) -> bytes:
        head = bytearray([0x80 | opcode])
        n = len(payload)
        maskbit = 0x80 if self.mask else 0
        if n < 126:
            head.append(maskbit | n)
        elif n < 65536:
            head.append(maskbit | 126)
            head += struct.pack(">H", n)
        else:
            head.append(maskbit | 127)
            head += struct.pack(">Q", n)
        if self.mask:
            key = os.urandom(4)
            head += key
            payload = bytes(b ^ key[i & 3] for i, b in enumerate(payload))
        return bytes(head) + payload

    def write_frame(self, frame: bytes) -> None:
        self.stream.write(self._encode(frame))

    async def drain(self) -> None:
        await self.stream.drain()

    def paused(self) -> bool:
        return self.stream.paused()

    async def wait_writable(self) -> None:
        await self.stream.wait_writable()

    async def close(self) -> None:
        self.closed = True
        try:
            self.stream.write(self._encode(b"", OP_CLOSE))
        except Exception:
            pass
        try:
            self.stream.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# client side
# --------------------------------------------------------------------------


async def client_connect(carrier: str, host: str, port: int, token: str, *,
                         domain: str | None = None, path: str = "/",
                         cert_fingerprint: str | None = None,
                         insecure: bool = False,
                         connect_timeout: float = 10.0,
                         padding: bool = True,
                         ws_headers: dict | None = None,
                         chunk: int = DEFAULT_CHUNK) -> BaseChannel:
    """Open a client-side tunnel channel using *carrier*."""
    carrier = carrier.lower()
    sni = domain or host

    if carrier == "plain":
        conn = await tcp_connect(host, port, connect_timeout)
        ts = int(time.time())
        conn.write(client_header(token, ts))
        await conn.drain()
        resp = await asyncio.wait_for(conn.read_exactly(25), connect_timeout)
        if not check_server_header(resp, token, ts):
            conn.close()
            raise ConnectionError("server failed authentication (wrong token?)")
        return ByteFrameChannel(conn, peer=conn.peer, name="plain")

    if carrier == "raw":
        from .obfs import raw_client  # lazy: needs `cryptography`

        return await raw_client(host, port, token, connect_timeout, padding)

    if carrier in ("tls", "wss"):
        ctx = client_context(sni, insecure or bool(cert_fingerprint))
        conn = await tcp_connect(host, port, connect_timeout)
        tls = TlsLayer(conn, ctx, server_side=False, server_hostname=sni)
        await tls.handshake(connect_timeout)
        if cert_fingerprint:
            verify_pin(tls, cert_fingerprint)
        ts = int(time.time())
        if carrier == "tls":
            # The auth header travels as raw bytes right after the TLS
            # handshake (Trojan style); frames follow it.
            tls.write(client_header(token, ts))
            await tls.drain()
            resp = await asyncio.wait_for(read_at_least(tls, 25), connect_timeout)
            if not check_server_header(resp[:25], token, ts):
                raise ConnectionError("server failed authentication (wrong token?)")
            return ByteFrameChannel(tls, peer=conn.peer, name="tls")
        ch = await _ws_client_hello(tls, sni, path, ws_headers)
        ch.write_frame(client_header(token, ts))
        await ch.drain()
        resp = await asyncio.wait_for(ch.read_frame(), connect_timeout)
        if not check_server_header(resp, token, ts):
            raise ConnectionError("server failed authentication (wrong token?)")
        return ch

    raise ValueError(f"unknown carrier: {carrier!r}")


async def read_at_least(stream, n: int, timeout: float = 15.0) -> bytes:
    """Read at least *n* bytes (more is fine, it stays buffered upstream)."""
    data = bytearray()
    while len(data) < n:
        chunk = await asyncio.wait_for(stream.read_some(n - len(data)), timeout)
        if not chunk:
            break
        data += chunk
    return bytes(data)


async def _ws_client_hello(tls, host: str, path: str, extra: dict | None = None) -> WsChannel:
    import base64

    key = base64.b64encode(os.urandom(16)).decode()
    headers = {
        "Host": host,
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Key": key,
        "Sec-WebSocket-Version": "13",
    }
    if extra:
        headers.update(extra)
    req = f"GET {path or '/'} HTTP/1.1\r\n" + "".join(
        f"{k}: {v}\r\n" for k, v in headers.items()
    ) + "\r\n"
    tls.write(req.encode())
    await tls.drain()
    head = await _read_http_head(tls, 8192)
    status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    if "101" not in status:
        raise ConnectionError(f"websocket upgrade refused: {status}")
    digest = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    if digest.lower() not in head.decode("latin-1", "replace").lower():
        raise ConnectionError("bad websocket accept key")
    return WsChannel(tls, mask=True, name="wss")


async def _read_http_head(stream, limit: int = 16384) -> bytes:
    """Read one HTTP head; anything read beyond it is pushed back."""
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = await stream.read_some(4096)
        if not chunk:
            raise ConnectionError("connection closed during HTTP head")
        data += chunk
        if len(data) > limit:
            raise ConnectionError("HTTP head too large")
    end = data.index(b"\r\n\r\n") + 4
    leftover = bytes(data[end:])
    if leftover:
        stream.pushback(leftover)
    return bytes(data[:end])


def verify_pin(tls: TlsLayer, fingerprint: str) -> None:
    """Certificate pinning: the exit presents a self-signed certificate and the
    relay checks its SHA-256 fingerprint.  No CA, no expiry, no MITM."""
    der = tls.sslobj.getpeercert(binary_form=True)
    if not der:
        raise ConnectionError("server presented no certificate")
    got = hashlib.sha256(der).hexdigest()
    want = fingerprint.replace(":", "").replace(" ", "").lower()
    if not hmac.compare_digest(got, want):
        raise ConnectionError(
            f"certificate fingerprint mismatch (got {got[:16]}..., want {want[:16]}...)"
        )


# --------------------------------------------------------------------------
# server side
# --------------------------------------------------------------------------


class ServerCarrier:
    """Server-side view of one configured carrier."""

    def __init__(self, carrier: str, token: str, *, cert_file: str | None = None,
                 key_file: str | None = None, path: str = "/",
                 fallback: str = "decoy", decoy_file: str | None = None,
                 padding: bool = True, chunk: int = DEFAULT_CHUNK):
        self.carrier = carrier.lower()
        self.token = token
        self.path = path or "/"
        self.fallback = fallback or "decoy"
        self.decoy_file = decoy_file
        self.padding = padding
        self.chunk = chunk
        self.replay = ReplayCache()
        self.ctx = None
        if self.carrier in ("tls", "wss"):
            if not (cert_file and key_file):
                raise ValueError(f"carrier {carrier!r} needs cert_file and key_file")
            self.ctx = server_context(cert_file, key_file)
        elif self.carrier == "raw" and padding:
            from .certs import have_cryptography

            if not have_cryptography():  # pragma: no cover
                raise ValueError(
                    "carrier 'raw' needs the 'cryptography' package "
                    "(pip install cryptography)"
                )

    def protocol_factory(self, on_channel):
        """An asyncio protocol factory for ``loop.create_server``."""

        def factory() -> RawConn:
            conn = RawConn()
            task = asyncio.ensure_future(self._serve(conn, on_channel))
            conn._handler = task  # type: ignore[attr-defined]
            task.add_done_callback(_swallow)
            return conn

        return factory

    async def _serve(self, conn: RawConn, on_channel) -> None:
        try:
            if self.carrier == "plain":
                await _server_plain(conn, self, on_channel)
            elif self.carrier == "raw":
                from .obfs import raw_server

                await raw_server(conn, self, on_channel)
            else:
                await _server_tls_like(conn, self, on_channel)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("carrier %s error from %s: %s", self.carrier, conn.peer, exc)
        finally:
            conn.close()


def _swallow(task: asyncio.Task) -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


async def _server_plain(conn: RawConn, carrier: ServerCarrier, on_channel) -> None:
    try:
        header = await asyncio.wait_for(conn.read_exactly(25), 15)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
        conn.close()
        return
    if check_client_header(header, carrier.token, carrier.replay) is None:
        conn.close()
        return
    ts = struct.unpack_from(">Q", header, 1)[0]
    conn.write(server_header(carrier.token, ts))
    await conn.drain()
    channel = ByteFrameChannel(conn, peer=conn.peer, name="plain")
    await on_channel(channel)


async def _server_tls_like(conn: RawConn, carrier: ServerCarrier, on_channel) -> None:
    """Sniff the first bytes: TLS tunnel, or a prober we must fool."""
    from .decoy import serve_decoy_plain

    try:
        first = await asyncio.wait_for(conn.read_some(3), 20)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        conn.close()
        return
    if not first:
        conn.close()
        return

    if not (len(first) >= 3 and first[0] == 0x16 and first[1] == 0x03):
        # Not TLS at all: someone is speaking HTTP/anything to our port.
        await serve_decoy_plain(conn, first, carrier)
        return

    tls = TlsLayer(conn, carrier.ctx, server_side=True, preseed=first)
    try:
        await tls.handshake(20)
    except Exception:
        conn.close()
        return

    if carrier.carrier == "wss":
        # --- WebSocket life -------------------------------------------------
        try:
            head = await asyncio.wait_for(_read_http_head(tls), 15)
        except Exception:
            conn.close()
            return
        if not _ws_upgrade_ok(head, carrier):
            await serve_decoy(tls, carrier, initial=head)
            return
        _send_ws_upgrade_ok(tls, head)
        await tls.drain()
        channel: BaseChannel = WsChannel(tls, mask=False, name="wss")
        try:
            auth = await asyncio.wait_for(channel.read_frame(), 15)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            conn.close()
            return
        ts = check_client_header(auth, carrier.token) if len(auth) >= 25 else None
        if ts is None:
            conn.close()   # spoke WebSocket but not our protocol: say nothing
            return
        channel.write_frame(server_header(carrier.token, ts))
        await channel.drain()
        await on_channel(channel)
        return

    # --- plain TLS life -----------------------------------------------------
    try:
        head = await asyncio.wait_for(read_at_least(tls, 25), 15)
    except (asyncio.TimeoutError, ConnectionError, OSError):
        conn.close()
        return
    ts = check_client_header(head[:25], carrier.token) if len(head) >= 25 else None
    if ts is None:
        # Valid TLS, wrong (or missing) credentials: show the decoy website
        # over this very TLS session, exactly like a normal web server.
        await serve_decoy(tls, carrier, initial=head)
        return
    if len(head) > 25:
        tls.pushback(head[25:])
    tls.write(server_header(carrier.token, ts))
    await tls.drain()
    await on_channel(ByteFrameChannel(tls, peer=conn.peer, name="tls"))


def _ws_upgrade_ok(head: bytes, carrier: ServerCarrier) -> bool:
    text = head.decode("latin-1", "replace")
    lines = text.split("\r\n")
    request = lines[0].split(" ")
    path = request[1] if len(request) > 1 else "/"
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    up = headers.get("upgrade", "").lower()
    conn = headers.get("connection", "").lower()
    return (
        path.split("?")[0] == carrier.path
        and up == "websocket"
        and "upgrade" in conn
        and "sec-websocket-key" in headers
    )


def _send_ws_upgrade_ok(tls, head: bytes) -> None:
    import base64

    key = ""
    for line in head.decode("latin-1", "replace").split("\r\n")[1:]:
        k, _, v = line.partition(":")
        if k.strip().lower() == "sec-websocket-key":
            key = v.strip()
            break
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    tls.write((
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode())

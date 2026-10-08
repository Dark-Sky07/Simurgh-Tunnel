"""TLS driven through ``ssl.MemoryBIO``.

Why not ``loop.start_tls``?  Because on the relay side one port has to serve
*two* different lives at once: the tunnel (TLS) and the decoy website (plain
HTTP) for probers.  ``MemoryBIO`` lets us peek at the first bytes, decide which
life to live, and then hand the already-read bytes to the TLS engine.

The transport callbacks feed the BIOs directly (``data_received``) instead of a
separate reader task: one less coroutine and one less hop through a queue on
every packet.
"""

from __future__ import annotations

import asyncio
import ssl


class TlsError(Exception):
    pass


class TlsLayer:
    """Decrypted byte stream on top of :class:`RawConn`."""

    def __init__(self, conn, ctx: ssl.SSLContext, server_side: bool,
                 server_hostname: str | None = None, preseed: bytes = b""):
        self.conn = conn
        self.ctx = ctx
        self.server_side = server_side
        self.server_hostname = server_hostname
        self.inbio = ssl.MemoryBIO()
        self.outbio = ssl.MemoryBIO()
        self.sslobj = ctx.wrap_bio(
            self.inbio, self.outbio, server_side=server_side,
            server_hostname=None if server_side else server_hostname,
        )
        if preseed:
            self.inbio.write(preseed)
        self.abuf = bytearray()
        self.eof = False
        self._err: Exception | None = None
        self._waiter: asyncio.Future | None = None
        # set_on_data (not a plain assignment): bytes already sniffed from the
        # socket -- e.g. the whole ClientHello -- must reach the TLS engine.
        if hasattr(conn, "set_on_data"):
            conn.set_on_data(self._on_data)
        else:  # pragma: no cover
            conn.on_data = self._on_data
        conn.on_lost = self._on_lost

    # ------------------------------------------------------------ callbacks
    def _on_data(self, data: bytes) -> None:
        try:
            self.inbio.write(data)
        except Exception as exc:  # BIO overflow / closed
            self._on_lost(exc)
            return
        self._pump_in()
        # Wake unconditionally: during the handshake there is no application
        # data at all, yet the handshake loop must be told to continue.
        self._wake()

    def _on_lost(self, exc: Exception | None) -> None:
        self.eof = True
        self._err = exc
        self._wake()

    def _wake(self) -> None:
        w = self._waiter
        if w is not None and not w.done():
            w.set_result(None)

    def _pump_in(self) -> None:
        """Move every decrypted chunk the engine is holding into our buffer."""
        buf = self.abuf
        while True:
            try:
                chunk = self.sslobj.read(262144)
            except ssl.SSLWantReadError:
                break
            except (ssl.SSLError, ssl.SSLEOFError, ConnectionError, OSError) as exc:
                self.eof = True
                self._err = exc
                break
            if not chunk:
                break
            buf += chunk
        if buf:
            self._wake()

    def _flush_out(self) -> None:
        data = self.outbio.read()
        if data:
            self.conn.write(data)

    # -------------------------------------------------------------- control
    async def handshake(self, timeout: float = 15.0) -> None:
        async def _do() -> None:
            while True:
                try:
                    self.sslobj.do_handshake()
                    break
                except ssl.SSLWantReadError:
                    self._flush_out()
                    if self.eof:
                        raise TlsError("connection closed during handshake")
                    await self._wait_more()
                    if self.eof:
                        # one last try: the peer may have sent its final flight
                        try:
                            self.sslobj.do_handshake()
                            break
                        except Exception as exc:
                            raise TlsError(f"handshake failed: {exc}") from exc
                except ssl.SSLWantWriteError:
                    self._flush_out()
                    await self.conn.drain()
                except Exception as exc:
                    raise TlsError(f"handshake failed: {exc}") from exc
            self._flush_out()
            await self.conn.drain()

        await asyncio.wait_for(_do(), timeout)

    async def _wait_more(self) -> None:
        """Wait until the BIOs got more bytes (or the connection died)."""
        # Anything the engine already has buffered?
        self._pump_in()
        marker = len(self.abuf)
        waiter = asyncio.get_running_loop().create_future()
        self._waiter = waiter
        try:
            await waiter
        finally:
            self._waiter = None
        _ = marker  # (kept for clarity: the loop re-reads unconditionally)

    # ------------------------------------------------------------------ read
    def pushback(self, data: bytes) -> None:
        """Put bytes back at the front of the app buffer (HTTP head readers)."""
        if data:
            self.abuf[:0] = data
            self._wake()

    async def read_some(self, n: int = 262144) -> bytes:
        while not self.abuf:
            if self._err is not None:
                raise self._err
            if self.eof:
                return b""
            loop = asyncio.get_running_loop()
            waiter = loop.create_future()
            self._waiter = waiter
            try:
                await waiter
            finally:
                self._waiter = None
            self._pump_in()
        if n >= len(self.abuf):
            out = bytes(self.abuf)
            self.abuf.clear()
        else:
            out = bytes(self.abuf[:n])
            del self.abuf[:n]
        return out

    # ----------------------------------------------------------------- write
    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                n = self.sslobj.write(view)
            except ssl.SSLWantWriteError:
                self._flush_out()
                break
            except (ssl.SSLError, OSError):
                raise TlsError("TLS write failed") from None
            if n <= 0:
                break
            self._flush_out()
            view = view[n:]
        self._flush_out()

    async def drain(self) -> None:
        self._flush_out()
        await self.conn.drain()

    # -- transport-like interface used by the multiplexer -------------------
    def paused(self) -> bool:
        return self.conn.paused()

    async def wait_writable(self) -> None:
        await self.conn.wait_writable()

    def pause_reading(self) -> None:
        self.conn.pause_reading()

    def resume_reading(self) -> None:
        self.conn.resume_reading()

    def close(self) -> None:
        try:
            self.sslobj.unwrap()
        except Exception:
            pass
        try:
            self._flush_out()
        except Exception:
            pass
        self.conn.close()


# --------------------------------------------------------------------------
# contexts
# --------------------------------------------------------------------------

def server_context(cert_file: str, key_file: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_file, key_file)
    ctx.options |= ssl.OP_NO_COMPRESSION
    try:
        ctx.set_alpn_protocols(["http/1.1", "h2"])
    except NotImplementedError:  # pragma: no cover
        pass
    return ctx


def client_context(sni: str | None = None, insecure: bool = False) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.options |= ssl.OP_NO_COMPRESSION
    ctx.set_alpn_protocols(["http/1.1"])
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def fingerprint(cert_file: str) -> str:
    """SHA-256 of the certificate (for ``cert_fingerprint`` pinning)."""
    import hashlib

    with open(cert_file, "rb") as f:
        data = f.read()
    # strip PEM armour if present
    if b"-----BEGIN" in data:
        import base64

        body = b""
        for line in data.splitlines():
            if line.startswith(b"-----") or not line.strip():
                continue
            body += line.strip()
        try:
            data = base64.b64decode(body)
        except Exception:
            pass
    return hashlib.sha256(data).hexdigest()

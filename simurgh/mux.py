"""Stream multiplexer.

One tunnel connection between the Iranian relay and a foreign exit carries
every user connection inside it.  That is the whole point: a filter sees a
single long-lived flow instead of a thousand short ones, the kernel keeps one
socket instead of a thousand, and a network blip costs one reconnect.

The multiplexer is written for the Python hot path:

* **push, not pull.**  Received stream data is handed to a callback which
  writes it straight into the destination transport -- no per-stream task, no
  extra queue, one copy.
* **credit based flow control.**  A receiver only grants credit for bytes it
  has really pushed downstream, so a slow user cannot make the far side grow
  without limit.  Per stream *and* per connection.
* **no per-frame allocation** beyond the payload itself, and frames are
  dispatched with a dict lookup on the stream id.
"""

from __future__ import annotations

import asyncio
import struct
import time

from .protocol import (
    DEFAULT_CHUNK,
    GLOBAL_WINDOW_CAP,
    HEADER,
    DEFAULT_GLOBAL_WINDOW,
    DEFAULT_MAX_STREAM_WINDOW,
    DEFAULT_STREAM_WINDOW,
    T_CLOSE,
    T_CTRL,
    T_DATA,
    T_EOF,
    T_OPEN,
    T_OPEN_ERR,
    T_OPEN_OK,
    T_PING,
    T_PONG,
    T_WIN,
    WIN,
    err_frame,
    frame_sid,
    frame_type,
    make_frame,
)

OPEN_TIMEOUT = 20.0
PING_INTERVAL = 25.0
#: used to size the window until the first RTT measurement comes in
DEFAULT_RTT = 0.12
#: never re-evaluate a window more often than this
WINDOW_EVAL_MIN = 0.05

_log = __import__("logging").getLogger("simurgh.mux")


class StreamOpenError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


class Stream:
    """One logical connection inside the tunnel."""

    __slots__ = (
        "mux", "sid", "mode", "target", "send_credit", "recv_unacked",
        "closed", "eof", "_on_data", "on_eof", "on_credit", "open_ok",
        "open_error", "open_event", "created", "last_active", "pending",
        "credit_flush", "window", "base_window", "max_window", "peer_credit",
        "backlog", "backlog_peak", "last_flush", "rate_bytes", "rate_start",
        "data_hdr",
    )

    def __init__(self, mux: "Mux", sid: int, mode: int = 0, target: bytes = b""):
        self.mux = mux
        self.sid = sid
        #: type+sid of every DATA frame this stream sends (built once)
        self.data_hdr = HEADER.pack(T_DATA, sid)
        self.mode = mode
        self.target = target
        self.send_credit = mux.stream_window
        self.recv_unacked = 0
        self.closed = False
        self.eof = False
        self.open_ok = False
        self.open_error = ""
        self.open_event = asyncio.Event()
        self._on_data = None     # callable(bytes) -> None
        self.on_eof = None       # callable() -> None
        self.on_credit = None    # callable() -> None (more send credit arrived)
        #: data that arrived before the owner attached on_data (a target that
        #: speaks first -- SSH banners, TLS servers, ...).  Bounded by the
        #: peer's flow-control window, so it cannot grow without limit.
        self.pending = bytearray()
        self.credit_flush = self.CREDIT_FLUSH
        self.created = time.monotonic()
        self.last_active = self.created
        # --- adaptive receive window -------------------------------------
        # ``window`` is what we currently let the peer keep in flight; it
        # starts at the configured base and doubles (up to ``max_window``)
        # while this stream keeps draining quickly.  A window that is too
        # small caps one user at window/RTT, which is exactly what hurts on a
        # long path (256 KiB / 120 ms ~= 17 Mbit/s no matter how fast the
        # servers are).  Growing is only allowed while we are *not* behind.
        self.base_window = mux.stream_window
        self.max_window = mux.max_stream_window
        self.window = self.base_window
        self.peer_credit = self.base_window      # our estimate of the peer's budget
        self.backlog = 0                         # received but not yet consumed
        self.backlog_peak = 0
        self.last_flush = self.created
        self.rate_bytes = 0
        self.rate_start = self.created

    # ``on_data`` is a property so that attaching a handler automatically
    # replays whatever was buffered while nobody was listening.
    @property
    def on_data(self):
        return self._on_data

    @on_data.setter
    def on_data(self, cb):
        self._on_data = cb
        if cb is not None and self.pending:
            data = bytes(self.pending)
            self.pending.clear()
            cb(data)

    # -------------------------------------------------------------- receive
    def grant(self, n: int) -> None:
        """Credit the peer for *n* bytes we have pushed downstream."""
        if n <= 0:
            return
        self.recv_unacked += n

    #: credit is flushed once this much has piled up (keeps WIN frames rare)
    CREDIT_FLUSH = 16 * 1024

    def flush_credit(self, force: bool = False) -> None:  # noqa: D401
        """Send the accumulated credit back to the peer (one tiny frame).

        Not flushing on every chunk matters: at 1 Gbit/s a WIN frame per DATA
        frame would cost ~20% overhead, while a 16 KiB threshold keeps the peer
        with plenty of headroom and the control traffic invisible.
        """
        n = self.recv_unacked
        if n <= 0:
            return
        if not force and n < self.credit_flush:
            return
        m = self.mux
        if m is None or m.closed:
            return
        self.recv_unacked = 0
        self.backlog = max(0, self.backlog - n)
        self.rate_bytes += n
        bonus = self._autotune()
        grant = n + bonus
        self.peer_credit += grant
        g = m.take_global_credit(grant)
        m.send_soon(make_frame(T_WIN, self.sid, WIN.pack(min(grant, 0xFFFFFFFF), g)))

    def _autotune(self) -> int:
        """Decide how much *extra* credit to hand back (window autotuning).

        The rule is the classic bandwidth-delay product one: a window of ``W``
        can carry at most ``W / RTT``; if the stream is really running that
        fast, the window is the bottleneck and doubling it doubles what one
        user can pull.  If the stream runs slower than the window allows, it is
        limited by something else (the sender itself, a rate limit, a slow
        client) and a bigger window would only park memory here.

        Two safety valves: never grow while we are behind (the backlog shows
        that our own socket is the slow side) and fall back towards the base
        window when a stream goes quiet, so nothing is held for idle users.
        """
        now = time.monotonic()
        dt = now - self.rate_start
        peak, self.backlog_peak = self.backlog_peak, self.backlog
        if self.mux is None:
            return 0
        rtt = self.mux.rtt or DEFAULT_RTT
        if dt < max(WINDOW_EVAL_MIN, rtt) or self.max_window <= self.base_window:
            return 0
        rate = self.rate_bytes / dt
        self.rate_bytes = 0
        self.rate_start = now
        self.last_flush = now
        capacity = self.window / max(rtt, 0.001)
        bonus = 0
        if rate >= 0.7 * capacity and self.window < self.max_window:
            # window limited and keeping up: probe a bigger one
            new_window = min(self.max_window, self.window * 2)
            bonus = new_window - self.window
            self._set_window(new_window)
            _log.debug("stream %s: window -> %d (rate %.1f MB/s over %.0f ms)",
                       self.sid, new_window, rate / 1e6, dt * 1e3)
        elif peak == 0 and self.backlog == 0 and rate < 0.05 * capacity \
                and self.window > self.base_window:
            # Shrinking is for streams that went quiet: it gives their share of
            # the shared credit budget back.  A busy stream that is merely slow
            # is limited by its own client, and halving its window here is what
            # once collapsed a whole connection to a crawl.
            self._set_window(max(self.base_window, self.window // 2))
            _log.debug("stream %s: window -> %d (idle)", self.sid, self.window)
        return bonus

    def _set_window(self, window: int) -> None:
        """Resize this stream's window and keep the connection total honest."""
        if self.mux is not None:
            self.mux.window_sum += window - self.window
            if self.mux.window_sum < 0:
                self.mux.window_sum = 0
        self.window = window

    # --------------------------------------------------------------- send
    def can_send(self, n: int) -> int:
        """How many of *n* bytes the flow-control budget allows right now."""
        return max(0, min(n, self.send_credit, self.mux.global_credit))

    def write_now(self, data: bytes) -> None:
        """Send *data* immediately (caller must have checked ``can_send``)."""
        n = len(data)
        self.send_credit -= n
        self.mux.global_credit -= n
        self.mux.send_data(self.data_hdr, data)

    def close(self, rst: bool = False) -> None:
        """Half close (default) or hard close the stream."""
        if self.closed:
            return
        self.closed = True
        self.flush_credit(force=True)
        if rst:
            self.mux.send_soon(make_frame(T_CLOSE, self.sid))
            self.mux.drop_stream(self.sid)
        else:
            self.eof = True
            self.mux.send_soon(make_frame(T_EOF, self.sid))
            if self.mux is not None:
                self.mux.maybe_drop(self.sid)

    def touch(self) -> None:
        self.last_active = time.monotonic()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Stream {self.sid} mode={self.mode} closed={self.closed}>"


class Mux:
    """Multiplexer over one :class:`~simurgh.carriers.Channel`."""

    def __init__(
        self,
        channel,
        is_relay: bool,
        on_open=None,
        on_ctrl=None,
        stream_window: int = DEFAULT_STREAM_WINDOW,
        global_window: int = 0,
        max_stream_window: int = DEFAULT_MAX_STREAM_WINDOW,
        chunk: int = DEFAULT_CHUNK,
    ):
        self.channel = channel
        self.is_relay = is_relay
        self.on_open = on_open          # async callable(stream) -> None
        self.on_ctrl = on_ctrl          # async callable(dict) -> None
        self.stream_window = stream_window
        self.max_stream_window = max(stream_window, max_stream_window)
        self.chunk = chunk
        #: explicit ceiling, or 0 to scale with the number of live streams
        self.global_window_limit = global_window
        #: sum of the live streams' current windows (see _global_window)
        self.window_sum = 0
        #: how much of the peer's budget we have consumed and not yet credited
        self.recv_unacked = 0
        #: how much *we* may still send before the peer has to credit us again
        #: (with no streams open the ceiling is the floor; self.streams does not
        #: exist yet, so it is spelled out here)
        self.global_credit = self.global_window_limit or DEFAULT_GLOBAL_WINDOW
        self.streams: dict[int, Stream] = {}
        self.closed = False
        self.started = time.monotonic()
        self.stats = {
            "bytes_in": 0, "bytes_out": 0, "frames_in": 0, "frames_out": 0,
            "streams_total": 0, "streams_open": 0, "last_error": "",
        }
        #: round trip time measured from our own keepalives (None until known)
        self.rtt: float | None = None
        self._next_sid = 1 if is_relay else 2
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_writes: list[bytes] = []
        self._flush_handle: asyncio.TimerHandle | None = None
        self._opened = asyncio.Event()

    # ------------------------------------------------------------ utilities
    def set_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    def _send_failed(self, exc: Exception) -> None:
        # dead channel: the read loop will tear down
        self.closed = True
        self.stats["last_error"] = "%s: %s" % (type(exc).__name__, exc)
        _log.debug("mux send failed (%s)", exc, exc_info=True)

    def send(self, frame: bytes) -> None:
        """Write one frame, using the channel (called from the read loop)."""
        self.stats["frames_out"] += 1
        self.stats["bytes_out"] += len(frame)
        try:
            self.channel.write_frame(frame)
        except Exception as exc:
            self._send_failed(exc)

    def send_data(self, hdr: bytes, payload: bytes) -> None:
        """Write one DATA frame without gluing the payload to its header."""
        self.stats["frames_out"] += 1
        self.stats["bytes_out"] += len(hdr) + len(payload)
        try:
            self.channel.write_data(hdr, payload)
        except Exception as exc:
            self._send_failed(exc)

    def send_soon(self, frame: bytes) -> None:
        """Coalesce small control frames into the next event-loop tick.

        A read loop that produces many ``WIN``/``EOF`` frames would otherwise do
        a syscall per frame; batching them keeps the control traffic invisible.
        """
        self._pending_writes.append(frame)
        loop = self._loop or asyncio.get_running_loop()
        self._loop = loop
        if self._flush_handle is None:
            self._flush_handle = loop.call_soon(self._flush_pending)

    def _flush_pending(self) -> None:
        self._flush_handle = None
        pending, self._pending_writes = self._pending_writes, []
        for frame in pending:
            if self.closed:
                break
            self.send(frame)

    def _global_window(self) -> int:
        """The connection wide credit ceiling.

        A tunnel carrying a thousand users must not be limited to the in-flight
        budget of one stream, so the shared ceiling follows the sum of the live
        streams' *current* windows: a 16 MiB floor for the quiet case, and a
        64 MiB cap so a peer that stops reading still cannot park more than
        that here.  Following the current windows (not the initial ones) is what
        keeps a busy connection from refusing every credit grant once its
        streams have grown.  ``global_window`` overrides both.
        """
        if self.global_window_limit > 0:
            return self.global_window_limit
        return min(max(DEFAULT_GLOBAL_WINDOW, self.window_sum), GLOBAL_WINDOW_CAP)

    def take_global_credit(self, n: int) -> int:
        """How much connection-wide credit we can hand back for *n* bytes we
        just consumed.

        The rule that keeps the tunnel alive: we never allow the peer to have
        more bytes in flight than the shared ceiling, and we credit back
        everything we consume.  (Getting this wrong is how a tunnel stalls
        after exactly one window of download traffic.)
        """
        room = self._global_window() - self.recv_unacked
        grant = max(0, min(n, room))
        self.recv_unacked -= grant
        return grant

    # --------------------------------------------------------------- streams
    async def open(self, target: bytes, mode: int = 0) -> Stream:
        sid = self._next_sid
        self._next_sid += 2
        stream = Stream(self, sid, mode=mode, target=target)
        self.streams[sid] = stream
        self.window_sum += stream.window
        self.stats["streams_total"] += 1
        self.stats["streams_open"] = len(self.streams)
        self.send(make_frame(T_OPEN, sid, bytes((mode,)) + target))
        try:
            await asyncio.wait_for(stream.open_event.wait(), OPEN_TIMEOUT)
        except asyncio.TimeoutError:
            stream.close(rst=True)
            raise StreamOpenError("tunnel open timeout") from None
        if not stream.open_ok:
            if self.streams.pop(sid, None) is not None:
                self.window_sum = max(0, self.window_sum - stream.window)
            raise StreamOpenError(stream.open_error or "open failed")
        return stream

    def accept(self, sid: int, mode: int = 0, target: bytes = b"") -> Stream:
        stream = Stream(self, sid, mode=mode, target=target)
        self.streams[sid] = stream
        self.window_sum += stream.window
        self.stats["streams_total"] += 1
        self.stats["streams_open"] = len(self.streams)
        return stream

    def drop_stream(self, sid: int) -> None:
        s = self.streams.pop(sid, None)
        if s is not None:
            self.window_sum = max(0, self.window_sum - s.window)
            s.closed = True
            s.eof = True
            if s.on_eof:
                try:
                    s.on_eof()
                except Exception:
                    pass
        self.stats["streams_open"] = len(self.streams)

    def maybe_drop(self, sid: int) -> None:
        """A half-closed stream is kept until both sides are done."""
        s = self.streams.get(sid)
        if s is not None and s.closed and s.eof:
            self.streams.pop(sid, None)
            self.window_sum = max(0, self.window_sum - s.window)
            self.stats["streams_open"] = len(self.streams)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for sid in list(self.streams):
            s = self.streams.pop(sid, None)
            if s is not None:
                self.window_sum = max(0, self.window_sum - s.window)
                s.closed = s.eof = True
                if s.on_eof:
                    try:
                        s.on_eof()
                    except Exception:
                        pass
        self._opened.set()
        # Wake the reader up: without this the read loop sits on a socket that
        # nobody will ever write to again, and the peer never learns we left.
        ch = self.channel
        if ch is not None and not getattr(ch, "closed", False):
            try:
                asyncio.get_running_loop().create_task(self._close_channel(ch))
            except RuntimeError:  # no running loop (unit tests)
                pass

    async def _close_channel(self, ch) -> None:
        try:
            await ch.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def wait_closed(self) -> None:
        await self._opened.wait()

    # ---------------------------------------------------------------- reader
    async def run(self) -> None:
        """Consume frames until the channel dies."""
        self.set_loop()
        ch = self.channel
        try:
            while True:
                frames = await ch.read_frames()
                if not frames:
                    break
                for frame in frames:
                    self.stats["frames_in"] += 1
                    self._dispatch(frame)
                if ch.paused():
                    await ch.wait_writable()
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
            # normal end of a tunnel (EOF, reset, refused): not worth a warning
            _log.debug("read loop ended: %r", exc)
        except asyncio.CancelledError:
            # never swallow cancellation: a shutdown that ignores it turns
            # into a reconnect storm (the caller's supervisor keeps looping).
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self.stats["last_error"] = f"{type(exc).__name__}: {exc}"
            _log.debug("read loop failed on %s: %r", getattr(ch, "name", "?"), exc,
                       exc_info=True)
        finally:
            self.close()
            try:
                await ch.close()
            except Exception:
                pass

    # -------------------------------------------------------------- dispatch
    def _dispatch(self, frame: bytes) -> None:
        ftype = frame_type(frame)
        sid = frame_sid(frame)

        if ftype == T_DATA:
            s = self.streams.get(sid)
            if s is not None:
                size = len(frame) - 5
                self.stats["bytes_in"] += size
                self.recv_unacked += size
                s.touch()
                s.peer_credit -= size
                s.backlog += size
                if s.backlog > s.backlog_peak:
                    s.backlog_peak = s.backlog
                payload = frame[5:]
                if s._on_data is None:
                    s.pending += payload
                else:
                    s.on_data(payload)
                    s.flush_credit()
            return

        if ftype == T_WIN:
            s = self.streams.get(sid)
            if s is not None:
                sc, gc = WIN.unpack_from(frame, 5)
                s.send_credit += sc
                self.global_credit = min(self.global_credit + gc, self._global_window())
                if s.on_credit is not None and sc:
                    s.on_credit()
            return

        if ftype == T_EOF:
            s = self.streams.get(sid)
            if s is not None:
                s.eof = True
                if s.on_eof is not None:
                    s.on_eof()
                self.maybe_drop(sid)
            return

        if ftype == T_CLOSE:
            self.drop_stream(sid)
            return

        if ftype == T_OPEN:
            payload = frame[5:]
            mode = payload[0] if payload else 0
            target = payload[1:] if payload else b""
            s = self.accept(sid, mode=mode, target=target)
            if self.on_open is None:
                self.send(err_frame(sid, 5, "peer cannot open streams"))
                self.drop_stream(sid)
                return
            loop = self._loop or asyncio.get_running_loop()
            loop.create_task(self._safe_open(s))
            return

        if ftype == T_OPEN_OK:
            s = self.streams.get(sid)
            if s is not None:
                s.open_ok = True
                s.open_event.set()
            return

        if ftype == T_OPEN_ERR:
            s = self.streams.get(sid)
            if s is not None:
                text = frame[6:].decode("utf-8", "replace") if len(frame) > 6 else "refused"
                s.open_error = text
                s.open_event.set()
            return

        if ftype == T_PING:
            payload = frame[5:]
            self.send_soon(make_frame(T_PONG, 0, payload))
            return

        if ftype == T_PONG:
            payload = frame[5:]
            if len(payload) >= 8:
                try:
                    (sent,) = struct.unpack(">d", payload[:8])
                except struct.error:      # pragma: no cover - defensive
                    return
                took = time.time() - sent
                if 0 < took < 30:
                    self.rtt = took
            return

        if ftype == T_CTRL:
            if self.on_ctrl is not None:
                loop = self._loop or asyncio.get_running_loop()
                payload = frame[5:]
                loop.create_task(self._safe_ctrl(payload))
            return

    async def _safe_open(self, stream: Stream) -> None:
        try:
            await self.on_open(stream)
        except StreamOpenError as exc:
            self.send(err_frame(stream.sid, exc.code, str(exc)))
            self.drop_stream(stream.sid)
        except Exception as exc:
            self.send(err_frame(stream.sid, 3, str(exc)[:80]))
            self.drop_stream(stream.sid)

    async def _safe_ctrl(self, payload: bytes) -> None:
        try:
            await self.on_ctrl(payload)
        except Exception:
            pass

    # ------------------------------------------------------------ keepalive
    async def keepalive(self, interval: float = PING_INTERVAL) -> None:
        if interval <= 0:
            return
        import random

        try:
            while not self.closed:
                # jitter: a perfectly periodic ping every N seconds is itself
                # a fingerprint, so walk the interval by +/-15%
                await asyncio.sleep(interval * (0.85 + random.random() * 0.30))
                self.send_soon(make_frame(T_PING, 0, struct.pack(">d", time.time())))
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    def ctrl(self, obj) -> None:
        """Send a control-plane message (dict -> JSON)."""
        import json

        self.send(make_frame(T_CTRL, 0, json.dumps(obj, separators=(",", ":")).encode()))


def credit_bytes(chunk: int) -> int:
    return chunk * 2

"""The ``udp`` carrier: the same multiplexed tunnel, carried on UDP.

Why it exists
-------------

Every other carrier runs on TCP.  On the Iran ⇄ Europe path one TCP flow that
carries *all* user traffic pays for every loss twice: the tunnel's own
retransmission and the retransmission of the TCP connections inside it.  The
symptom is what users complain about first — WireGuard, Hysteria2, TUIC and
QUIC (UDP) inbounds travel inside our TCP stream, so one lost packet holds up
everything behind it (head-of-line blocking) and the whole tunnel feels spiky
after a loss.

This carrier takes the TCP layer out: it implements a reliable, ordered *byte
stream on top of datagrams* (a compact KCP-like design) with

* selective acknowledgement (32-bit SACK) and fast retransmit, so a loss is
  repaired from the receiver's side instead of waiting for a timer;
* a real congestion window (slow start + Reno-style avoidance) — it stays a
  good neighbour on a shared path;
* a one-round-trip, HMAC-authenticated handshake with the same token and the
  same ±180 s window as every other carrier;
* ChaCha20-Poly1305 for every packet (like the ``raw`` carrier it needs the
  ``cryptography`` package, which the installer adds);
* optional FEC (``fec = "10:2"``): two parity packets per ten, so a loss inside
  a group costs no retransmission at all — the trick that makes gaming, voice
  and QUIC usable over a lossy path;
* exactly the same frames on top as ``tls``/``raw``/``wss``/``plain``, so the
  mux, the Go engine and Go ⇄ Python interop are unchanged.

Wire format (big endian)::

    HELLO_C  1 type | 8 cookie | 8 ts | 16 mac       mac = f(token, cookie, ts)
    HELLO_S  1 type | 8 cookie | 8 ts | 16 mac
    DATA     1 type | 4 sid | 4 seq | 4 ack | 4 ackbits | 2 rwnd |
             1 fec_k | 1 fec_r | 1 fec_i | 2 len | ciphertext
    ACK      1 type | 4 sid | 4 ack | 4 ackbits | 2 rwnd | 4 echo
    CLOSE    1 type | 4 sid

``fec_r = 0`` marks an ordinary data segment.  A parity packet has ``fec_r > 0``
and ``fec_i`` in ``[0, fec_r)``: it is the XOR of the *ciphertexts* of its
group's data segments whose position inside the group is congruent to ``fec_i``
modulo ``fec_r``.  Parity packets carry a packet id from a separate range
(``0x8000_0000 | group << 8 | fec_i``), so a nonce is never reused.

The stream interface (``read_some``/``write``/``drain``/``paused``/
``wait_writable``/``close``) is exactly what ``ByteFrameChannel`` wants, so the
carrier plugs into the existing carrier layer with nothing changed above it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import socket
import struct
import time
from collections import OrderedDict, deque

from .carriers import ByteFrameChannel
from .util import get_logger

log = get_logger("simurgh.udpcarrier")

VERSION = 1
T_HELLO_C, T_HELLO_S, T_DATA, T_ACK, T_CLOSE = 1, 2, 3, 4, 5

HELLO_HEAD = struct.Struct(">B8sQ16s")
HELLO_SIZE = HELLO_HEAD.size
#: cleartext part of a DATA packet: type, sid and the piggybacked ack.
#: Deliberately *outside* the AEAD's associated data: those fields change on
#: every retransmit and on every recovered (FEC) packet, while the ciphertext
#: must stay verifiable.  A tampered ack is not a security problem -- it is the
#: peer's own accounting, and ChaCha20-Poly1305 still protects the payload.
DATA_OUTER = struct.Struct(">BIIIH")
#: authenticated part: seq, fec_k, fec_r, fec_i, length (= the AAD)
DATA_INNER = struct.Struct(">IBBBH")
DATA_HEAD = struct.Struct(">BIIIHIBBBH")
ACK_HEAD = struct.Struct(">BIIIHI")
CLOSE_HEAD = struct.Struct(">BI")

DEFAULT_MTU = 1200
AEAD_TAG = 16
MIN_MTU = 512
MAX_MTU = 60000

RTO_MIN = 0.03
RTO_MAX = 2.0
RTO_INITIAL = 0.25
MAX_RETRIES = 14
FAST_RETRANS = 3
SACK_BITS = 32
TICK = 0.005
IDLE_TIMEOUT = 120.0
MAX_BUFFERED = 8 * 1024 * 1024
#: datagram queues to ask the kernel for on both ends.  A quiet socket is a
#: silent liar: once its receive queue overflows, the kernel drops datagrams
#: without telling anyone, and the sender only finds out by timing out.
SOCK_BUFFER = 4 * 1024 * 1024
#: bytes one flush may hand to the kernel, so a fat window can never flood a
#: small queue in one go
BURST_BYTES = 128 * 1024
#: A queued datagram costs the kernel more than the bytes you can see in it
#: (sk_buff + headers, measured at ~1.9x for ~1.2 KiB packets), so the window
#: we advertise is *not* ``SO_RCVBUF`` but a fraction of it.
SOCK_PAYLOAD_RATIO = 3 / 8
PARITY_BASE = 0x80000000
MAX_GROUPS = 8


class UdpCarrierError(ConnectionError):
    pass


def tune_udp_socket(sock, want: int = SOCK_BUFFER) -> int:
    """Ask the kernel for roomy datagram queues; report the receive size we got.

    ``SO_RCVBUFFORCE`` needs ``CAP_NET_ADMIN`` (the installer has it, an
    ordinary user does not), so it is best effort with the plain option as the
    fallback.  Whatever the kernel grants is what we advertise as our window.
    """
    if sock is None:
        return 0
    for name in ("SO_RCVBUFFORCE", "SO_RCVBUF"):
        flag = getattr(socket, name, None)
        if flag is None:
            continue
        try:
            sock.setsockopt(socket.SOL_SOCKET, flag, int(want))
            break
        except OSError:
            continue
    for name in ("SO_SNDBUFFORCE", "SO_SNDBUF"):
        flag = getattr(socket, name, None)
        if flag is None:
            continue
        try:
            sock.setsockopt(socket.SOL_SOCKET, flag, int(want))
            break
        except OSError:
            continue
    try:
        return int(sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF))
    except OSError:
        return 0


class _RawSender:
    """A duplicate handle on the transport's socket, used for sending.

    asyncio's datagram transport swallows ``BlockingIOError`` -- a send into a
    full queue disappears without a word.  A stream that believes such a packet
    is on the wire stalls until it times out, so we send ourselves and report
    the truth (``False`` = keep it queued and try again).
    """

    def __init__(self, sock):
        self.sock = None
        if sock is None:
            return
        try:
            fd = os.dup(sock.fileno())
        except (OSError, AttributeError, ValueError):
            return
        family = socket.AF_INET
        try:
            if len(sock.getsockname()) >= 4:
                family = socket.AF_INET6
        except OSError:
            pass
        try:
            self.sock = socket.socket(family, socket.SOCK_DGRAM, 0, fileno=fd)
        except OSError:                                # pragma: no cover
            os.close(fd)

    def send(self, data: bytes, addr=None) -> bool:
        if self.sock is None:                          # pragma: no cover
            raise ConnectionError("udp socket is closed")
        try:
            if addr is None:
                self.sock.send(data)
            else:
                self.sock.sendto(data, addr)
        except BlockingIOError:
            return False
        except OSError as exc:
            raise ConnectionError(f"udp send failed: {exc}") from exc
        return True

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:                            # pragma: no cover
                pass
            self.sock = None


def datagram_socket(host: str, port: int, *, local_addr=None, connect_to=None):
    """A tuned datagram socket, family picked by the resolver (v4 or v6)."""
    flags = socket.AI_PASSIVE if host in ("", "::", "0.0.0.0", None) else 0
    infos = socket.getaddrinfo(host or None, port, 0, socket.SOCK_DGRAM, 0, flags)
    family, kind, proto, _canon, sockaddr = infos[0]
    sock = socket.socket(family, kind, proto)
    tune_udp_socket(sock)
    try:
        sock.bind(local_addr or sockaddr)
        if connect_to is not None:
            sock.connect(connect_to)
    except Exception:
        sock.close()
        raise
    return sock


try:  # pragma: no cover - capability check at import time
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    HAVE_CRYPTO = True
except Exception:                                     # pragma: no cover
    ChaCha20Poly1305 = None                           # type: ignore[assignment]
    HAVE_CRYPTO = False


def _aead(key: bytes):
    if ChaCha20Poly1305 is None:                      # pragma: no cover
        raise UdpCarrierError(
            "the 'udp' carrier needs the 'cryptography' package "
            "(the installer adds it; or pip install 'simurgh-tunnel[secure]')"
        )
    return ChaCha20Poly1305(key)


def _nonce(packet_id: int) -> bytes:
    return packet_id.to_bytes(12, "big")


def mss_for(mtu: int) -> int:
    return max(200, int(mtu) - DATA_OUTER.size - DATA_INNER.size - AEAD_TAG)


def fec_params(value) -> tuple[int, int]:
    """``"10:2"`` -> (10, 2); ``"5"`` -> (5, 1); ``"off"`` -> (0, 0)."""
    text = str(value or "off").strip().lower()
    if text in ("", "off", "none", "no", "0"):
        return (0, 0)
    try:
        if ":" in text:
            left, _, right = text.partition(":")
            k, r = int(left), int(right)
        else:
            k, r = int(text), 1
    except ValueError as exc:
        raise ValueError(f"cannot read fec {value!r} (try \"10:2\")") from exc
    if k < 2:
        return (0, 0)
    return (min(64, k), max(0, min(4, r)))


def auth_key(token: str) -> bytes:
    return hashlib.sha256(b"simurgh/v2/udp|" + token.encode("utf-8")).digest()


def _hello_mac(key: bytes, kind: int, cookie: bytes, ts: int) -> bytes:
    return hmac.new(key, bytes([kind]) + cookie + struct.pack(">Q", ts),
                    hashlib.sha256).digest()[:16]


# ---------------------------------------------------------------------------
# the reliable byte stream
# ---------------------------------------------------------------------------


class _Segment:
    __slots__ = ("seq", "size", "cipher", "packet", "sent", "retries", "first_sent")

    def __init__(self, seq: int, size: int):
        self.seq = seq
        self.size = size
        self.cipher = b""
        #: the exact bytes of the first send: a retransmit is byte-identical,
        #: which is what keeps the AEAD tag verifiable
        self.packet = b""
        self.sent = 0.0
        self.retries = 0
        self.first_sent = 0.0


class UdpStream:
    """Reliable, ordered byte stream over an unreliable datagram session."""

    def __init__(self, core: "SessionCore"):
        self.core = core
        self.name = "udp"
        self.peer = core.peer
        self.eof = False
        self.on_eof = None
        self._recv = bytearray()
        self._rcv_nxt = 0
        self._ooo: dict[int, bytes] = {}
        self._ooo_bytes = 0
        self._sendq: "OrderedDict[int, _Segment]" = OrderedDict()
        self._unsent: "deque[int]" = deque()
        self._next_seq = 0
        self._in_flight = 0
        self._peer_window = 1024 * 1200
        self._cwnd = 16 * 1024
        self._ssthresh = 128 * 1024
        self._rtt = RTO_INITIAL
        self._rttvar = RTO_INITIAL / 2
        self._rto = RTO_INITIAL
        self._dup_acks = 0
        self._last_ack_value = -1
        self._last_ack_sent = 0.0
        self._pending_ack = 0
        self._closed = False
        self._read_waiters: list[asyncio.Future] = []
        self._write_waiters: list[asyncio.Future] = []
        # metrics
        self.sent_segments = 0
        self.retransmits = 0
        self.parity_sent = 0
        self.recovered = 0
        self.bytes_in = 0
        self.bytes_out = 0
        self.dup_packets = 0
        self.mss = core.mss

    # ------------------------------------------------------------ receiving
    def deliver(self, seq: int, data: bytes) -> bool:
        if seq < self._rcv_nxt:
            self.dup_packets += 1
            return False
        self.bytes_in += len(data)
        if seq == self._rcv_nxt:
            self._recv += data
            self._rcv_nxt += 1
            while self._rcv_nxt in self._ooo:
                chunk = self._ooo.pop(self._rcv_nxt)
                self._ooo_bytes -= len(chunk)
                self._recv += chunk
                self._rcv_nxt += 1
            self._wake(self._read_waiters)
        elif seq - self._rcv_nxt < 1 << 16:
            if seq not in self._ooo:
                if self._ooo_bytes + len(data) > MAX_BUFFERED:
                    return False
                self._ooo[seq] = data
                self._ooo_bytes += len(data)
        return True

    def recv_window_packets(self) -> int:
        """Advertised window = app buffer, capped by what the socket can hold."""
        used = len(self._recv) + self._ooo_bytes
        free = min(MAX_BUFFERED - used, self.core.rwnd_bytes)
        if free <= 0:
            return 1
        return max(1, min(65535, free // max(1, self.mss)))

    def sack_bits(self) -> int:
        """Selective ack, KCP style: bit *i* (i > 0) means ``ack + i`` arrived."""
        bits = 0
        for seq in self._ooo:
            delta = seq - self._rcv_nxt
            if 0 < delta < SACK_BITS:
                bits |= 1 << delta
        return bits

    def queue_ack(self, force: bool = False) -> None:
        self._pending_ack += 1
        now = time.monotonic()
        if not force and self._pending_ack < 2 and now - self._last_ack_sent < 0.02:
            return
        self.send_ack()

    def send_ack(self, echo: int = 0) -> None:
        self._pending_ack = 0
        self._last_ack_sent = time.monotonic()
        self.core.send(ACK_HEAD.pack(T_ACK, self.core.sid, self._rcv_nxt,
                                     self.sack_bits(),
                                     self.recv_window_packets(),
                                     echo & 0xFFFFFFFF))

    # ------------------------------------------------------------ sending
    def write(self, data) -> None:
        if self._closed:
            raise ConnectionError("udp stream is closed")
        view = memoryview(bytes(data))
        room = self.mss
        for start in range(0, len(view), room):
            piece = bytes(view[start:start + room])
            seq = self._next_seq
            self._next_seq += 1
            self._sendq[seq] = _Segment(seq, len(piece))
            self._unsent.append(seq)
            self.core.payloads[seq] = piece
            self.bytes_out += len(piece)
        self.transmit()

    def _window(self) -> int:
        return max(self.mss, min(self._cwnd, self._peer_window))

    def transmit(self) -> None:
        if self._closed:
            return
        now = time.monotonic()
        budget = BURST_BYTES
        unsent = self._unsent
        while unsent:
            segment = self._sendq.get(unsent[0])
            if segment is None or segment.sent:
                unsent.popleft()                     # acked or already away
                continue
            if self._in_flight + segment.size > self._window():
                break
            if budget <= 0:
                break
            if not self._send_segment(segment, now):
                break                                # socket queue full: later
            unsent.popleft()
            budget -= segment.size + 48
        parity = self.core._parity
        while parity and budget > 0:
            packet = parity[0]
            if self._in_flight + len(packet) > self._window() * 5 // 4:
                break                                # parity is best effort
            if not self.core.send(packet):
                break
            parity.popleft()
            budget -= len(packet) + 48

    def _send_segment(self, segment: _Segment, now: float) -> bool:
        payload = self.core.payloads.get(segment.seq)
        if payload is None:
            return False
        first = not segment.packet
        if first:
            packet, segment.cipher = self.core.pack_encrypted(segment.seq, payload)
            segment.packet = packet
            self.core.ciphers[segment.seq] = segment.cipher
        else:
            packet = segment.packet
        if not self.core.send(packet):
            # the socket queue was full: this packet is *not* on the wire, so it
            # must not count as sent (or as a retry) and is tried again later
            self.core.send_blocks += 1
            return False
        if first:
            self._in_flight += segment.size
            segment.first_sent = now
            self.core.note_part(segment.seq, segment.cipher)
        segment.sent = now
        self.sent_segments += 1
        if segment.retries:
            self.retransmits += 1
        return True

    def _wake(self, waiters: list[asyncio.Future]) -> None:
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)
        waiters.clear()

    def ack(self, ack: int, bits: int, rwnd: int) -> None:
        """A selective acknowledgement from the peer (rwnd in segments).

        Loss is detected from the *selective* part: when the peer acknowledges
        something *after* a gap, the segment at the edge of the gap is gone and
        is retransmitted at once.  Counting duplicate acks instead (the TCP way)
        fires on a paced ack stream and throws the congestion window away for
        nothing -- which is exactly what an earlier version of this file did.
        """
        self._peer_window = max(self.mss, rwnd * self.mss)
        now = time.monotonic()
        acked = 0
        sacked_ahead = False
        # the cumulative part is a prefix of the send queue (it is ordered), so
        # only those few segments are visited, not the whole window
        while self._sendq:
            seq = next(iter(self._sendq))
            if seq >= ack:
                break
            self._release(self._sendq.pop(seq), now)
            acked += 1
        if bits:
            # only the bits that are set are visited, so the common case (a peer
            # that has everything, bits == 0) costs nothing at all
            while bits:
                delta = (bits & -bits).bit_length() - 1
                bits &= bits - 1
                if delta <= 0 or delta >= SACK_BITS:
                    continue
                segment = self._sendq.pop(ack + delta, None)
                if segment is not None:
                    self._release(segment, now)
                    acked += 1
                    sacked_ahead = True
        self._last_ack_value = ack
        if sacked_ahead and self._sendq:
            gap = next(iter(self._sendq))
            if gap >= ack:
                self._fast_retransmit(gap)
        if acked and self._in_flight < self._window():
            self._grow_window()
        self.transmit()
        if self._in_flight <= max(self._cwnd, 256 * 1024):
            self._wake(self._write_waiters)

    def _release(self, segment: _Segment, now: float) -> bool:
        """Account for a segment the peer has (we are done with it)."""
        self.core.forget(segment.seq)
        if segment.first_sent and not segment.retries:
            # Karn: a retransmitted packet has an ambiguous trip time
            self._update_rtt(now - segment.first_sent)
        self._in_flight = max(0, self._in_flight - segment.size)
        return True

    def _grow_window(self) -> None:
        if self._cwnd < self._ssthresh:
            self._cwnd = min(self._ssthresh, self._cwnd + self.mss)
        else:
            self._cwnd += max(1, (self.mss * self.mss) // max(1, self._cwnd))

    def _fast_retransmit(self, seq: int) -> None:
        """Send the segment at the edge of a SACK-reported gap again."""
        segment = self._sendq.get(seq)
        if segment is None:
            return
        now = time.monotonic()
        if segment.sent and now - segment.sent < max(0.002, self._rtt / 2):
            return                                   # too fresh: likely reordering
        if not segment.sent and segment.retries == 0:
            return                                   # never sent: nothing to repair
        segment.retries += 1
        if segment.retries == 1:
            # one halving per loss event, not per retransmit
            self._ssthresh = max(self.mss * 2, self._cwnd // 2)
            self._cwnd = self._ssthresh
        self._send_segment(segment, now)

    def on_timeout(self) -> None:
        now = time.monotonic()
        for segment in self._sendq.values():
            if not segment.sent or now - segment.sent < self._rto:
                continue
            if segment.retries >= MAX_RETRIES:
                self.core.fail(TimeoutError(
                    "the udp tunnel stopped answering (no acks for "
                    f"{self._rto * MAX_RETRIES:.1f}s)"))
                return
            segment.retries += 1
            if segment.retries == 1:
                # one halving per loss event; dropping to a single packet (the
                # textbook reaction) makes a lossy path crawl, and this tunnel
                # is not competing with a TCP flow for the same queue
                self._ssthresh = max(8 * self.mss, self._cwnd // 2)
                self._cwnd = max(2 * self.mss, self._cwnd // 2)
            self._send_segment(segment, now)
            self.core.rto_events += 1
            return

    def _update_rtt(self, sample: float) -> None:
        if sample <= 0 or sample > 10:
            return
        self._rttvar = 0.75 * self._rttvar + 0.25 * abs(self._rtt - sample)
        self._rtt = 0.875 * self._rtt + 0.125 * sample
        self._rto = min(RTO_MAX, max(RTO_MIN, self._rtt + 4 * self._rttvar))

    # ------------------------------------------------------------ interface
    async def read_some(self, n: int = 262144) -> bytes:
        while not self._recv:
            if self._closed or self.eof:
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
        if n >= len(self._recv):
            out = bytes(self._recv)
            self._recv.clear()
        else:
            out = bytes(self._recv[:n])
            del self._recv[:n]
        return out

    async def drain(self) -> None:
        if self._in_flight <= max(self._cwnd, 256 * 1024):
            return
        await self.wait_writable()

    def paused(self) -> bool:
        return self._in_flight > max(self._cwnd * 2, 512 * 1024)

    async def wait_writable(self) -> None:
        while self.paused():
            loop = asyncio.get_running_loop()
            waiter = loop.create_future()
            self._write_waiters.append(waiter)
            try:
                await asyncio.wait_for(waiter, 30.0)
            except asyncio.TimeoutError:
                return
            finally:
                try:
                    self._write_waiters.remove(waiter)
                except ValueError:
                    pass

    def eof_received(self) -> None:
        self.eof = True
        if self.on_eof is not None:
            try:
                self.on_eof()
            except Exception:
                pass
        self._wake(self._read_waiters)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.core.close()

    def snapshot(self) -> dict:
        return {
            "sent": self.sent_segments,
            "retransmits": self.retransmits,
            "parity": self.parity_sent,
            "recovered": self.recovered,
            "rtt_ms": round(self._rtt * 1000, 1),
            "cwnd": self._cwnd,
            "in_flight": self._in_flight,
            "in": self.bytes_in,
            "out": self.bytes_out,
        }


# ---------------------------------------------------------------------------
# session core: packets, FEC, timers (shared by both sides)
# ---------------------------------------------------------------------------


class SessionCore:
    def __init__(self, token_value: str, token_name: str = "", *,
                 mtu: int = DEFAULT_MTU, fec: tuple[int, int] = (0, 0),
                 window: int = 1024, is_server: bool = False, peer: str = "",
                 send=None):
        self.token_value = token_value
        self.token_name = token_name
        self.is_server = is_server
        self.peer = peer
        self.mtu = max(MIN_MTU, min(MAX_MTU, int(mtu)))
        self.mss = mss_for(self.mtu)
        self.fec = fec
        self.window = max(32, int(window))
        self.sid = 0
        self.key: bytes | None = None
        self.stream = UdpStream(self)
        self.payloads: dict[int, bytes] = {}
        self.ciphers: dict[int, bytes] = {}
        self.established = asyncio.Event()
        self.closed = False
        self.failed: Exception | None = None
        self.last_rx = time.monotonic()
        self.rto_events = 0
        self.parity_groups = 0
        self.bad_packets = 0
        self.send_blocks = 0
        self._send = send or (lambda data: None)
        self._cipher = None
        #: how much the kernel queues for us; a window larger than this is an
        #: invitation to silent packet loss, so we never advertise one
        self.sock_capacity = 0
        self.rwnd_bytes = max(32 * 1024, min(self.window * self.mss,
                                             int(192 * 1024 * SOCK_PAYLOAD_RATIO)))
        self._groups: "OrderedDict[int, dict]" = OrderedDict()
        #: sender side: ciphertexts of the group being assembled, in send order
        self._send_groups: "OrderedDict[int, dict]" = OrderedDict()
        #: parity packets waiting for room in the window
        self._parity: "deque[bytes]" = deque()
        self._on_failed = None
        self._hello_ts = int(time.time())
        self._cookie = os.urandom(8)
        self.started = time.monotonic()

    # ------------------------------------------------------------ plumbing
    def cipher(self):
        """One AEAD object per session: constructing one costs more than a packet."""
        if self._cipher is None:
            self._cipher = _aead(self.key)
        return self._cipher

    def note_socket(self, sock) -> None:
        """Learn how large a receive queue the kernel gave this endpoint."""
        if sock is None:
            return
        try:
            capacity = int(sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF))
        except OSError:
            return
        if capacity <= 0:
            return
        self.sock_capacity = capacity
        self.rwnd_bytes = max(32 * 1024, min(self.window * self.mss,
                                             int(capacity * SOCK_PAYLOAD_RATIO)))

    def send(self, data: bytes) -> bool:
        """Hand one datagram to the socket; ``False`` means its queue was full."""
        if self.closed:
            return False
        try:
            result = self._send(data)
        except Exception as exc:                       # pragma: no cover
            self.fail(exc)
            return False
        return result is not False

    def fail(self, exc: Exception) -> None:
        if self.closed:
            return
        self.closed = True
        self.failed = exc
        self.stream._closed = True
        self.stream._wake(self.stream._read_waiters)
        self.stream._wake(self.stream._write_waiters)
        self.established.set()
        if self._on_failed is not None:
            try:
                self._on_failed(exc)
            except Exception:
                pass

    def close(self) -> None:
        if self.closed:
            return
        self.send(CLOSE_HEAD.pack(T_CLOSE, self.sid))
        self.fail(ConnectionError("closed locally"))

    def forget(self, seq: int) -> None:
        self.payloads.pop(seq, None)
        self.ciphers.pop(seq, None)

    def stats(self) -> dict:
        data = self.stream.snapshot()
        data.update({
            "token": self.token_name or (self.token_value[:4] + "…"
                                         if self.token_value else ""),
            "rto_events": self.rto_events,
            "blocked": self.send_blocks,
            "rwnd": self.rwnd_bytes,
            "parity_groups": self.parity_groups,
            "bad": self.bad_packets,
            "peer": self.peer,
        })
        return data

    # ------------------------------------------------------------ handshake
    def hello_packet(self, kind: int, key: bytes, ts: int | None = None) -> bytes:
        stamp = int(time.time()) if ts is None else ts
        mac = _hello_mac(key, kind, self._cookie, stamp)
        return HELLO_HEAD.pack(kind, self._cookie, stamp, mac)

    def client_hello(self) -> bytes:
        return self.hello_packet(T_HELLO_C, auth_key(self.token_value))

    def accept_hello(self, data: bytes, keys: list[tuple[str, str, bytes]]) -> bool:
        """Server side: is this a client we serve?  A stranger hears nothing."""
        if len(data) < HELLO_SIZE:
            return False
        kind, cookie, ts, mac = HELLO_HEAD.unpack_from(data, 0)
        if kind != T_HELLO_C or abs(time.time() - ts) > 180:
            return False
        for value, name, key in keys:
            if hmac.compare_digest(mac, _hello_mac(key, T_HELLO_C, cookie, ts)):
                self.token_value, self.token_name, self.key = value, name, key
                self._cookie = cookie
                #: both sides derive the same session id from the client's
                #: random cookie -- no extra field in the reply is needed
                self.sid = int.from_bytes(cookie[:4], "big") or 1
                self.send(self.hello_packet(T_HELLO_S, key, int(time.time())))
                self.established.set()
                return True
        return False

    def accept_reply(self, data: bytes, key: bytes) -> bool:
        if len(data) < HELLO_SIZE:
            return False
        kind, cookie, ts, mac = HELLO_HEAD.unpack_from(data, 0)
        if kind != T_HELLO_S or cookie != self._cookie:
            return False
        if abs(time.time() - ts) > 180:
            return False
        if not hmac.compare_digest(mac, _hello_mac(key, T_HELLO_S, cookie, ts)):
            return False
        self.key = key
        self.sid = int.from_bytes(cookie[:4], "big") or 1
        self.established.set()
        return True

    # ------------------------------------------------------------ packets
    def outer(self) -> bytes:
        stream = self.stream
        return DATA_OUTER.pack(T_DATA, self.sid, stream._rcv_nxt,
                               stream.sack_bits(), stream.recv_window_packets())

    def inner(self, seq: int, length: int, fec_k: int = 0, fec_r: int = 0,
              fec_i: int = 0) -> bytes:
        return DATA_INNER.pack(seq, fec_k & 0xFF, fec_r & 0xFF, fec_i & 0xFF,
                               length & 0xFFFF)

    def pack_encrypted(self, seq: int, payload: bytes) -> tuple[bytes, bytes]:
        """-> (packet, ciphertext); the ciphertext is reused for retransmits."""
        if self.key is None:
            raise ConnectionError("udp session is not authenticated yet")
        head = self.inner(seq, len(payload) + AEAD_TAG, self.fec[0])
        cipher = self.cipher().encrypt(_nonce(seq), payload, head)
        return self.outer() + head + cipher, cipher

    def deliver_packet(self, data: bytes) -> bool:
        if len(data) < DATA_OUTER.size + DATA_INNER.size or self.key is None:
            return False
        _t, sid, ack, bits, rwnd = DATA_OUTER.unpack_from(data, 0)
        if sid and self.sid and sid != self.sid:
            return False
        if ack:
            self.stream.ack(ack, bits, rwnd)
        pos = DATA_OUTER.size
        seq, fec_k, fec_r, fec_i, length = DATA_INNER.unpack_from(data, pos)
        if fec_r:
            return self._recover(data, seq, fec_k, fec_r, fec_i, length)
        if not length or len(data) < pos + DATA_INNER.size + length:
            return False
        head = data[pos:pos + DATA_INNER.size]
        body = data[pos + DATA_INNER.size:pos + DATA_INNER.size + length]
        try:
            plain = self.cipher().decrypt(_nonce(seq), body, head)
        except Exception:
            self.bad_packets += 1             # forged, corrupted or replayed
            return False
        self._track_group(seq, body, fec_k)
        fresh = self.stream.deliver(seq, plain)
        # A fresh segment gets the normal (paced) ack; a retransmit of
        # something we already hold is answered *immediately*, because that ack
        # is what unsticks a sender whose own acks were lost.
        self.stream.queue_ack(force=not fresh)
        return fresh

    # ------------------------------------------------------------ FEC
    def _track_group(self, seq: int, cipher: bytes, k: int) -> None:
        if not k:
            return
        group = seq // k
        bucket = self._groups.get(group)
        if bucket is None:
            bucket = self._groups[group] = {"k": k, "parts": {}}
        bucket["parts"][seq] = cipher
        if len(bucket["parts"]) >= k:
            self._groups.pop(group, None)
        while len(self._groups) > MAX_GROUPS:
            self._groups.popitem(last=False)

    def note_part(self, seq: int, cipher: bytes) -> None:
        """Sender side: remember a ciphertext so the group's parity can be built.

        The group is assembled from what was *sent*, not from what is still
        unacknowledged: an ack that arrives while the rest of the group is on
        its way used to erase the ciphertext and silently cancel the repair.
        """
        k, r = self.fec
        if not k or not r or len(self._parity) > 4096:
            return
        group = seq // k
        bucket = self._send_groups.get(group)
        if bucket is None:
            bucket = self._send_groups[group] = {}
        bucket[seq % k] = cipher
        if len(bucket) >= k:
            self._send_groups.pop(group, None)
            self._emit_parity(group, bucket)
        while len(self._send_groups) > MAX_GROUPS:
            self._send_groups.popitem(last=False)

    def _emit_parity(self, group: int, parts: dict) -> None:
        k, r = self.fec
        for parity_index in range(r):
            covered = [index for index in range(k) if index % r == parity_index]
            chunks = [parts.get(index) for index in covered]
            if any(chunk is None for chunk in chunks):
                continue
            acc = bytearray(chunks[0])
            for chunk in chunks[1:]:
                if len(chunk) != len(acc):
                    acc = None
                    break
                for pos in range(len(acc)):
                    acc[pos] ^= chunk[pos]
            if acc is None:
                continue
            packet_id = PARITY_BASE | (group << 8) | parity_index
            head = self.inner(packet_id, len(acc), k, r, parity_index)
            self._parity.append(self.outer() + head + bytes(acc))
            self.stream.parity_sent += 1
        self.parity_groups += 1

    def _recover(self, data: bytes, packet_id: int, k: int, r: int, parity_index: int,
                 length: int) -> bool:
        if not self.fec[0] or k != self.fec[0] or not length or not r:
            return False
        pos = DATA_OUTER.size + DATA_INNER.size
        if len(data) < pos + length:
            return False
        body = data[pos:pos + length]
        group = (packet_id & 0x7FFFFFFF) >> 8
        base = group * k
        bucket = self._groups.get(group)
        parts = bucket["parts"] if bucket else {}
        covered = [base + i for i in range(k) if i % r == parity_index]
        missing = [seq for seq in covered
                   if seq not in parts and seq >= self.stream._rcv_nxt]
        if len(missing) != 1:
            return False
        acc = bytearray(body)
        for seq in covered:
            if seq == missing[0]:
                continue
            chunk = parts.get(seq)
            if chunk is None or len(chunk) != len(acc):
                return False
            for pos in range(len(acc)):
                acc[pos] ^= chunk[pos]
        seq = missing[0]
        head = self.inner(seq, len(acc), k)
        try:
            plain = self.cipher().decrypt(_nonce(seq), bytes(acc), head)
        except Exception:
            return False
        self.stream.recovered += 1
        self._track_group(seq, bytes(acc), k)
        fresh = self.stream.deliver(seq, plain)
        self.stream.queue_ack(force=not fresh)
        return fresh

    # ------------------------------------------------------------ timers
    def tick(self) -> None:
        if self.closed:
            return
        self.stream.on_timeout()
        self.stream.transmit()
        now = time.monotonic()
        if self.stream._ooo or now - self.stream._last_ack_sent > 0.2:
            self.stream.send_ack()


# ---------------------------------------------------------------------------
# client side
# ---------------------------------------------------------------------------


class UdpClientProtocol(asyncio.DatagramProtocol):
    """One datagram socket carrying one carrier connection (client side)."""

    def __init__(self, token: str, *, mtu: int = DEFAULT_MTU, fec=(0, 0),
                 window: int = 1024, timeout: float = 15.0):
        self.core = SessionCore(token, "", mtu=mtu, fec=fec, window=window,
                                send=self._raw_send)
        self.key = auth_key(token)
        self.timeout = timeout
        self.transport: asyncio.DatagramTransport | None = None
        self.channel: ByteFrameChannel | None = None
        self.channel_ready = asyncio.Event()
        self.error: Exception | None = None
        self._tick: asyncio.Task | None = None
        self._hello: asyncio.Task | None = None
        self.sender = _RawSender(None)
        self.core._on_failed = self._failed

    def attach_socket(self, sock) -> None:
        self.sender = _RawSender(sock)
        self.core.note_socket(sock)

    def _raw_send(self, data: bytes) -> bool:
        if self.sender.sock is None:
            if self.transport is None:
                raise ConnectionError("udp socket is not open")
            self.transport.sendto(data)            # pragma: no cover
            return True
        return self.sender.send(data)

    def connection_made(self, transport) -> None:
        self.transport = transport
        self.core.note_socket(transport.get_extra_info("socket"))
        try:
            peer = transport.get_extra_info("peername")
            if peer:
                self.core.peer = self.core.stream.peer = f"{peer[0]}:{peer[1]}"
        except Exception:
            pass
        self._tick = asyncio.ensure_future(self._run_tick())
        self._hello = asyncio.ensure_future(self._run_hello())

    def connection_lost(self, exc) -> None:
        self.sender.close()
        self.core.fail(exc or ConnectionError("udp socket closed"))

    def error_received(self, exc) -> None:                 # pragma: no cover
        log.debug("udp client error: %s", exc)

    def datagram_received(self, data: bytes, addr) -> None:
        self.core.last_rx = time.monotonic()
        if not data:
            return
        kind = data[0]
        if kind == T_HELLO_S:
            if not self.core.established.is_set():
                self.core.accept_reply(data, self.key)
            return
        if kind == T_DATA:
            try:
                self.core.deliver_packet(data)
            except Exception as exc:               # pragma: no cover - defensive
                log.debug("udp packet error: %s", exc)
            return
        if kind == T_ACK:
            if len(data) >= ACK_HEAD.size:
                _t, sid, ack, bits, rwnd, _echo = ACK_HEAD.unpack_from(data, 0)
                if not sid or not self.core.sid or sid == self.core.sid:
                    self.core.stream.ack(ack, bits, rwnd)
            return
        if kind == T_CLOSE:
            self.core.fail(ConnectionError("the far side closed the udp tunnel"))

    async def _run_hello(self) -> None:
        deadline = time.monotonic() + self.timeout
        backoff = 0.25
        while not self.core.established.is_set() and time.monotonic() < deadline:
            self.core.send(self.core.client_hello())
            wait = min(backoff, max(0.0, deadline - time.monotonic()))
            try:
                await asyncio.wait_for(self.core.established.wait(), wait)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 1.6, 1.0)
        if not self.core.established.is_set():
            self.error = TimeoutError(
                "no answer to the udp handshake (wrong token, blocked UDP, "
                "or nothing listening on that port)")
            self.core.fail(self.error)
            return
        channel = ByteFrameChannel(self.core.stream, peer=self.core.peer, name="udp")
        channel.token_value = self.core.token_value
        channel.udp_session = self.core
        channel._proto = self
        self.channel = channel
        self.channel_ready.set()

    async def _run_tick(self) -> None:
        while not self.core.closed:
            await asyncio.sleep(TICK)
            self.core.tick()
            if (self.core.established.is_set()
                    and time.monotonic() - self.core.last_rx > IDLE_TIMEOUT):
                self.core.fail(TimeoutError("the udp tunnel went silent"))
                return

    def _failed(self, exc: Exception) -> None:
        if self.error is None:
            self.error = exc
        self.channel_ready.set()

    def close(self) -> None:
        for task in (self._tick, self._hello):
            if task is not None:
                task.cancel()
        self.core.fail(ConnectionError("closed locally"))
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass


async def connect_udp(host: str, port: int, token: str, *, timeout: float = 12.0,
                      mtu: int = DEFAULT_MTU, fec=(0, 0), window: int = 1024,
                      local_addr=None):
    """Open a client-side ``udp`` carrier channel (used by ``client_connect``)."""
    if not HAVE_CRYPTO:
        raise UdpCarrierError(
            "the 'udp' carrier needs the 'cryptography' package "
            "(the installer adds it; or pip install 'simurgh-tunnel[secure]')"
        )
    loop = asyncio.get_running_loop()
    holder: dict = {}

    protocol = UdpClientProtocol(token, mtu=mtu, fec=fec, window=window,
                                 timeout=timeout)
    holder["proto"] = protocol

    def factory():
        return protocol

    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    if not infos:                                  # pragma: no cover
        raise UdpCarrierError(f"cannot resolve {host}:{port}")
    family, _kind, proto, _canon, sockaddr = infos[0]
    sock = socket.socket(family, socket.SOCK_DGRAM, proto)
    tune_udp_socket(sock)
    try:
        if local_addr:
            sock.bind(local_addr)
        sock.connect(sockaddr)
        protocol.attach_socket(sock)
        await loop.create_datagram_endpoint(factory, sock=sock)
    except Exception:
        sock.close()
        raise
    proto: UdpClientProtocol = holder["proto"]
    try:
        await asyncio.wait_for(proto.channel_ready.wait(), timeout + 2.0)
    except asyncio.TimeoutError as exc:
        proto.close()
        raise TimeoutError("the udp handshake timed out") from exc
    if proto.channel is None or proto.error is not None:
        error = proto.error or ConnectionError("the udp carrier closed immediately")
        proto.close()
        raise error
    return proto.channel


def token_pairs(token_value: str, token_name: str = "") -> list[tuple[str, str, bytes]]:
    return [(token_value, token_name, auth_key(token_value))]


# ---------------------------------------------------------------------------
# server side
# ---------------------------------------------------------------------------


class UdpServerProtocol(asyncio.DatagramProtocol):
    """One socket, many carrier connections (exit/relay side)."""

    def __init__(self, token_values: list[tuple[str, str]] | str, *,
                 mtu: int = DEFAULT_MTU, fec=(0, 0), window: int = 1024,
                 on_channel=None, max_sessions: int = 64):
        if isinstance(token_values, str):
            token_values = [(token_values, "")]
        self.tokens = [(value, name, auth_key(value)) for value, name in token_values]
        self.mtu = mtu
        self.fec = fec
        self.window = window
        self.on_channel = on_channel
        self.max_sessions = max(1, int(max_sessions))
        self.transport: asyncio.DatagramTransport | None = None
        self.sessions: dict[tuple, SessionCore] = {}
        self._tick: asyncio.Task | None = None
        self.accepted = 0
        self.refused = 0
        self.capacity = None
        self.sender = _RawSender(None)

    # ------------------------------------------------------------ lifecycle
    def connection_made(self, transport) -> None:
        self.transport = transport
        if self.sender.sock is None:
            self.attach_socket(transport.get_extra_info("socket"))
        self._tick = asyncio.ensure_future(self._run_tick())

    def connection_lost(self, exc) -> None:
        self.sender.close()
        if self._tick is not None:
            self._tick.cancel()
        for session in list(self.sessions.values()):
            session.fail(exc or ConnectionError("udp listener closed"))
        self.sessions.clear()

    def datagram_received(self, data: bytes, addr) -> None:
        if not data:
            return
        kind = data[0]
        key = self._key_for(addr)
        session = self.sessions.get(key)
        if kind == T_HELLO_C:
            if session is not None and not session.closed:
                cookie = data[1:9] if len(data) >= 9 else b""
                if cookie == session._cookie:
                    # a re-sent hello (our first answer was lost)
                    session.last_rx = time.monotonic()
                    session.send(session.hello_packet(T_HELLO_S, session.key or b"",
                                                      int(time.time())))
                    return
                session.fail(ConnectionError("replaced by a new connection"))
                self.sessions.pop(key, None)
                session = None
            if len(self.sessions) >= self.max_sessions:
                self.refused += 1
                return
            session = self._new_session(addr)
            if not session.accept_hello(data, self.tokens):
                self.sessions.pop(key, None)
                self.refused += 1
                return                        # a stranger hears nothing at all
            self.accepted += 1
            asyncio.ensure_future(self._hand_off(session))
            return
        if session is None or session.closed:
            return
        session.last_rx = time.monotonic()
        if kind == T_DATA:
            try:
                session.deliver_packet(data)
            except Exception as exc:               # pragma: no cover - defensive
                log.debug("udp packet error from %s: %s", addr, exc)
        elif kind == T_ACK:
            if len(data) >= ACK_HEAD.size:
                _t, sid, ack, bits, rwnd, _echo = ACK_HEAD.unpack_from(data, 0)
                if not sid or not session.sid or sid == session.sid:
                    session.stream.ack(ack, bits, rwnd)
        elif kind == T_CLOSE:
            session.fail(ConnectionError("the peer closed the udp tunnel"))
            self.sessions.pop(key, None)

    def error_received(self, exc) -> None:             # pragma: no cover
        log.debug("udp listener error: %s", exc)

    # ------------------------------------------------------------ helpers
    def _key_for(self, addr):
        return (addr[0], addr[1]) if addr else ("", 0)

    def _new_session(self, addr) -> SessionCore:
        peer = f"{addr[0]}:{addr[1]}" if addr else ""
        session = SessionCore("", "", mtu=self.mtu, fec=self.fec,
                              window=self.window, is_server=True, peer=peer,
                              send=lambda data, a=addr: self._send_to(a, data))
        session.note_socket(self.capacity)
        session._on_failed = lambda exc, k=(addr[0], addr[1]): self.sessions.pop(k, None)
        self.sessions[(addr[0], addr[1])] = session
        return session

    def attach_socket(self, sock) -> None:
        self.sender = _RawSender(sock)
        self.capacity = sock

    def _send_to(self, addr, data: bytes) -> bool:
        if self.sender.sock is None:
            if self.transport is None:
                raise ConnectionError("udp listener is closed")
            self.transport.sendto(data, addr)      # pragma: no cover
            return True
        return self.sender.send(data, addr)

    async def _hand_off(self, session: SessionCore) -> None:
        if self.on_channel is None:
            return
        channel = ByteFrameChannel(session.stream, peer=session.peer, name="udp")
        channel.token_value = session.token_value
        channel.token_name = session.token_name
        channel.udp_session = session
        try:
            await self.on_channel(channel)
        except Exception as exc:                        # pragma: no cover
            log.debug("udp channel ended: %s", exc)
        finally:
            session.close()

    async def _run_tick(self) -> None:
        while True:
            await asyncio.sleep(TICK)
            now = time.monotonic()
            for key, session in list(self.sessions.items()):
                if session.closed:
                    self.sessions.pop(key, None)
                    continue
                session.tick()
                if now - session.last_rx > IDLE_TIMEOUT:
                    session.fail(TimeoutError("udp session idle"))
                    self.sessions.pop(key, None)

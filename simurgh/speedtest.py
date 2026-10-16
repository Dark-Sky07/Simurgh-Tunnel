"""Through-the-tunnel speed test.

The exit node runs a tiny localhost-only endpoint that gives data away and
swallows whatever it receives; the relay opens streams straight to it (no extra
port mapping needed) and measures.

Three questions, because "how many Mbit/s" is only the first one a tunnel
operator has:

* **throughput** -- the Iran <-> Kharej link as the tunnel sees it, including
  the carrier, the multiplexer and flow control (one or more streams);
* **latency under load** -- the round trip while the link is saturated.  A
  tunnel that is fast when idle and 400 ms slow when busy is the thing users
  actually complain about, and bufferbloat is what DPI boxes and cheap uplinks
  do best;
* **UDP** -- datagrams sent through the tunnel and echoed back, with loss and
  jitter, because a game or a call does not care about TCP throughput.

The endpoint understands three things on the same port:

* a connection that says nothing: it gets a firehose (the throughput test);
* a connection whose first bytes are :data:`ECHO_MAGIC`: every byte sent after
  that comes straight back (the latency probe);
* UDP datagrams: each one is echoed back (the loss/jitter probe).
"""

from __future__ import annotations

import asyncio
import os
import statistics
import struct
import time

from .protocol import MODE_TCP, MODE_UDP, encode_addr
from .util import get_logger

log = get_logger("simurgh.speedtest")

BLOCK = 64 * 1024
DEFAULT_SECONDS = 6.0
#: opens a stream in echo mode (the server stops pouring and answers instead)
ECHO_MAGIC = b"SIMURGH-ECHO\n"
#: bytes per latency sample: small enough to fit in one frame
PROBE_BYTES = 8
#: datagram header: magic + sequence + send timestamp (microseconds)
UDP_MAGIC = b"SUDP"
UDP_HEADER = struct.Struct(">4sId")
UDP_PAYLOAD = 480
UDP_DEFAULT_COUNT = 200
IDLE_SAMPLES = 12
LOAD_SAMPLES = 24
#: keep loading for this much longer if the samples are still coming in
LOAD_GRACE = 2.0
#: how long to keep reading after the last datagram was sent
UDP_DRAIN = 2.0


class SpeedServer(asyncio.Protocol):
    """Localhost-only. Never exposed on a public interface."""

    def __init__(self):
        self.transport = None
        self.sent = 0
        self.received = 0
        self.paused = False
        self.echo = False
        self._head = bytearray()
        self._decided = False
        self._task: asyncio.Task | None = None
        self.block = os.urandom(BLOCK)

    def connection_made(self, transport) -> None:
        self.transport = transport
        self.paused = False

    # ------------------------------------------------------------- deciding
    def data_received(self, data: bytes) -> None:
        if not self._decided:
            self._head += data
            head = bytes(self._head)
            if head.startswith(ECHO_MAGIC):
                # the probe may send the magic and its first sample together
                self._decide(echo=True)
                return
            if ECHO_MAGIC.startswith(head):
                return                                   # still could be magic
            self._decide(echo=False)
            return                                       # _decide() took the data
        self._consume(data)

    def _decide(self, echo: bool) -> None:
        """First bytes seen: echo mode, or start pouring at the peer."""
        self._decided = True
        self.echo = echo
        head = bytes(self._head)
        self._head.clear()
        if head:
            self._consume(head)
        if not echo:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:       # a protocol driven outside a loop
                return                 # (only tests do that): stay quiet
            self._task = loop.create_task(self._pour())

    def _consume(self, data: bytes) -> None:
        self.received += len(data)
        if self.echo:
            try:
                self.transport.write(data)
            except (ConnectionError, OSError):
                pass

    async def _pour(self) -> None:
        """Push as fast as the socket accepts, without burning a whole core."""
        try:
            while True:
                if self.paused or self.transport.get_write_buffer_size() > 2 * 1024 * 1024:
                    await asyncio.sleep(0.002)
                    continue
                for _ in range(16):
                    self.transport.write(self.block)
                    self.sent += BLOCK
                await asyncio.sleep(0)
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass

    def pause_writing(self) -> None:
        self.paused = True

    def resume_writing(self) -> None:
        self.paused = False

    def connection_lost(self, exc) -> None:
        if self._task is not None:
            self._task.cancel()


class UdpEchoServer(asyncio.DatagramProtocol):
    """Localhost-only, and it does exactly one thing: answer."""

    def __init__(self):
        self.transport = None
        self.datagrams = 0
        self.bytes = 0

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.datagrams += 1
        self.bytes += len(data)
        try:
            self.transport.sendto(data, addr)
        except (ConnectionError, OSError):
            pass


# ------------------------------------------------------------------ the probe
class EchoProbe:
    """Round trips over one tunnel stream (the server echoes what we send).

    Reading is push-based (``on_data``), so a sample is a small state machine:
    write a sequence number, wait until that many bytes have come back, keep
    the leftovers for the next one.
    """

    def __init__(self, stream):
        self.stream = stream
        self.buffer = bytearray()
        self.waiter: asyncio.Future | None = None
        stream.on_data = self._on_data
        stream.on_eof = self._on_eof
        stream.credit_flush = 1

    def _on_data(self, data: bytes) -> None:
        self.buffer += data
        self.stream.grant(len(data))
        self.stream.flush_credit()
        self._wake()

    def _on_eof(self) -> None:
        if self.waiter is not None and not self.waiter.done():
            self.waiter.set_exception(ConnectionError("the echo stream closed"))
            self.waiter = None

    def _wake(self) -> None:
        if self.waiter is not None and not self.waiter.done():
            self.waiter.set_result(None)

    async def handshake(self, timeout: float = 5.0) -> None:
        """Send the magic and wait for it to come back: echo mode confirmed.

        Only the magic travels: sending a sample with it would leave an extra
        reply in the buffer, and the first "round trip" would then be free.
        """
        payload = ECHO_MAGIC
        await self._write(payload)
        deadline = time.monotonic() + timeout
        while len(self.buffer) < len(payload):
            if time.monotonic() > deadline:
                raise TimeoutError("the exit did not answer the echo handshake")
            await self._wait(deadline)
        del self.buffer[:len(payload)]

    async def roundtrip(self, seq: int, timeout: float = 3.0) -> float:
        """One sample: returns the round-trip time in milliseconds."""
        payload = struct.pack(">I", seq)
        await self._write(payload)
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        while len(self.buffer) < len(payload):
            if time.monotonic() > deadline:
                raise TimeoutError("no echo within %.1f s" % timeout)
            await self._wait(deadline)
        del self.buffer[:len(payload)]
        return (time.monotonic() - started) * 1000.0

    async def _write(self, payload: bytes) -> None:
        deadline = time.monotonic() + 5.0
        while True:
            if self.stream.can_send(len(payload)) >= len(payload):
                self.stream.write_now(payload)
                return
            if time.monotonic() > deadline:
                raise TimeoutError("the echo stream has no credit")
            await asyncio.sleep(0.001)

    async def _wait(self, deadline: float) -> None:
        loop = asyncio.get_running_loop()
        self.waiter = loop.create_future()
        timeout = max(0.001, deadline - time.monotonic())
        try:
            await asyncio.wait_for(self.waiter, timeout)
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            self.waiter = None

    async def sample(self, count: int, seq: int = 0) -> tuple[list[float], int]:
        """*count* samples; returns (samples in ms, the next sequence number)."""
        samples: list[float] = []
        for _ in range(count):
            try:
                samples.append(await self.roundtrip(seq))
            except (TimeoutError, ConnectionError) as exc:
                log.debug("latency sample lost: %s", exc)
            seq += 1
            await asyncio.sleep(0.02)
        return samples, seq

    def close(self) -> None:
        try:
            self.stream.close()
        except Exception:      # pragma: no cover - already gone
            pass


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def jitter(values: list[float]) -> float | None:
    """Mean absolute difference between consecutive samples."""
    if len(values) < 2:
        return None
    diffs = [abs(b - a) for a, b in zip(values, values[1:])]
    return sum(diffs) / len(diffs)


#: how much the *median* round trip may grow while the link is saturated
GRADES = ((30.0, "A"), (60.0, "B"), (120.0, "C"), (250.0, "D"))


def grade_for(increase_ms: float | None) -> str:
    """Bufferbloat grade: A is "the link stays usable under load"."""
    if increase_ms is None:
        return "?"
    for limit, letter in GRADES:
        if increase_ms <= limit:
            return letter
    return "F"


def _summarise(samples: list[float], prefix: str) -> dict:
    if not samples:
        return {f"{prefix}_ms": None, f"{prefix}_p95_ms": None,
                f"{prefix}_min_ms": None, f"{prefix}_max_ms": None,
                f"{prefix}_samples": 0}
    median = statistics.median(samples)
    return {
        f"{prefix}_ms": round(median, 2),
        f"{prefix}_p95_ms": round(percentile(samples, 0.95) or median, 2),
        f"{prefix}_min_ms": round(min(samples), 2),
        f"{prefix}_max_ms": round(max(samples), 2),
        f"{prefix}_samples": len(samples),
    }


# ------------------------------------------------------------------- the test
def exit_speed_port(node) -> int:
    """Where the exit's speed endpoint listens (it tells us in its hello)."""
    info = getattr(node, "exit_info", None) or {}
    try:
        port = int(info.get("speedtest_port") or 0)
    except (TypeError, ValueError):
        port = 0
    return port or 8808


async def run_speedtest(node, seconds: float = DEFAULT_SECONDS, port: int = 0,
                        udp: bool = False, udp_count: int = UDP_DEFAULT_COUNT,
                        streams: int = 1, latency: bool = True) -> dict:
    """Measure throughput, latency under load and (optionally) UDP.

    ``node`` is a started relay node with at least one live tunnel; ``port`` is
    the exit's ``speedtest_port`` (localhost, on the exit side) and defaults to
    whatever that exit announced when it connected.
    """
    port = int(port) or exit_speed_port(node)
    target = encode_addr("127.0.0.1", port)
    streams = max(1, int(streams))
    opened: list = []
    result: dict = {}

    probe: EchoProbe | None = None
    if latency:
        try:
            echo_stream = await node.open_stream(target, mode=MODE_TCP, timeout=15.0)
            probe = EchoProbe(echo_stream)
            await probe.handshake()
        except Exception as exc:
            log.warning("latency probe unavailable: %s", exc)
            if probe is not None:
                probe.close()
            probe = None

    idle: list[float] = []
    seq = 0
    if probe is not None:
        idle, seq = await probe.sample(IDLE_SAMPLES)

    for _ in range(streams):
        try:
            opened.append(await node.open_stream(target, mode=MODE_TCP, timeout=15.0))
        except Exception as exc:
            log.warning("cannot open a speed-test stream: %s", exc)
    if not opened:
        if probe is not None:
            probe.close()
        raise ConnectionError("no stream to the speed-test endpoint could be opened")

    received = 0
    for stream in opened:
        def _on_data(data: bytes, stream=stream) -> None:
            nonlocal received
            received += len(data)
            stream.grant(len(data))
            stream.flush_credit()

        stream.on_data = _on_data

    stop = asyncio.Event()
    started = time.monotonic()
    deadline = started + seconds

    async def upload(stream) -> int:
        total = 0
        block = os.urandom(BLOCK)
        while not stop.is_set() and not stream.closed:
            room = stream.can_send(BLOCK)
            if room <= 0:
                await asyncio.sleep(0.001)
                continue
            n = min(room, BLOCK)
            stream.write_now(block if n == BLOCK else block[:n])
            total += n
            await asyncio.sleep(0)
        return total

    load = [asyncio.ensure_future(upload(stream)) for stream in opened]
    loaded: list[float] = []
    while True:
        now = time.monotonic()
        if now >= deadline and (probe is None or len(loaded) >= LOAD_SAMPLES):
            break
        if now >= deadline + LOAD_GRACE:
            break
        if probe is None:
            await asyncio.sleep(0.02)
            continue
        try:
            loaded.append(await probe.roundtrip(seq))
            seq += 1
        except (TimeoutError, ConnectionError) as exc:
            log.debug("latency sample lost under load: %s", exc)
        await asyncio.sleep(0.01)
    stop.set()

    try:
        sent = sum(await asyncio.gather(*load))
    finally:
        elapsed = max(0.001, time.monotonic() - started)
        for stream in opened:
            try:
                stream.close()
            except Exception:
                pass

    result.update({
        "seconds": round(elapsed, 2),
        "streams": len(opened),
        "download_bytes": received,
        "upload_bytes": sent,
        "download_mbps": round(received * 8 / elapsed / 1e6, 2),
        "upload_mbps": round(sent * 8 / elapsed / 1e6, 2),
    })

    if probe is not None:
        idle_m = statistics.median(idle) if idle else None
        loaded_m = statistics.median(loaded) if loaded else None
        increase = (loaded_m - idle_m) if (idle_m is not None and loaded_m is not None) else None
        latency_info: dict = {}
        latency_info.update(_summarise(idle, "idle"))
        latency_info.update(_summarise(loaded, "loaded"))
        latency_info.update({
            "increase_ms": round(increase, 2) if increase is not None else None,
            "jitter_ms": round(jitter(loaded) or 0.0, 2) if loaded else None,
            "jitter_idle_ms": round(jitter(idle) or 0.0, 2) if idle else None,
            "grade": grade_for(increase),
        })
        result["latency"] = latency_info
        probe.close()

    if udp:
        result["udp"] = await udp_loss(node, port=port, count=udp_count)
    return result


# ------------------------------------------------------------------ the udp bit
class _UdpProbeOwner:
    """Stands in for :class:`~simurgh.udp.UdpRelayListener` on the relay side.

    The real listener forwards replies to a socket; here they land in a dict
    keyed by sequence number, because this is a probe and not a data path.
    """

    counter = None

    def __init__(self):
        self.flows: dict = {}
        self.transport = None
        self.replies: dict[int, float] = {}
        self.received = 0

    def on_reply(self, data: bytes) -> None:
        self.received += 1
        if len(data) < UDP_HEADER.size:
            return
        magic, seq, stamp = UDP_HEADER.unpack_from(data)
        if magic != UDP_MAGIC:
            return
        if stamp:
            self.replies[int(seq)] = (time.monotonic() - stamp / 1e6) * 1000.0
        else:
            self.replies[int(seq)] = 0.0


def _probe_flow(owner: _UdpProbeOwner):
    from .udp import UdpRelayFlow      # kept local: this module must stay light

    class _Flow(UdpRelayFlow):
        def _from_tunnel(self, data: bytes) -> None:
            self.last = time.monotonic()
            owner.on_reply(data)
            if self.stream is not None and not self.stream.closed:
                self.stream.grant(len(data))
                self.stream.flush_credit()

    return _Flow(owner, ("127.0.0.1", 0))


async def udp_loss(node, port: int = 0, count: int = UDP_DEFAULT_COUNT,
                   size: int = UDP_PAYLOAD) -> dict:
    """Send *count* datagrams through the tunnel and count the echoes.

    A missing sequence number is a datagram that did not make the round trip:
    the tunnel carries UDP as an *ordered* byte stream, so coalescing can only
    move the bytes around, never hide them.
    """
    port = int(port) or exit_speed_port(node)
    count = max(1, int(count))
    owner = _UdpProbeOwner()
    flow = _probe_flow(owner)
    try:
        stream = await node.open_stream(encode_addr("127.0.0.1", port),
                                        mode=MODE_UDP, timeout=15.0)
    except Exception as exc:
        return {"sent": 0, "received": 0, "loss_percent": None,
                "rtt_ms": None, "jitter_ms": None, "error": str(exc)[:120]}
    flow.attach(stream)
    body = os.urandom(max(0, size))
    gap = 0.002 if count > 100 else 0.01

    sent = 0
    started = time.monotonic()
    for seq in range(count):
        payload = UDP_HEADER.pack(UDP_MAGIC, seq, int(time.monotonic() * 1e6)) + body
        # flow-control credit: a datagram that never left is not "loss"
        wait = time.monotonic() + 1.0
        while stream.can_send(len(payload)) < len(payload):
            if time.monotonic() > wait or stream.closed:
                break
            await asyncio.sleep(0.002)
        if stream.closed or stream.can_send(len(payload)) < len(payload):
            continue
        flow.send(payload)
        sent += 1
        await asyncio.sleep(gap)

    deadline = time.monotonic() + UDP_DRAIN
    while time.monotonic() < deadline and len(owner.replies) < sent:
        await asyncio.sleep(0.05)

    rtts = [value for value in owner.replies.values() if value]
    got = len(owner.replies)
    seconds = max(0.001, time.monotonic() - started)
    flow.close()
    return {
        "sent": sent,
        "received": got,
        "loss_percent": round(100.0 * (sent - got) / sent, 2) if sent else None,
        "rtt_ms": round(statistics.median(rtts), 2) if rtts else None,
        "jitter_ms": round(jitter(rtts) or 0.0, 2) if rtts else None,
        "bytes": size,
        "seconds": round(seconds, 2),
    }

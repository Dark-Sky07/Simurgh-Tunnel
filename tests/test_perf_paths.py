"""The fast paths: batched frame reads, copy-free writes, window autotuning.

These are the bits that decide how much traffic one CPU core can push and how
big a window a long (Iran <-> abroad) stream may grow to.  They are easy to
break with a "harmless" refactor, so they are pinned here.
"""

from __future__ import annotations

import asyncio
import struct
import time

import pytest

from simurgh.carriers import ByteFrameChannel, HEADER_LEN_FIELD
from simurgh.mux import Mux, Stream
from simurgh.protocol import (DEFAULT_GLOBAL_WINDOW,HEADER, T_DATA, T_PONG, T_WIN, frame_sid,
                              frame_type, make_frame)
from test_mux import MemoryChannel

pytestmark = pytest.mark.asyncio

KIB = 1024


class FakeStream:
    """A stream that hands out preset chunks, then blocks forever."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.written: list[bytes] = []

    async def read_some(self, n: int = 262144) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        await asyncio.sleep(3600)
        return b""

    def write(self, data) -> None:
        self.written.append(bytes(data))


class SocketStream:
    """A tiny adapter so a StreamReader/Writer looks like a carrier stream."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    async def read_some(self, n: int = 262144) -> bytes:
        return await self.reader.read(n)

    def write(self, data) -> None:
        self.writer.write(data)

    async def drain(self) -> None:
        await self.writer.drain()

    def close(self) -> None:
        self.writer.close()


def _frame(ftype: int, sid: int, payload: bytes) -> bytes:
    body = HEADER.pack(ftype, sid) + payload
    return HEADER_LEN_FIELD.pack(len(body)) + body


# --------------------------------------------------------------- frame paths
async def test_read_frames_collects_a_batch_across_awkward_splits():
    p1, p2 = b"a" * 100000, b"bb"
    blob = (_frame(T_DATA, 7, p1) + _frame(T_DATA, 7, p2)
            + _frame(T_WIN, 7, b"12345678"))
    # three bytes, then one, then five, then the rest: the parser must cope
    channel = ByteFrameChannel(FakeStream([blob[:3], blob[3:4], blob[4:9], blob[9:]]))
    frames = await channel.read_frames()
    assert [len(f) for f in frames] == [5 + len(p1), 5 + len(p2), 13]
    assert frames[0][5:] == p1 and frames[1][5:] == p2
    assert frame_type(frames[2]) == T_WIN
    assert channel._buf == b""


async def test_read_frames_keeps_a_partial_tail_for_the_next_call():
    first = _frame(T_DATA, 3, b"x" * 10)
    blob = first + HEADER_LEN_FIELD.pack(20) + b"half"
    channel = ByteFrameChannel(FakeStream([blob]))
    frames = await channel.read_frames()
    assert len(frames) == 1 and frames[0][5:] == b"x" * 10
    # the incomplete second frame stays buffered for the next round
    assert bytes(channel._buf) == blob[len(first):]


async def test_write_data_is_one_frame_on_the_wire():
    stream = FakeStream([])
    channel = ByteFrameChannel(stream)
    hdr = HEADER.pack(T_DATA, 9)
    payload = b"z" * 70000
    channel.write_data(hdr, payload)
    wire = b"".join(stream.written)
    (n,) = HEADER_LEN_FIELD.unpack_from(wire, 0)
    assert n == len(hdr) + len(payload)      # the length counts the frame only
    assert wire[4:9] == hdr
    assert wire[9:] == payload


async def test_write_data_and_read_frames_agree_over_a_socket():
    got: list = []

    async def handler(reader, writer):
        channel = ByteFrameChannel(SocketStream(reader, writer))
        got.append(await channel.read_frames())

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    channel = ByteFrameChannel(SocketStream(reader, writer))
    channel.write_data(HEADER.pack(T_DATA, 11), b"first")
    channel.write_frame(make_frame(T_WIN, 11, b"\x00\x00\x10\x00\x00\x00\x10\x00"))
    await writer.drain()
    for _ in range(100):
        if got:
            break
        await asyncio.sleep(0.02)
    writer.close()
    server.close()
    frames = got[0]
    assert len(frames) == 2
    assert frame_type(frames[0]) == T_DATA and frame_sid(frames[0]) == 11
    assert frames[0][5:] == b"first"
    assert frame_type(frames[1]) == T_WIN


# ------------------------------------------------------------ window tuning
def _tuned_stream(max_window: int = 4 * KIB * KIB, rtt: float = 0.1):
    channel, _peer = MemoryChannel.pair()
    mux = Mux(channel, is_relay=True, stream_window=256 * KIB,
              max_stream_window=max_window)
    mux.rtt = rtt
    stream = Stream(mux, 1)
    mux.streams[stream.sid] = stream
    return channel, mux, stream


def _feed(stream, rate: float, dt: float, backlog: int = 0) -> None:
    """Pretend the last ``dt`` seconds carried ``rate`` bytes per second."""
    stream.rate_bytes = int(rate * dt)
    stream.rate_start = time.monotonic() - dt
    stream.backlog = stream.backlog_peak = backlog


async def test_the_window_grows_while_the_stream_saturates_the_path():
    _channel, _mux, stream = _tuned_stream()
    _feed(stream, 3_000_000, 0.2)          # 3 MB/s on a 100 ms path: window bound
    bonus = stream._autotune()
    assert stream.window == 512 * KIB      # doubled
    assert bonus == 256 * KIB              # and the peer is handed the extra


async def test_the_window_never_grows_past_the_ceiling():
    _channel, _mux, stream = _tuned_stream(max_window=512 * KIB)
    _feed(stream, 3_000_000, 0.2)
    stream._autotune()
    assert stream.window == 512 * KIB      # clamped
    _feed(stream, 3_000_000, 0.2)
    assert stream._autotune() == 0
    assert stream.window == 512 * KIB


async def test_a_busy_stream_keeps_its_window_even_when_it_looks_behind():
    """Regression: halving the window of every busy-but-slow stream collapsed a
    whole tunnel connection once dozens of users shared it.

    A stream whose own client is slow is not a reason to shrink: the shared
    credit budget bounds the memory, and a small window throttles everyone on
    the connection (one user gets ``window / RTT``, not the link).
    """
    _channel, _mux, stream = _tuned_stream()
    stream.window = 2 * KIB * KIB
    _feed(stream, 1_000_000, 0.2, backlog=stream.window)
    assert stream._autotune() == 0
    assert stream.window == 2 * KIB * KIB   # untouched: the client is the limit


async def test_the_shared_ceiling_follows_the_current_windows():
    channel, _peer = MemoryChannel.pair()
    mux = Mux(channel, is_relay=True, stream_window=256 * KIB)
    assert mux._global_window() == DEFAULT_GLOBAL_WINDOW          # the floor
    stream = Stream(mux, 1)
    mux.streams[1] = stream
    mux.window_sum += stream.window
    stream._set_window(32 * KIB * KIB)                            # it grew
    assert mux._global_window() == 32 * KIB * KIB                 # above the floor
    assert mux.window_sum == 32 * KIB * KIB
    mux.drop_stream(1)                                            # and back
    assert mux.window_sum == 0
    assert mux._global_window() == DEFAULT_GLOBAL_WINDOW


async def test_an_idle_window_falls_back_towards_the_base():
    _channel, _mux, stream = _tuned_stream()
    stream.window = 4 * KIB * KIB
    _feed(stream, 1000, 0.2)               # nothing is flowing
    stream._autotune()
    assert stream.window == 2 * KIB * KIB
    for _ in range(6):                     # and it keeps falling to the base
        _feed(stream, 1000, 0.2)
        stream._autotune()
    assert stream.window == 256 * KIB


async def test_rtt_is_learned_from_a_pong():
    channel, _peer = MemoryChannel.pair()
    mux = Mux(channel, is_relay=True)
    mux.rtt = None
    mux._dispatch(make_frame(T_PONG, 0, struct.pack(">d", time.time() - 0.05)))
    assert mux.rtt is not None and 0.04 < mux.rtt < 1.0
    # a nonsense timestamp is ignored instead of poisoning the tuner
    mux._dispatch(make_frame(T_PONG, 0, struct.pack(">d", time.time() + 5000)))
    assert 0.04 < mux.rtt < 1.0

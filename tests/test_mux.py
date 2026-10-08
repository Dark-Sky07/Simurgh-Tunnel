"""The multiplexer: open/data/eof, flow control and cancellation."""

from __future__ import annotations

import asyncio

import pytest

from simurgh.carriers import BaseChannel
from simurgh.mux import Mux, StreamOpenError
from simurgh.protocol import (ERR_REFUSED, T_OPEN_OK, encode_addr, make_frame)

pytestmark = pytest.mark.asyncio


class MemoryChannel(BaseChannel):
    """An in-memory channel pair, one end per instance."""

    def __init__(self, name: str = "mem"):
        super().__init__(peer="memory")
        self.name = name
        self.other: "MemoryChannel | None" = None
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.written: list[bytes] = []

    @classmethod
    def pair(cls):
        a, b = cls("a"), cls("b")
        a.other, b.other = b, a
        return a, b

    async def read_frame(self) -> bytes:
        return await self.queue.get()

    def write_frame(self, frame: bytes) -> None:
        self.written.append(frame)
        assert self.other is not None
        self.other.queue.put_nowait(frame)

    async def close(self) -> None:
        self.closed = True
        if self.other is not None:
            self.other.queue.put_nowait(b"")


async def _pair(on_open=None, *, stream_window=64 * 1024):
    ca, cb = MemoryChannel.pair()
    relay = Mux(ca, is_relay=True, stream_window=stream_window)
    node = Mux(cb, is_relay=False, on_open=on_open, stream_window=stream_window)
    tasks = [asyncio.ensure_future(relay.run()), asyncio.ensure_future(node.run())]
    return relay, node, tasks


async def _stop(*tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _settle(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def test_open_and_data_roundtrip():
    received: list[bytes] = []

    async def on_open(stream):
        stream.mux.send(make_frame(T_OPEN_OK, stream.sid))
        stream.on_data = lambda data: stream.write_now(data.upper())
        stream.flush_credit(force=True)

    relay, _node, tasks = await _pair(on_open)
    stream = await relay.open(encode_addr("127.0.0.1", 80))
    assert stream.open_ok
    stream.on_data = received.append
    stream.write_now(b"hello")
    assert await _settle(lambda: received)
    assert received == [b"HELLO"]
    await _stop(*tasks)


async def test_data_that_arrives_before_a_handler_is_replayed():
    async def on_open(stream):
        stream.mux.send(make_frame(T_OPEN_OK, stream.sid))
        # nobody is listening yet: the peer speaks first
        stream.grant(5)
        stream.write_now(b"banner")

    relay, _node, tasks = await _pair(on_open)
    stream = await relay.open(encode_addr("127.0.0.1", 22))
    await asyncio.sleep(0.05)
    got: list[bytes] = []
    stream.on_data = got.append          # now the banner must show up
    assert got == [b"banner"]
    await _stop(*tasks)


async def test_eof_is_delivered():
    events: list[str] = []

    async def on_open(stream):
        stream.mux.send(make_frame(T_OPEN_OK, stream.sid))
        stream.on_eof = lambda: events.append("eof")

    relay, _node, tasks = await _pair(on_open)
    stream = await relay.open(encode_addr("127.0.0.1", 80))
    stream.close()                       # half close -> peer sees EOF
    assert await _settle(lambda: events)
    await _stop(*tasks)


async def test_open_error_is_reported_to_the_opener():
    async def on_open(stream):
        raise StreamOpenError("nothing listening there", ERR_REFUSED)

    relay, _node, tasks = await _pair(on_open)
    with pytest.raises(StreamOpenError):
        await relay.open(encode_addr("127.0.0.1", 9))
    await _stop(*tasks)


async def test_sender_never_exceeds_one_window():
    async def on_open(stream):
        stream.mux.send(make_frame(T_OPEN_OK, stream.sid))

    relay, _node, tasks = await _pair(on_open, stream_window=4096)
    stream = await relay.open(encode_addr("127.0.0.1", 80))
    sent = 0
    for _ in range(64):
        room = stream.can_send(1024)
        if room == 0:
            break
        stream.write_now(b"x" * room)
        sent += room
    assert sent == 4096                  # exactly one window, not a byte more
    await _stop(*tasks)


async def test_cancel_does_not_hang():
    async def on_open(stream):
        stream.mux.send(make_frame(T_OPEN_OK, stream.sid))

    relay, _node, tasks = await _pair(on_open)
    await relay.open(encode_addr("127.0.0.1", 80))
    tasks[0].cancel()
    done = await asyncio.wait_for(asyncio.gather(tasks[0], return_exceptions=True), 5)
    assert isinstance(done[0], asyncio.CancelledError)
    await _stop(*tasks[1:])


async def test_send_on_dead_channel_is_swallowed():
    ca, _cb = MemoryChannel.pair()

    class Exploding(BaseChannel):
        def write_frame(self, frame):
            raise ConnectionError("socket gone")

    mux = Mux(Exploding(), is_relay=True)
    mux.send(make_frame(T_OPEN_OK, 1))    # must not raise
    assert mux.closed
    assert mux.stats["last_error"]


async def test_global_credit_is_not_spent_by_the_receive_path():
    """Regression: crediting the peer used to be confused with our own budget."""
    mux = Mux(MemoryChannel(), is_relay=True, global_window=1024)
    assert mux.take_global_credit(100) == 100
    mux.recv_unacked += 100
    assert mux.take_global_credit(500) == 500   # independent of the send budget
    mux.global_credit = 0
    assert mux.take_global_credit(10) == 10

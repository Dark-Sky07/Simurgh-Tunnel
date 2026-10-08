"""Through-the-tunnel speed test.

The exit node runs a tiny localhost-only endpoint that gives data away and
swallows whatever it receives; the relay opens a stream straight to it (no
extra port mapping needed) and measures both directions.

This measures the thing that actually matters: the Iran <-> Kharej link as the
tunnel sees it, including the carrier, the multiplexer and flow control.
"""

from __future__ import annotations

import asyncio
import os
import time

from .protocol import MODE_TCP, encode_addr
from .util import get_logger

log = get_logger("simurgh.speedtest")

BLOCK = 64 * 1024
DEFAULT_SECONDS = 6.0


class SpeedServer(asyncio.Protocol):
    """Localhost-only. Never exposed on a public interface."""

    def __init__(self):
        self.transport = None
        self.sent = 0
        self.received = 0
        self._task: asyncio.Task | None = None
        self.block = os.urandom(BLOCK)

    def connection_made(self, transport) -> None:
        self.transport = transport
        self.paused = False
        self._task = asyncio.ensure_future(self._pour())

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

    def data_received(self, data: bytes) -> None:
        self.received += len(data)

    def connection_lost(self, exc) -> None:
        if self._task is not None:
            self._task.cancel()


async def run_speedtest(node, seconds: float = DEFAULT_SECONDS,
                        port: int = 8808) -> dict:
    """Measure download/upload through the tunnel of a running relay node."""
    target = encode_addr("127.0.0.1", port)
    stream = await node.open_stream(target, mode=MODE_TCP, timeout=15.0)

    received = 0
    deadline = time.monotonic() + seconds

    def on_data(data: bytes) -> None:
        nonlocal received
        received += len(data)
        stream.grant(len(data))
        stream.flush_credit()

    stream.on_data = on_data

    async def upload() -> int:
        sent = 0
        block = os.urandom(BLOCK)
        while time.monotonic() < deadline and not stream.closed:
            room = stream.can_send(BLOCK)
            if room <= 0:
                await asyncio.sleep(0.001)
                continue
            n = min(room, BLOCK)
            stream.write_now(block if n == BLOCK else block[:n])
            sent += n
            await asyncio.sleep(0)
        return sent

    started = time.monotonic()
    try:
        sent = await upload()
    finally:
        elapsed = max(0.001, time.monotonic() - started)
        try:
            stream.close()
        except Exception:
            pass
    return {
        "seconds": round(elapsed, 2),
        "download_bytes": received,
        "upload_bytes": sent,
        "download_mbps": round(received * 8 / elapsed / 1e6, 2),
        "upload_mbps": round(sent * 8 / elapsed / 1e6, 2),
    }

"""Measure a *single* user stream through the tunnel over a delayed link.

The Iranian <-> foreign path is long: typically 60-120 ms RTT.  A receive
window that is too small caps one user's throughput at window/RTT no matter how
fast both servers are, so this harness emulates a long fat link with a delay
proxy and reports what one stream really gets.

    python tools/latency_bench.py [delay_ms] [megabytes] [carrier] [window]

It prints the throughput of one stream, and (with --parallel) of 8 streams
sharing the tunnel, which is what the relay connection pool is about.
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import os
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simurgh.config import (ExitConfig, ExitEndpoint, ListenSpec, Mapping,  # noqa: E402
                            RelayConfig, TunnelSpec)  # noqa: E402,F401
from simurgh.exit import ExitNode  # noqa: E402
from simurgh.relay import RelayNode  # noqa: E402
from simurgh.util import Home, setup_logging  # noqa: E402

TOKEN = "bench-token-42"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class DelayProxy:
    """A long-fat-pipe emulator: forwards both ways with a fixed delay.

    Reading and writing are decoupled through a queue, so the proxy itself is
    never the bottleneck -- it only adds propagation delay.
    """

    def __init__(self, listen_port: int, target_port: int, delay: float,
                 rate_mbs: float = 0.0):
        self.listen_port = listen_port
        self.target_port = target_port
        self.delay = delay
        #: per connection limit, emulating "one TCP flow cannot fill the link"
        self.rate = rate_mbs * 1e6 if rate_mbs else 0.0
        self.server: asyncio.AbstractServer | None = None
        self.tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._handle, "127.0.0.1", self.listen_port, backlog=64)

    async def stop(self) -> None:
        if self.server:
            self.server.close()
        for task in self.tasks:
            task.cancel()

    async def _handle(self, reader, writer) -> None:
        try:
            up_r, up_w = await asyncio.open_connection("127.0.0.1", self.target_port)
        except OSError:
            writer.close()
            return
        self.tasks.append(asyncio.ensure_future(self._pump(reader, up_w)))
        self.tasks.append(asyncio.ensure_future(self._pump(up_r, writer)))

    async def _pump(self, reader, writer) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        next_allowed = time.monotonic()

        async def sender():
            nonlocal next_allowed
            while True:
                due, data = await queue.get()
                wait = due - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                if self.rate:
                    # a plain token bucket: this connection may not exceed
                    # ``rate`` bytes per second, whatever the link could do
                    now = time.monotonic()
                    next_allowed = max(next_allowed, now) + len(data) / self.rate
                    if next_allowed > now:
                        await asyncio.sleep(next_allowed - now)
                writer.write(data)
                try:
                    await writer.drain()
                except (ConnectionError, OSError):
                    return

        task = asyncio.ensure_future(sender())
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await queue.put((time.monotonic() + self.delay, data))
            await queue.join()
        except (ConnectionError, OSError):
            pass
        finally:
            task.cancel()
            try:
                writer.close()
            except Exception:
                pass


class Sink:
    """A target service that streams a fixed number of bytes to the client."""

    def __init__(self, total: int):
        self.total = total
        self.port = free_port()
        self.server: asyncio.AbstractServer | None = None
        self.chunk = 65536

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)

    async def stop(self) -> None:
        if self.server:
            self.server.close()

    async def _handle(self, reader, writer):
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except Exception:
            writer.close()
            return
        block = bytes(self.chunk)
        sent = 0
        try:
            while sent < self.total:
                n = min(self.chunk, self.total - sent)
                writer.write(block[:n])
                sent += n
                if sent % (4 * 1024 * 1024) < self.chunk:
                    await writer.drain()
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


async def _spread(relay, streams: int) -> list[int]:
    """Sample how many streams each tunnel connection carries, mid-transfer."""
    await asyncio.sleep(0.5)
    return [len(m.streams) for m in relay.muxes]


async def one_stream(relay_port: int, total: int) -> float:
    reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
    writer.write(b"GET / HTTP/1.1\r\nHost: bench\r\n\r\n")
    await writer.drain()
    started = time.monotonic()
    got = 0
    while got < total:
        chunk = await reader.read(1 << 20)
        if not chunk:
            break
        got += len(chunk)
    took = time.monotonic() - started
    writer.close()
    return (got / (1 << 20)) / took if took > 0 else 0.0


async def run(delay: float, megabytes: int, carrier: str, window: int,
              parallel: int, max_window: int | None, connections: int,
              rate_mbs: float = 0.0) -> None:
    home = Home(Path("/tmp/simurgh-bench") / f"{int(time.time())}")
    home.ensure()
    sink = Sink(megabytes * (1 << 20))
    await sink.start()

    exit_port, proxy_port, relay_port = free_port(), free_port(), free_port()
    tunnel_port = 0
    if carrier in ("tls", "wss"):
        from simurgh.certs import ensure_certificate

        cert, key = ensure_certificate(home, "bench.local")
    else:
        cert = key = None

    if connections > 1:
        # a listening relay (reverse mode) accepts several exit connections;
        # the exit dials the delay proxy so the emulated path is shared
        tunnel_port = free_port()
        relay_cfg = RelayConfig(
            token=TOKEN, name="bench-relay", dial="exit", panel_port=0,
            keepalive=25,
            tunnel=TunnelSpec(carrier=carrier, host="127.0.0.1", port=tunnel_port,
                              cert_file=cert, key_file=key, cert_auto=False),
            mappings=[Mapping(name="bench", listen=relay_port, target_port=sink.port)],
        )
        relay_cfg.stream_window = window
        if max_window:
            relay_cfg.max_stream_window = max_window
        relay_cfg.connections = connections
        relay = RelayNode(relay_cfg, home=home)
        await relay.start()
        node = ExitNode(ExitConfig(
            token=TOKEN, cert_auto=False, cert_file=cert, key_file=key,
            connections=connections,
            listen=[ListenSpec(carrier=carrier, host="127.0.0.1", port=proxy_port,
                               dial="127.0.0.1", insecure_skip_verify=True,
                               fingerprint=None)]))
        proxy = DelayProxy(proxy_port, tunnel_port, delay, rate_mbs)
        await proxy.start()
        await node.start()
    else:
        node = ExitNode(ExitConfig(
            token=TOKEN, cert_auto=False, cert_file=cert, key_file=key,
            stream_window=window,
            listen=[ListenSpec(carrier=carrier, host="127.0.0.1", port=exit_port)]))
        await node.start()
        proxy = DelayProxy(proxy_port, exit_port, delay, rate_mbs)
        await proxy.start()
        relay_cfg = RelayConfig(
            token=TOKEN, name="bench-relay",
            exit=ExitEndpoint(carrier=carrier, address="127.0.0.1", port=proxy_port,
                              insecure_skip_verify=True),
            mappings=[Mapping(name="bench", listen=relay_port, target_port=sink.port)],
        )
        relay_cfg.stream_window = window
        if max_window:
            relay_cfg.max_stream_window = max_window
        relay = RelayNode(relay_cfg, home=home)
        await relay.start()

    want = max(1, min(16, connections))
    for _ in range(300):
        if relay.connected.is_set() and len(relay.muxes) >= want:
            break
        await asyncio.sleep(0.05)
    if not relay.connected.is_set():
        print("tunnel did not come up")
        return

    label = (f"carrier={carrier} delay={delay*1000:.0f}ms "
             f"window={window//1024}KiB "
             f"max={(str(max_window // 1024) + 'KiB') if max_window else 'default'} "
             f"conns={connections}"
             + (f" cap={rate_mbs:.0f}MB/s/conn" if rate_mbs else ""))
    if parallel > 1:
        started = time.monotonic()
        spread = asyncio.ensure_future(_spread(relay, parallel))
        results = await asyncio.gather(*[one_stream(relay_port, megabytes * (1 << 20))
                                         for _ in range(parallel)])
        took = time.monotonic() - started
        total = sum(results) * parallel if False else megabytes * parallel
        per_mux = await spread
        print(f"{label} streams={parallel}: aggregate "
              f"{total / took:.1f} MB/s ({total * 8 / took:.0f} Mbit/s), "
              f"per stream {min(results):.1f}-{max(results):.1f} MB/s in {took:.1f}s")
        print(f"   streams per tunnel connection: {per_mux}")
    else:
        mbps = await one_stream(relay_port, megabytes * (1 << 20))
        print(f"{label}: {mbps:.1f} MB/s ({mbps * 8:.0f} Mbit/s)")

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    print(f"   peak memory of this process (relay + exit + proxy + sink): "
          f"{peak:.0f} MB")
    st = relay.status()
    print(f"   relay: {st['connections']} tunnel connection(s), "
          f"{len(relay.muxes)} live mux, exit side reaches {node.reach_out}")
    for mux in relay.muxes:
        print(f"   mux {id(mux) % 1000}: {len(mux.streams)} streams, "
              f"rtt={mux.rtt and round(mux.rtt * 1000)}ms")
    await relay.stop()
    await node.stop()
    if proxy:
        await proxy.stop()
    await sink.stop()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("delay_ms", nargs="?", type=float, default=60.0)
    ap.add_argument("megabytes", nargs="?", type=int, default=40)
    ap.add_argument("carrier", nargs="?", default="plain")
    ap.add_argument("window_kib", nargs="?", type=int, default=256)
    ap.add_argument("--max-window-kib", type=int, default=0,
                    help="0 = disabled (no window autotuning)")
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--connections", type=int, default=1)
    ap.add_argument("--rate-mbs", type=float, default=0.0,
                    help="cap every tunnel connection at N MB/s (0 = unlimited)")
    args = ap.parse_args()
    setup_logging(os.environ.get("BENCH_DEBUG") == "1")
    asyncio.run(run(args.delay_ms / 1000.0, args.megabytes, args.carrier,
                    args.window_kib * 1024, args.parallel,
                    (args.max_window_kib * 1024) or None, args.connections,
                    args.rate_mbs))


if __name__ == "__main__":
    main()

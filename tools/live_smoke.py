"""End-to-end smoke test for the new Simurgh Tunnel core.

Everything runs inside one Python process: a fake "foreign panel" TCP server,
an ExitNode listening on localhost, a RelayNode whose mapping points at the
target, and a client that speaks through the relay.  Four carriers are tested.
"""

import asyncio
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])

from simurgh.certs import ensure_certificate
from simurgh.config import (ExitConfig, ExitEndpoint, ListenSpec, Mapping,
                            RelayConfig)
from simurgh.exit import ExitNode
from simurgh.relay import RelayNode
from simurgh.util import Home, setup_logging

setup_logging(False)

TARGET_PORT = int(os.environ.get("SMOKE_TARGET_PORT") or free_port())
TOKEN = "TESTTOKEN-123"
CARRIERS = [(name, free_port(), free_port())
            for name in ("raw", "tls", "wss", "plain")]


async def echo_server(reader, writer):
    while True:
        data = await reader.read(65536)
        if not data:
            break
        writer.write(data)
        await writer.drain()
    writer.close()


async def roundtrip(port, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    got = b""
    deadline = time.monotonic() + 20
    while len(got) < len(payload) and time.monotonic() < deadline:
        chunk = await asyncio.wait_for(reader.read(len(payload) - len(got)), 20)
        if not chunk:
            break
        got += chunk
    writer.close()
    return got


async def carrier_test(name, exit_port, relay_port, cert, key) -> list[str]:
    results = []
    home = Home(f"/tmp/smoke-{name}")
    home.ensure()
    exit_cfg = ExitConfig(
        token=TOKEN, name=f"exit-{name}", cert_file=cert, key_file=key,
        cert_auto=False,
        listen=[ListenSpec(carrier=name, host="127.0.0.1", port=exit_port)],
    )
    node = ExitNode(exit_cfg)
    await node.start()
    relay_cfg = RelayConfig(
        token=TOKEN, name=f"relay-{name}",
        exit=ExitEndpoint(carrier=name, address="127.0.0.1", port=exit_port,
                          insecure_skip_verify=True),
        mappings=[Mapping(name="echo", listen=relay_port, target_port=TARGET_PORT)],
    )
    relay = RelayNode(relay_cfg)
    await relay.start()
    for _ in range(60):
        if relay.connected.is_set():
            break
        await asyncio.sleep(0.1)
    if not relay.connected.is_set():
        results.append(f"[FAIL] {name}: tunnel never came up ({relay.last_error})")
        await relay.stop()
        await node.stop()
        return results

    try:
        payload = b"simurgh-" + name.encode() + b"-hello"
        got = await roundtrip(relay_port, payload)
        results.append(f"[{'OK ' if got == payload else 'FAIL'}] {name} small: "
                       f"sent {len(payload)}B, got {len(got)}B")

        big = os.urandom(100_000)
        got = await roundtrip(relay_port, big)
        results.append(f"[{'OK ' if got == big else 'FAIL'}] {name} 100KB: "
                       f"sent {len(big)}B, got {len(got)}B")

        async def worker(i):
            data = (f"worker-{i}-" * 60).encode()
            got = await roundtrip(relay_port, data)
            return i, data == got, len(data), len(got)

        for i, ok, sent, size in sorted(await asyncio.gather(*(worker(i) for i in range(8)))):
            results.append(f"[{'OK ' if ok else 'FAIL'}] {name} worker {i}: "
                           f"sent {sent}B, got {size}B")
    finally:
        await relay.stop()
        await node.stop()
    return results


async def main() -> int:
    home = Home("/tmp/smoke-home")
    home.ensure()
    cert, key = ensure_certificate(home, "localhost")
    target = await asyncio.start_server(echo_server, "127.0.0.1", TARGET_PORT)
    print(f"echo target on 127.0.0.1:{TARGET_PORT}")
    total, bad = 0, 0
    for name, exit_port, relay_port in CARRIERS:
        results = await carrier_test(name, exit_port, relay_port, cert, key)
        for line in results:
            print(line)
        total += len(results)
        bad += sum(1 for line in results if line.startswith("[FAIL"))
    target.close()
    await target.wait_closed()
    print(f"RESULT: {'ALL GREEN' if bad == 0 else str(bad) + ' FAILURES'} "
          f"({total - bad}/{total} checks passed)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(asyncio.wait_for(main(), 300)))

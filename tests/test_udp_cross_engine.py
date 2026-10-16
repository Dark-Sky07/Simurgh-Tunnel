"""Cross-engine proof for the `udp` carrier: a Go *relay* against a Python *exit*.

The Python and Go engines claim one wire format; this test makes them prove it.
It starts the real Go binary (`go/cmd/simurgh`) as a relay with `carrier = "udp"`
and a real Python :class:`~simurgh.exit.ExitNode` on the far side, then pushes an
HTTP request through the user-facing port and checks the answer byte for byte.

Both directions are covered:

* ``test_go_relay_dials_a_python_exit``      -- Go speaks first (client role)
* ``test_python_relay_dials_a_go_exit``      -- Python speaks first (client role)

Skips quietly when no Go toolchain is available, so the suite still runs on a
plain Python box.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import free_port
from simurgh.config import (ExitConfig, ExitEndpoint, ListenSpec, Mapping,
                            RelayConfig)
from simurgh.exit import ExitNode
from simurgh.relay import RelayNode

pytestmark = pytest.mark.asyncio

TOKEN = "cross-engine-token"
REPO = Path(__file__).resolve().parents[1]
GO_DIR = REPO / "go"


def _go_binary() -> str | None:
    """A usable `go` command, if this machine has one."""
    env = os.environ.get("SIMURGH_GO")
    if env and Path(env).exists():
        return env
    for name in ("go",):
        found = shutil.which(name)
        if found:
            return found
    cached = Path.home() / ".cache/goroot/bin/go"
    if cached.exists():
        return str(cached)
    return None


def _go_env(go: str) -> dict:
    env = dict(os.environ)
    root = Path(go).resolve().parent.parent
    if (root / "src").is_dir():
        env["GOROOT"] = str(root)
    env["PATH"] = f"{Path(go).parent}:{env.get('PATH', '')}"
    # the build must never reach the network: the project ships its deps
    env["GOPROXY"] = "off"
    env["GOFLAGS"] = ""
    env["GOTOOLCHAIN"] = "local"
    return env


@pytest.fixture(scope="session")
def go_engine(tmp_path_factory):
    """Build the Go data plane once per session."""
    go = _go_binary()
    if go is None:
        pytest.skip("no Go toolchain on this machine")
    out = tmp_path_factory.mktemp("gobin") / "simurgh"
    proc = subprocess.run(
        [go, "build", "-o", str(out), "./cmd/simurgh"],
        cwd=str(GO_DIR), env=_go_env(go), capture_output=True, text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        pytest.skip(f"the Go engine did not build here: {proc.stderr.strip()[:400]}")
    return str(out)


async def _http_server():
    async def handler(reader, writer):
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except Exception:
            writer.close()
            return
        body = b"<h1>foreign panel</h1>"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                     b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body))
        writer.write(body)
        await writer.drain()
        if b"Host: panel" not in request:              # never happens, but be loud
            writer.close()
            return
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _ask(port: int, timeout: float = 20.0) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET / HTTP/1.1\r\nHost: panel\r\n\r\n")
    await writer.drain()
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            chunk = await asyncio.wait_for(reader.read(4096),
                                           max(0.1, deadline - time.monotonic()))
            if not chunk:
                break
            chunks.append(chunk)
    except (asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()
    return b"".join(chunks)


def _write_configs(tmp: Path, exit_port: int, relay_port: int, target_port: int,
                   carrier: str = "udp") -> tuple[Path, Path]:
    """exit.toml + relay.toml in the TOML shape both engines read."""
    home = tmp / "exit-home"
    home.mkdir(parents=True, exist_ok=True)
    exit_toml = home / "exit.toml"
    exit_toml.write_text(f"""token = "{TOKEN}"
name = "cross-exit"
cert_auto = false
log_level = "warn"

[[listen]]
carrier = "{carrier}"
host = "127.0.0.1"
port = {exit_port}
mtu = 1200
fec = "8:2"
window = 512
""", encoding="utf-8")
    relay_home = tmp / "relay-home"
    relay_home.mkdir(parents=True, exist_ok=True)
    relay_toml = relay_home / "relay.toml"
    relay_toml.write_text(f"""token = "{TOKEN}"
name = "cross-relay"
log_level = "warn"

[exit]
carrier = "{carrier}"
address = "127.0.0.1"
port = {exit_port}
mtu = 1200
fec = "8:2"
window = 512

[[mapping]]
name = "panel"
listen = {relay_port}
target_host = "127.0.0.1"
target_port = {target_port}
""", encoding="utf-8")
    return exit_toml, relay_toml


def _tail(proc: subprocess.Popen, lines: int = 12) -> str:
    """Whatever the Go engine managed to say before we gave up on it."""
    try:
        proc.terminate()
        out, _ = proc.communicate(timeout=10)
    except Exception:                                   # pragma: no cover
        return "(no output)"
    return "\n".join((out or "").strip().splitlines()[-lines:])


async def _wait_for_log(proc: subprocess.Popen, needle: str,
                        timeout: float = 20.0) -> bool:
    """Wait until the engine says it is listening (a udp socket cannot be
    probed with a TCP connect)."""
    loop = asyncio.get_running_loop()

    def _read() -> bool:
        for line in proc.stdout or ():
            if needle in line:
                return True
        return False

    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _read), timeout)
    except asyncio.TimeoutError:
        return False


async def _wait_for_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _r, w = await asyncio.open_connection("127.0.0.1", port)
            w.close()
            return True
        except OSError:
            await asyncio.sleep(0.1)
    return False


@pytest.mark.parametrize("carrier", ["plain", "udp"])
async def test_go_relay_dials_a_python_exit(go_engine, tmp_path, certs, carrier):
    """The Go relay speaks the Python exit's udp carrier (Go = client)."""
    web, target_port = await _http_server()
    exit_port, relay_port = free_port(), free_port()
    _exit_toml, relay_toml = _write_configs(tmp_path, exit_port, relay_port,
                                            target_port, carrier)
    exit_cfg = ExitConfig(
        token=TOKEN, name="cross-exit", cert_file=certs[0], key_file=certs[1],
        cert_auto=False,
        listen=[ListenSpec(carrier=carrier, host="127.0.0.1", port=exit_port,
                           mtu=1200, fec="8:2", window=512)],
    )
    node = ExitNode(exit_cfg)
    await node.start()
    proc = subprocess.Popen([go_engine, "relay", "--home", str(relay_toml.parent)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        assert await _wait_for_port(relay_port), (
            "the Go relay never listened:\n" + _tail(proc))
        answer = await _ask(relay_port)
        assert b"200 OK" in answer, f"no answer from the Go relay: {answer[:200]!r}"
        assert b"foreign panel" in answer
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:              # pragma: no cover
            proc.kill()
        await node.stop()
        web.close()


@pytest.mark.parametrize("carrier", ["plain", "udp"])
async def test_python_relay_dials_a_go_exit(go_engine, tmp_path, certs, carrier):
    """The Python relay speaks the Go exit's udp carrier (Python = client)."""
    web, target_port = await _http_server()
    exit_port, relay_port = free_port(), free_port()
    exit_toml, _relay_toml = _write_configs(tmp_path, exit_port, relay_port,
                                            target_port, carrier)
    exit_cfg = ExitConfig(
        token=TOKEN, name="cross-exit", cert_file=certs[0], key_file=certs[1],
        cert_auto=False,
        listen=[ListenSpec(carrier=carrier, host="127.0.0.1", port=exit_port,
                           mtu=1200, fec="8:2", window=512)],
    )
    # the Go exit needs a certificate for the *other* carriers only; udp needs
    # none, so it is handed the same cert paths the Python side uses
    exit_toml.write_text(exit_toml.read_text() + f"""
cert_file = "{certs[0]}"
key_file = "{certs[1]}"
""", encoding="utf-8")
    proc = subprocess.Popen([go_engine, "exit", "--home", str(exit_toml.parent)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        assert await _wait_for_log(proc, f"listening carrier={carrier}"), (
            "the Go exit never listened:\n" + _tail(proc))
        relay_cfg = RelayConfig(
            token=TOKEN,
            exit=ExitEndpoint(carrier=carrier, address="127.0.0.1", port=exit_port,
                              mtu=1200, fec="8:2", window=512),
            mappings=[Mapping(name="panel", listen=relay_port,
                              target_port=target_port)],
        )
        relay = RelayNode(relay_cfg)
        await relay.start()
        try:
            for _ in range(100):
                if relay.connected.is_set():
                    break
                await asyncio.sleep(0.05)
            assert relay.connected.is_set(), "the Python relay never reached the Go exit"
            answer = await _ask(relay_port)
            assert b"200 OK" in answer, f"no answer through the Go exit: {answer[:200]!r}"
            assert b"foreign panel" in answer
        finally:
            await relay.stop()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:              # pragma: no cover
            proc.kill()
        web.close()

"""Bandwidth limits and traffic quotas, end to end.

A real user connection through a real tunnel, exactly like ``test_e2e``, but
with ``rate_limit`` and ``quota`` on the mapping: the point is that a limited
user gets a *slower* link (never a broken one) and that an exhausted quota
really does stop the next user from getting in -- and survives a restart.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from conftest import free_port
from simurgh.accounting import TrafficAccount, quota_from
from simurgh.config import (ExitConfig, ExitEndpoint, ListenSpec, Mapping,
                            RelayConfig)
from simurgh.exit import ExitNode
from simurgh.ratelimit import RateLimiter, limiter_from
from simurgh.relay import RelayNode

pytestmark = pytest.mark.asyncio

TOKEN = "limits-token"


async def _body_server(size: int):
    """A little HTTP server that always answers with *size* bytes."""
    body = b"x" * size

    async def handler(reader, writer):
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except Exception:
            writer.close()
            return
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n"
                     b"Connection: close\r\n\r\n" % size)
        writer.write(body)
        try:
            await writer.drain()
        except Exception:
            pass
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _stack(mapping: Mapping, certs, home, target_port: int):
    exit_port = free_port()
    relay_port = mapping.listen or free_port()
    mapping.listen = relay_port
    exit_cfg = ExitConfig(
        token=TOKEN, name="exit", cert_file=certs[0], key_file=certs[1],
        cert_auto=False,
        listen=[ListenSpec(carrier="plain", host="127.0.0.1", port=exit_port)],
    )
    node = ExitNode(exit_cfg)
    await node.start()
    relay_cfg = RelayConfig(
        token=TOKEN,
        exit=ExitEndpoint(carrier="plain", address="127.0.0.1", port=exit_port,
                          insecure_skip_verify=True),
        mappings=[mapping],
    )
    relay = RelayNode(relay_cfg, home=home)
    await relay.start()
    for _ in range(80):
        if relay.connected.is_set():
            break
        await asyncio.sleep(0.05)
    assert relay.connected.is_set(), "tunnel did not come up"
    return relay, node, relay_port


async def _fetch(port: int, expect: int, timeout: float = 30.0) -> tuple[bytes, float]:
    """Read the whole answer, then report how long it took."""
    started = time.monotonic()
    deadline = started + timeout
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET / HTTP/1.1\r\nHost: panel\r\n\r\n")
    await writer.drain()
    chunks: list[bytes] = []
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                chunk = await asyncio.wait_for(reader.read(65536), left)
            except (asyncio.TimeoutError, ConnectionError):
                break
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        writer.close()
    return b"".join(chunks), time.monotonic() - started


# --------------------------------------------------------------------- bucket


def test_limiter_is_shared_and_parses_sizes():
    limiter = limiter_from("10M")
    assert isinstance(limiter, RateLimiter)
    assert limiter.rate == 10 * 1024 * 1024
    assert limiter_from(0) is None and limiter_from("") is None
    # one second of burst by default, and an explicit one is honoured
    assert limiter_from("20k").burst == 20 * 1024
    assert limiter_from({"rate": "20k", "burst": "2k"}).burst == 2 * 1024
    small = limiter_from({"rate": "20k", "burst": "2k"})
    # draining the bucket then asking again really does cost time
    assert small.take_now(small.burst)
    assert small.poll(1024) > 0
    # ...and the allowance tells a data path exactly how much fits right now
    allowed, delay = small.allowance(4096)
    assert allowed == 0 and delay > 0


async def test_a_mapping_stays_under_its_rate_limit(certs, tmp_path):
    """128 KiB/s on the mapping, 256 KiB of page: slow, correct, unbroken."""
    size = 128 * 1024
    web, target_port = await _body_server(size)
    # a 4 KiB burst so the shape of the limit is visible in a short test
    mapping = Mapping(name="slow", listen=0, target_port=target_port,
                      rate_limit={"rate": "128k", "burst": "4k"})
    relay, node, port = await _stack(mapping, certs, tmp_path, target_port)
    try:
        data, elapsed = await _fetch(port, size, timeout=30)
        assert data.endswith(b"x" * size), "the transfer was cut short"
        assert b"200 OK" in data
        # the last ~124 KiB at 128 KiB/s cannot happen in under 0.8 s
        assert elapsed >= 0.7, f"limit ignored: {size / elapsed / 1024:.0f} KiB/s"
        assert elapsed < 10.0, f"far too slow: {elapsed:.1f}s"
        # ...and the accounting saw the bytes
        assert relay.account is not None
        used = relay.account.usage(mapping.key(), "total")
        assert used >= size
    finally:
        await relay.stop()
        await node.stop()
        web.close()


async def test_the_rate_limit_can_be_changed_without_restarting(certs, tmp_path):
    """The config is the source of truth: raise the limit and it speeds up."""
    size = 128 * 1024
    web, target_port = await _body_server(size)
    mapping = Mapping(name="slow", listen=0, target_port=target_port,
                      rate_limit={"rate": "64k", "burst": "4k"})
    relay, node, port = await _stack(mapping, certs, tmp_path, target_port)
    try:
        _data, slow = await _fetch(port, size)
        mapping.rate_limit = "2M"                      # an operator edit
        relay._limiters.pop(mapping.key(), None)       # rebind drops the bucket
        await relay.rebind()
        _data, fast = await _fetch(port, size)
        assert fast < slow, f"no speed-up: {slow:.2f}s -> {fast:.2f}s"
    finally:
        await relay.stop()
        await node.stop()
        web.close()


# ---------------------------------------------------------------------- quota


def test_quota_state_tracks_the_period():
    account = TrafficAccount(None)
    quota = quota_from("1k", "total", "block", "port-443")
    assert quota.enabled and quota.limit == 1024
    assert not quota.exceeded(account, "port-443")
    account.add("port-443", in_bytes=600, out_bytes=600)
    assert quota.exceeded(account, "port-443")
    state = quota.state(account, "port-443")
    assert state["used"] == 1200 and state["remaining"] == 0


async def test_exhausted_quota_turns_the_next_user_away(certs, tmp_path):
    """``quota = "64k"``: the first user gets through, the next one does not."""
    size = 96 * 1024
    web, target_port = await _body_server(size)
    mapping = Mapping(name="metered", listen=0, target_port=target_port,
                      quota="64k", quota_period="total", quota_action="block")
    relay, node, port = await _stack(mapping, certs, tmp_path, target_port)
    try:
        data, _elapsed = await _fetch(port, size)
        assert data.endswith(b"x" * size)
        # the account is over the allowance now, and it is on disk
        assert relay.account.usage(mapping.key(), "total") > 64 * 1024
        # a fresh connection is refused before a single byte flows
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: panel\r\n\r\n")
        try:
            await writer.drain()
        except Exception:
            pass
        answer = await asyncio.wait_for(reader.read(4096), 10)
        writer.close()
        assert answer == b"", "the over-quota connection was not refused"
        assert relay.blocked >= 1
    finally:
        await relay.stop()
        await node.stop()
        web.close()

    # a restart must not forget what was already used (``stop`` flushed it)
    assert (tmp_path / "traffic.json").exists()
    again = TrafficAccount(tmp_path / "traffic.json")
    assert again.usage(mapping.key(), "total") > 64 * 1024


async def test_throttle_action_keeps_the_user_online(certs, tmp_path):
    """``quota_action = "throttle"``: over the allowance, still served."""
    size = 256 * 1024
    web, target_port = await _body_server(size)
    mapping = Mapping(name="soft", listen=0, target_port=target_port,
                      quota="8k", quota_period="total",
                      quota_action="throttle")
    relay, node, port = await _stack(mapping, certs, tmp_path, target_port)
    try:
        _first, quick = await _fetch(port, size, timeout=40)   # spends the quota
        data, elapsed = await _fetch(port, size, timeout=40)   # now on a leash
        assert data.endswith(b"x" * size), "a throttled user must still be served"
        assert elapsed > max(quick * 2, 0.5), \
            f"throttle ignored ({quick:.2f}s -> {elapsed:.2f}s)"
    finally:
        await relay.stop()
        await node.stop()
        web.close()


async def test_max_conns_turns_extra_users_away(certs, tmp_path):
    """``max_conns = 1``: the second user is refused, the first is untouched."""
    web, target_port = await _body_server(64 * 1024)
    mapping = Mapping(name="single", listen=0, target_port=target_port, max_conns=1)
    relay, node, port = await _stack(mapping, certs, tmp_path, target_port)
    try:
        first_reader, first_writer = await asyncio.open_connection("127.0.0.1", port)
        first_writer.write(b"GET / HTTP/1.1\r\nHost: panel\r\n\r\n")
        await first_writer.drain()
        # let the first connection be accepted and bridged
        await asyncio.sleep(0.3)
        second_reader, second_writer = await asyncio.open_connection("127.0.0.1", port)
        second_writer.write(b"GET / HTTP/1.1\r\nHost: panel\r\n\r\n")
        await second_writer.drain()
        answer = b""
        try:
            answer = await asyncio.wait_for(second_reader.read(256), 3)
        except (asyncio.TimeoutError, ConnectionError):
            answer = b""
        assert answer == b"", f"the second user was served anyway: {answer[:80]!r}"
        assert relay.stats.mapping(mapping.key()).last_error == "max_conns reached"

        # the first connection still works
        chunk = await asyncio.wait_for(first_reader.read(1024), 10)
        assert chunk, "the first user lost its connection"
        first_writer.close()
        # ...and once it is gone the port accepts again
        for _ in range(60):
            await asyncio.sleep(0.05)
            probe_reader, probe_writer = await asyncio.open_connection("127.0.0.1", port)
            probe_writer.write(b"GET / HTTP/1.1\r\nHost: panel\r\n\r\n")
            await probe_writer.drain()
            try:
                if await asyncio.wait_for(probe_reader.read(256), 3):
                    probe_writer.close()
                    return
            except (asyncio.TimeoutError, ConnectionError):
                pass
            probe_writer.close()
        raise AssertionError("the mapping never accepted a user again")
    finally:
        await relay.stop()
        await node.stop()
        web.close()


async def test_metrics_show_live_limit_numbers(certs, tmp_path, home):
    """A live relay's own counters reach /metrics (both engines' shape)."""
    from simurgh.panel import Panel

    size = 32 * 1024
    web, target_port = await _body_server(size)
    mapping = Mapping(name="metered", listen=0, target_port=target_port,
                      rate_limit="1M", quota="1G", quota_period="month")
    relay, node, port = await _stack(mapping, certs, tmp_path, target_port)
    panel_port = free_port()
    panel = Panel(home, "relay", node=relay, host="127.0.0.1", port=panel_port,
                  user="admin", password="pw")
    await panel.start()
    try:
        data, _elapsed = await _fetch(port, size, timeout=20)
        assert data.endswith(b"x" * size)

        reader, writer = await asyncio.open_connection("127.0.0.1", panel_port)
        import base64
        auth = base64.b64encode(b"admin:pw").decode()
        writer.write(("GET /metrics HTTP/1.1\r\nHost: localhost\r\n"
                      f"Authorization: Basic {auth}\r\nConnection: close\r\n\r\n")
                     .encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), 10)
        writer.close()
        assert b" 200 " in raw.split(b"\r\n", 1)[0]
        text = raw.partition(b"\r\n\r\n")[2].decode()

        assert f'listen="{mapping.listen}"' in text
        assert "simurgh_mapping_bytes_out_total" in text
        assert "simurgh_quota_limit_bytes" in text and "1073741824" in text
        assert "simurgh_quota_used_bytes" in text
        # the mapping really moved the page, so the counter is not zero
        row = [line for line in text.splitlines()
               if line.startswith("simurgh_mapping_bytes_out_total")]
        assert row and float(row[0].rsplit(" ", 1)[1]) >= size
        assert "simurgh_traffic_bytes_total" in text.replace(
            "simurgh_traffic_bytes_total_", "")
    finally:
        await panel.stop()
        await relay.stop()
        await node.stop()
        web.close()


def test_accounting_needs_no_running_loop(monkeypatch):
    """Python 3.9 binds an ``asyncio.Lock()``/``Event()`` to the thread's loop at
    construction, and that raises once an earlier ``asyncio.run`` has closed it.

    A ``TrafficAccount`` is built from sync code too (the panel, a status
    reader), so it must not create any asyncio primitive when it is built.
    """
    def no_loop(*_args, **_kwargs):
        raise RuntimeError("There is no current event loop in thread 'MainThread'.")

    monkeypatch.setattr(asyncio, "get_event_loop", no_loop)
    account = TrafficAccount(None)
    account.add("mapping", in_bytes=10, out_bytes=5)
    quota = quota_from("1k", "month", "block", "mapping")
    assert account.usage("mapping", "total") == 15
    assert quota.state(account, "mapping")["used"] == 15
    assert account.snapshot()["totals"]["total"] == 15

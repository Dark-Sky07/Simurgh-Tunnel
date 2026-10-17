"""The speed test: throughput, latency under load, and UDP loss.

The important one is ``test_speedtest_through_a_real_tunnel``: a real relay, a
real exit, the real speed endpoint, a real measurement -- the only fake part is
that both ends run in this process.
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import free_port
from simurgh import speedtest as st
from simurgh.config import (ExitConfig, ExitEndpoint, ListenSpec, Mapping,
                            RelayConfig)
from simurgh.exit import ExitNode
from simurgh.relay import RelayNode

pytestmark = pytest.mark.asyncio
TOKEN = "speed-token"


# ------------------------------------------------------------ pure functions
def test_percentile_and_jitter():
    assert st.percentile([], 0.5) is None
    assert st.percentile([4.0], 0.9) == 4.0
    assert st.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert st.percentile([1.0, 2.0, 3.0, 4.0], 0.95) == pytest.approx(3.85)
    assert st.jitter([1.0]) is None
    assert st.jitter([10.0, 12.0, 9.0]) == pytest.approx(2.5)


def test_bufferbloat_grade():
    assert st.grade_for(None) == "?"
    assert st.grade_for(0.5) == "A"
    assert st.grade_for(30.0) == "A"
    assert st.grade_for(31.0) == "B"
    assert st.grade_for(600.0) == "F"


def test_summarise_reports_an_empty_run_honestly():
    empty = st._summarise([], "idle")
    assert empty["idle_ms"] is None and empty["idle_samples"] == 0
    rows = st._summarise([1.0, 2.0, 3.0], "loaded")
    assert rows["loaded_ms"] == 2.0
    assert rows["loaded_min_ms"] == 1.0 and rows["loaded_max_ms"] == 3.0
    assert rows["loaded_samples"] == 3


def test_the_exit_speed_port_comes_from_what_the_exit_announced():
    class Node:
        exit_info: dict = {}

    assert st.exit_speed_port(Node()) == 8808
    Node.exit_info = {"speedtest_port": 9999}
    assert st.exit_speed_port(Node()) == 9999
    Node.exit_info = {"speedtest_port": "what?"}
    assert st.exit_speed_port(Node()) == 8808


# --------------------------------------------------------------- the endpoint
class _FakeTransport:
    def __init__(self):
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def get_write_buffer_size(self) -> int:
        return 0


def test_the_endpoint_decides_echo_or_firehose():
    """The first bytes decide: the magic means "answer me", anything else pours."""
    transport = _FakeTransport()
    echo_server = st.SpeedServer()
    echo_server.connection_made(transport)
    echo_server.data_received(st.ECHO_MAGIC)
    assert echo_server.echo is True
    assert echo_server._task is None            # nothing is poured at a prober
    echo_server.data_received(b"ping")
    assert transport.written == [st.ECHO_MAGIC, b"ping"]

    # the magic may arrive in pieces, and a partial magic is never mistaken
    # for real traffic
    partial = st.SpeedServer()
    partial.connection_made(_FakeTransport())
    partial.data_received(st.ECHO_MAGIC[:4])
    assert partial.echo is False and partial._decided is False

    # the firehose half needs a running loop, so it has its own test below
    silent = st.SpeedServer()
    silent.connection_made(_FakeTransport())
    silent.data_received(b"GET / HTTP/1.1\r\n")
    assert silent.echo is False and silent._decided is True
    assert silent.received == len(b"GET / HTTP/1.1\r\n")


async def test_the_firehose_pours_and_stops_on_cancel():
    """The other half of the decision: a silent (or greedy) peer gets data."""
    server = st.SpeedServer()
    transport = _FakeTransport()
    server.connection_made(transport)
    server.data_received(b"not-the-magic")
    task = server._task
    await asyncio.sleep(0.05)
    assert transport.written, "nothing was poured at the peer"
    assert server.sent > 0
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_the_udp_endpoint_answers_every_datagram():
    class Transport:
        def __init__(self):
            self.sent = []

        def sendto(self, data, addr):
            self.sent.append((data, addr))

    transport = Transport()
    server = st.UdpEchoServer()
    server.connection_made(transport)
    server.datagram_received(b"hello", ("127.0.0.1", 1234))
    assert transport.sent == [(b"hello", ("127.0.0.1", 1234))]
    assert (server.datagrams, server.bytes) == (1, 5)


# ------------------------------------------------------------- through a tunnel
async def _pair(speed_port: int, certs):
    """A relay and an exit, both real, sharing one token."""
    cert, key = certs
    exit_port = free_port()
    exit_cfg = ExitConfig(
        token=TOKEN, name="exit", cert_file=cert, key_file=key, cert_auto=False,
        speedtest_port=speed_port,
        listen=[ListenSpec(carrier="raw", host="127.0.0.1", port=exit_port)],
    )
    node = ExitNode(exit_cfg)
    await node.start()
    relay_cfg = RelayConfig(
        token=TOKEN,
        exit=ExitEndpoint(carrier="raw", address="127.0.0.1", port=exit_port,
                          insecure_skip_verify=True),
        mappings=[Mapping(name="panel", listen=free_port(), target_port=free_port())],
    )
    relay = RelayNode(relay_cfg)
    await relay.start()
    for _ in range(100):
        if relay.connected.is_set():
            break
        await asyncio.sleep(0.05)
    assert relay.connected.is_set(), "the tunnel did not come up"
    return relay, node


async def test_speedtest_through_a_real_tunnel(certs):
    speed_port = free_port()
    relay, node = await _pair(speed_port, certs)
    try:
        assert relay.exit_info.get("speedtest_port") == speed_port
        result = await st.run_speedtest(relay, seconds=1.0)
    finally:
        await relay.stop()
        await node.stop()

    assert result["seconds"] >= 1.0
    assert result["download_bytes"] > 0 and result["upload_bytes"] > 0
    assert result["download_mbps"] > 0 and result["upload_mbps"] > 0
    assert result["streams"] == 1
    latency = result["latency"]
    assert latency["idle_samples"] >= 1
    assert latency["loaded_samples"] >= 1
    assert latency["idle_ms"] is not None and latency["loaded_ms"] is not None
    assert latency["grade"] in "ABCDF?"
    assert "udp" not in result


async def test_speedtest_without_the_latency_probe(certs):
    relay, node = await _pair(free_port(), certs)
    try:
        result = await st.run_speedtest(relay, seconds=1.0, latency=False)
    finally:
        await relay.stop()
        await node.stop()
    assert "latency" not in result
    assert result["download_bytes"] > 0


async def test_udp_survives_the_tunnel(certs):
    speed_port = free_port()
    relay, node = await _pair(speed_port, certs)
    try:
        result = await st.udp_loss(relay, count=60)
    finally:
        await relay.stop()
        await node.stop()
    assert result["sent"] == 60
    assert result["received"] == 60, result
    assert result["loss_percent"] == 0.0
    assert result["rtt_ms"] is not None and result["rtt_ms"] >= 0


async def test_udp_loss_is_reported_when_the_exit_has_no_echo(certs):
    """UDP is not answered: the test says 100% loss instead of pretending.

    The exit's own endpoint is on a different port, so nothing on this one
    answers the datagrams.
    """
    port = free_port()
    relay, node = await _pair(free_port(), certs)
    try:
        result = await st.udp_loss(relay, port=port, count=10)
    finally:
        await relay.stop()
        await node.stop()
    assert result["sent"] == 10
    assert result["received"] == 0
    assert result["loss_percent"] == 100.0


async def test_a_closed_endpoint_is_an_error_not_a_crash(certs):
    relay, node = await _pair(free_port(), certs)
    try:
        result = await st.run_speedtest(relay, seconds=1.0, port=free_port())
    except ConnectionError as exc:
        assert "speed-test" in str(exc)
    else:
        raise AssertionError(f"an unreachable endpoint must fail: {result}")
    finally:
        await relay.stop()
        await node.stop()

"""UDP over the tunnel (for Hysteria2/TUIC/WireGuard style configs).

Datagrams are carried one per frame, so they keep their boundaries; there is no
retransmission (UDP semantics are preserved: what the network drops stays
dropped) and flow control degrades to "drop when the far side is behind",
which is exactly what a UDP socket does when its buffer is full.

Honest note for the operator: tunnelling UDP over a *TCP* tunnel adds
head-of-line blocking, so latency-sensitive protocols (QUIC, WireGuard) do not
love it.  It is here for compatibility; for those protocols a dedicated UDP
forward directly from the Iranian server is still the faster option.
"""

from __future__ import annotations

import asyncio
import time

from .protocol import MODE_UDP, encode_addr
from .util import get_logger

log = get_logger("simurgh.udp")

FLOW_TIMEOUT = 60.0
MAX_PENDING = 64 * 1024


# --------------------------------------------------------------------------
# exit side
# --------------------------------------------------------------------------


class UdpExitFlow(asyncio.DatagramProtocol):
    """Tunnel stream <-> one target UDP socket."""

    def __init__(self, stream, counter=None):
        self.stream = stream
        self.counter = counter
        self.transport = None
        self.last = time.monotonic()

    def connection_made(self, transport) -> None:
        self.transport = transport
        self.stream.on_data = self._from_tunnel
        self.stream.on_eof = self._close
        self.stream.credit_flush = 4096

    def datagram_received(self, data: bytes, addr) -> None:
        self.last = time.monotonic()
        if self.counter is not None:
            self.counter.in_bytes += len(data)
        s = self.stream
        if s is None or s.closed:
            return
        if s.can_send(len(data)) >= len(data):
            s.write_now(data)
        # else: drop -- the peer is behind, and UDP does not wait.

    def _from_tunnel(self, data: bytes) -> None:
        self.last = time.monotonic()
        if self.counter is not None:
            self.counter.out_bytes += len(data)
        if self.transport is not None:
            try:
                self.transport.sendto(data)
            except Exception:
                pass
        self.stream.grant(len(data))
        self.stream.flush_credit()

    def error_received(self, exc) -> None:
        pass

    def _close(self) -> None:
        self.stream = None
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass

    def connection_lost(self, exc) -> None:
        if self.stream is not None:
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None


# --------------------------------------------------------------------------
# relay side
# --------------------------------------------------------------------------


class UdpRelayFlow:
    """One user (ip:port) -- buffered until the tunnel stream is ready."""

    def __init__(self, owner: "UdpRelayListener", addr, stream=None):
        self.owner = owner
        self.addr = addr
        self.stream = stream
        self.pending: list[bytes] = []
        self.pending_bytes = 0
        self.last = time.monotonic()
        self.task: asyncio.Task | None = None

    def send(self, data: bytes) -> None:
        self.last = time.monotonic()
        if self.stream is None:
            if self.pending_bytes < MAX_PENDING:
                self.pending.append(data)
                self.pending_bytes += len(data)
            return
        self._write(data)

    def _write(self, data: bytes) -> None:
        s = self.stream
        if s is None or s.closed:
            return
        if s.can_send(len(data)) >= len(data):
            s.write_now(data)
            if self.owner.counter is not None:
                self.owner.counter.in_bytes += len(data)
        # else drop

    def attach(self, stream) -> None:
        self.stream = stream
        stream.credit_flush = 4096
        stream.on_data = self._from_tunnel
        stream.on_eof = self.close
        for d in self.pending:
            self._write(d)
        self.pending.clear()
        self.pending_bytes = 0

    def _from_tunnel(self, data: bytes) -> None:
        self.last = time.monotonic()
        if self.owner.counter is not None:
            self.owner.counter.out_bytes += len(data)
        try:
            self.owner.transport.sendto(data, self.addr)
        except Exception:
            pass
        self.stream.grant(len(data))
        self.stream.flush_credit()

    def close(self) -> None:
        self.owner.flows.pop(self.addr, None)
        if self.task is not None:
            self.task.cancel()
        s, self.stream = self.stream, None
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
        if self.owner.counter is not None:
            self.owner.counter.active = max(0, self.owner.counter.active - 1)


class UdpRelayListener(asyncio.DatagramProtocol):
    """Listens on the Iranian server for user UDP traffic."""

    def __init__(self, node, mapping):
        self.node = node
        self.mapping = mapping
        self.transport = None
        self.flows: dict = {}
        self.counter = node.stats.mapping(f"udp {mapping.listen}")
        self._reaper: asyncio.Task | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport
        self._reaper = asyncio.ensure_future(self._reap())

    def datagram_received(self, data: bytes, addr) -> None:
        flow = self.flows.get(addr)
        if flow is None:
            flow = UdpRelayFlow(self, addr)
            self.flows[addr] = flow
            self.counter.conns += 1
            self.counter.active += 1
            flow.task = asyncio.ensure_future(self._open(flow))
        flow.send(data)

    async def _open(self, flow: UdpRelayFlow) -> None:
        addr = encode_addr(self.mapping.target_host, self.mapping.target_port)
        try:
            stream = await self.node.open_stream(addr, mode=MODE_UDP)
        except Exception as exc:
            self.counter.errors += 1
            self.counter.last_error = str(exc)[:120]
            flow.close()
            return
        flow.attach(stream)

    async def _reap(self) -> None:
        try:
            while True:
                await asyncio.sleep(10)
                now = time.monotonic()
                for addr, flow in list(self.flows.items()):
                    if now - flow.last > FLOW_TIMEOUT:
                        flow.close()
        except asyncio.CancelledError:
            pass

    def close(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
        for flow in list(self.flows.values()):
            flow.close()
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass

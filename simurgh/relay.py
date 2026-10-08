"""The relay -- runs on the Iranian server.

Users connect here (with the same config they always used, only the address
changed) and their bytes are pushed into the tunnel, which forwards them to the
exit node on the foreign server.

Responsibilities:

* listen on every mapped port (TCP, optionally UDP);
* keep exactly one tunnel connection alive, with failover across endpoints and
  automatic reconnects;
* never lose the backpressure -- a user's socket is only read while the peer
  has granted flow-control credit;
* accept a new port list from the exit (config push) and rebind on the fly.
"""

from __future__ import annotations

import asyncio
import json
import time

from .bridge import Bridge, proxy_v1_header
from .carriers import client_connect
from .config import Mapping, RelayConfig
from .mux import Mux
from .protocol import MODE_TCP, encode_addr
from .stats import Stats
from .udp import UdpRelayListener
from .util import free_port, get_logger, is_ip

log = get_logger("simurgh.relay")

STREAM_WAIT = 12.0
FAIL_COOLDOWN = 30.0
STATS_INTERVAL = 5.0
PING_INTERVAL = 10.0


class UserBridge(Bridge):
    """A user connection being pushed into the tunnel."""

    def __init__(self, node: "RelayNode", mapping: Mapping, **kw):
        super().__init__(opener=lambda: node.open_stream(
            encode_addr(mapping.target_host, mapping.target_port), MODE_TCP),
            counter=node.stats.mapping(mapping.key()), **kw)
        self.node = node
        self.mapping = mapping

    def connection_made(self, transport) -> None:
        if self.mapping.proxy_protocol in ("v1", "v2"):
            peer = transport.get_extra_info("peername") or ("", 0)
            if isinstance(peer, tuple) and len(peer) >= 2 and is_ip(str(peer[0])):
                self.proxy_header = proxy_v1_header(
                    str(peer[0]), int(peer[1]),
                    self.mapping.target_host, self.mapping.target_port,
                )
        super().connection_made(transport)


class RelayNode:
    def __init__(self, cfg: RelayConfig, home=None, on_change=None, listen: bool = True):
        self.cfg = cfg
        self.home = home
        self.on_change = on_change
        #: False for short-lived helpers (speed test): dial the tunnel only,
        #: never steal the user-facing ports from the running service.
        self.listen_enabled = listen
        self.stats = Stats()
        self.connected = asyncio.Event()
        self.mux: Mux | None = None
        self.current: str = ""
        self.rtt_ms: float | None = None
        self.exit_info: dict = {}
        self.last_error = ""
        self.bind_errors: list[str] = []
        self._servers: dict[str, asyncio.AbstractServer] = {}
        self._udp: dict[str, UdpRelayListener] = {}
        self._udp_transports: dict[str, asyncio.DatagramTransport] = {}
        self._fail_until: dict[str, float] = {}
        self._stopping = False
        self._tasks: set[asyncio.Task] = set()
        self.started_at = time.time()
        self.connect_count = 0

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        await self.rebind()
        task = asyncio.ensure_future(self._supervisor())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._tasks):
            task.cancel()
        for server in self._servers.values():
            server.close()
        for listener in self._udp.values():
            listener.close()
        if self.mux is not None:
            self.mux.close()

    def reconnect(self) -> None:
        """Drop the current tunnel; the supervisor dials a fresh one."""
        self._fail_until.clear()
        if self.mux is not None:
            self.mux.close()
        self.current = ""

    def status(self) -> dict:
        return {
            "role": "relay",
            "name": self.cfg.name or "relay",
            "connected": self.connected.is_set(),
            "current_exit": self.current,
            "rtt_ms": self.rtt_ms,
            "uptime": time.time() - self.started_at,
            "reconnects": self.connect_count,
            "last_error": self.last_error,
            "bind_errors": list(self.bind_errors),
            "exit_info": self.exit_info,
            "mappings": [
                {"key": m.key(), "name": m.name, "listen": m.listen,
                 "target": f"{m.target_host}:{m.target_port}",
                 "udp": m.udp, "enabled": m.enabled,
                 "bound": m.key() in self._servers or m.key() in self._udp}
                for m in self.cfg.mappings
            ],
            "stats": self.stats.snapshot(),
        }

    # -------------------------------------------------------------- listeners
    async def rebind(self) -> None:
        """(Re)create the user-facing listeners so they match the config."""
        if not self.listen_enabled:
            return
        loop = asyncio.get_running_loop()
        self.bind_errors = []
        wanted: dict[str, Mapping] = {}
        for m in self.cfg.mappings:
            if not m.enabled:
                continue
            if not m.listen or not m.target_port:
                continue
            wanted[m.key()] = m

        for key in list(self._servers):
            if key not in wanted:
                self._servers.pop(key).close()
        for key in list(self._udp):
            if key not in wanted or not wanted[key].udp:
                self._udp.pop(key).close()
                transport = self._udp_transports.pop(key, None)
                if transport is not None:
                    transport.close()

        for key, mapping in wanted.items():
            if key in self._servers or key in self._udp:
                continue
            if not mapping.udp:
                try:
                    server = await loop.create_server(
                        lambda m=mapping: UserBridge(self, m),
                        mapping.listen_host, mapping.listen, backlog=1024,
                    )
                except OSError as exc:
                    msg = (f"cannot listen on {mapping.listen_host}:{mapping.listen}"
                           f" ({exc.strerror or exc})")
                    self.bind_errors.append(msg)
                    log.error("%s", msg)
                    continue
                self._servers[key] = server
                log.info("port %d -> %s:%d (TCP)", mapping.listen,
                         mapping.target_host, mapping.target_port)
            else:
                listener = UdpRelayListener(self, mapping)
                try:
                    transport, _ = await loop.create_datagram_endpoint(
                        lambda: listener, local_addr=(mapping.listen_host, mapping.listen)
                    )
                except OSError as exc:
                    msg = (f"cannot listen on udp/{mapping.listen_host}:"
                           f"{mapping.listen} ({exc.strerror or exc})")
                    self.bind_errors.append(msg)
                    log.error("%s", msg)
                    continue
                self._udp[key] = listener
                self._udp_transports[key] = transport
                log.info("port %d -> %s:%d (UDP)", mapping.listen,
                         mapping.target_host, mapping.target_port)

    def port_conflicts(self) -> list[str]:
        """Ports in the config that somebody else already holds."""
        used_before = set(self._servers) | set(self._udp)
        out = []
        for m in self.cfg.mappings:
            if not m.enabled or m.key() in used_before:
                continue
            if not free_port(m.listen, m.listen_host):
                out.append(f"{m.listen_host}:{m.listen}")
        return out

    # ---------------------------------------------------------------- tunnel
    async def open_stream(self, addr: bytes, mode: int = MODE_TCP,
                          timeout: float = STREAM_WAIT):
        try:
            await asyncio.wait_for(self.connected.wait(), timeout)
        except asyncio.TimeoutError:
            raise ConnectionError("tunnel is not connected yet") from None
        mux = self.mux
        if mux is None or mux.closed:
            raise ConnectionError("tunnel is down")
        return await mux.open(addr, mode)

    async def _supervisor(self) -> None:
        backoff = 0.5
        endpoints = self.cfg.endpoints()
        if not endpoints:
            self.last_error = "no exit endpoint configured"
            return
        while not self._stopping:
            ep = self._pick(endpoints)
            if ep is None:
                await asyncio.sleep(1.0)
                self._fail_until.clear()
                continue
            try:
                channel = await client_connect(
                    ep.carrier, ep.address, ep.port, self.cfg.token,
                    domain=ep.domain, path=ep.path,
                    cert_fingerprint=ep.fingerprint,
                    insecure=ep.insecure_skip_verify,
                    padding=ep.padding,
                    connect_timeout=12.0,
                    chunk=self.cfg.chunk,
                )
            except Exception as exc:
                self.last_error = f"{ep.describe()}: {exc}"
                log.warning("cannot reach exit %s: %s", ep.describe(), exc)
                self._fail_until[ep.describe()] = time.monotonic() + FAIL_COOLDOWN
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.7, 15.0)
                continue
            backoff = 0.5
            self._fail_until.pop(ep.describe(), None)
            await self._run_tunnel(ep, channel)
            if not self._stopping:
                await asyncio.sleep(0.4)

    def _pick(self, endpoints):
        now = time.monotonic()
        ready = [e for e in endpoints if self._fail_until.get(e.describe(), 0) <= now]
        if ready:
            return ready[0]
        return None

    async def _run_tunnel(self, ep, channel) -> None:
        mux = Mux(
            channel,
            is_relay=True,
            on_ctrl=self._on_ctrl,
            stream_window=self.cfg.stream_window,
            chunk=self.cfg.chunk,
        )
        self.mux = mux
        self.current = ep.describe()
        self.connect_count += 1
        self.connected.set()
        self.last_error = ""
        log.info("tunnel up via %s", ep.describe())
        mux.ctrl({
            "kind": "hello",
            "name": self.cfg.name or "relay",
            "mappings": [{"listen": m.listen, "target": m.target_port,
                          "name": m.name} for m in self.cfg.mappings],
            "uptime": time.time() - self.started_at,
        })
        keepalive = asyncio.ensure_future(mux.keepalive(self.cfg.keepalive))
        reporter = asyncio.ensure_future(self._reporter(mux))
        pinger = asyncio.ensure_future(self._pinger(mux))
        try:
            await mux.run()
        finally:
            for t in (keepalive, reporter, pinger):
                t.cancel()
            self.connected.clear()
            self.mux = None
            if self._stopping:
                log.info("tunnel closed (%s)", ep.describe())
            else:
                log.warning("tunnel down (%s)", ep.describe())

    async def _reporter(self, mux: Mux) -> None:
        try:
            while True:
                await asyncio.sleep(STATS_INTERVAL)
                snap = self.stats.snapshot()
                mux.ctrl({
                    "kind": "stats",
                    "stats": {
                        "totals": snap["totals"],
                        "rates": snap["rates"],
                        "mappings": snap["mappings"],
                        "uptime": snap["uptime"],
                    },
                })
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _pinger(self, mux: Mux) -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL)
                mux.ctrl({"kind": "ping", "t": time.time()})
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _on_ctrl(self, payload: bytes) -> None:
        try:
            msg = json.loads(payload.decode("utf-8", "replace"))
        except Exception:
            return
        kind = msg.get("kind")
        if kind == "pong":
            t = msg.get("t")
            if isinstance(t, (int, float)):
                self.rtt_ms = round((time.time() - t) * 1000, 1)
            return
        if kind == "mappings":
            if not self.cfg.accept_push:
                log.info("ignored a port list pushed by the exit (accept_push = false)")
                return
            mappings = msg.get("mappings") or []
            self.apply_mappings(mappings)
            return

    # ----------------------------------------------------------- config push
    def apply_mappings(self, items: list[dict]) -> None:
        """Adopt the port list sent by the exit."""
        new: list[Mapping] = []
        for item in items:
            try:
                listen = int(item.get("port") or item.get("listen") or 0)
            except (TypeError, ValueError):
                continue
            if not listen:
                continue
            new.append(Mapping(
                name=str(item.get("name") or f"port-{listen}"),
                listen=listen,
                target_port=int(item.get("target_port") or item.get("target") or listen),
                target_host=str(item.get("target_host") or "127.0.0.1"),
                udp=bool(item.get("udp", False)),
                proxy_protocol=str(item.get("proxy_protocol", "off")),
            ))
        if not new:
            return
        old_keys = {m.key() for m in self.cfg.mappings}
        new_keys = {m.key() for m in new}
        if old_keys == new_keys:
            return
        self.cfg.mappings = new
        log.info("exit pushed a new port list: %s",
                 ", ".join(str(m.listen) for m in new))
        asyncio.ensure_future(self._rebind_and_persist())

    async def _rebind_and_persist(self) -> None:
        await self.rebind()
        if self.home is not None:
            try:
                from .config import save_relay

                save_relay(self.cfg, self.home.relay_cfg)
            except Exception as exc:
                log.debug("could not persist relay config: %s", exc)
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                pass


async def run_relay(cfg: RelayConfig, home=None) -> None:
    node = RelayNode(cfg, home=home)
    conflicts = node.port_conflicts()
    for conflict in conflicts:
        log.error("port %s is already in use -- move the panel (or the tunnel) "
                  "to another port", conflict)
    await node.start()
    log.info("Simurgh relay is running")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await node.stop()

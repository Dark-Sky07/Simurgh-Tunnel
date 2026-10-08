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
from .carriers import ServerCarrier, client_connect
from .config import Mapping, RelayConfig
from .mux import Mux, StreamOpenError
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
        #: every live tunnel connection (a pool: ``connections`` in the config)
        self._tunnels: list[Mux] = []
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
        self._tunnel_servers: list[asyncio.AbstractServer] = []
        self.started_at = time.time()
        self.connect_count = 0

    # ------------------------------------------------------------- lifecycle
    @property
    def muxes(self) -> list[Mux]:
        """Live tunnel connections, oldest first."""
        return [m for m in self._tunnels if not m.closed]

    @property
    def mux(self) -> Mux | None:
        """The primary tunnel: where the control plane talks."""
        live = self.muxes
        return live[0] if live else None

    def _refresh_connected(self) -> None:
        if self.muxes:
            self.connected.set()
        else:
            self.connected.clear()

    async def start(self) -> None:
        await self.rebind()
        if self.cfg.reverse:
            await self._start_tunnel_listener()
            return
        total = max(1, min(16, self.cfg.connections))
        if total > 1:
            log.info("keeping %d tunnel connections to the exit", total)
        for slot in range(total):
            task = asyncio.ensure_future(self._supervisor(slot))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _start_tunnel_listener(self) -> None:
        """Reverse mode: wait for the foreign exit to connect to us."""
        spec = self.cfg.tunnel
        if not spec.enabled:
            self.last_error = "the tunnel listener is disabled"
            log.error("%s", self.last_error)
            return
        cert_file, key_file = spec.cert_file, spec.key_file
        if spec.carrier in ("tls", "wss") and not (cert_file and key_file):
            if not spec.cert_auto:
                self.last_error = ("carrier tls/wss needs a certificate: set "
                                   "[tunnel] cert_file/key_file")
                log.error("%s", self.last_error)
                return
            if self.home is None:
                self.last_error = "no home directory to create a certificate in"
                log.error("%s", self.last_error)
                return
            from .certs import ensure_certificate

            cert_file, key_file = ensure_certificate(self.home,
                                                     self.cfg.name or "simurgh.local")
            spec.cert_file, spec.key_file = cert_file, key_file
        loop = asyncio.get_running_loop()
        carrier = ServerCarrier(
            spec.carrier, self.cfg.token,
            cert_file=cert_file, key_file=key_file,
            path=spec.path, fallback=spec.fallback,
            decoy_file=spec.decoy_file, padding=spec.padding,
        )
        factory = carrier.protocol_factory(self._accept_tunnel)
        try:
            server = await loop.create_server(factory, spec.host, spec.port,
                                              backlog=128)
        except OSError as exc:
            self.last_error = f"{spec.endpoint()}: {exc}"
            self.bind_errors.append(self.last_error)
            log.error("cannot listen on %s: %s", spec.endpoint(), exc)
            return
        self._tunnel_servers.append(server)
        mode = "decoy website" if spec.carrier in ("tls", "wss") else "noise"
        log.info("waiting for the exit on %s (probers see: %s)",
                 spec.endpoint(), mode)

    async def _accept_tunnel(self, channel) -> None:
        """A tunnel connection from an exit node (reverse mode)."""
        peer = getattr(channel, "peer", "") or "?"
        wanted = max(1, min(16, self.cfg.connections))
        if len(self.muxes) >= wanted:
            # a pool is expected: an extra connection replaces the oldest, which
            # is how a restarted exit takes over without duplicating tunnels
            log.warning("a second exit connected (%s); replacing the oldest tunnel",
                        peer)
            self.muxes[0].close()
            await asyncio.sleep(0.2)
        self.connect_count += 1
        await self._run_tunnel(None, channel, label=f"in {channel.name}<-{peer}")

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._tasks):
            task.cancel()
        for server in self._tunnel_servers:
            server.close()
        for server in self._servers.values():
            server.close()
        for listener in self._udp.values():
            listener.close()
        self._tunnel_servers.clear()
        self._servers.clear()
        self._udp.clear()
        for mux in list(self._tunnels):
            mux.close()
        # the tunnel handlers run outside our task tree, so make the stopped
        # state visible at once instead of waiting for their finally blocks
        self._tunnels.clear()
        self.current = ""
        self.connected.clear()
        await asyncio.sleep(0)

    def reconnect(self) -> None:
        """Drop every tunnel; the supervisors dial fresh ones."""
        self._fail_until.clear()
        for mux in list(self._tunnels):
            mux.close()
        self.current = ""

    def status(self) -> dict:
        return {
            "role": "relay",
            "name": self.cfg.name or "relay",
            "connected": self.connected.is_set(),
            "current_exit": self.current,
            "connections": len(self.muxes),
            "tunnel_targets": [self.current] * len(self.muxes) if self.current else [],
            "rtt_ms": self.rtt_ms,
            "uptime": time.time() - self.started_at,
            "reconnects": self.connect_count,
            "last_error": self.last_error,
            "bind_errors": list(self.bind_errors),
            "dial": self.cfg.dial,
            "tunnel_listen": ([self.cfg.tunnel.endpoint()] if self.cfg.reverse else []),
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
        if self.cfg.reverse and self.cfg.tunnel.enabled and not self._tunnel_servers:
            spec = self.cfg.tunnel
            if all(spec.port != m.listen for m in self.cfg.mappings):
                if not free_port(spec.port, spec.host):
                    out.append(f"tunnel: {spec.host}:{spec.port}")
        for m in self.cfg.mappings:
            if not m.enabled or m.key() in used_before:
                continue
            if not free_port(m.listen, m.listen_host):
                out.append(f"{m.listen_host}:{m.listen}")
        return out

    # ---------------------------------------------------------------- tunnel
    async def open_stream(self, addr: bytes, mode: int = MODE_TCP,
                          timeout: float = STREAM_WAIT):
        """Open a stream on the least busy tunnel connection.

        With a pool, a user connection lands on whichever tunnel currently
        carries the fewest streams, so the load spreads and one dying
        connection costs a fraction of the users.  A transport-level failure
        (timeout, tunnel closed) is retried on another connection; a target
        level failure (refused, denied) is final.
        """
        try:
            await asyncio.wait_for(self.connected.wait(), timeout)
        except asyncio.TimeoutError:
            raise ConnectionError("tunnel is not connected yet") from None
        candidates = sorted(self.muxes, key=lambda m: len(m.streams))
        if not candidates:
            raise ConnectionError("tunnel is down")
        last: Exception | None = None
        for mux in candidates:
            if mux.closed:
                continue
            try:
                return await mux.open(addr, mode)
            except StreamOpenError as exc:
                last = exc
                if exc.code and exc.code != 2:      # target said no: stop here
                    raise
                log.debug("stream open failed on one tunnel (%s); trying another",
                          exc)
            except ConnectionError as exc:
                last = exc
        raise last or ConnectionError("no tunnel connection could open the stream")

    async def _supervisor(self, slot: int = 0) -> None:
        backoff = 0.5
        endpoints = self.cfg.endpoints()
        if not endpoints:
            self.last_error = "no exit endpoint configured"
            return
        if slot:
            # stagger a pool so the tunnels do not all flap in lockstep
            await asyncio.sleep(0.15 * slot)
        while not self._stopping:
            ep = self._pick(endpoints)
            if ep is None:
                await asyncio.sleep(1.0)
                if slot == 0:
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
            await self._run_tunnel(ep, channel, slot=slot)
            if not self._stopping:
                await asyncio.sleep(0.4)

    def _pick(self, endpoints):
        now = time.monotonic()
        ready = [e for e in endpoints if self._fail_until.get(e.describe(), 0) <= now]
        if ready:
            return ready[0]
        return None

    async def _run_tunnel(self, ep, channel, label: str = "", slot: int = 0) -> None:
        label = label or ep.describe()
        mux = Mux(
            channel,
            is_relay=True,
            on_ctrl=self._on_ctrl,
            stream_window=self.cfg.stream_window,
            max_stream_window=self.cfg.max_stream_window,
            chunk=self.cfg.chunk,
        )
        self._tunnels.append(mux)
        primary = self.mux is mux
        self.current = label
        self.connect_count += 1
        self._refresh_connected()
        self.last_error = ""
        log.info("tunnel up via %s%s", label,
                 f" [{len(self.muxes)}/{max(1, self.cfg.connections)}]" if self.cfg.connections > 1 else "")
        tasks = [asyncio.ensure_future(mux.keepalive(self.cfg.keepalive))]
        if primary:
            mux.ctrl({
                "kind": "hello",
                "name": self.cfg.name or "relay",
                "mappings": [{"listen": m.listen, "target": m.target_port,
                              "name": m.name} for m in self.cfg.mappings],
                "uptime": time.time() - self.started_at,
            })
            tasks.append(asyncio.ensure_future(self._reporter(mux)))
            tasks.append(asyncio.ensure_future(self._pinger(mux)))
        try:
            await mux.run()
        finally:
            for t in tasks:
                t.cancel()
            if mux in self._tunnels:
                self._tunnels.remove(mux)
            remaining = self.muxes
            if not remaining:
                self.connected.clear()
                self.current = ""
            elif self.current == label:
                # the primary left; promote whatever is still up
                self.current = label
            if self._stopping:
                log.info("tunnel closed (%s)", label)
            elif self.cfg.reverse:
                log.warning("the exit disconnected (%s); still listening", label)
            else:
                log.warning("tunnel down (%s); %d connection(s) left",
                            label, len(remaining))

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

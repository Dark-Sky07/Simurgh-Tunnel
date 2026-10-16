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
from pathlib import Path

from .accounting import TrafficAccount, quota_from
from .alerts import alerts_from_config
from .bridge import Bridge, proxy_v1_header
from .carriers import ServerCarrier, client_connect, create_tcp_server
from .config import Mapping, RelayConfig
from .mux import Mux, StreamOpenError
from .protocol import MODE_TCP, encode_addr
from .ratelimit import limiter_from
from .stats import Stats
from .udp import UdpRelayListener
from .certs import expiry_days
from .util import free_port, get_logger, human_rate, is_ip

log = get_logger("simurgh.relay")

STREAM_WAIT = 12.0
FAIL_COOLDOWN = 30.0
#: when a mapping is over quota and its action is "throttle", the crawl it drops
#: to until the period rolls over
THROTTLE_RATE = 128 * 1024
#: warn when a served certificate has this little life left
CERT_ALERT_DAYS = 14
STATS_INTERVAL = 5.0
PING_INTERVAL = 10.0


class UserBridge(Bridge):
    """A user connection being pushed into the tunnel."""

    def __init__(self, node: "RelayNode", mapping: Mapping, **kw):
        super().__init__(opener=lambda: node.open_stream(
            encode_addr(mapping.target_host, mapping.target_port), MODE_TCP,
            mapping=mapping),
            counter=node.stats.mapping(mapping.key()), **kw)
        self.node = node
        self.mapping = mapping
        self.limiter = node.limiter_for(mapping)
        self.on_bytes = lambda direction, n: node.count_bytes(mapping, direction, n)
        #: set once the mapping's connection slot is taken (see connection_made)
        self._slot = False

    def connection_lost(self, exc) -> None:
        if self._slot:
            self._slot = False
            self.node.release_slot(self.mapping)
        super().connection_lost(exc)

    def connection_made(self, transport) -> None:
        if not self.node.get_slot(self.mapping):
            self.node.stats.mapping(self.mapping.key()).errors += 1
            self.node.stats.mapping(self.mapping.key()).last_error = "max_conns reached"
            log.warning("mapping %s is at max_conns; refusing %s",
                        self.mapping.name or self.mapping.key(), transport)
            try:
                transport.close()
            except Exception:
                pass
            return
        self._slot = True
        quota = self.node.quota_for(self.mapping)
        if self.node._over_quota(self.mapping):
            action = getattr(quota, "action", "block")
            if action == "throttle":
                # keep the user online, but at a crawl until the window resets
                self.limiter = limiter_from(THROTTLE_RATE, self.mapping.key())
                self.node.alerts.notify(
                    "throttle", f"mapping {self.mapping.name or self.mapping.key()} "
                    f"is over its quota; throttled to "
                    f"{human_rate(THROTTLE_RATE)}", level="warning",
                    mapping=self.mapping.name or self.mapping.key())
            else:
                self.node.blocked += 1
                self.node.stats.mapping(self.mapping.key()).errors += 1
                self.node.stats.mapping(self.mapping.key()).last_error = "quota exceeded"
                self.node.alerts.notify(
                    "quota", f"mapping {self.mapping.name or self.mapping.key()} "
                    f"is over its quota; refusing users",
                    level="warning", mapping=self.mapping.name or self.mapping.key(),
                    used=self.node.account.usage(self.mapping.key(), quota.period)
                    if self.node.account else 0, limit=quota.limit)
                log.warning("mapping %s is over its quota; refusing %s",
                            self.mapping.name or self.mapping.key(), transport)
                try:
                    transport.close()
                except Exception:
                    pass
                return
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
        self._mux_endpoint: dict[int, ExitEndpoint] = {}
        self._health: dict[str, dict] = {}
        self._ping_sent: dict[int, float] = {}
        self.current: str = ""
        #: per-mapping bandwidth buckets, quotas and their traffic history
        self._limiters: dict[str, object] = {}
        self._quotas: dict[str, object] = {}
        #: concurrent user connections per mapping (``max_conns``)
        self._active: dict[str, int] = {}
        self._warned: dict[str, float] = {}
        self.blocked = 0
        #: telegram / webhook notifications (see simurgh/alerts.py)
        self.alerts = alerts_from_config(cfg, name=cfg.name or "relay")
        #: certificates this node serves, for the "expiring soon" alert
        self._watched_certs: dict[str, str] = {}
        self._cert_warned: dict[str, float] = {}
        self.account: TrafficAccount | None = None
        if home is not None:
            # `home` is a util.Home in every real caller and a plain path in a
            # few tools; both have to reach <home>/traffic.json
            base = getattr(home, "path", home)
            self.account = TrafficAccount(Path(base) / "traffic.json")
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
        if self.account is not None:
            self.account.start()
        await self.alerts.start()
        self.alerts.notify("started", f"relay {self.cfg.name or ''} started "
                           f"(engine: {self.cfg.engine})".strip(),
                           mappings=len(self.cfg.mappings),
                           exit=self.cfg.exit.describe() if self.cfg.exit.address else "")
        if self.cfg.reverse:
            await self._start_tunnel_listener()
            return
        endpoints = self.cfg.endpoints()
        total = max(1, min(16, self.cfg.connections))
        if total > 1:
            log.info("keeping %d tunnel connections to %d exit(s)",
                     total, max(1, len(endpoints)))
        if not endpoints:
            plan = [(None, 0)]
        else:
            # every configured exit gets its own tunnel, then the extra
            # connections of `connections` are spread round robin
            plan = [(endpoint, index) for index, endpoint in enumerate(endpoints)]
            for index in range(max(0, total - len(endpoints))):
                plan.append((endpoints[index % len(endpoints)], len(plan)))
        for endpoint, slot in plan:
            task = asyncio.ensure_future(self._supervisor(slot, endpoint))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        if endpoints:
            health = asyncio.ensure_future(self.health_loop())
            self._tasks.add(health)
            health.add_done_callback(self._tasks.discard)
        certs = asyncio.ensure_future(self.cert_loop())
        self._tasks.add(certs)
        certs.add_done_callback(self._tasks.discard)

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
        tokens = self.cfg.token_list()
        if not tokens:
            self.last_error = f"{spec.endpoint()}: no token configured"
            log.error("%s has no token to authenticate exits with", spec.endpoint())
            return
        carrier = ServerCarrier(
            spec.carrier, tokens[0].token, tokens=tokens,
            cert_file=cert_file, key_file=key_file,
            path=spec.path, fallback=spec.fallback,
            decoy_file=spec.decoy_file, padding=spec.padding,
            chunk=self.cfg.chunk,
        )
        try:
            if spec.carrier == "udp":
                server, _proto = await carrier.create_udp_endpoint(
                    loop, spec.host, spec.port, self._accept_tunnel,
                    mtu=spec.mtu, fec=spec.fec, window=spec.window,
                )
            else:
                factory = carrier.protocol_factory(self._accept_tunnel)
                server = await create_tcp_server(
                    loop, factory, spec.host, spec.port, backlog=128,
                    dual_stack=self.cfg.dual_stack,
                )
        except OSError as exc:
            self.last_error = f"{spec.endpoint()}: {exc}"
            self.bind_errors.append(self.last_error)
            log.error("cannot listen on %s: %s", spec.endpoint(), exc)
            return
        self._tunnel_servers.append(server)
        if spec.carrier == "udp":
            log.info("waiting for the exit on %s/udp (mtu %d, fec %s)",
                     spec.endpoint(), spec.mtu, spec.fec)
            return
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
        if self.account is not None:
            await self.account.stop()
        self.alerts.notify("stopped", f"relay {self.cfg.name or ''} stopped".strip())
        await self.alerts.stop()
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
            "mappings": [self._mapping_row(m) for m in self.cfg.mappings],
            "quotas": {m.key(): self.quota_state(m) for m in self.cfg.mappings},
            "stats": self.stats.snapshot(),
            "health": self.health_snapshot(),
            "traffic": (self.account.rows("month") if self.account else []),
            "ledger": (self.account.snapshot() if self.account else {}),
            "blocked": self.blocked,
        }

    def _mapping_row(self, m) -> dict:
        """One panel/metrics row per mapping, with its own counters."""
        counter = self.stats.mappings.get(m.key())
        stats = counter.as_dict() if counter is not None else {}
        return {
            "key": m.key(), "name": m.name, "listen": m.listen,
            "target": f"{m.target_host}:{m.target_port}",
            "udp": m.udp, "enabled": m.enabled,
            "bound": m.key() in self._servers or m.key() in self._udp,
            "stats": stats,
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
    def _candidates(self, mapping=None):
        """Tunnels this stream may use, least busy first.

        A mapping may pin itself to one exit (``exit = "kharej-2"``, matching
        the endpoint's label or address); otherwise every healthy tunnel is a
        candidate and the least loaded one wins.
        """
        now = time.monotonic()
        wanted = (getattr(mapping, "exit", "") or "").strip().lower()
        out = []
        for mux in self.muxes:
            if mux.closed:
                continue
            endpoint = self._mux_endpoint.get(id(mux))
            if wanted:
                names = self._endpoint_names(endpoint) if endpoint else set()
                if wanted not in names:
                    continue
            if endpoint is not None and self._fail_until.get(endpoint.describe(), 0) > now:
                continue
            out.append(mux)
        out.sort(key=lambda m: len(m.streams))
        if out or not wanted:
            return out
        # the pinned exit has no tunnel right now: better a working tunnel
        # through another exit than a dead port
        return sorted((m for m in self.muxes if not m.closed),
                      key=lambda m: len(m.streams))

    @staticmethod
    def _endpoint_names(endpoint) -> set[str]:
        if endpoint is None:
            return set()
        return {endpoint.address.lower(), (endpoint.label or "").lower(),
                endpoint.describe().lower()}

    async def open_stream(self, addr: bytes, mode: int = MODE_TCP,
                          timeout: float = STREAM_WAIT, mapping=None):
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
        candidates = self._candidates(mapping)
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

    async def _supervisor(self, slot: int = 0, pinned=None) -> None:
        backoff = 0.5
        endpoints = self.cfg.endpoints()
        if not endpoints:
            self.last_error = "no exit endpoint configured"
            return
        if slot:
            # stagger a pool so the tunnels do not all flap in lockstep
            await asyncio.sleep(0.15 * slot)
        while not self._stopping:
            ep = pinned
            if ep is None or self._fail_until.get(ep.describe(), 0) > time.monotonic():
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
                    split=ep.split,
                    connect_timeout=12.0,
                    chunk=self.cfg.chunk,
                    # the datagram carrier's knobs and the upstream dial path
                    mtu=ep.mtu, fec=ep.fec, window=ep.window,
                    proxy=ep.proxy or self.cfg.proxy,
                    net=self._net_opts(ep),
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

    async def cert_loop(self, interval: float = 6 * 3600) -> None:
        """Warn before a certificate dies (the certificate the exit pins).

        Runs on the relay in reverse mode and on the exit otherwise; it only
        looks at the files this node actually serves.  One alert a day per
        certificate, because the operator cannot do anything faster than that.
        """
        if self.cfg.reverse:
            cert_file = self.cfg.tunnel.cert_file
            if cert_file and not self.cfg.tunnel.cert_auto:
                self._watch_certificate("tunnel", cert_file)
        self._check_certs()
        while not self._stopping:
            await asyncio.sleep(interval)
            self._check_certs()

    def _check_certs(self) -> list[str]:
        """Alert for every served certificate that is close to expiry.

        Returns the labels that were just alerted for, which keeps the loop
        testable without waiting six hours for the first pass.
        """
        alerted: list[str] = []
        for label, cert_file in list(self._watched_certs.items()):
            days = expiry_days(cert_file)
            if days is None:
                continue
            if days > CERT_ALERT_DAYS:
                self._cert_warned.pop(label, None)
                continue
            last = self._cert_warned.get(label)
            if last is not None and time.monotonic() - last <= 86400:
                continue
            self._cert_warned[label] = time.monotonic()
            alerted.append(label)
            self.alerts.notify(
                "cert", f"certificate {cert_file} expires in "
                f"{days:.0f} day(s)", level="warning",
                cert=cert_file, days=round(days, 1))
        return alerted

    def _watch_certificate(self, label: str, cert_file: str) -> None:
        if cert_file:
            self._watched_certs[label] = cert_file

    async def health_loop(self, interval: float = 10.0) -> None:
        """Measure each tunnel's round trip and skip exits that stop answering.

        This is the exit side of the 'which exit is alive' question: the panel
        shows the latency, and three missed probes in a row take the endpoint
        out of the candidate list until its tunnel reconnects.
        """
        while not self._stopping:
            await asyncio.sleep(interval)
            now = time.time()
            for mux in list(self.muxes):
                endpoint = self._mux_endpoint.get(id(mux))
                if endpoint is None:
                    continue
                row = self._health.setdefault(endpoint.describe(), {
                    "up": True, "rtt_ms": None, "fails": 0, "since": now,
                    "label": endpoint.label or endpoint.address,
                })
                sent = self._ping_sent.get(id(mux))
                if sent is not None:
                    # no pong since the last round
                    row["fails"] = int(row.get("fails", 0)) + 1
                    if row["fails"] >= 3:
                        row["up"] = False
                        self._fail_until[endpoint.describe()] = time.monotonic() + 15.0
                        log.warning("exit %s stopped answering its probes",
                                    endpoint.describe())
                        try:
                            mux.close()
                        except Exception:
                            pass
                        continue
                try:
                    mux.ctrl({"kind": "ping", "t": now})
                    self._ping_sent[id(mux)] = now
                except Exception:
                    row["fails"] = int(row.get("fails", 0)) + 1

    def health_snapshot(self) -> list[dict]:
        out = []
        for name, row in self._health.items():
            item = dict(row)
            item["endpoint"] = name
            item["failed_until"] = max(0.0, self._fail_until.get(name, 0) - time.monotonic())
            out.append(item)
        return out

    # ------------------------------------------------------------- limits
    def limiter_for(self, mapping) -> object:
        """The bandwidth bucket of one mapping (created on first use)."""
        key = mapping.key()
        limiter = self._limiters.get(key)
        if limiter is None:
            limiter = limiter_from(getattr(mapping, "rate_limit", 0)
                                   or self.cfg.rate_limit, key)
            self._limiters[key] = limiter
        return limiter

    def quota_for(self, mapping):
        """The traffic quota of one mapping (created on first use)."""
        key = mapping.key()
        quota = self._quotas.get(key)
        if quota is None:
            quota = quota_from(getattr(mapping, "quota", 0) or self.cfg.quota,
                               getattr(mapping, "quota_period", self.cfg.quota_period),
                               getattr(mapping, "quota_action", self.cfg.quota_action),
                               key)
            self._quotas[key] = quota
        return quota

    def quota_state(self, mapping) -> dict:
        quota = self.quota_for(mapping)
        if self.account is None or not quota.enabled:
            return {"enabled": False}
        return quota.state(self.account, mapping.key())

    def count_bytes(self, mapping, direction: str, nbytes: int) -> None:
        """Count one chunk of a mapping's traffic.

        Both the panel's live counter and the persistent ledger are fed here,
        which is also what the quota checks and `/metrics` read.
        """
        if not nbytes:
            return
        key = mapping.key()
        counter = self.stats.mapping(key)
        up = direction == "up"
        if up:
            counter.in_bytes += nbytes
        else:
            counter.out_bytes += nbytes
        if self.account is not None:
            if up:
                self.account.add(key, in_bytes=nbytes)
            else:
                self.account.add(key, out_bytes=nbytes)

    def max_conns(self, mapping) -> int:
        """The concurrent-connection ceiling of one mapping (0 = unlimited)."""
        return max(0, int(getattr(mapping, "max_conns", 0)
                          or getattr(self.cfg, "max_conns", 0) or 0))

    def get_slot(self, mapping) -> bool:
        """Reserve one of the mapping's connection slots."""
        limit = self.max_conns(mapping)
        if not limit:
            return True
        key = mapping.key()
        if self._active.get(key, 0) >= limit:
            return False
        self._active[key] = self._active.get(key, 0) + 1
        return True

    def release_slot(self, mapping) -> None:
        if not self.max_conns(mapping):
            return
        key = mapping.key()
        self._active[key] = max(0, self._active.get(key, 0) - 1)

    def _over_quota(self, mapping) -> bool:
        """Has this mapping used up its allowance?  Warns once per 5 minutes."""
        quota = self.quota_for(mapping)
        if self.account is None or not quota.enabled:
            return False
        if not quota.exceeded(self.account, mapping.key()):
            return False
        action = getattr(quota, "action", "block")
        if action == "warn":
            now = time.monotonic()
            if now - self._warned.get(mapping.key(), 0) > 300:
                self._warned[mapping.key()] = now
                used = self.account.usage(mapping.key(), quota.period)
                log.warning("mapping %s is over its quota (%d/%d this %s)",
                            mapping.name or mapping.key(), used, quota.limit,
                            quota.period)
            return False
        return True

    def _net_opts(self, ep) -> dict:
        """Socket options for the outgoing tunnel (endpoint first, node second)."""
        return {
            "nodelay": ep.tcp_nodelay if ep.tcp_nodelay is not None
            else self.cfg.tcp_nodelay,
            "keepalive": ep.tcp_keepalive if ep.tcp_keepalive is not None
            else self.cfg.tcp_keepalive,
            "so_mark": ep.so_mark if ep.so_mark is not None else self.cfg.so_mark,
            "prefer": ep.prefer_ipv6 if ep.prefer_ipv6 is not None
            else self.cfg.prefer_ipv6,
        }

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
        if ep is not None:
            self._mux_endpoint[id(mux)] = ep
            self._health.setdefault(ep.describe(), {
                "up": True, "rtt_ms": None, "fails": 0,
                "since": time.time(), "label": ep.label or ep.address,
            })
            self._health[ep.describe()]["up"] = True
            self._health[ep.describe()]["fails"] = 0
        primary = self.mux is mux
        self.current = label
        self.connect_count += 1
        self._refresh_connected()
        self.last_error = ""
        self.alerts.notify("connected", f"tunnel up via {label} "
                           f"({len(self.muxes)} live)", endpoint=label,
                           carrier=getattr(channel, "name", ""))
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
                if not self._stopping:
                    self.alerts.notify("disconnected",
                                       f"tunnel to {label} is down "
                                       f"({self.last_error or 'connection closed'})",
                                       level="warning", endpoint=label)
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
            for mux in self.muxes:
                if self._ping_sent.pop(id(mux), None) is not None:
                    endpoint = self._mux_endpoint.get(id(mux))
                    if endpoint is not None:
                        row = self._health.setdefault(endpoint.describe(), {})
                        row["rtt_ms"] = self.rtt_ms
                        row["up"] = True
                        row["fails"] = 0
                        row["checked"] = time.time()
                    break
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

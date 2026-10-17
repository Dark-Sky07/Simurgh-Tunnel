"""The exit node -- runs on the foreign (Kharej) server.

It listens for the tunnel, and for every ``OPEN`` it receives it dials the
target *on that machine* (normally ``127.0.0.1:<the panel's port>``) and pipes
the bytes.  It never looks inside the traffic: TLS stays end to end, so the
panel authenticates the real user exactly as before.

Nothing about the tunnel's presence leaks to the target: it is a plain TCP
connection from localhost.
"""

from __future__ import annotations

import asyncio
import json
import time

from .alerts import alerts_from_config
from .bridge import Bridge
from .certs import expiry_days
from .carriers import ServerCarrier, client_connect, create_tcp_server
from .config import ExitConfig
from .mux import Mux, StreamOpenError
from .protocol import (
    ERR_DENIED,
    ERR_REFUSED,
    ERR_TIMEOUT,
    ERR_UNREACHABLE,
    MODE_UDP,
    T_OPEN_OK,
    decode_addr,
    make_frame,
)
from .speedtest import SpeedServer, UdpEchoServer
from .stats import Stats
from .udp import UdpExitFlow
from .util import dial_stream, get_logger

log = get_logger("simurgh.exit")

#: warn when a served certificate has this little life left
CERT_ALERT_DAYS = 14
DIAL_TIMEOUT = 12.0
RECONNECT_BACKOFF_MAX = 15.0


class ExitNode:
    def __init__(self, cfg: ExitConfig, push_callback=None):
        self.cfg = cfg
        self.stats = Stats()
        self.servers: list = []
        #: (kind, handle) pairs, printed by the panel
        self._endpoints: list[tuple[str, object]] = []
        self.tunnels: set[Mux] = set()
        self.tunnel_peers: dict[int, str] = {}
        self.exit_info = {
            "name": cfg.name or "exit",
            "mappings": [],           # filled by the panel/config push
            "proxy_protocol": cfg.proxy_protocol,
        }
        self.push_callback = push_callback
        self.speed_port = cfg.speedtest_port
        self._speed_server: asyncio.AbstractServer | None = None
        self._speed_udp: asyncio.DatagramTransport | None = None
        self.connected_at: float | None = None
        #: telegram / webhook notifications (see simurgh/alerts.py)
        self.alerts = alerts_from_config(cfg, name=cfg.name or "exit")
        #: certificates this node serves, for the "expiring soon" alert
        self._watched_certs: dict[str, str] = {}
        self._cert_warned: dict[str, float] = {}
        self._cert_task: asyncio.Task | None = None
        self.last_error = ""
        #: reverse mode (relay dial = "exit"): we connect out instead of listening
        self.reach_out: list[str] = []
        self._dial_tasks: set[asyncio.Task] = set()
        self._stopping = False

    # ------------------------------------------------------------------ start
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        await self.alerts.start()
        self.alerts.notify("started", f"exit {self.cfg.name or ''} started "
                           f"(engine: {self.cfg.engine})".strip(),
                           listeners=len([s for s in self.cfg.listen if s.enabled]))
        self._collect_certificates()
        self._cert_task = asyncio.ensure_future(self.cert_loop())
        for spec in self.cfg.listen:
            if not spec.enabled:
                continue
            if spec.reverse:
                # reverse mode: the relay listens, we dial it and keep it up.
                # ``connections`` dials several tunnels so one flow between the
                # two servers is not the ceiling for every user at once.
                total = max(1, min(16, self.cfg.connections))
                self.reach_out.append(spec.endpoint())
                for slot in range(total):
                    task = asyncio.ensure_future(self._dial_supervisor(spec, slot))
                    self._dial_tasks.add(task)
                    task.add_done_callback(self._dial_tasks.discard)
                log.info("dialling the relay at %s:%s (%s)%s", spec.dial, spec.port,
                         spec.carrier,
                         f" x{total}" if total > 1 else "")
                continue
            tokens = self.cfg.token_list()
            if not tokens:
                self.last_error = f"{spec.endpoint()}: no token configured"
                log.error("%s has no token to authenticate with", spec.endpoint())
                continue
            carrier = ServerCarrier(
                spec.carrier, tokens[0].token, tokens=tokens,
                cert_file=spec.cert_file or self.cfg.cert_file,
                key_file=spec.key_file or self.cfg.key_file,
                path=spec.path, fallback=spec.fallback,
                decoy_file=spec.decoy_file, padding=spec.padding,
                chunk=self.cfg.chunk,
            )
            try:
                endpoint = await self._listen(carrier, spec)
            except OSError as exc:
                self.last_error = f"{spec.endpoint()}: {exc}"
                log.error("cannot listen on %s: %s", spec.endpoint(), exc)
                continue
            self._endpoints.append(endpoint)
            if spec.carrier == "udp":
                log.info("listening %s/udp (mtu %d, fec %s, window %d)",
                         spec.endpoint(), spec.mtu, spec.fec, spec.window)
            else:
                mode = ("decoy website" if spec.carrier in ("tls", "wss")
                        else "noise")
                log.info("listening %s (probers see: %s)", spec.endpoint(), mode)

        # localhost-only speed test endpoint (used by `simurgh speedtest`)
        try:
            self._speed_server = await loop.create_server(
                SpeedServer, "127.0.0.1", self.speed_port, backlog=16
            )
            log.info("speed test endpoint on 127.0.0.1:%d (localhost only)",
                     self.speed_port)
        except OSError:
            self._speed_server = None
        # ... and the same port for datagrams: the udp half of the test asks
        # the exit to bounce a datagram back through the tunnel
        try:
            transport, _ = await loop.create_datagram_endpoint(
                UdpEchoServer, local_addr=("127.0.0.1", self.speed_port))
            self._speed_udp = transport
            log.info("udp speed test endpoint on 127.0.0.1:%d (localhost only)",
                     self.speed_port)
        except OSError:
            self._speed_udp = None

    async def _listen(self, carrier: ServerCarrier, spec):
        """Open one listener: a datagram endpoint for 'udp', a socket otherwise."""
        loop = asyncio.get_running_loop()
        if spec.carrier == "udp":
            transport, _proto = await carrier.create_udp_endpoint(
                loop, spec.host, spec.port, self._on_channel,
                mtu=spec.mtu, fec=spec.fec, window=spec.window,
                max_sessions=getattr(spec, "max_sessions", 64),
            )
            self.servers.append(transport)
            return ("udp", transport)
        factory = carrier.protocol_factory(self._on_channel)
        server = await create_tcp_server(
            loop, factory, spec.host, spec.port, backlog=512,
            dual_stack=self.cfg.dual_stack,
        )
        self.servers.append(server)
        return ("tcp", server)

    async def cert_loop(self, interval: float = 6 * 3600) -> None:
        """Warn before a served certificate dies (see RelayNode.cert_loop)."""
        self._check_certs()
        while not self._stopping:
            await asyncio.sleep(interval if self._cert_warned else 5)
            self._check_certs()

    def _collect_certificates(self) -> None:
        """Watch the certificate of every tls/wss listener we did not generate.

        ``cert_auto`` certificates are made by this process and renewed by it,
        so there is nothing for the operator to do about them; a certificate
        that was copied in by hand (acme.sh, certbot, a CA) is exactly the one
        that quietly expires on a Sunday.
        """
        if self.cfg.cert_auto:
            return
        for spec in self.cfg.listen:
            if not spec.enabled or spec.carrier not in ("tls", "wss"):
                continue
            cert_file = spec.cert_file or self.cfg.cert_file
            if cert_file:
                self._watch_certificate(f"{spec.carrier}:{spec.port}", cert_file)

    def _watch_certificate(self, label: str, cert_file: str) -> None:
        """Remember a certificate this exit serves (see ``cert_loop``)."""
        if cert_file:
            self._watched_certs[label] = cert_file

    def _check_certs(self) -> list[str]:
        """Alert for every served certificate that is close to expiry.

        See ``RelayNode._check_certs``; the label this exit uses is the
        listener name from its own configuration.
        """
        alerted: list[str] = []
        for label, cert_file in list(self._watched_certs.items()):
            days = expiry_days(cert_file)
            if days is None or days > CERT_ALERT_DAYS:
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

    async def stop(self) -> None:
        self._stopping = True
        if self._cert_task is not None:
            self._cert_task.cancel()
            self._cert_task = None
        self.alerts.notify("stopped", f"exit {self.cfg.name or ''} stopped".strip())
        await self.alerts.stop()
        for task in list(self._dial_tasks):
            task.cancel()
        for s in self.servers:
            s.close()
        if self._speed_udp is not None:
            self._speed_udp.close()
            self._speed_udp = None
        if self._speed_server:
            self._speed_server.close()
        for mux in list(self.tunnels):
            mux.close()
        self.servers.clear()
        self.tunnels.clear()
        self.tunnel_peers.clear()
        self.connected_at = None
        await asyncio.sleep(0)

    async def _dial_supervisor(self, spec, slot: int = 0) -> None:
        """Keep one outbound tunnel to the relay alive (reverse mode)."""
        backoff = 0.5
        if slot:
            await asyncio.sleep(0.15 * slot)
        while True:
            try:
                channel = await client_connect(
                    spec.carrier, spec.dial, spec.port, self.cfg.token,
                    domain=spec.dial, path=spec.path,
                    cert_fingerprint=spec.fingerprint,
                    insecure=spec.insecure_skip_verify,
                    padding=spec.padding,
                    split=spec.split,
                    connect_timeout=15.0,
                    mtu=spec.mtu, fec=spec.fec, window=spec.window,
                    proxy=spec.proxy,
                    net=self._net_opts(spec),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{spec.endpoint()}: {exc}"
                log.warning("cannot reach the relay at %s: %s", spec.endpoint(), exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.7, RECONNECT_BACKOFF_MAX)
                continue
            backoff = 0.5
            try:
                await self._on_channel(channel, check_ips=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                      # pragma: no cover
                self.last_error = f"{spec.endpoint()}: {exc}"
                log.warning("tunnel to the relay failed: %s", exc)
            if self._stopping:
                return
            await asyncio.sleep(0.4)

    def _net_opts(self, spec) -> dict:
        """Socket options for the outbound tunnel to the relay (reverse mode)."""
        return {
            "nodelay": self.cfg.tcp_nodelay,
            "keepalive": self.cfg.tcp_keepalive,
            "so_mark": self.cfg.so_mark,
            "prefer": spec.prefer_ipv6,
        }

    # ------------------------------------------------------------ tunnel side
    async def _on_channel(self, channel, check_ips: bool = True) -> None:
        peer = getattr(channel, "peer", "") or ""
        ip = _ip_of(peer)
        if check_ips and self.cfg.allow_ips and ip not in self.cfg.allow_ips:
            log.warning("refused tunnel from %s (not in allow_ips)", peer)
            try:
                await channel.close()
            except Exception:
                pass
            return

        mux = Mux(
            channel,
            is_relay=False,
            on_open=self._on_open,
            on_ctrl=self._on_ctrl,
            stream_window=self.cfg.stream_window,
            max_stream_window=self.cfg.max_stream_window,
            chunk=self.cfg.chunk,
        )
        self.tunnels.add(mux)
        self.tunnel_peers[id(mux)] = peer
        self.stats.tunnel.active = len(self.tunnels)
        if self.connected_at is None:
            self.connected_at = time.time()
        # tell the relay where our speed endpoint listens (it is localhost-only,
        # so this is the only way `simurgh speedtest` can find a moved port)
        try:
            mux.ctrl({"kind": "hello", "name": self.cfg.name or "exit",
                      "uptime": time.time() - self.connected_at,
                      "speedtest_port": self.speed_port})
        except Exception:      # pragma: no cover - the mux may be gone already
            pass
        log.info("tunnel established with %s (carrier: %s)", peer or "?", channel.name)
        self.alerts.notify("connected", f"relay connected from {peer or '?'} "
                           f"(carrier: {channel.name}); "
                           f"{len(self.tunnels)} tunnel(s) live",
                           peer=peer, carrier=channel.name)
        ping = asyncio.ensure_future(mux.keepalive(25))
        try:
            await mux.run()
        finally:
            ping.cancel()
            self.tunnels.discard(mux)
            self.tunnel_peers.pop(id(mux), None)
            self.stats.tunnel.active = len(self.tunnels)
            if not self.tunnels:
                self.connected_at = None
            log.warning("tunnel from %s closed", peer or "?")
            if not self._stopping:
                self.alerts.notify("disconnected",
                                   f"relay {peer or '?'} disconnected; "
                                   f"{len(self.tunnels)} tunnel(s) left",
                                   level="warning", peer=peer)

    async def _on_open(self, stream) -> None:
        try:
            host, port, _ = decode_addr(stream.target)
        except ValueError as exc:
            raise StreamOpenError(f"bad target: {exc}") from exc

        if self.cfg.strict_ports and self.cfg.push_ports and port not in self.cfg.push_ports:
            raise StreamOpenError(f"port {port} not allowed", ERR_DENIED)

        if stream.mode == MODE_UDP:
            await self._open_udp(stream, host, port)
            return

        key = f"{host}:{port}"
        counter = self.stats.mapping(key)
        loop = asyncio.get_running_loop()

        def factory():
            proto = Bridge(stream=stream, counter=counter)
            return proto

        try:
            await asyncio.wait_for(
                dial_stream(loop, factory, host, port,
                            nodelay=self.cfg.tcp_nodelay,
                            keepalive=self.cfg.tcp_keepalive,
                            so_mark=self.cfg.so_mark,
                            prefer=self.cfg.prefer_ipv6),
                DIAL_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            counter.errors += 1
            counter.last_error = "timeout"
            raise StreamOpenError(f"timeout connecting to {host}:{port}", ERR_TIMEOUT) from exc
        except ConnectionRefusedError as exc:
            counter.errors += 1
            counter.last_error = "refused"
            # The single most common misconfiguration: the panel is not
            # listening on 127.0.0.1 at that port.
            raise StreamOpenError(
                f"connection refused by {host}:{port} -- is the panel listening "
                f"there? (check `ss -tlnp`)", ERR_REFUSED
            ) from exc
        except OSError as exc:
            counter.errors += 1
            counter.last_error = str(exc)[:80]
            raise StreamOpenError(f"cannot reach {host}:{port}: {exc}", ERR_UNREACHABLE) from exc

        stream.mux.send(make_frame(T_OPEN_OK, stream.sid))
        log.debug("stream %d -> %s:%d", stream.sid, host, port)

    async def _open_udp(self, stream, host: str, port: int) -> None:
        loop = asyncio.get_running_loop()
        key = f"udp {host}:{port}"
        counter = self.stats.mapping(key)
        flow = UdpExitFlow(stream, counter)

        def factory():
            return flow

        try:
            await asyncio.wait_for(
                loop.create_datagram_endpoint(factory, remote_addr=(host, port)),
                DIAL_TIMEOUT,
            )
        except Exception as exc:
            counter.errors += 1
            counter.last_error = str(exc)[:80]
            raise StreamOpenError(f"udp {host}:{port}: {exc}", ERR_UNREACHABLE) from exc
        stream.mux.send(make_frame(T_OPEN_OK, stream.sid))

    # ------------------------------------------------------------- control
    async def _on_ctrl(self, payload: bytes) -> None:
        try:
            msg = json.loads(payload.decode("utf-8", "replace"))
        except Exception:
            return
        kind = msg.get("kind")
        if kind == "ping":
            for mux in self.tunnels:
                mux.ctrl({"kind": "pong", "t": msg.get("t")})
                break
            return
        if kind == "hello":
            log.info("relay '%s' connected (version %s)",
                     msg.get("name") or "?", msg.get("version") or "?")
            self.exit_info["relay_name"] = msg.get("name") or ""
            self.exit_info["relay_mappings"] = msg.get("mappings") or []
            self.exit_info["relay_uptime"] = msg.get("uptime")
            return
        if kind == "stats":
            self.exit_info["relay_stats"] = msg.get("stats") or {}
            return
        if kind == "mappings_request":
            if self.push_callback:
                mappings = self.push_callback()
                for mux in self.tunnels:
                    mux.ctrl({"kind": "mappings", "mappings": mappings})
                    break
            return

    def push_mappings(self, mappings: list[dict]) -> None:
        """Send a new port list to every connected relay."""
        self.exit_info["mappings"] = mappings
        for mux in self.tunnels:
            mux.ctrl({"kind": "mappings", "mappings": mappings})

    # ------------------------------------------------------------- reporting
    def status(self) -> dict:
        return {
            "role": "exit",
            "name": self.cfg.name or "exit",
            "version": None,
            "listening": [
                {"endpoint": ls.endpoint(), "carrier": ls.carrier,
                 "enabled": ls.enabled}
                for ls in self.cfg.listen
            ],
            "tunnels": [
                {"peer": self.tunnel_peers.get(id(m), ""),
                 "uptime": time.time() - m.started,
                 "streams": len(m.streams)}
                for m in self.tunnels
            ],
            "tunnel_count": len(self.tunnels),
            "connections": len(self.tunnels),
            "reach_out": list(self.reach_out),
            "targets": list(self.reach_out),
            "connected_at": self.connected_at,
            "last_error": self.last_error,
            "speedtest_port": self.speed_port,
            "push_ports": self.cfg.push_ports,
            "stats": self.stats.snapshot(),
        }


def _ip_of(peer: str) -> str:
    if not peer:
        return ""
    if peer.startswith("["):
        return peer[1:].split("]")[0]
    host, _, _port = peer.rpartition(":")
    return host or peer


async def run_exit(cfg: ExitConfig, push_callback=None) -> None:
    node = ExitNode(cfg, push_callback=push_callback)
    await node.start()
    log.info("Simurgh exit node is running")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await node.stop()

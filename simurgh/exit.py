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

from .bridge import Bridge
from .carriers import ServerCarrier, client_connect
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
from .speedtest import SpeedServer
from .stats import Stats
from .udp import UdpExitFlow
from .util import get_logger

log = get_logger("simurgh.exit")

DIAL_TIMEOUT = 12.0
RECONNECT_BACKOFF_MAX = 15.0


class ExitNode:
    def __init__(self, cfg: ExitConfig, push_callback=None):
        self.cfg = cfg
        self.stats = Stats()
        self.servers: list[asyncio.AbstractServer] = []
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
        self.connected_at: float | None = None
        self.last_error = ""
        #: reverse mode (relay dial = "exit"): we connect out instead of listening
        self.reach_out: list[str] = []
        self._dial_tasks: set[asyncio.Task] = set()
        self._stopping = False

    # ------------------------------------------------------------------ start
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        for spec in self.cfg.listen:
            if not spec.enabled:
                continue
            if spec.reverse:
                # reverse mode: the relay listens, we dial it and keep it up
                self.reach_out.append(spec.endpoint())
                task = asyncio.ensure_future(self._dial_supervisor(spec))
                self._dial_tasks.add(task)
                task.add_done_callback(self._dial_tasks.discard)
                log.info("dialling the relay at %s:%s (%s)", spec.dial, spec.port,
                         spec.carrier)
                continue
            carrier = ServerCarrier(
                spec.carrier, self.cfg.token,
                cert_file=self.cfg.cert_file, key_file=self.cfg.key_file,
                path=spec.path, fallback=spec.fallback,
                decoy_file=spec.decoy_file, padding=spec.padding,
            )
            factory = carrier.protocol_factory(self._on_channel)
            try:
                server = await loop.create_server(factory, spec.host, spec.port,
                                                  backlog=512)
            except OSError as exc:
                self.last_error = f"{spec.endpoint()}: {exc}"
                log.error("cannot listen on %s: %s", spec.endpoint(), exc)
                continue
            self.servers.append(server)
            mode = "decoy website" if spec.carrier in ("tls", "wss") else "noise"
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

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._dial_tasks):
            task.cancel()
        for s in self.servers:
            s.close()
        if self._speed_server:
            self._speed_server.close()
        for mux in list(self.tunnels):
            mux.close()
        self.servers.clear()
        self.tunnels.clear()
        self.tunnel_peers.clear()
        self.connected_at = None
        await asyncio.sleep(0)

    async def _dial_supervisor(self, spec) -> None:
        """Keep one outbound tunnel to the relay alive (reverse mode)."""
        backoff = 0.5
        while True:
            try:
                channel = await client_connect(
                    spec.carrier, spec.dial, spec.port, self.cfg.token,
                    domain=spec.dial, path=spec.path,
                    cert_fingerprint=spec.fingerprint,
                    insecure=spec.insecure_skip_verify,
                    padding=spec.padding,
                    connect_timeout=15.0,
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
            chunk=65536,
        )
        self.tunnels.add(mux)
        self.tunnel_peers[id(mux)] = peer
        self.stats.tunnel.active = len(self.tunnels)
        if self.connected_at is None:
            self.connected_at = time.time()
        log.info("tunnel established with %s (carrier: %s)", peer or "?", channel.name)
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
                loop.create_connection(factory, host, port), DIAL_TIMEOUT
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
            "reach_out": list(self.reach_out),
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

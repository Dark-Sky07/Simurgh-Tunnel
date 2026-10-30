"""Direct-TLS reverse TCP/UDP forwarding with fixed targets and bounded sessions."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import ssl
from dataclasses import dataclass, field

from .tunnel import (
    _FIN,
    _UDP_DATA,
    CONNECT_TIMEOUT,
    HANDSHAKE_TIMEOUT,
    MAX_HANDSHAKE,
    MAX_TCP_SESSIONS,
    MAX_UDP_ASSOCIATIONS,
    MAX_UDP_DATAGRAM,
    TLS_HANDSHAKE_TIMEOUT,
    UDP_IDLE_TIMEOUT,
    UDP_QUEUE_LIMIT,
    ReverseAgentConfig,
    ReverseHubConfig,
    _client_ssl,
    _copy_from_tunnel,
    _copy_to_tunnel,
    _enable_tcp_keepalive,
    _join_directions,
    _limited_handler,
    _read_frame,
    _read_json,
    _send_json,
    _server_ssl,
    _stop_when_either_finishes,
    _UdpRateBucket,
    _write_frame,
)

LOG = logging.getLogger("simurgh.rebuild.reverse")
_HEARTBEAT_INTERVAL = 25.0
_HEARTBEAT_TIMEOUT = 75.0
_CHANNEL_OPEN_TIMEOUT = 30.0
_CHANNEL_LENGTH = 48


@dataclass
class _PendingChannel:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    error: str | None = None


@dataclass
class _PendingUdpChannel:
    transport: asyncio.DatagramTransport
    client: tuple
    listener: _ReverseUdpListener
    queue: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue(UDP_QUEUE_LIMIT))
    opened: asyncio.Event = field(default_factory=asyncio.Event)
    completed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _HubState:
    config: ReverseHubConfig
    agent_writer: asyncio.StreamWriter | None = None
    agent_ready: asyncio.Event = field(default_factory=asyncio.Event)
    agent_write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[str, _PendingChannel] = field(default_factory=dict)
    pending_udp: dict[str, _PendingUdpChannel] = field(default_factory=dict)
    public_udp: _ReverseUdpListener | None = None
    handler_tasks: set[asyncio.Task] = field(default_factory=set)


def _decode_line(line: bytes) -> dict:
    if not line or len(line) > MAX_HANDSHAKE or not line.endswith(b"\n"):
        raise ConnectionError("missing or oversized reverse-control message")
    try:
        message = json.loads(line)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ConnectionError("reverse-control message is not valid JSON") from exc
    if not isinstance(message, dict):
        raise ConnectionError("reverse-control message must be a JSON object")
    return message


async def _read_idle_json(reader: asyncio.StreamReader) -> dict:
    try:
        line = await reader.readline()
    except (asyncio.LimitOverrunError, ValueError) as exc:
        raise ConnectionError("reverse-control message exceeded its limit") from exc
    return _decode_line(line)


def _authenticated(message: dict, *, secret: str, route: str) -> bool:
    peer_secret = message.get("secret")
    return (
        type(message.get("version")) is int and message["version"] == 1
        and isinstance(message.get("route"), str) and message["route"] == route
        and isinstance(peer_secret, str)
        and hmac.compare_digest(peer_secret.encode("utf-8"), secret.encode("utf-8"))
    )


async def _hub_control(state: _HubState, reader: asyncio.StreamReader,
                       writer: asyncio.StreamWriter, message: dict) -> None:
    config = state.config
    registered = False
    try:
        if (message.get("op") != "register"
                or message.get("transport", "tcp") != config.transport
                or not _authenticated(message, secret=config.secret, route=config.route)):
            await _send_json(writer, {"ok": False, "error": "denied"})
            return
        if state.agent_writer is not None and not state.agent_writer.is_closing():
            await _send_json(writer, {"ok": False, "error": "route_busy"})
            return
        state.agent_writer = writer
        state.agent_ready.set()
        registered = True
        await _send_json(writer, {"ok": True})
        while True:
            message = await asyncio.wait_for(_read_idle_json(reader), _HEARTBEAT_TIMEOUT)
            operation = message.get("op")
            if operation == "ping":
                await _send_json(writer, {"op": "pong"})
            elif operation == "failed":
                channel = message.get("channel")
                if isinstance(channel, str) and len(channel) == _CHANNEL_LENGTH:
                    pending = state.pending.get(channel)
                    if pending is not None and not pending.completed.is_set():
                        pending.error = "target_unavailable"
                        pending.completed.set()
                    pending_udp = state.pending_udp.get(channel)
                    if pending_udp is not None:
                        pending_udp.opened.set()
                        pending_udp.completed.set()
            else:
                raise ConnectionError("unsupported reverse-control operation")
    except (TimeoutError, ConnectionError, OSError, ssl.SSLError) as exc:
        LOG.debug("reverse agent control session ended: %s", exc)
    finally:
        if registered and state.agent_writer is writer:
            state.agent_writer = None
            state.agent_ready.clear()
            for pending in tuple(state.pending.values()):
                if not pending.completed.is_set():
                    pending.error = "agent_disconnected"
                    pending.completed.set()
            for pending in tuple(state.pending_udp.values()):
                pending.opened.set()
                pending.completed.set()
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def _hub_data(state: _HubState, reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter, message: dict) -> None:
    try:
        channel = message.get("channel")
        config = state.config
        if (message.get("op") != "data" or message.get("transport", "tcp") != "tcp"
                or config.transport != "tcp"
                or not _authenticated(message, secret=config.secret, route=config.route)
                or not isinstance(channel, str) or len(channel) != _CHANNEL_LENGTH):
            await _send_json(writer, {"ok": False, "error": "denied"})
            return
        pending = state.pending.get(channel)
        if pending is None or pending.completed.is_set():
            await _send_json(writer, {"ok": False, "error": "expired"})
            return
        _enable_tcp_keepalive(writer)
        _enable_tcp_keepalive(pending.writer)
        await _send_json(writer, {"ok": True})
        activity = asyncio.Event()
        try:
            await _join_directions(
                _copy_from_tunnel(reader, pending.writer, activity=activity),
                _copy_to_tunnel(pending.reader, writer, activity=activity),
                activity=activity,
            )
        except (TimeoutError, ConnectionError, OSError, ssl.SSLError) as exc:
            LOG.debug("reverse data channel ended: %s", exc)
        finally:
            pending.completed.set()
    except (TimeoutError, ConnectionError, OSError, ssl.SSLError) as exc:
        LOG.debug("invalid reverse data connection: %s", exc)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def _hub_udp_data(state: _HubState, reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter, message: dict) -> None:
    pending = None
    try:
        config = state.config
        channel = message.get("channel")
        if (message.get("op") != "data" or message.get("transport") != "udp"
                or config.transport != "udp"
                or not _authenticated(message, secret=config.secret, route=config.route)
                or not isinstance(channel, str) or len(channel) != _CHANNEL_LENGTH):
            await _send_json(writer, {"ok": False, "error": "denied"})
            return
        pending = state.pending_udp.get(channel)
        if pending is None or pending.completed.is_set():
            await _send_json(writer, {"ok": False, "error": "expired"})
            return
        await _send_json(writer, {"ok": True})
        pending.opened.set()

        async def public_to_agent() -> None:
            while True:
                try:
                    payload = await asyncio.wait_for(pending.queue.get(), UDP_IDLE_TIMEOUT)
                except TimeoutError:
                    await _write_frame(writer, _FIN)
                    return
                await _write_frame(writer, _UDP_DATA, payload)

        async def agent_to_public() -> None:
            while True:
                kind, payload = await _read_frame(reader)
                if kind == _FIN:
                    return
                if kind != _UDP_DATA:
                    raise ConnectionError("TCP frame received on a reverse UDP channel")
                peer = pending.listener.peer_key(pending.client)
                if pending.listener._allow_rate(peer, "download"):
                    pending.transport.sendto(payload, pending.client)

        await _stop_when_either_finishes(public_to_agent(), agent_to_public())
    except (TimeoutError, ConnectionError, OSError, ssl.SSLError) as exc:
        LOG.debug("reverse UDP data channel ended: %s", exc)
    finally:
        if pending is not None:
            pending.opened.set()
            pending.completed.set()
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def _hub_probe(state: _HubState, writer: asyncio.StreamWriter,
                     message: dict) -> None:
    config = state.config
    try:
        if (message.get("transport", "tcp") != config.transport
                or not _authenticated(message, secret=config.secret, route=config.route)):
            await _send_json(writer, {"ok": False, "error": "denied"})
            return
        await _send_json(writer, {"ok": True})
    except (TimeoutError, ConnectionError, OSError, ssl.SSLError) as exc:
        LOG.debug("reverse hub health probe failed: %s", exc)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def _hub_connection(state: _HubState, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
    try:
        first = await _read_json(reader)
    except (TimeoutError, ConnectionError, OSError, ssl.SSLError) as exc:
        LOG.debug("invalid reverse control connection: %s", exc)
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return
    if first.get("op") == "probe":
        await _hub_probe(state, writer, first)
    elif first.get("op") == "register":
        await _hub_control(state, reader, writer, first)
    elif first.get("op") == "data" and first.get("transport") == "udp":
        await _hub_udp_data(state, reader, writer, first)
    elif first.get("op") == "data":
        await _hub_data(state, reader, writer, first)
    else:
        try:
            await _send_json(writer, {"ok": False, "error": "denied"})
        except OSError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def _public_connection(state: _HubState, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
    channel = secrets.token_urlsafe(36)
    pending = _PendingChannel(reader, writer)
    state.pending[channel] = pending
    try:
        try:
            await asyncio.wait_for(state.agent_ready.wait(), timeout=CONNECT_TIMEOUT)
            control = state.agent_writer
            if control is None or control.is_closing():
                raise ConnectionError("reverse agent is not connected")
            async with state.agent_write_lock:
                await _send_json(control, {"op": "open", "channel": channel})
        except (TimeoutError, ConnectionError, OSError) as exc:
            LOG.info("closing public reverse connection; no agent available: %s", exc)
            return
        try:
            await asyncio.wait_for(pending.completed.wait(), timeout=_CHANNEL_OPEN_TIMEOUT)
        except TimeoutError:
            LOG.info("reverse agent did not open channel before timeout")
            return
        if pending.error:
            LOG.info("reverse agent could not open the configured target: %s", pending.error)
    finally:
        state.pending.pop(channel, None)
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


class _HubUdpSession:
    def __init__(self, owner: _ReverseUdpListener, client: tuple):
        self.owner = owner
        self.client = client
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(UDP_QUEUE_LIMIT)
        self.task = asyncio.create_task(self.run())

    def enqueue(self, payload: bytes) -> None:
        if self.queue.full():
            LOG.debug("dropping reverse UDP datagram from %r: queue full", self.client)
            return
        self.queue.put_nowait(payload)

    async def run(self) -> None:
        channel = secrets.token_urlsafe(36)
        pending = _PendingUdpChannel(self.owner.transport, self.client, self.owner, self.queue)
        state = self.owner.state
        state.pending_udp[channel] = pending
        try:
            await asyncio.wait_for(state.agent_ready.wait(), CONNECT_TIMEOUT)
            control = state.agent_writer
            if control is None or control.is_closing():
                raise ConnectionError("reverse agent is not connected")
            async with state.agent_write_lock:
                await _send_json(control, {
                    "op": "open", "channel": channel, "transport": "udp",
                })
            await asyncio.wait_for(pending.opened.wait(), _CHANNEL_OPEN_TIMEOUT)
            await pending.completed.wait()
        except (TimeoutError, ConnectionError, OSError) as exc:
            LOG.debug("reverse UDP channel to %r ended: %s", self.client, exc)
        finally:
            state.pending_udp.pop(channel, None)
            self.owner.remove_session(self.client, self)


class _ReverseUdpListener(asyncio.DatagramProtocol):
    def __init__(self, state: _HubState, per_peer_limit: int):
        self.state = state
        self.per_peer_limit = per_peer_limit
        self.transport: asyncio.DatagramTransport | None = None
        self.sessions: dict[tuple, _HubUdpSession] = {}
        self.peer_sessions: dict[str, int] = {}
        self.rate_buckets: dict[tuple[str, str], _UdpRateBucket] = {}

    def _allow_rate(self, peer: str, direction: str) -> bool:
        key = (peer, direction)
        bucket = self.rate_buckets.get(key)
        if bucket is None:
            bucket = _UdpRateBucket(self.state.config.udp_rate_limit_pps)
            self.rate_buckets[key] = bucket
        return bucket.allow()

    @staticmethod
    def peer_key(addr) -> str:
        return str(addr[0]) if isinstance(addr, tuple) and addr else str(addr)

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if not data or len(data) > MAX_UDP_DATAGRAM:
            return
        session = self.sessions.get(addr)
        peer = self.peer_key(addr)
        if session is None:
            if len(self.sessions) >= MAX_UDP_ASSOCIATIONS:
                LOG.warning("dropping reverse UDP client %r: association limit reached", addr)
                return
            if self.peer_sessions.get(peer, 0) >= self.per_peer_limit:
                LOG.debug("dropping reverse UDP client %r: per-address session limit reached", addr)
                return
            if not self._allow_rate(peer, "upload"):
                return
            session = _HubUdpSession(self, addr)
            self.sessions[addr] = session
            self.peer_sessions[peer] = self.peer_sessions.get(peer, 0) + 1
        elif not self._allow_rate(peer, "upload"):
            return
        session.enqueue(data)

    def remove_session(self, addr, session: _HubUdpSession) -> None:
        if self.sessions.get(addr) is not session:
            return
        del self.sessions[addr]
        peer = self.peer_key(addr)
        remaining = self.peer_sessions[peer] - 1
        if remaining:
            self.peer_sessions[peer] = remaining
        else:
            del self.peer_sessions[peer]
            self.rate_buckets.pop((peer, "upload"), None)
            self.rate_buckets.pop((peer, "download"), None)

    def error_received(self, exc: OSError) -> None:
        LOG.warning("reverse UDP public listener error: %s", exc)

    async def close(self) -> None:
        sessions = tuple(self.sessions.values())
        for session in sessions:
            session.task.cancel()
        if sessions:
            await asyncio.gather(*(session.task for session in sessions),
                                 return_exceptions=True)
        self.sessions.clear()
        self.peer_sessions.clear()
        self.rate_buckets.clear()


async def _agent_channel(config: ReverseAgentConfig, channel: str,
                         control_writer: asyncio.StreamWriter,
                         control_lock: asyncio.Lock) -> None:
    target_writer = None
    data_writer = None
    try:
        try:
            target_reader, target_writer = await asyncio.wait_for(
                asyncio.open_connection(config.target_host, config.target_port,
                                        limit=MAX_HANDSHAKE), timeout=CONNECT_TIMEOUT)
        except (TimeoutError, OSError):
            async with control_lock:
                await _send_json(control_writer, {"op": "failed", "channel": channel})
            return
        data_reader, data_writer = await asyncio.wait_for(
            asyncio.open_connection(
                config.hub_host,
                config.hub_port,
                ssl=_client_ssl(config),
                server_hostname=config.server_name,
                limit=MAX_HANDSHAKE,
            ),
            timeout=CONNECT_TIMEOUT,
        )
        await _send_json(data_writer, {
            "version": 1, "op": "data", "route": config.route,
            "secret": config.secret, "channel": channel,
        })
        response = await asyncio.wait_for(_read_json(data_reader), timeout=HANDSHAKE_TIMEOUT)
        if response.get("ok") is not True:
            raise ConnectionError("reverse hub rejected the data channel")
        _enable_tcp_keepalive(target_writer)
        _enable_tcp_keepalive(data_writer)
        activity = asyncio.Event()
        await _join_directions(
            _copy_from_tunnel(data_reader, target_writer, activity=activity),
            _copy_to_tunnel(target_reader, data_writer, activity=activity),
            activity=activity,
        )
    except (TimeoutError, ConnectionError, OSError, ssl.SSLError) as exc:
        LOG.debug("reverse data channel %s ended: %s", channel, exc)
    finally:
        for stream_writer in (target_writer, data_writer):
            if stream_writer is not None:
                stream_writer.close()
                try:
                    await stream_writer.wait_closed()
                except OSError:
                    pass


class _AgentUdpProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(UDP_QUEUE_LIMIT)

    def datagram_received(self, data: bytes, addr) -> None:
        if not data or self.queue.full():
            return
        self.queue.put_nowait(data)


async def _agent_udp_channel(config: ReverseAgentConfig, channel: str,
                             control_writer: asyncio.StreamWriter,
                             control_lock: asyncio.Lock) -> None:
    udp_transport = None
    data_writer = None
    established = False
    protocol = _AgentUdpProtocol()
    try:
        udp_transport, _ = await asyncio.wait_for(
            asyncio.get_running_loop().create_datagram_endpoint(
                lambda: protocol, remote_addr=(config.target_host, config.target_port)),
            timeout=CONNECT_TIMEOUT,
        )
        data_reader, data_writer = await asyncio.wait_for(
            asyncio.open_connection(
                config.hub_host, config.hub_port, ssl=_client_ssl(config),
                server_hostname=config.server_name, limit=MAX_HANDSHAKE,
            ),
            timeout=CONNECT_TIMEOUT,
        )
        await _send_json(data_writer, {
            "version": 1, "op": "data", "transport": "udp",
            "route": config.route, "secret": config.secret, "channel": channel,
        })
        response = await asyncio.wait_for(_read_json(data_reader), timeout=HANDSHAKE_TIMEOUT)
        if response.get("ok") is not True:
            raise ConnectionError("reverse hub rejected the UDP data channel")
        established = True

        async def tunnel_to_udp() -> None:
            while True:
                kind, payload = await _read_frame(data_reader)
                if kind == _FIN:
                    return
                if kind != _UDP_DATA:
                    raise ConnectionError("TCP frame received on a reverse UDP agent channel")
                udp_transport.sendto(payload)

        async def udp_to_tunnel() -> None:
            while True:
                payload = await protocol.queue.get()
                await _write_frame(data_writer, _UDP_DATA, payload)

        await _stop_when_either_finishes(tunnel_to_udp(), udp_to_tunnel())
    except (TimeoutError, ConnectionError, OSError, ssl.SSLError) as exc:
        if not established:
            try:
                async with control_lock:
                    await _send_json(control_writer, {"op": "failed", "channel": channel})
            except (ConnectionError, OSError):
                pass
        LOG.debug("reverse UDP channel %s ended: %s", channel, exc)
    finally:
        if udp_transport is not None:
            udp_transport.close()
        if data_writer is not None:
            data_writer.close()
            try:
                await data_writer.wait_closed()
            except OSError:
                pass


async def _agent_heartbeat(writer: asyncio.StreamWriter,
                           lock: asyncio.Lock) -> None:
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL)
        async with lock:
            await _send_json(writer, {"op": "ping"})


async def _agent_session(config: ReverseAgentConfig) -> None:
    control_reader, control_writer = await asyncio.wait_for(
        asyncio.open_connection(
            config.hub_host,
            config.hub_port,
            ssl=_client_ssl(config),
            server_hostname=config.server_name,
            limit=MAX_HANDSHAKE,
        ),
        timeout=CONNECT_TIMEOUT,
    )
    lock = asyncio.Lock()
    tasks: set[asyncio.Task] = set()
    heartbeat = None
    try:
        await _send_json(control_writer, {
            "version": 1, "op": "register", "route": config.route,
            "secret": config.secret, "transport": config.transport,
        })
        response = await asyncio.wait_for(_read_json(control_reader), timeout=HANDSHAKE_TIMEOUT)
        if response.get("ok") is not True:
            raise ConnectionError(f"reverse hub rejected registration ({response.get('error', 'denied')})")
        heartbeat = asyncio.create_task(_agent_heartbeat(control_writer, lock))
        while True:
            message = await asyncio.wait_for(_read_idle_json(control_reader), _HEARTBEAT_TIMEOUT)
            operation = message.get("op")
            if operation == "open":
                channel = message.get("channel")
                transport = message.get("transport", "tcp")
                if (not isinstance(channel, str) or len(channel) != _CHANNEL_LENGTH
                        or transport != config.transport):
                    raise ConnectionError("reverse hub sent an invalid channel request")
                handler = _agent_udp_channel if transport == "udp" else _agent_channel
                task = asyncio.create_task(handler(config, channel, control_writer, lock))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            elif operation == "pong":
                continue
            else:
                raise ConnectionError("reverse hub sent an unsupported control operation")
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        control_writer.close()
        try:
            await control_writer.wait_closed()
        except OSError:
            pass
        for task in tuple(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def serve_reverse_agent(config: ReverseAgentConfig) -> None:
    """Maintain an outbound, authenticated control channel from behind NAT."""
    delay = 1.0
    while True:
        try:
            await _agent_session(config)
            delay = 1.0
        except asyncio.CancelledError:
            raise
        except (TimeoutError, ConnectionError, OSError, ssl.SSLError) as exc:
            LOG.warning("reverse hub connection failed: %s; retrying in %.0f s", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


def _tracked_handler(state: _HubState, handler):
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            state.handler_tasks.add(task)
        try:
            await handler(reader, writer)
        finally:
            if task is not None:
                state.handler_tasks.discard(task)
    return serve


async def _start_reverse_hub(config: ReverseHubConfig):
    state = _HubState(config)
    control_active = [0]
    public_active = [0]
    control = await asyncio.start_server(
        _limited_handler(_tracked_handler(state,
                                          lambda reader, writer: _hub_connection(state, reader, writer)),
                         control_active, MAX_TCP_SESSIONS),
        config.control_host, config.control_port,
        ssl=_server_ssl(config),
        ssl_handshake_timeout=TLS_HANDSHAKE_TIMEOUT,
        limit=MAX_HANDSHAKE,
    )
    try:
        if config.transport == "udp":
            public, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: _ReverseUdpListener(state, config.client_session_limit),
                local_addr=(config.public_host, config.public_port),
            )
            state.public_udp = protocol
        else:
            public = await asyncio.start_server(
                _limited_handler(_tracked_handler(
                    state, lambda reader, writer: _public_connection(state, reader, writer)),
                    public_active, MAX_TCP_SESSIONS,
                    per_peer_limit=config.client_session_limit),
                config.public_host, config.public_port, limit=MAX_HANDSHAKE,
            )
    except BaseException:
        control.close()
        await control.wait_closed()
        raise
    return control, public, state


async def serve_reverse_hub(config: ReverseHubConfig) -> None:
    """Accept one authenticated reverse agent and expose its fixed TCP or UDP target."""
    control = public = None
    state = None
    try:
        control, public, state = await _start_reverse_hub(config)
        control_addresses = ", ".join(str(sock.getsockname()) for sock in control.sockets or ())
        if config.transport == "udp":
            public_addresses = str(public.get_extra_info("sockname"))
            LOG.info("reverse hub control listening on %s; public UDP listener on %s route=%s",
                     control_addresses, public_addresses, config.route)
            await control.serve_forever()
        else:
            public_addresses = ", ".join(str(sock.getsockname()) for sock in public.sockets or ())
            LOG.info("reverse hub control listening on %s; public TCP listener on %s route=%s",
                     control_addresses, public_addresses, config.route)
            await asyncio.gather(control.serve_forever(), public.serve_forever())
    finally:
        if control is not None:
            control.close()
            await control.wait_closed()
        if isinstance(public, asyncio.Server):
            public.close()
            await public.wait_closed()
        elif public is not None:
            public.close()
        if state is not None:
            if state.public_udp is not None:
                await state.public_udp.close()
            handlers = tuple(state.handler_tasks)
            for task in handlers:
                task.cancel()
            if handlers:
                await asyncio.gather(*handlers, return_exceptions=True)

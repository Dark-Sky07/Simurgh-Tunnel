"""Direct-TLS reverse TCP forwarding: outbound agents, fixed targets, bounded sessions."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import ssl
from dataclasses import dataclass, field

from .tunnel import (
    CONNECT_TIMEOUT,
    HANDSHAKE_TIMEOUT,
    MAX_HANDSHAKE,
    MAX_TCP_SESSIONS,
    TLS_HANDSHAKE_TIMEOUT,
    ReverseAgentConfig,
    ReverseHubConfig,
    _client_ssl,
    _copy_from_tunnel,
    _copy_to_tunnel,
    _join_directions,
    _limited_handler,
    _read_json,
    _send_json,
    _server_ssl,
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
class _HubState:
    config: ReverseHubConfig
    agent_writer: asyncio.StreamWriter | None = None
    agent_ready: asyncio.Event = field(default_factory=asyncio.Event)
    agent_write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[str, _PendingChannel] = field(default_factory=dict)
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
        if message.get("op") != "register" or not _authenticated(
                message, secret=config.secret, route=config.route):
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
        if (message.get("op") != "data"
                or not _authenticated(message, secret=config.secret, route=config.route)
                or not isinstance(channel, str) or len(channel) != _CHANNEL_LENGTH):
            await _send_json(writer, {"ok": False, "error": "denied"})
            return
        pending = state.pending.get(channel)
        if pending is None or pending.completed.is_set():
            await _send_json(writer, {"ok": False, "error": "expired"})
            return
        await _send_json(writer, {"ok": True})
        try:
            await _join_directions(
                _copy_from_tunnel(reader, pending.writer),
                _copy_to_tunnel(pending.reader, writer),
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
    if first.get("op") == "register":
        await _hub_control(state, reader, writer, first)
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
        await _join_directions(
            _copy_from_tunnel(data_reader, target_writer),
            _copy_to_tunnel(target_reader, data_writer),
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
            "secret": config.secret,
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
                if not isinstance(channel, str) or len(channel) != _CHANNEL_LENGTH:
                    raise ConnectionError("reverse hub sent an invalid channel id")
                task = asyncio.create_task(_agent_channel(config, channel, control_writer, lock))
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
    """Accept one authenticated reverse agent and expose its fixed TCP target."""
    control = public = None
    state = None
    try:
        control, public, state = await _start_reverse_hub(config)
        control_addresses = ", ".join(str(sock.getsockname()) for sock in control.sockets or ())
        public_addresses = ", ".join(str(sock.getsockname()) for sock in public.sockets or ())
        LOG.info("reverse hub control listening on %s; public TCP listener on %s route=%s",
                 control_addresses, public_addresses, config.route)
        await asyncio.gather(control.serve_forever(), public.serve_forever())
    finally:
        for server in (control, public):
            if server is not None:
                server.close()
                await server.wait_closed()
        if state is not None:
            handlers = tuple(state.handler_tasks)
            for task in handlers:
                task.cancel()
            if handlers:
                await asyncio.gather(*handlers, return_exceptions=True)

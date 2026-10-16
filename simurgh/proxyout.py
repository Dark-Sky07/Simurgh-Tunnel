"""Dialing the far side *through* an upstream proxy (SOCKS5 or HTTP CONNECT).

Why an operator wants this:

* the exit sits behind a NAT/provider that only lets traffic out through a
  local proxy;
* the tunnel itself must leave the box along a particular route (a proxy on
  ``127.0.0.1`` fed by a tunnel, a WireGuard interface, a policy-routed path);
* "tunnel over tunnel" chains, which are common on the Iran side.

``proxy = "socks5://user:pass@127.0.0.1:1080"`` in ``[exit]`` (direct mode) or
in ``[[listen]]`` (reverse mode) is all it takes.  ``socks5h`` resolves the
name *at the proxy*, ``socks5`` resolves it here; ``http://`` speaks HTTP
CONNECT.  The result is an ordinary :class:`~simurgh.carriers.RawConn`, so
every carrier (tls, wss, raw, plain) sits on top of it unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import struct
from urllib.parse import unquote, urlsplit

from .util import get_logger

log = get_logger("simurgh.proxy")

DEFAULT_PORTS = {"socks5": 1080, "socks5h": 1080, "http": 3128, "https": 3128}


class ProxyError(ConnectionError):
    pass


def parse_proxy(url: str) -> dict:
    """``scheme://user:pass@host:port`` -> a dict we can dial with."""
    text = str(url or "").strip()
    if not text:
        raise ProxyError("empty proxy url")
    if "://" not in text:
        text = "socks5://" + text
    parts = urlsplit(text)
    scheme = (parts.scheme or "socks5").lower()
    if scheme not in DEFAULT_PORTS:
        raise ProxyError(f"unsupported proxy scheme {scheme!r} "
                         "(use socks5, socks5h or http)")
    host = parts.hostname
    if not host:
        raise ProxyError(f"proxy url without a host: {url!r}")
    return {
        "scheme": scheme,
        "host": host,
        "port": parts.port or DEFAULT_PORTS[scheme],
        "user": unquote(parts.username) if parts.username else "",
        "password": unquote(parts.password) if parts.password else "",
        "url": text,
    }


def _describe(info: dict) -> str:
    auth = "***@" if info["user"] else ""
    return f"{info['scheme']}://{auth}{info['host']}:{info['port']}"


async def open_through_proxy(url: str, host: str, port: int,
                             timeout: float = 12.0):
    """Connect to ``host:port`` through the proxy, ready for a carrier."""
    from .carriers import tcp_connect            # local import: no cycle

    info = parse_proxy(url)
    conn = await tcp_connect(info["host"], info["port"], timeout)
    try:
        if info["scheme"] in ("socks5", "socks5h"):
            await asyncio.wait_for(
                _socks5_connect(conn, info, host, port,
                                remote_dns=info["scheme"] == "socks5h"),
                timeout,
            )
        else:
            await asyncio.wait_for(_http_connect(conn, info, host, port), timeout)
    except Exception:
        conn.close()
        raise
    log.debug("tunnel dialed through %s -> %s:%s", _describe(info), host, port)
    return conn


async def _socks5_connect(conn, info: dict, host: str, port: int,
                          remote_dns: bool = False) -> None:
    want_auth = bool(info["user"] or info["password"])
    methods = b"\x00\x02" if want_auth else b"\x00"
    conn.write(b"\x05" + bytes([len(methods)]) + methods)
    await conn.drain()
    head = await conn.read_exactly(2)
    if head[0] != 5:
        raise ProxyError(f"proxy is not SOCKS5 (version {head[0]})")
    method = head[1]
    if method == 0x02:
        user = info["user"].encode()[:255]
        pw = info["password"].encode()[:255]
        conn.write(b"\x01" + bytes([len(user)]) + user + bytes([len(pw)]) + pw)
        await conn.drain()
        reply = await conn.read_exactly(2)
        if reply[1] != 0:
            raise ProxyError("SOCKS5 authentication rejected")
    elif method == 0xFF:
        raise ProxyError("proxy refused every authentication method")
    elif method != 0x00:
        raise ProxyError(f"proxy chose unsupported auth method {method}")

    if remote_dns and not _is_ip(host):
        raw = host.encode("idna") if host else b""
        if len(raw) > 255:
            raise ProxyError("host name too long for SOCKS5")
        addr = b"\x03" + bytes([len(raw)]) + raw
    else:
        addr = _socks5_addr(host)
    try:
        dest = addr + struct.pack(">H", port)
    except struct.error as exc:  # pragma: no cover
        raise ProxyError(f"bad target port {port}") from exc
    conn.write(b"\x05\x01\x00" + dest)
    await conn.drain()

    reply = await conn.read_exactly(4)
    if reply[0] != 5:
        raise ProxyError("malformed SOCKS5 reply")
    code = reply[1]
    if code != 0:
        raise ProxyError(f"SOCKS5 connect failed: {_socks5_error(code)}")
    atyp = reply[3]
    if atyp == 0x01:
        await conn.read_exactly(4 + 2)
    elif atyp == 0x04:
        await conn.read_exactly(16 + 2)
    elif atyp == 0x03:
        length = (await conn.read_exactly(1))[0]
        await conn.read_exactly(length + 2)
    else:  # pragma: no cover - defensive
        raise ProxyError(f"SOCKS5 reply with unknown address type {atyp}")


def _socks5_addr(host: str) -> bytes:
    import socket

    try:
        return b"\x01" + socket.inet_pton(socket.AF_INET, host)
    except OSError:
        pass
    try:
        return b"\x04" + socket.inet_pton(socket.AF_INET6, host)
    except OSError:
        pass
    raise ProxyError(f"SOCKS5 needs an address or socks5h for {host!r}")


def _is_ip(host: str) -> bool:
    from .util import is_ip

    return is_ip(host)


_SOCKS_ERRORS = {
    1: "general failure",
    2: "connection not allowed by the ruleset",
    3: "network unreachable",
    4: "host unreachable",
    5: "connection refused",
    6: "TTL expired",
    7: "command not supported",
    8: "address type not supported",
}


def _socks5_error(code: int) -> str:
    return _SOCKS_ERRORS.get(code, f"error {code}")


async def _http_connect(conn, info: dict, host: str, port: int) -> None:
    target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    head = (f"CONNECT {target} HTTP/1.1\r\n"
            f"Host: {target}\r\n"
            f"Proxy-Connection: Keep-Alive\r\n")
    if info["user"] or info["password"]:
        raw = f"{info['user']}:{info['password']}".encode()
        head += "Proxy-Authorization: Basic " + base64.b64encode(raw).decode() + "\r\n"
    conn.write(head.encode("latin-1") + b"\r\n")
    await conn.drain()

    data = bytearray()
    while b"\r\n\r\n" not in data:
        piece = await conn.read_exactly(1)
        data += piece
        if len(data) > 32768:
            raise ProxyError("proxy sent an oversized reply")
    line = bytes(data).split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = line.split()
    if len(parts) < 2 or not parts[1].startswith("2"):
        raise ProxyError(f"HTTP proxy refused the tunnel: {line.strip()}")

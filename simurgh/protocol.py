"""Wire protocol of the Simurgh data channel.

A *frame* is the unit the multiplexer exchanges:

    +--------+--------+--------+----- ... -----+
    | type   |  sid   |        payload         |
    | 1 byte | 4 byte |   (type specific)      |
    +--------+--------+--------+----- ... -----+

Frames travel inside a *channel* supplied by a carrier (see ``carriers.py``).
The carrier is responsible for delimiting frames on the wire (length prefix,
AEAD envelope, WebSocket message, ...), so the multiplexer only ever deals with
whole frames -- which keeps the hot path free of parsing.

Frame types
-----------
``OPEN``      + address  -> please dial this target (mode byte first: 0 TCP / 1 UDP)
``OPEN_OK``   + ""       -> connected, start sending
``OPEN_ERR``  + code     -> could not connect
``DATA``      + bytes    -> stream payload (one frame == one chunk)
``EOF``       + ""       -> I will not send more data on this stream (half close)
``CLOSE``     + ""       -> stream is gone (both directions)
``WIN``       + 8 bytes  -> flow-control credit: 4B per stream + 4B per connection
``PING``      + token    -> keepalive
``PONG``      + token    -> keepalive answer
``CTRL``      + JSON     -> control plane (config push, traffic reports, ...)
"""

from __future__ import annotations

import struct

HEADER = struct.Struct(">BI")
HEADER_LEN = HEADER.size  # 5
_SID = struct.Struct(">I")              # sid lives at offset 1 of every frame

T_OPEN = 1
T_OPEN_OK = 2
T_OPEN_ERR = 3
T_DATA = 4
T_EOF = 5
T_CLOSE = 6
T_WIN = 7
T_PING = 8
T_PONG = 9
T_CTRL = 10

MODE_TCP = 0
MODE_UDP = 1

WIN = struct.Struct(">II")

#: how much a peer may send before it has to wait for credit
DEFAULT_STREAM_WINDOW = 256 * 1024
#: ceiling for the adaptive window: a stream that keeps draining fast is
#: allowed this much in flight, so one user is not capped at window/RTT
DEFAULT_MAX_STREAM_WINDOW = 16 * 1024 * 1024
#: safety net across all streams of one tunnel connection
DEFAULT_GLOBAL_WINDOW = 16 * 1024 * 1024
#: biggest payload chunk the multiplexer puts in a single DATA frame
DEFAULT_CHUNK = 64 * 1024

#: error codes carried by OPEN_ERR
ERR_REFUSED = 1
ERR_TIMEOUT = 2
ERR_UNREACHABLE = 3
ERR_DENIED = 4
ERR_BAD_REQUEST = 5

ERROR_TEXT = {
    ERR_REFUSED: "connection refused",
    ERR_TIMEOUT: "connection timed out",
    ERR_UNREACHABLE: "host unreachable",
    ERR_DENIED: "denied by policy",
    ERR_BAD_REQUEST: "bad request",
}


def make_frame(ftype: int, sid: int = 0, payload: bytes = b"") -> bytes:
    return HEADER.pack(ftype, sid) + payload


def frame_type(frame: bytes) -> int:
    return frame[0]


def frame_sid(frame: bytes) -> int:
    return _SID.unpack_from(frame, 1)[0]


def frame_payload(frame: bytes) -> bytes:
    return frame[HEADER_LEN:]


def err_frame(sid: int, code: int, text: str = "") -> bytes:
    msg = text or ERROR_TEXT.get(code, "error")
    return make_frame(T_OPEN_ERR, sid, struct.pack(">B", code) + msg.encode("utf-8", "replace"))


# --------------------------------------------------------------------------
# addresses (SOCKS style, reused by the multiplexer)
# --------------------------------------------------------------------------

ATYP_V4 = 1
ATYP_DOMAIN = 3
ATYP_V6 = 4


def encode_addr(host: str, port: int) -> bytes:
    """``ATYP + ADDR + PORT`` -- what the relay sends inside OPEN."""
    import ipaddress

    if not (0 < port < 65536):
        raise ValueError(f"bad port: {port}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raw = host.encode("utf-8")
        if not (0 < len(raw) < 256):
            raise ValueError(f"bad host: {host!r}")
        return bytes((ATYP_DOMAIN, len(raw))) + raw + struct.pack(">H", port)
    if ip.version == 4:
        return bytes((ATYP_V4,)) + ip.packed + struct.pack(">H", port)
    return bytes((ATYP_V6,)) + ip.packed + struct.pack(">H", port)


def decode_addr(data: bytes) -> tuple[str, int, bytes]:
    """Decode an address from the front of *data*; returns (host, port, rest)."""
    import ipaddress

    if len(data) < 2:
        raise ValueError("address too short")
    atyp = data[0]
    if atyp == ATYP_V4:
        if len(data) < 7:
            raise ValueError("short ipv4 address")
        host = str(ipaddress.ip_address(data[1:5]))
        offset = 5
    elif atyp == ATYP_DOMAIN:
        n = data[1]
        if len(data) < 4 + n:
            raise ValueError("short domain address")
        host = data[2:2 + n].decode("utf-8", "replace")
        offset = 2 + n
    elif atyp == ATYP_V6:
        if len(data) < 19:
            raise ValueError("short ipv6 address")
        host = str(ipaddress.ip_address(data[1:17]))
        offset = 17
    else:
        raise ValueError(f"unknown atyp: {atyp}")
    port = struct.unpack(">H", data[offset:offset + 2])[0]
    return host, port, data[offset + 2:]

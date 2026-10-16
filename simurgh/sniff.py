"""Peeking at the first bytes of a connection: is it TLS, and for which name?

Used by the relay's *shared* ports (port reuse): several customers can share
one public port as long as each one names a different TLS server name.  The
first ClientHello decides where the connection goes; anything that is not a
ClientHello goes to the default mapping or to the decoy site.
"""

from __future__ import annotations

import struct

TLS_HANDSHAKE = 0x16
CLIENT_HELLO = 0x01
#: enough for a normal ClientHello with SNI (chrome sends ~500 bytes)
PEEK_BYTES = 2048
#: how long we wait for those bytes before deciding (milliseconds matter here:
#: a real browser sends the ClientHello immediately)
PEEK_TIMEOUT = 5.0


def _decode_name(raw: bytes) -> str | None:
    """A server name from the wire: IDNA if we can, else best effort."""
    for decoder in ("idna", "ascii", "latin-1"):
        try:
            text = raw.decode(decoder)
        except (UnicodeError, LookupError):
            continue
        text = text.strip().rstrip(".").lower()
        if text and all(ch.isprintable() for ch in text):
            return text
    return None


def looks_like_tls(data: bytes) -> bool:
    """A TLS *handshake* record (not just any 0x16 byte)."""
    if len(data) < 5:
        return False
    if data[0] != TLS_HANDSHAKE:
        return False
    major, minor = data[1], data[2]
    if major != 3 or minor > 4:
        return False
    length = int.from_bytes(data[3:5], "big")
    return 0 < length <= 16384 + 2048


def looks_like_http(data: bytes) -> bool:
    head = bytes(data[:8]).upper()
    return head.startswith((b"GET ", b"POST ", b"HEAD ", b"PUT ", b"OPTION",
                            b"DELETE ", b"PATCH ", b"CONNECT ", b"PRI * HT"))


def sni_from_clienthello(data: bytes) -> str | None:
    """The SNI of a TLS ClientHello, or ``None`` when there is not one.

    Deliberately forgiving: we only need the name, not a full parse, and the
    bytes are attacker-controlled.
    """
    try:
        if len(data) < 6 or data[0] != TLS_HANDSHAKE or data[5] != CLIENT_HELLO:
            return None
        # record -> handshake
        pos = 5 + 4                       # handshake header (type+len)
        pos += 2 + 32                     # legacy version + random
        if pos >= len(data):
            return None
        session_len = data[pos]
        pos += 1 + session_len
        if pos + 2 > len(data):
            return None
        cipher_len = struct.unpack_from(">H", data, pos)[0]
        pos += 2 + cipher_len
        if pos >= len(data):
            return None
        comp_len = data[pos]
        pos += 1 + comp_len
        if pos + 2 > len(data):
            return None
        ext_total = struct.unpack_from(">H", data, pos)[0]
        pos += 2
        end = min(len(data), pos + ext_total)
        while pos + 4 <= end:
            ext_type, ext_len = struct.unpack_from(">HH", data, pos)
            pos += 4
            if ext_type == 0x0000:        # server_name
                if pos + 5 > end:
                    return None
                list_len = struct.unpack_from(">H", data, pos)[0]
                name_type = data[pos + 2]
                name_len = struct.unpack_from(">H", data, pos + 3)[0]
                if name_type != 0 or name_len == 0:
                    return None
                raw = data[pos + 5:pos + 5 + name_len]
                del list_len
                return _decode_name(raw)
            pos += ext_len
        return None
    except (IndexError, struct.error, UnicodeError, ValueError):
        return None


def http_host(data: bytes) -> str | None:
    """The ``Host:`` of a plain HTTP request (for the decoy site's routing)."""
    try:
        head = bytes(data[:4096]).decode("latin-1", "replace")
    except Exception:  # pragma: no cover - defensive
        return None
    for line in head.split("\r\n")[1:]:
        if not line:
            break
        name, _, value = line.partition(":")
        if name.strip().lower() == "host":
            return value.strip().split(":")[0].lower() or None
    return None


def match_name(name: str | None, wanted: list[str]) -> str | None:
    """Which of *wanted* matches *name* -- exact, or a wildcard ``*.example.com``."""
    if not name:
        return None
    low = name.lower()
    for candidate in wanted:
        if not candidate:
            continue
        pattern = candidate.lower()
        if pattern == low:
            return candidate
        if pattern.startswith("*."):
            suffix = pattern[1:]                      # ".example.com"
            head, _, tail = low.partition(".")
            if tail and "." + tail == suffix and head:
                return candidate
    return None

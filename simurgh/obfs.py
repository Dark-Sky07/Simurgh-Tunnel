"""The ``raw`` carrier: no TLS, no certificate, no recognisable protocol.

On the wire the first bytes are random (a random-length noise prefix), then an
X25519 public key, a timestamp and an HMAC tag -- after that, ChaCha20-Poly1305
frames.  To a filter this is "encrypted something"; to a prober it is noise.
There is no banner, no response to a wrong password, nothing to fingerprint
except "somebody is speaking an unknown protocol here".

Cheap on the CPU (no TLS machinery, no certificate chain) and works on ports
where TLS would look out of place.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import struct
import time

from .carriers import BaseChannel, ByteFrameChannel, RawConn
from .util import get_logger

log = get_logger("simurgh.raw")

MAGIC = b"smr2"
PREFIX_MAX = 96
HELLO_TIMEOUT = 15.0


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = 64) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def _x25519() -> tuple:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    key = X25519PrivateKey.generate()
    from cryptography.hazmat.primitives import serialization

    pub = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return key, pub


def _exchange(priv, peer_pub: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

    return priv.exchange(X25519PublicKey.from_public_bytes(peer_pub))


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------


async def raw_client(host: str, port: int, token: str, timeout: float,
                     padding: bool = True) -> BaseChannel:
    from .carriers import auth_key, tcp_connect

    conn = await tcp_connect(host, port, timeout)
    key = auth_key(token)
    priv, pub = _x25519()
    ts = int(time.time())
    tag = hmac.new(key, b"simurgh/v2/raw" + struct.pack(">Q", ts) + pub,
                   hashlib.sha256).digest()[:16]
    plen = os.urandom(1)[0] % (PREFIX_MAX + 1)
    hello = bytes((plen,)) + os.urandom(plen) + pub + struct.pack(">Q", ts) + tag
    conn.write(hello)
    await conn.drain()

    reply = await asyncio.wait_for(conn.read_exactly(48), timeout)
    spub, stag = reply[:32], reply[32:48]
    expect = hmac.new(key, b"simurgh/v2/raw-s" + struct.pack(">Q", ts) + pub + spub,
                      hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(stag, expect):
        conn.close()
        raise ConnectionError("raw handshake failed (wrong token?)")
    shared = _exchange(priv, spub)
    okm = _hkdf(shared, salt=struct.pack(">Q", ts) + pub + spub, info=b"simurgh/v2/keys")
    from .carriers import AeadFrameChannel

    inner = ByteFrameChannel(conn, peer=conn.peer, name="raw")
    return AeadFrameChannel(inner, okm[:32], okm[32:], pad=padding)


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------


async def raw_server(conn: RawConn, carrier, on_channel) -> None:
    from .carriers import AeadFrameChannel

    key = __import__("simurgh.carriers", fromlist=["auth_key"]).auth_key(carrier.token)

    # random-length noise prefix, then 32 byte public key, 8 byte timestamp,
    # 16 byte tag -- mirrored by the client.
    try:
        (plen,) = await asyncio.wait_for(conn.read_exactly(1), HELLO_TIMEOUT)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        conn.close()
        return
    if plen > PREFIX_MAX:
        # Not our protocol: answer with noise and hang up.
        conn.write(os.urandom(64))
        await _safe_drain(conn)
        conn.close()
        return
    try:
        rest = await asyncio.wait_for(conn.read_exactly(plen + 56), HELLO_TIMEOUT)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        conn.close()
        return
    cpub = rest[plen:plen + 32]
    ts_b = rest[plen + 32:plen + 40]
    tag = rest[plen + 40:plen + 56]
    (ts,) = struct.unpack(">Q", ts_b)
    expect = hmac.new(key, b"simurgh/v2/raw" + ts_b + cpub, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(tag, expect) or abs(time.time() - ts) > 300:
        conn.write(os.urandom(64))
        await _safe_drain(conn)
        conn.close()
        return
    priv, spub = _x25519()
    stag = hmac.new(key, b"simurgh/v2/raw-s" + ts_b + cpub + spub,
                    hashlib.sha256).digest()[:16]
    conn.write(spub + stag)
    await conn.drain()
    shared = _exchange(priv, cpub)
    okm = _hkdf(shared, salt=ts_b + cpub + spub, info=b"simurgh/v2/keys")
    inner = ByteFrameChannel(conn, peer=conn.peer, name="raw")
    channel = AeadFrameChannel(inner, okm[32:], okm[:32], pad=carrier.padding)
    await on_channel(channel)


async def _safe_drain(conn) -> None:
    try:
        await conn.drain()
    except Exception:
        pass

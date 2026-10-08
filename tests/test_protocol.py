"""Wire format: frames, addresses and the authentication header."""

from __future__ import annotations

import time

import pytest

from simurgh.carriers import (AUTH_WINDOW, ReplayCache, auth_key,
                              check_client_header, check_server_header,
                              client_header, server_header)
from simurgh.protocol import (HEADER_LEN, T_CLOSE, T_DATA, T_OPEN, T_OPEN_OK,
                              T_WIN, decode_addr, encode_addr, frame_payload,
                              frame_sid, frame_type, make_frame)


def test_frame_roundtrip():
    frame = make_frame(T_DATA, 42, b"hello")
    assert frame_type(frame) == T_DATA
    assert frame_sid(frame) == 42
    assert frame_payload(frame) == b"hello"
    assert len(frame) == HEADER_LEN + 5


def test_frame_defaults_and_types():
    assert frame_payload(make_frame(T_CLOSE, 7)) == b""
    assert frame_sid(make_frame(T_CLOSE, 7)) == 7
    assert frame_type(make_frame(T_OPEN_OK, 0)) == T_OPEN_OK
    assert frame_type(make_frame(T_WIN, 1, b"\x00" * 8)) == T_WIN


def test_frame_sid_is_big_endian():
    assert make_frame(T_OPEN, 0x01020304)[1:5] == b"\x01\x02\x03\x04"


@pytest.mark.parametrize("host,port,size", [
    ("127.0.0.1", 443, 1 + 4 + 2),
    ("example.com", 8443, 1 + 1 + len("example.com") + 2),
    ("::1", 22, 1 + 16 + 2),
])
def test_encode_addr_shapes(host, port, size):
    blob = encode_addr(host, port)
    assert len(blob) == size
    assert decode_addr(blob)[:2] == (host, port)


def test_decode_addr_leaves_the_tail():
    host, port, rest = decode_addr(encode_addr("10.0.0.5", 1080) + b"payload")
    assert (host, port) == ("10.0.0.5", 1080)
    assert rest == b"payload"


def test_decode_addr_rejects_garbage():
    with pytest.raises(ValueError):
        decode_addr(b"\x09\x01\x02")


def test_header_agreement_and_rejection():
    token, now = "s3cret-token", int(time.time())
    header = client_header(token, now)
    assert len(header) == 25
    ts = check_client_header(header, token)
    assert ts == now
    echo = server_header(token, ts)
    assert check_server_header(echo, token, ts) is True
    assert check_server_header(echo, "other", ts) is False
    assert check_client_header(header, "wrong") is None


def test_header_rejects_stale_and_future_timestamps():
    stale = int(time.time() - (AUTH_WINDOW + 60))
    assert check_client_header(client_header("tok", stale), "tok") is None
    ahead = int(time.time() + (AUTH_WINDOW + 60))
    assert check_client_header(client_header("tok", ahead), "tok") is None


def test_header_replay_is_refused():
    cache = ReplayCache()
    header = client_header("tok", int(time.time()))
    assert check_client_header(header, "tok", cache) is not None
    assert check_client_header(header, "tok", cache) is None  # same bytes twice


def test_auth_key_depends_on_token():
    assert auth_key("a") != auth_key("b")
    assert len(auth_key("a")) == 32

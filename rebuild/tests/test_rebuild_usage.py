from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rebuild import tunnel
from rebuild.usage import QuotaReached, UsageMeter, read_usage_snapshot


def test_monthly_meter_reserves_both_directions_and_persists_quota(tmp_path):
    path = tmp_path / "entry-usage.json"
    meter = UsageMeter(path, quota_mib=1)

    assert meter.consume("upload", 768 * 1024) == 768 * 1024
    assert meter.consume("download", 512 * 1024) == 256 * 1024
    assert meter.used_bytes == 1024 * 1024
    assert meter.exhausted
    assert meter.consume("upload", 1) == 0

    state = read_usage_snapshot(path)
    assert state is not None
    assert state["upload_bytes"] == 768 * 1024
    assert state["download_bytes"] == 256 * 1024
    assert state["quota_bytes"] == 1024 * 1024
    assert UsageMeter(path, quota_mib=1).used_bytes == 1024 * 1024
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_udp_reservation_is_atomic_at_quota_boundary(tmp_path):
    meter = UsageMeter(tmp_path / "entry-usage.json", quota_mib=1)
    assert meter.consume("upload", 1024 * 1024 - 1) == 1024 * 1024 - 1
    assert not meter.allow_datagram("download", 2)
    assert meter.used_bytes == 1024 * 1024 - 1
    assert meter.allow_datagram("download", 1)
    assert meter.exhausted


def test_tcp_forwarding_stops_at_monthly_quota(tmp_path):
    class Writer:
        def __init__(self):
            self.data = bytearray()

        def write(self, data):
            self.data.extend(data)

        async def drain(self):
            return None

    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(b"x" * (1024 * 1024 + 1))
        reader.feed_eof()
        writer = Writer()
        meter = UsageMeter(tmp_path / "entry-usage.json", quota_mib=1)
        with pytest.raises(QuotaReached):
            await tunnel._copy_to_tunnel(reader, writer, meter)
        frames = []
        data = bytes(writer.data)
        offset = 0
        while offset < len(data):
            kind, size = tunnel._FRAME_HEADER.unpack_from(data, offset)
            offset += tunnel._FRAME_HEADER.size
            frames.append((kind, size))
            offset += size
        assert offset == len(data)
        assert sum(size for kind, size in frames if kind == tunnel._DATA) == 1024 * 1024
        assert meter.used_bytes == 1024 * 1024

    asyncio.run(scenario())


def test_usage_meter_resets_old_month(tmp_path, monkeypatch):
    from rebuild import usage

    month = {"value": "2026-10"}
    monkeypatch.setattr(usage, "_month_now", lambda: month["value"])
    path = tmp_path / "entry-usage.json"
    meter = UsageMeter(path, quota_mib=1)
    meter.consume("upload", 500)
    assert meter.used_bytes == 500
    month["value"] = "2026-11"
    assert meter.used_bytes == 0
    assert meter.snapshot()["month"] == "2026-11"


def test_usage_reader_rejects_symlink_and_invalid_state(tmp_path):
    target = tmp_path / "real.json"
    target.write_text(json.dumps({
        "version": 1, "month": "2026-10", "upload_bytes": 0,
        "download_bytes": 0, "quota_bytes": None,
    }))
    link = tmp_path / "entry-usage.json"
    link.symlink_to(target)
    with pytest.raises(OSError, match="symlinked"):
        UsageMeter(link, quota_mib=1)
    assert target.exists()

    link.unlink()
    link.write_text('{"version":1,"month":"2026-10","upload_bytes":-1,'
                    '"download_bytes":0,"quota_bytes":null}')
    with pytest.raises(ValueError, match="invalid byte counter"):
        UsageMeter(link, quota_mib=1)

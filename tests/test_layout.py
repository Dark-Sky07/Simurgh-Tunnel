"""The panel's map, its chart history and the wizard's plan/backup plumbing.

These are the parts of the dashboard that are pure data: they can be tested
without a socket, which is exactly why they live outside ``panel.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from simurgh.config import (ExitConfig, ExitEndpoint, ListenSpec, Mapping,
                            RelayConfig, load_exit, load_relay, save_exit,
                            save_relay)
from simurgh.layout import History, RateTracker, build, human_bytes
from simurgh.panel_ui import (LoginTokens, apply_plan, backup, backup_path,
                              plan_changes, read_asset, rollback)

RELAY_STATUS = {
    "role": "relay", "name": "ir-tehran", "connected": True, "dial": "relay",
    "current_exit": "203.0.113.9:443", "rtt_ms": 12.5, "uptime": 3600.0,
    "reconnects": 2, "last_error": "", "bind_errors": [], "blocked": 3,
    "mappings": [
        {"key": "tcp:0.0.0.0:443/127.0.0.1:8443", "name": "panel", "listen": 443,
         "target": "127.0.0.1:8443", "enabled": True,
         "stats": {"in_bytes": 1000, "out_bytes": 2000, "conns": 2, "active": 2}},
        {"key": "tcp:0.0.0.0:2053/127.0.0.1:2053", "name": "relay", "listen": 2053,
         "target": "127.0.0.1:2053", "enabled": True, "listener": True,
         "stats": {"in_bytes": 10, "out_bytes": 20, "conns": 1, "active": 1}},
    ],
    "quotas": {"tcp:0.0.0.0:443/127.0.0.1:8443": {"limit": 1024, "used": 512,
                                                  "period": "month", "action": "block"}},
    "stats": {"rates": {"in_rate": 2048.0, "out_rate": 4096.0},
              "totals": {"in": 1000, "out": 2000, "sum": 3000, "conns": 3}},
    "traffic": [{"key": "tcp:0.0.0.0:443/127.0.0.1:8443", "today": 100,
                 "month": 200, "total": 300}],
    "version": "3.0.0", "product": "Simurgh Tunnel",
}


def test_chart_history_is_sampled_and_downsampled():
    history = History()
    # a poll every 2 s with a rising rate
    for i in range(400):
        history.add(1000.0 * i, 500.0 * i, now=1000.0 + i * 2)
    chart = history.chart(now=1800.0)
    assert chart["in"] and chart["out"] and chart["t"]
    assert len(chart["t"]) == len(chart["in"]) == len(chart["out"])
    assert chart["t"] == sorted(chart["t"])
    assert chart["in"][-1] > chart["in"][0]          # it is going up
    # 400 samples are not shipped whole: the wire copy is capped
    assert len(chart["t"]) <= 181, len(chart["t"])
    assert chart["window"] > 0
    # the history itself is capped at the half hour we promise
    assert len(history.samples) <= 900
    assert history.last()[0] == pytest.approx(1000.0 * 399)


def test_history_keeps_the_newest_value_inside_one_sample():
    history = History(sample_seconds=2.0)
    history.add(1.0, 2.0, now=100.0)
    history.add(9.0, 8.0, now=101.0)                 # too soon: replaces, not appends
    assert len(history.samples) == 1
    assert history.last() == (9.0, 8.0)
    history.add(3.0, 4.0, now=103.0)                 # a new sample now
    assert len(history.samples) == 2


def test_chart_survives_an_empty_and_a_silly_history():
    history = History()
    empty = history.chart(now=5.0)
    assert empty["t"] == [] and empty["in"] == [] and empty["out"] == [] and empty["window"] == 0
    history.add(-5.0, -1.0, now=1.0)                 # a counter reset is not a rate
    assert history.last() == (0.0, 0.0)
    assert history.chart(now=3.0)["in"] == [0.0]


def test_rate_tracker_turns_counters_into_a_rate():
    tracker = RateTracker()
    first = tracker.rate("a", 1000, 0, now=100.0)
    assert first["in_rate"] == 0.0            # nothing to compare against yet
    later = tracker.rate("a", 5000, 1000, now=102.0)
    assert later["in_rate"] == pytest.approx(2000.0)
    assert later["out_rate"] == pytest.approx(500.0)
    # a counter that went backwards (a restart) is not a negative rate
    reset = tracker.rate("a", 10, 0, now=104.0)
    assert reset["in_rate"] == 0.0 and reset["out_rate"] == 0.0
    # two polls at the same instant cannot invent a rate
    assert tracker.rate("a", 10 ** 6, 0, now=104.0)["in_rate"] == 0.0
    tracker.forget({"b"})
    assert tracker.rate("a", 1, 1, now=110.0)["in_rate"] == 0.0


def test_build_draws_the_whole_map():
    tracker = RateTracker()
    wrapped = {"node": RELAY_STATUS, "panel": {"port": 8787, "user": "admin",
                                              "requests": 5, "lockouts": 0,
                                              "failed_logins": 1},
               "version": "3.0.0", "product": "Simurgh Tunnel"}
    payload = build(wrapped, "relay", History(), tracker,
                    certificates=[{"name": "example.com", "days": 61}],
                    engine="go", now=1000.0)
    assert payload["role"] == "relay" and payload["engine"] == "go"
    assert payload["state"] == "up"
    assert payload["peer"]["endpoint"] == "203.0.113.9:443"
    # the user ports, with their quota and their own rate
    assert [row["listen"] for row in payload["mappings"]] == [443, 2053]
    assert payload["mappings"][0]["quota"]["used"] == 512
    assert payload["mappings"][0]["quota"]["limit"] == 1024
    assert payload["mappings"][0]["today"] == 100
    assert payload["mappings"][0]["conns"] == 2
    assert payload["mappings"][1]["listener"] is True    # the tunnel's own port
    assert payload["totals"]["today"] == 100
    assert payload["chart"]["t"] == [0.0]      # the first sample is in the chart
    # a second poll two seconds later: the map has a rate to animate
    moved = json.loads(json.dumps(wrapped))
    moved["node"]["mappings"][0]["stats"]["in_bytes"] += 4096     # 2 KiB/s
    moved["node"]["mappings"][0]["stats"]["out_bytes"] += 2048
    again = build(moved, "relay", History(), tracker, now=1002.0)
    assert again["mappings"][0]["in_rate"] == pytest.approx(2048.0)
    assert again["mappings"][0]["out_rate"] == pytest.approx(1024.0)
    assert again["rates"]["in"] == 2048.0
    assert json.dumps(again)                   # the payload is JSON-clean
    # an exit draws relay links instead of user ports
    exit_payload = build({"node": {"connected": True, "name": "de-frankfurt",
                                   "tunnel_count": 2, "connections": 2,
                                   "listening": [{"endpoint": "tls://0.0.0.0:443",
                                                  "carrier": "tls", "enabled": True}],
                                   "tunnels": [{"peer": "203.0.113.9:8443"}]}},
                         "exit", History(), RateTracker(), now=1.0)
    assert exit_payload["role"] == "exit"
    assert exit_payload["peer"]["listening"] == ["tls://0.0.0.0:443"]
    assert exit_payload["pool"][0]["endpoint"] == "203.0.113.9:8443"


def test_state_is_read_the_same_way_from_either_shape():
    """A relay that is down, then a peer list from the health checker."""
    down = build({"node": {"connected": False, "last_error": "refused"}}, "relay",
                 History(), RateTracker(), now=1.0)
    assert down["state"] == "down" and down["last_error"] == "refused"
    connecting = build({"node": {"connected": False, "dial": "relay",
                                 "current_exit": ""}}, "relay", History(),
                       RateTracker(), now=1.0)
    assert connecting["state"] == "connecting"
    # nothing is up on the exit side until a relay is in
    idle = build({"node": {"tunnel_count": 0, "connections": 0}}, "exit",
                 History(), RateTracker(), now=1.0)
    assert idle["state"] == "connecting"
    live = build({"node": {"tunnel_count": 1}}, "exit", History(), RateTracker(), now=1.0)
    assert live["state"] == "up"
    # the real relay publishes health as a list of rows
    real = build({"node": {**RELAY_STATUS,
                           "health": [{"endpoint": "203.0.113.9:443", "up": True,
                                       "rtt_ms": 12.5, "fails": 0, "label": "de"},
                                      {"endpoint": "203.0.113.10:443", "up": False,
                                       "failed_until": 9.5, "fails": 3, "rtt_ms": None,
                                       "label": "nl"}]}},
                 "relay", History(), RateTracker(), now=1.0)["pool"]
    rows = {row["endpoint"]: row for row in real}
    assert rows["203.0.113.9:443"]["state"] == "active"
    assert rows["203.0.113.9:443"]["label"] == "de"
    assert rows["203.0.113.10:443"]["state"] == "down"
    assert rows["203.0.113.10:443"]["fails"] == 3

    pool = build({"node": {**RELAY_STATUS,
                  "health": {"exits": [{"endpoint": "203.0.113.10:443", "rtt_ms": 90.0},
                                       {"endpoint": "203.0.113.9:443", "rtt_ms": 12.5}]}}},
                 "relay", History(), RateTracker(), now=1.0)["pool"]
    states = {row["endpoint"]: row["state"] for row in pool}
    assert states["203.0.113.9:443"] == "active"
    assert states["203.0.113.10:443"] == "standby"


def test_human_units_are_readable():
    assert human_bytes(0) == "0 B"
    assert human_bytes(2048) == "2.0 KiB"
    assert human_bytes(5 * 1024 ** 3) == "5.0 GiB"
    assert human_bytes(None) == "0 B"
    assert human_bytes("nonsense") == "0 B"


def test_login_tokens_are_single_use_and_expire():
    tokens = LoginTokens()
    made = tokens.create("cli", ttl=600)
    assert made["token"] and made["expires_in"] == 600
    assert tokens.consume(made["token"]) is True
    assert tokens.consume(made["token"]) is False        # burnt
    assert tokens.consume("guess") is False

    short = tokens.create("cli", ttl=0.001)
    import time as _time

    _time.sleep(0.01)
    assert tokens.consume(short["token"]) is False
    assert tokens.prune() == 0
    # a flood of links cannot grow without bound
    for _ in range(20):
        tokens.create("cli")
    assert len(tokens.snapshot()) <= 5
    assert tokens.revoke() >= 1
    assert tokens.snapshot()["outstanding"] == 0


def test_assets_are_named_not_pathed():
    assert read_asset("app.css")[1].startswith("text/css")
    assert read_asset("../panel.py") is None
    assert read_asset("nope.js") is None
    assert read_asset("") is None                 # a directory is not an asset


@pytest.fixture()
def relay_cfg():
    return RelayConfig(
        token="tok", name="ir-tehran", exit=ExitEndpoint(carrier="tls", port=443,
                                                         address="203.0.113.9"),
        mappings=[Mapping(name="panel", listen=443, target_port=443)])


def test_the_wizard_plan_is_a_diff_not_a_rewrite(relay_cfg):
    changes = plan_changes(relay_cfg, "relay", {"carrier": "wss", "port": 8443,
                                                "split": 2, "mapping": "8443:8443"})
    assert any("carrier tls -> wss" in c for c in changes)
    assert any("port 443 -> 8443" in c for c in changes)
    assert any("8443" in c and "added" in c for c in changes)
    # the same plan twice changes nothing the second time
    apply_plan(relay_cfg, "relay", {"carrier": "wss", "port": 8443, "split": 2,
                                    "mapping": "8443:8443"})
    assert plan_changes(relay_cfg, "relay", {"carrier": "wss", "port": 8443,
                                             "split": 2, "mapping": "8443:8443"}) == []


def test_the_wizard_plans_an_exit_listener():
    cfg = ExitConfig(token="tok", listen=[ListenSpec(carrier="tls", port=443)])
    changes = plan_changes(cfg, "exit", {"carrier": "raw", "port": 2053, "split": 1})
    assert any("carrier tls -> raw" in c for c in changes)
    apply_plan(cfg, "exit", {"carrier": "raw", "port": 2053, "split": 1})
    assert cfg.listen[0].carrier == "raw" and cfg.listen[0].port == 2053
    assert cfg.listen[0].split == 1
    # an exit with no listener at all gets one
    empty = ExitConfig(token="tok")
    apply_plan(empty, "exit", {"carrier": "wss", "port": 8443})
    assert empty.listen and empty.listen[0].carrier == "wss"


def test_backup_and_rollback_round_trip(tmp_path, relay_cfg):
    path = tmp_path / "relay.toml"
    save_relay(relay_cfg, path)
    original = path.read_text()

    saved = backup(path)
    assert saved and backup_path(path).exists()
    apply_plan(relay_cfg, "relay", {"carrier": "wss", "port": 8443})
    save_relay(relay_cfg, path)
    assert path.read_text() != original

    ok, detail = rollback(path, saved)
    assert ok, detail
    assert path.read_text() == original
    assert load_relay(path).exit.carrier == "tls"

    # and the failure paths are honest, not silent
    ok, detail = rollback(path, str(tmp_path / "nope.bak"))
    assert ok is False and detail
    missing = tmp_path / "elsewhere.toml"
    assert backup(missing) == ""
    # a rollback is repeatable: the backup is left in place on purpose
    ok, detail = rollback(path, saved)
    assert ok, detail


def test_exit_config_backup_round_trip(tmp_path):
    path = tmp_path / "exit.toml"
    save_exit(ExitConfig(token="tok", listen=[ListenSpec(carrier="tls")]), path)
    original = path.read_text()
    saved = backup(path)
    save_exit(ExitConfig(token="tok", listen=[ListenSpec(carrier="wss", port=8443)]), path)
    assert load_exit(path).listen[0].carrier == "wss"
    ok, _ = rollback(path, saved)
    assert ok and path.read_text() == original


# ------------------------------------------------- the map, against real nodes
async def _live_pair(tmp_path, certs):
    """A real relay + real exit on loopback, like the demo but in a test."""
    from conftest import free_port

    from simurgh.exit import ExitNode
    from simurgh.relay import RelayNode

    exit_port, relay_port, target_port = free_port(), free_port(), free_port()

    async def handler(reader, writer):
        try:
            await asyncio.wait_for(reader.read(65536), 5)
        except Exception:
            pass
        writer.write(b"x" * 65536)
        await writer.drain()
        writer.close()

    served = await asyncio.start_server(handler, "127.0.0.1", target_port)
    exit_cfg = ExitConfig(token="tok", name="de-frankfurt", cert_file=certs[0],
                          key_file=certs[1], cert_auto=False,
                          listen=[ListenSpec(carrier="tls", host="127.0.0.1",
                                             port=exit_port)])
    node = ExitNode(exit_cfg)
    await node.start()
    relay_cfg = RelayConfig(
        token="tok", name="ir-tehran",
        exit=ExitEndpoint(carrier="tls", address="127.0.0.1", port=exit_port,
                          insecure_skip_verify=True),
        mappings=[Mapping(name="panel", listen=relay_port, target_port=target_port)])
    relay = RelayNode(relay_cfg)
    await relay.start()
    for _ in range(100):
        if relay.connected.is_set():
            break
        await asyncio.sleep(0.05)
    assert relay.connected.is_set(), relay.last_error
    return node, relay, relay_port, served, exit_port


async def test_the_map_draws_a_live_tunnel(tmp_path, certs):
    """The one test that would have caught the pool's shape: real status dicts."""
    from simurgh.panel import Panel
    from simurgh.util import Home

    node, relay, relay_port, served, exit_port = await _live_pair(tmp_path, certs)
    home = Home(str(tmp_path / "home"))
    home.ensure()
    panel = Panel(home, "relay", node=relay, host="127.0.0.1", port=0,
                  user="admin", password="pw")
    try:
        payload = panel.layout_payload()
        assert payload["state"] == "up"
        assert payload["peer"]["carrier"] == "tls"
        assert payload["peer"]["endpoint"] == f"tls://127.0.0.1:{exit_port}"
        assert payload["peer"]["address"] == f"127.0.0.1:{exit_port}"
        assert payload["pool"], "the relay knows about its exit"
        assert payload["pool"][0]["state"] == "active"
        assert payload["mappings"][0]["listen"] == relay_port
        assert payload["engine"] in ("python", "go")
        assert json.dumps(payload)                      # JSON-clean, so HTTP-safe

        # the exit side draws too, and an exit has listeners, not user ports
        exit_panel = Panel(home, "exit", node=node, host="127.0.0.1", port=0,
                           user="admin", password="pw")
        drawn = exit_panel.layout_payload()
        assert drawn["role"] == "exit" and drawn["state"] == "up"
        assert drawn["peer"]["listening"], drawn["peer"]
        assert drawn["pool"] and drawn["pool"][0]["streams"] >= 0
        assert json.dumps(drawn)

        # real traffic moves the numbers the map shows
        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        got = await asyncio.wait_for(reader.read(65536), 10)
        writer.close()
        assert got == b"x" * 65536
        await asyncio.sleep(0.3)
        moved = panel.layout_payload()
        assert moved["totals"]["sum"] > 0
        assert moved["mappings"][0]["in_bytes"] > 0 or moved["mappings"][0]["out_bytes"] > 0
    finally:
        await relay.stop()
        await node.stop()
        served.close()

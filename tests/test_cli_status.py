"""``simurgh status`` — pairing state, JSON purity and ``--detail``."""

from __future__ import annotations

import json

from simurgh import cli, systemd
from simurgh.config import (ExitConfig, ExitEndpoint, ListenSpec, RelayConfig,
                            save_exit, save_relay)


def _run(tmp_path, *argv):
    return cli.main(["--home", str(tmp_path), *argv])


def _stub_systemd(monkeypatch):
    monkeypatch.setattr(systemd, "service_state", lambda name: {
        "ActiveState": "active", "SubState": "running", "UnitFileState": "enabled",
    })


def test_status_reports_an_unpaired_relay(tmp_path, capsys, monkeypatch):
    _stub_systemd(monkeypatch)
    save_relay(RelayConfig(token="", name="ir-1", unpaired=True,
                           exit=ExitEndpoint(carrier="tls", address="", port=443)),
               tmp_path / "relay.toml")

    assert _run(tmp_path, "status") == 0
    out = capsys.readouterr().out
    assert "not paired yet" in out
    assert "simurgh join" in out


def test_status_on_a_paired_relay_stays_quiet_about_pairing(tmp_path, capsys,
                                                            monkeypatch):
    _stub_systemd(monkeypatch)
    save_relay(RelayConfig(token="tok", name="ir-1",
                           exit=ExitEndpoint(carrier="tls", address="203.0.113.9",
                                             port=443)),
               tmp_path / "relay.toml")

    assert _run(tmp_path, "status") == 0
    out = capsys.readouterr().out
    assert "not paired yet" not in out


def test_status_json_emits_only_valid_json(tmp_path, capsys, monkeypatch):
    """Container health checks parse ``simurgh status --json`` directly."""
    _stub_systemd(monkeypatch)
    save_relay(RelayConfig(token="", name="ir-1", unpaired=True,
                           exit=ExitEndpoint(carrier="tls", address="", port=443)),
               tmp_path / "relay.toml")

    assert _run(tmp_path, "status", "--json") == 0
    raw = capsys.readouterr().out.strip()
    data = json.loads(raw)
    assert data["pairing"] == "unpaired"
    assert data["services"][0]["name"] == "simurgh-relay"


def test_status_json_on_exit_has_no_pairing_field(tmp_path, capsys, monkeypatch):
    _stub_systemd(monkeypatch)
    cfg = ExitConfig(token="tok", name="de-1")
    cfg.listen = [ListenSpec(carrier="tls", port=443)]
    save_exit(cfg, tmp_path / "exit.toml")

    assert _run(tmp_path, "status", "--json") == 0
    data = json.loads(capsys.readouterr().out.strip())
    assert "pairing" not in data
    assert data["services"][0]["name"] == "simurgh-exit"


def test_status_detail_delegates_to_cmd_status_and_detects_role(tmp_path, capsys):
    save_relay(RelayConfig(token="tok", name="ir-1",
                           exit=ExitEndpoint(carrier="tls", address="203.0.113.9",
                                             port=443)),
               tmp_path / "relay.toml")
    (tmp_path / "state.json").write_text(json.dumps({
        "relay": {
            "connected": True, "current_exit": "tls://203.0.113.9:443",
            "rtt_ms": 42.3, "reconnects": 1, "uptime": 120,
            "stats": {"totals": {"in_bytes": 1024, "out_bytes": 2048},
                      "rates": {"in_bps": 100, "out_bps": 200}},
            "mappings": [{"listen": 443, "target": "127.0.0.1:443",
                          "udp": False, "bound": True}],
        },
        "updated": 1,
    }))

    assert _run(tmp_path, "status", "--detail") == 0
    out = capsys.readouterr().out
    assert "tls://203.0.113.9:443" in out
    assert "443 → 127.0.0.1:443" in out
    assert out.count("Simurgh Tunnel") == 1


def test_status_detail_without_state_does_not_print_a_duplicate_header(
        tmp_path, capsys, monkeypatch):
    _stub_systemd(monkeypatch)
    save_relay(RelayConfig(token="tok", name="ir-1",
                           exit=ExitEndpoint(carrier="tls", address="203.0.113.9",
                                             port=443)),
               tmp_path / "relay.toml")

    assert _run(tmp_path, "status", "--detail") == 0
    out = capsys.readouterr().out
    assert out.count("Simurgh Tunnel") == 1

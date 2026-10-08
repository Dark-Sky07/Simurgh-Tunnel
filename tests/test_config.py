"""Configuration files: round trip, validation and secrets on disk."""

from __future__ import annotations

import os
import stat

import pytest

from simurgh.config import (ConfigError, ExitConfig, ExitEndpoint, ListenSpec,
                            Mapping, RelayConfig, load_exit, load_relay,
                            new_token, save_exit, save_relay)


def test_new_token_is_random_and_url_safe():
    a, b = new_token(), new_token()
    assert a != b and len(a) >= 32
    assert all(c.isalnum() or c in "-_" for c in a)


def test_exit_roundtrip(home):
    cfg = ExitConfig(
        token="tok-123", name="omega-de", cert_auto=False,
        cert_file="/tmp/c.pem", key_file="/tmp/k.pem",
        listen=[ListenSpec(carrier="tls", port=443, path="/ws"),
                ListenSpec(carrier="wss", port=2053, path="/ws", enabled=False)],
        push_ports=[443, 2053], strict_ports=True, speedtest_port=9000,
        proxy_protocol="v1", allow_ips=["1.2.3.4"],
    )
    save_exit(cfg, home.exit_cfg)
    back = load_exit(home.exit_cfg)
    assert back.token == "tok-123"
    assert back.name == "omega-de"
    assert [s.port for s in back.listen] == [443, 2053]
    assert [s.carrier for s in back.listen] == ["tls", "wss"]
    assert back.listen[1].enabled is False
    assert back.strict_ports is True
    assert back.push_ports == [443, 2053]
    assert back.speedtest_port == 9000
    assert back.proxy_protocol == "v1"


def test_relay_roundtrip(home):
    cfg = RelayConfig(
        token="tok-abc", name="ir-tehran", panel_port=8787,
        exit=ExitEndpoint(carrier="tls", address="203.0.113.9", port=443,
                          domain="panel.example.com", fingerprint="aabb",
                          insecure_skip_verify=False),
        pool=[ExitEndpoint(carrier="wss", address="198.51.100.7", port=2053)],
        mappings=[Mapping(name="panel", listen=443, target_port=443),
                  Mapping(name="dns", listen=53, target_port=53, udp=True)],
    )
    save_relay(cfg, home.relay_cfg)
    back = load_relay(home.relay_cfg)
    assert back.token == "tok-abc"
    assert back.exit.address == "203.0.113.9"
    assert back.exit.domain == "panel.example.com"
    assert back.exit.fingerprint == "aabb"
    assert [e.address for e in back.pool] == ["198.51.100.7"]
    assert [m.listen for m in back.mappings] == [443, 53]
    assert back.mappings[1].udp is True
    assert back.mappings[0].target_host == "127.0.0.1"
    assert back.panel_port == 8787


def test_missing_token_is_an_error(home):
    home.exit_cfg.write_text('name = "x"\n')
    with pytest.raises(ConfigError):
        load_exit(home.exit_cfg)
    home.relay_cfg.write_text('name = "x"\n')
    with pytest.raises(ConfigError):
        load_relay(home.relay_cfg)


def test_relay_needs_an_exit_block(home):
    home.relay_cfg.write_text('token = "t"\n')
    with pytest.raises(ConfigError):
        load_relay(home.relay_cfg)


def test_unknown_carrier_is_refused():
    from simurgh.config import _endpoint_from

    with pytest.raises(ConfigError):
        _endpoint_from({"carrier": "quic", "address": "1.2.3.4"})


def test_mapping_string_form_is_accepted(home):
    home.relay_cfg.write_text(
        'token = "t"\nmapping = ["8443:443"]\n[exit]\ncarrier = "tls"\n'
        'address = "1.2.3.4"\nport = 443\n'
    )
    cfg = load_relay(home.relay_cfg)
    assert cfg.mappings[0].listen == 8443
    assert cfg.mappings[0].target_port == 443


def test_config_files_are_private(home):
    save_exit(ExitConfig(token="secret"), home.exit_cfg)
    mode = stat.S_IMODE(os.stat(home.exit_cfg).st_mode)
    assert mode == 0o600


def test_endpoints_skips_disabled_and_empty():
    cfg = RelayConfig(token="t", exit=ExitEndpoint(address="1.1.1.1", port=443))
    cfg.pool = [ExitEndpoint(address="2.2.2.2", port=443, enabled=False),
                ExitEndpoint(address="", port=443)]
    assert [e.address for e in cfg.endpoints()] == ["1.1.1.1"]


def test_mapping_key_is_unique_per_protocol():
    a = Mapping(listen=443, target_port=443)
    b = Mapping(listen=443, target_port=443, udp=True)
    assert a.key() != b.key()

def test_performance_keys_are_parsed_and_clamped(home):
    """``connections``/``chunk``/``max_stream_window`` survive a round trip."""
    from simurgh.config import ExitEndpoint
    cfg = RelayConfig(token="tok", name="relay", connections=4, chunk=128 * 1024,
                      max_stream_window=8 * 1024 * 1024,
                      exit=ExitEndpoint(carrier="tls", address="198.51.100.7",
                                        port=443))
    save_relay(cfg, home.relay_cfg)
    back = load_relay(home.relay_cfg)
    assert back.connections == 4
    assert back.chunk == 128 * 1024
    assert back.max_stream_window == 8 * 1024 * 1024
    assert back.to_dict()["connections"] == 4


def test_silly_performance_values_are_clamped_not_fatal(home):
    cfg = RelayConfig(token="tok", name="relay", connections=9999, chunk=1,
                      stream_window=1024, max_stream_window=1024,
                      exit=ExitEndpoint(carrier="tls", address="198.51.100.7",
                                        port=443))
    save_relay(cfg, home.relay_cfg)
    back = load_relay(home.relay_cfg)
    assert 1 <= back.connections <= 16
    assert back.chunk >= 4 * 1024
    assert back.stream_window >= 16 * 1024          # window has a sane floor
    assert back.max_stream_window >= back.stream_window


def test_engine_key_round_trips_and_defaults_to_go(home):
    """The data plane is a config choice; unknown names are rejected."""
    from simurgh.config import DEFAULT_ENGINE, ConfigError, ExitEndpoint
    cfg = RelayConfig(token="tok", name="relay", engine="go",
                      exit=ExitEndpoint(carrier="tls", address="198.51.100.7", port=443))
    save_relay(cfg, home.relay_cfg)
    assert load_relay(home.relay_cfg).engine == "go"
    # the compiled engine is the default for new installs
    assert DEFAULT_ENGINE == "go"
    assert RelayConfig(token="tok").engine == "go"

    cfg.engine = "python"
    save_relay(cfg, home.relay_cfg)
    assert load_relay(home.relay_cfg).engine == "python"

    home.relay_cfg.write_text(
        home.relay_cfg.read_text().replace('engine = "python"', 'engine = "brainfuck"'))
    try:
        load_relay(home.relay_cfg)
    except ConfigError as exc:
        assert "engine" in str(exc)
    else:                                            # pragma: no cover
        raise AssertionError("a bogus engine must be refused")

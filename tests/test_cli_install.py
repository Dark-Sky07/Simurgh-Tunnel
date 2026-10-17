"""``simurgh install`` — the bits that only matter when one machine runs both
roles (``--role both``).

A single-box setup cannot be a copy of the two-server one:

* the exit's certificate is self-signed and there is no setup link to carry its
  fingerprint to the relay, so the relay has to pin the certificate itself —
  otherwise every handshake dies with "cannot reach exit";
* the ports the exit listens on belong to the tunnel *and* are on this same
  machine, so the relay must not mirror that list onto itself (it would fight
  the exit for the ports, and drop the mappings the user asked for).
"""

from __future__ import annotations

from simurgh import cli
from simurgh.certs import fingerprint_of
from simurgh.config import (ExitConfig, ListenSpec, RelayConfig, load_exit,
                            load_relay, save_exit, save_relay)


def _install_args(tmp_path, *extra):
    parser = cli.build_parser()
    args = parser.parse_args([
        "install", "--token", "tok", "--name", "dual", "--engine", "python",
        "--no-units", "--no-start", "--no-enable", *extra,
    ])
    args.home = str(tmp_path)
    return args


def test_install_both_on_one_box_finalises_the_local_pair(tmp_path):
    args = _install_args(tmp_path, "--both", "--listen", "9443,9444")
    assert cli.cmd_install(args) == 0

    exit_cfg = load_exit(tmp_path / "exit.toml")
    relay_cfg = load_relay(tmp_path / "relay.toml")

    assert relay_cfg.exit.address == "127.0.0.1"
    assert relay_cfg.exit.fingerprint == fingerprint_of(exit_cfg.cert_file)
    assert relay_cfg.accept_push is False
    assert exit_cfg.token == relay_cfg.token


def test_a_remote_exit_is_left_alone(tmp_path):
    """Two servers: the setup link carries the fingerprint, and the exit really
    does own the port list the relay mirrors."""
    home = cli.Home(tmp_path).ensure()
    exit_cfg = ExitConfig(token="tok", name="de-1")
    exit_cfg.listen = [ListenSpec(carrier="tls", port=443, path="/ws")]
    save_exit(exit_cfg, home.exit_cfg)
    relay_cfg = RelayConfig(token="tok", name="ir-1")
    relay_cfg.exit.address = "203.0.113.9"
    relay_cfg.exit.port = 443
    save_relay(relay_cfg, home.relay_cfg)

    cli._finalize_single_box(home)

    after = load_relay(home.relay_cfg)
    assert after.accept_push is True
    assert after.exit.fingerprint is None


PAIR_PAYLOAD = {
    "token": "tok-from-exit", "name": "de-frankfurt",
    "exit": {"carrier": "tls", "address": "198.51.100.7", "port": 443},
    "mappings": [{"port": 443, "target": 443}],
}


def _install_later(tmp_path, *extra):
    parser = cli.build_parser()
    args = parser.parse_args([
        "install", "--role", "relay", "--pair-later", "--engine", "python",
        "--no-units", "--no-start", "--no-enable", *extra,
    ])
    args.home = str(tmp_path)
    return args


def test_install_pair_later_writes_an_unpaired_config(tmp_path):
    """The Iranian server can be installed before the foreign one exists."""
    assert cli.cmd_install(_install_later(tmp_path)) == 0

    cfg = load_relay(tmp_path / "relay.toml")
    assert cfg.unpaired is True and cfg.paired is False
    assert cfg.token == "" and cfg.exit.address == ""
    assert cfg.panel_port == 8787          # still a normal, reachable panel


def test_join_completes_a_server_that_was_installed_unpaired(tmp_path, monkeypatch):
    """Pairing later must not throw away what the install already decided."""
    assert cli.cmd_install(_install_later(
        tmp_path, "--name", "ir-tehran", "--mapping", "8443:9000")) == 0

    from simurgh import links

    monkeypatch.setattr(links, "fetch_setup",
                        lambda link, timeout=15.0: PAIR_PAYLOAD)
    parser = cli.build_parser()
    join = parser.parse_args(["join", "simurgh://198.51.100.7:8787/setup?u=a&p=b",
                              "--no-restart"])
    join.home = str(tmp_path)
    assert cli.cmd_join(join) == 0

    cfg = load_relay(tmp_path / "relay.toml")
    assert cfg.paired is True and cfg.token == "tok-from-exit"
    assert cfg.exit.address == "198.51.100.7"
    assert cfg.name == "ir-tehran"                     # kept
    assert [m.listen for m in cfg.mappings] == [8443, 443]   # kept + added
    assert (tmp_path / "relay.toml.bak-pair").is_file()


def test_join_keeps_refusing_a_paired_config_without_force(tmp_path, monkeypatch):
    from simurgh import links

    assert cli.cmd_install(_install_later(tmp_path)) == 0
    monkeypatch.setattr(links, "fetch_setup",
                        lambda link, timeout=15.0: PAIR_PAYLOAD)
    parser = cli.build_parser()
    for attempt in range(2):
        join = parser.parse_args(["join", "simurgh://x:8787/setup?u=a&p=b",
                                  "--no-restart"])
        join.home = str(tmp_path)
        code = cli.cmd_join(join)
    assert attempt == 1            # the loop really ran twice
    assert code == 2               # the second pairing needs --force

    forced = parser.parse_args(["join", "simurgh://x:8787/setup?u=a&p=b",
                                "--no-restart", "--force"])
    forced.home = str(tmp_path)
    assert cli.cmd_join(forced) == 0

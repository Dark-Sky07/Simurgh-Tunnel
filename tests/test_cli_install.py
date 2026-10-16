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

"""Tuning profiles: one word that sets the knobs, and says what it changed."""

from __future__ import annotations

import json

import pytest

from simurgh import tune
from simurgh.cli import build_parser
from simurgh.config import (ExitConfig, ExitEndpoint, ListenSpec, Mapping,
                            RelayConfig, load_exit, load_relay, save_exit,
                            save_relay)


def _run(home, *argv):
    """Drive the real parser and command, like the CLI does."""
    ns = build_parser().parse_args(["--home", str(home), *argv])
    return ns.func(ns)


def _relay_home(tmp_path):
    home = tmp_path / "simurgh"
    home.mkdir(exist_ok=True)
    save_relay(RelayConfig(
        token="tok", name="ir-1",
        exit=ExitEndpoint(carrier="raw", address="127.0.0.1", port=8443),
        mappings=[Mapping(name="panel", listen=8443, target_port=8443)],
        stream_window=262144, max_stream_window=8388608, chunk=65536),
        home / "relay.toml")
    return home


def _exit_home(tmp_path):
    home = tmp_path / "simurgh"
    home.mkdir(exist_ok=True)
    save_exit(ExitConfig(token="tok", name="kharej", cert_auto=True,
                         listen=[ListenSpec(carrier="raw", port=8080)]),
              home / "exit.toml")
    return home


# ------------------------------------------------------------------ the table
def test_every_profile_only_touches_keys_the_config_has():
    """A profile is shorthand for existing keys -- never a new mechanism."""
    relay_fields = set(RelayConfig.__dataclass_fields__)
    exit_fields = set(ExitConfig.__dataclass_fields__)
    for profile in tune.describe():
        assert set(profile["relay"]) <= relay_fields, profile["name"]
        assert set(profile["exit"]) <= exit_fields, profile["name"]
        assert set(profile["relay"]) - set(profile["exit"]) <= {
            "keepalive", "connections"}, profile["name"]


def test_gaming_is_lighter_than_balanced_is_lighter_than_speed():
    """The three profiles have to be ordered, or they are decoration."""
    gaming, balanced, speed = (tune.get(name) for name in
                               ("gaming", "balanced", "speed"))
    assert (gaming.relay["stream_window"] < balanced.relay["stream_window"]
            < speed.relay["stream_window"])
    assert (gaming.relay["max_stream_window"] < balanced.relay["max_stream_window"]
            < speed.relay["max_stream_window"])
    assert gaming.relay["chunk"] < balanced.relay["chunk"] <= speed.relay["chunk"]
    assert speed.relay["connections"] > balanced.relay["connections"]
    # gaming keeps an eye on dead peers, speed does not care
    assert gaming.relay["tcp_keepalive"] > balanced.relay["tcp_keepalive"]


def test_balanced_is_the_shipped_default():
    """`tune balanced` must be a way back to the documented defaults."""
    cfg = RelayConfig(token="tok")
    assert tune.apply(cfg, "relay", "balanced") == []
    cfg = ExitConfig(token="tok")
    assert tune.apply(cfg, "exit", "balanced") == []


def test_an_unknown_profile_says_what_is_available():
    with pytest.raises(KeyError) as excinfo:
        tune.get("turbo")
    assert "gaming, balanced, speed" in str(excinfo.value)


# ------------------------------------------------------------------ applying
def test_apply_reports_only_what_it_changed():
    cfg = RelayConfig(token="tok")
    changes = tune.apply(cfg, "relay", "speed")
    keys = [change["key"] for change in changes]
    assert "stream_window" in keys and "connections" in keys
    assert cfg.stream_window == tune.get("speed").relay["stream_window"]
    assert cfg.connections == 4
    # nothing to do a second time
    assert tune.apply(cfg, "relay", "speed") == []


def test_the_exit_does_not_get_the_relay_only_keys():
    cfg = ExitConfig(token="tok")
    before = cfg.connections
    tune.apply(cfg, "exit", "speed")
    assert cfg.connections == before              # only the relay dials
    assert cfg.stream_window == tune.get("speed").exit["stream_window"]


# ------------------------------------------------------------------ the CLI
def test_cli_tune_writes_the_profile_and_survives_a_reload(tmp_path, capsys):
    home = _relay_home(tmp_path)
    assert _run(home, "tune", "gaming") == 0
    printed = capsys.readouterr()
    text = printed.out + printed.err
    assert "stream_window" in text and "apply it now: simurgh restart" in text

    cfg = load_relay(home / "relay.toml")
    profile = tune.get("gaming")
    for key, value in profile.relay.items():
        assert getattr(cfg, key) == value, key
    # ... and the file still loads as a relay config
    assert cfg.token == "tok" and cfg.name == "ir-1"


def test_cli_tune_dry_run_writes_nothing(tmp_path, capsys):
    home = _relay_home(tmp_path)
    before = (home / "relay.toml").read_text()
    assert _run(home, "tune", "speed", "--dry-run") == 0
    printed = capsys.readouterr()
    assert "nothing was written" in printed.out + printed.err
    assert (home / "relay.toml").read_text() == before
    cfg = load_relay(home / "relay.toml")
    assert cfg.connections == 1                   # untouched


def test_cli_tune_on_the_exit_and_back_again(tmp_path, capsys):
    home = _exit_home(tmp_path)
    assert _run(home, "tune", "gaming") == 0
    assert load_exit(home / "exit.toml").max_stream_window == 2097152
    assert _run(home, "tune", "balanced") == 0
    cfg = load_exit(home / "exit.toml")
    assert cfg.max_stream_window == 8388608 and cfg.chunk == 65536
    # asking for the profile it already has is not an error
    assert _run(home, "tune", "balanced") == 0
    printed = capsys.readouterr()
    assert "already matches the balanced profile" in printed.out + printed.err


def test_cli_tune_json_is_for_scripts(tmp_path, capsys):
    home = _relay_home(tmp_path)
    assert _run(home, "tune", "speed", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"] == "speed" and payload["role"] == "relay"
    assert payload["wrote"] is True
    assert any(change["key"] == "connections" and change["new"] == 4
               for change in payload["changes"])

    assert _run(home, "tune", "gaming", "--json", "--dry-run") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["wrote"] is False
    assert payload["changes"], "a dry run still reports what it would do"


def test_cli_tune_lists_the_profiles_without_an_installation(tmp_path, capsys):
    assert _run(tmp_path / "nope", "tune", "--show") == 0
    printed = capsys.readouterr()
    text = printed.out + printed.err
    for name in ("gaming", "balanced", "speed"):
        assert name in text
    assert "stream_window" in text


def test_cli_tune_rejects_an_unknown_profile(tmp_path, capsys):
    home = _relay_home(tmp_path)
    assert _run(home, "tune", "turbo") == 2
    printed = capsys.readouterr()
    assert "unknown profile" in printed.out + printed.err
    assert "gaming, balanced, speed" in printed.out + printed.err


def test_the_menu_offers_the_profile():
    from simurgh import menu

    assert "16" in menu.MENU_RELAY
    assert "profile" in menu.MENU_RELAY["16"].lower()


def test_cli_tune_detects_the_role_from_the_config(tmp_path, capsys):
    """A home with one config file must not ask for --role."""
    home = _exit_home(tmp_path)
    assert _run(home, "tune", "gaming") == 0
    printed = capsys.readouterr()
    assert "exit.toml" in printed.out + printed.err

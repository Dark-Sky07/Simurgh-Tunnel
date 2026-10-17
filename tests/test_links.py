"""Setup links: building, parsing and the exit -> relay hand-over."""

from __future__ import annotations

import asyncio

import pytest

from conftest import free_port
from simurgh.config import (ExitConfig, ListenSpec, Mapping, RelayConfig,
                            load_exit, load_relay, save_exit, save_relay)
from simurgh.links import (LinkError, build_join_link, fetch_setup,
                           merge_relay_payload, parse_join_link,
                           relay_config_from_payload, relay_payload_from_link,
                           reverse_payload)
from simurgh.panel import Panel

pytestmark = pytest.mark.asyncio

USER, PASSWORD = "admin", "pw-123"


def test_build_and_parse_roundtrip():
    link = build_join_link("203.0.113.9", 8787, USER, PASSWORD, "omega-de")
    assert link.startswith("simurgh://203.0.113.9:8787/setup?")
    info = parse_join_link(link)
    assert info["host"] == "203.0.113.9"
    assert info["port"] == 8787
    assert info["user"] == USER
    assert info["password"] == PASSWORD
    assert info["name"] == "omega-de"


def test_password_is_not_plain_in_the_link():
    link = build_join_link("h", 8787, USER, "sup3r-secret", "")
    assert "sup3r-secret" not in link


@pytest.mark.parametrize("bad", [
    "http://example.com/setup?u=a&p=b",
    "simurgh://example.com/wrong?u=a&p=b",
    "simurgh://example.com/setup?u=&p=",
])
def test_bad_links_are_refused(bad):
    with pytest.raises(LinkError):
        parse_join_link(bad)


def test_payload_becomes_a_relay_config():
    payload = {
        "token": "tok", "name": "exit-1",
        "exit": {"carrier": "tls", "address": "203.0.113.9", "port": 443,
                 "path": "/ws", "fingerprint": "abc"},
        "pool": [{"carrier": "wss", "address": "203.0.113.9", "port": 2053}],
        "push_ports": [443, 2053],
    }
    cfg = relay_config_from_payload(payload)
    assert cfg.token == "tok"
    assert cfg.exit.address == "203.0.113.9"
    assert cfg.exit.fingerprint == "abc"
    assert cfg.pool[0].fingerprint == "abc"            # inherited pin
    assert [m.listen for m in cfg.mappings] == [443, 2053]
    assert cfg.mappings[0].target_port == 443


def test_payload_with_explicit_mappings_wins():
    payload = {
        "token": "tok",
        "exit": {"carrier": "tls", "address": "1.2.3.4", "port": 443},
        "push_ports": [443],
        "mappings": [{"port": 8443, "target": 443, "name": "panel",
                      "target_host": "127.0.0.1", "udp": True}],
    }
    cfg = relay_config_from_payload(payload)
    assert len(cfg.mappings) == 1
    assert cfg.mappings[0].listen == 8443
    assert cfg.mappings[0].target_port == 443
    assert cfg.mappings[0].udp is True


async def test_fetch_setup_against_a_live_panel(home, certs):
    """The whole one-string hand-over: link -> panel -> ready relay config."""
    save_exit(ExitConfig(token="join-token", name="omega-de",
                         cert_file=certs[0], key_file=certs[1], cert_auto=False,
                         push_ports=[443],
                         listen=[ListenSpec(carrier="tls", port=443, path="/ws")]),
              home.exit_cfg)
    port = free_port()
    panel = Panel(home, "exit", node=None, host="127.0.0.1", port=port,
                  user=USER, password=PASSWORD)
    await panel.start()
    try:
        link = build_join_link("127.0.0.1", port, USER, PASSWORD, "omega-de")
        payload = await asyncio.to_thread(fetch_setup, link)
        cfg = relay_config_from_payload(payload)
        assert cfg.token == "join-token"
        assert cfg.exit.address == "127.0.0.1"
        assert cfg.exit.port == 443
        assert cfg.exit.fingerprint
        assert [m.listen for m in cfg.mappings] == [443]

        # and the result is a config the relay can actually load
        save_relay(cfg, home.relay_cfg)
        back = load_relay(home.relay_cfg)
        assert back.exit.fingerprint == cfg.exit.fingerprint
    finally:
        await panel.stop()


async def test_fetch_setup_with_wrong_password(home, certs):
    save_exit(ExitConfig(token="t", cert_file=certs[0], key_file=certs[1],
                         cert_auto=False,
                         listen=[ListenSpec(carrier="tls", port=443)]),
              home.exit_cfg)
    port = free_port()
    panel = Panel(home, "exit", node=None, host="127.0.0.1", port=port,
                  user=USER, password=PASSWORD)
    await panel.start()
    try:
        link = build_join_link("127.0.0.1", port, USER, "wrong-pw", "")
        with pytest.raises(LinkError):
            await asyncio.to_thread(fetch_setup, link)
    finally:
        await panel.stop()


async def test_fetch_setup_without_a_panel():
    link = build_join_link("127.0.0.1", free_port(), USER, PASSWORD, "")
    with pytest.raises(LinkError):
        await asyncio.to_thread(fetch_setup, link, 2.0)


def test_a_reverse_exit_dials_with_the_anti_dpi_split(tmp_path):
    """In reverse mode the exit is the side that dials, so `simurgh join` must
    put the split there: a one-byte ClientHello fragment is then what a DPI box
    that reads only the first TCP segment gets to see."""
    from types import SimpleNamespace
    from simurgh import cli

    relay_cfg = RelayConfig(token="t0k", name="ir",
                            tunnel=ListenSpec(carrier="tls", port=8443, path="/ws"))
    payload = reverse_payload(relay_cfg, "203.0.113.9")
    args = SimpleNamespace(host="", force=True, split=3)
    home = cli.Home(tmp_path)
    assert cli._join_exit(home, payload, args) == 0
    spec = load_exit(home.exit_cfg).listen[0]
    assert spec.reverse and spec.split == 3

    args.split = 0
    assert cli._join_exit(home, payload, args) == 0
    assert load_exit(home.exit_cfg).listen[0].split == 0


def test_link_prefers_the_reverse_relay_even_with_both_configs(tmp_path, capsys):
    """``--role both`` on one server leaves both configs on disk. In reverse
    mode the link the *other* side needs is still the exit's one, so `simurgh
    link` must not fall back to the "import me on the Iranian server" text."""
    from types import SimpleNamespace
    from simurgh import cli

    home = cli.Home(tmp_path).ensure()
    exit_cfg = ExitConfig(token="t0k", name="dual")
    exit_cfg.listen = [ListenSpec(carrier="tls", port=443, path="/ws")]
    save_exit(exit_cfg, home.exit_cfg)
    relay_cfg = RelayConfig(token="t0k", name="dual")
    relay_cfg.dial = "exit"
    save_relay(relay_cfg, home.relay_cfg)

    assert cli.cmd_link(SimpleNamespace(home=str(tmp_path), host="127.0.0.1",
                                        panel_port=0, show=False)) == 0
    out = capsys.readouterr().out
    assert "exit setup link" in out
    assert "FOREIGN (exit) server" in out
    assert "Iranian server" not in out


def test_link_still_hands_the_relay_link_to_a_direct_exit(tmp_path, capsys):
    """The opposite case: a direct exit (no reverse relay next to it) keeps
    printing the link for the Iranian server."""
    from types import SimpleNamespace
    from simurgh import cli

    home = cli.Home(tmp_path).ensure()
    exit_cfg = ExitConfig(token="t0k", name="de-1")
    exit_cfg.listen = [ListenSpec(carrier="tls", port=443, path="/ws")]
    save_exit(exit_cfg, home.exit_cfg)
    relay_cfg = RelayConfig(token="t0k", name="de-1")      # dial = "relay"
    relay_cfg.dial = "relay"
    save_relay(relay_cfg, home.relay_cfg)

    assert cli.cmd_link(SimpleNamespace(home=str(tmp_path), host="127.0.0.1",
                                        panel_port=0, show=False)) == 0
    out = capsys.readouterr().out
    assert "relay setup link" in out
    assert "Iranian server" in out


async def test_merging_keeps_the_install_and_adds_the_exit():
    """Pairing later: the link supplies the secret and the exit, nothing else."""
    cfg = RelayConfig(name="ir-tehran", unpaired=True, panel_port=9911,
                      engine="python")
    cfg.exit.address = ""
    cfg.mappings.append(Mapping(name="mine", listen=8443, target_port=9000))
    info = merge_relay_payload(cfg, {
        "token": "tok", "name": "de-frankfurt",
        "exit": {"carrier": "tls", "address": "198.51.100.7", "port": 443},
        "pool": [{"carrier": "wss", "address": "198.51.100.8", "port": 2053}],
        "mappings": [{"port": 443, "target": 443},
                     {"port": 8443, "target": 9000}],   # already here: no dupe
    })
    assert cfg.unpaired is False and cfg.paired is True
    assert cfg.token == "tok" and cfg.name == "ir-tehran"
    assert cfg.panel_port == 9911 and cfg.engine == "python"
    assert cfg.exit.address == "198.51.100.7"
    assert [e.address for e in cfg.pool] == ["198.51.100.8"]
    assert [m.listen for m in cfg.mappings] == [8443, 443]
    assert info["added"] == [443] and info["exit"].startswith("tls://")


async def test_a_link_that_belongs_to_the_exit_is_refused(monkeypatch):
    """`simurgh link` on a reverse relay prints an exit link; a direct relay
    must not swallow it."""
    from simurgh import links

    monkeypatch.setattr(links, "fetch_setup",
                        lambda link, timeout=15.0: {"role": "exit", "token": "t",
                                                    "listen": {"address": "1.2.3.4"}})
    with pytest.raises(LinkError, match="exit server"):
        relay_payload_from_link("simurgh://x:8787/setup?u=a&p=b")

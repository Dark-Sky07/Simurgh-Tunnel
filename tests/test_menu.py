"""Interactive-menu safety and behaviour that does not need a real TTY."""

from __future__ import annotations

import pytest

from simurgh import menu
from simurgh.util import Home


def test_fresh_install_role_can_be_chosen_from_the_menu(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from simurgh import cli, systemd

    monkeypatch.setattr(menu, "clear", lambda: None)
    monkeypatch.setattr(menu, "ask", lambda *a, **k: "2")
    monkeypatch.setattr(systemd, "available", lambda: False)
    calls = []
    monkeypatch.setattr(cli, "cmd_init", lambda args:
                        calls.append(("init", args.role, args.pair_later)) or 0)
    monkeypatch.setattr(cli, "cmd_install", lambda args:
                        calls.append(("install", args.role, args.pair_later)) or 0)

    assert menu.main(SimpleNamespace(home=str(tmp_path))) == 0
    assert calls == [("init", "relay", True), ("install", "relay", True)]


def test_log_view_is_bounded_pauseable_and_never_uses_a_shell(
        tmp_path, monkeypatch, capsys):
    home = Home(tmp_path).ensure()
    home.logs.mkdir(parents=True, exist_ok=True)
    (home.logs / "relay.log").write_text(
        "".join(f"line-{number}\n" for number in range(1, 31)), encoding="utf-8")
    monkeypatch.setattr(menu, "clear", lambda: None)
    paused = []
    monkeypatch.setattr(menu, "pause", lambda: paused.append(True))

    def shell_must_not_run(command):
        raise AssertionError(f"unexpected shell execution: {command}")

    monkeypatch.setattr(menu.os, "system", shell_must_not_run)
    menu.show_logs(home, "relay")

    out = capsys.readouterr().out
    assert "line-1\n" not in out
    assert "line-6" in out and "line-30" in out       # exactly the last 25 lines
    assert paused == [True]                            # the next redraw cannot erase it


def test_menu_installs_updates_and_offers_a_health_checked_restart(
        tmp_path, monkeypatch):
    from simurgh import cli, systemd
    from simurgh import update as updater

    home = Home(tmp_path)
    monkeypatch.setattr(systemd, "available", lambda: True)
    monkeypatch.setattr(menu, "clear", lambda: None)
    monkeypatch.setattr(menu, "pause", lambda: None)
    monkeypatch.setattr(updater, "check", lambda: {
        "current": "1.0.0", "latest": "9.9.9", "tag": "v9.9.9",
        "available": True, "notes_url": "https://example.invalid/v9",
    })
    calls = []
    monkeypatch.setattr(updater, "update", lambda path, verbose=True:
                        calls.append(("update", path, verbose)) or {
                            "action": "updated", "steps": ["python", "go"],
                            "latest": "9.0.0", "restart_required": True,
                        })
    monkeypatch.setattr(cli, "_restart_updated_service",
                        lambda given, expected=None:
                        calls.append(("restart", given.path, expected)) or 0)
    answers = iter([True, True])
    monkeypatch.setattr(menu, "ask_yes", lambda *args: next(answers))

    menu.software_update(home, "relay")

    assert calls == [("update", tmp_path, False), ("restart", tmp_path, "9.0.0")]
    assert "17" in menu.MENU_COMMON


def test_menu_reports_system_package_updates_without_running_pip(tmp_path, monkeypatch, capsys):
    from simurgh import update as updater

    home = Home(tmp_path)
    monkeypatch.setattr(menu, "clear", lambda: None)
    monkeypatch.setattr(menu, "pause", lambda: None)
    monkeypatch.setattr(updater, "check", lambda: {
        "current": "1.0.0", "latest": "9.9.9", "tag": "v9.9.9",
        "available": True, "self_update_supported": False,
        "notes_url": "https://example.invalid/v9",
    })
    monkeypatch.setattr(updater, "update", lambda *a, **k:
                        (_ for _ in ()).throw(AssertionError("unsafe updater called")))

    menu.software_update(home, "relay")

    output = capsys.readouterr().out
    assert "apt/dnf" in output and "matching release package" in output


def test_menu_does_not_offer_a_systemd_restart_without_systemd(
        tmp_path, monkeypatch, capsys):
    from simurgh import cli, systemd, update as updater

    home = Home(tmp_path)
    monkeypatch.setattr(menu, "clear", lambda: None)
    monkeypatch.setattr(menu, "pause", lambda: None)
    monkeypatch.setattr(systemd, "available", lambda: False)
    monkeypatch.setattr(updater, "check", lambda: {
        "current": "1.0.0", "latest": "9.9.9", "tag": "v9.9.9",
        "available": True, "notes_url": "https://example.invalid/v9",
    })
    monkeypatch.setattr(updater, "update", lambda *a, **k: {
        "action": "updated", "steps": ["python"], "latest": "9.9.9",
        "restart_required": True,
    })
    monkeypatch.setattr(cli, "_restart_updated_service", lambda *a, **k:
                        pytest.fail("systemd restart should not be attempted"))
    monkeypatch.setattr(menu, "ask_yes", lambda *a, **k: True)

    menu.software_update(home, "relay")

    output = capsys.readouterr().out
    assert "systemd is unavailable" in output and "simurgh relay" in output


def test_panel_info_links_directly_to_the_login_form(tmp_path, monkeypatch, capsys):
    from simurgh.config import RelayConfig, save_relay

    home = Home(tmp_path).ensure()
    save_relay(RelayConfig(panel_port=8787), home.relay_cfg)
    monkeypatch.setattr(menu, "_guess_ip", lambda: "203.0.113.10")
    monkeypatch.setattr(menu, "clear", lambda: None)
    monkeypatch.setattr(menu, "pause", lambda: None)

    menu.panel_info(home, "relay")

    output = capsys.readouterr().out
    assert "http://203.0.113.10:8787/login" in output

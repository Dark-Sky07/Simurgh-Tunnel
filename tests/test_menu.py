"""Interactive-menu safety and behaviour that does not need a real TTY."""

from __future__ import annotations

from simurgh import menu
from simurgh.util import Home


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
    from simurgh import cli
    from simurgh import update as updater

    home = Home(tmp_path)
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

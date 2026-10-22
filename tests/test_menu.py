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

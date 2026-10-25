"""Generated systemd commands must be valid for the actual Python CLI parser."""

from __future__ import annotations

import shlex

import pytest

from simurgh import cli, systemd
from simurgh.util import Home


@pytest.mark.parametrize("role", ["relay", "exit"])
def test_generated_exec_start_is_accepted_by_the_cli(monkeypatch, tmp_path, role):
    """Regression: v3.2.0 put global --home after the role and exited with 2."""
    monkeypatch.setattr(systemd.shutil, "which",
                        lambda name: "/usr/local/bin/simurgh" if name == "simurgh" else None)
    home = Home(tmp_path)

    command = systemd.exec_start_for(home, role)
    args = cli.build_parser().parse_args(shlex.split(command)[1:])

    assert args.command == role
    assert args.role == role
    assert args.home == str(tmp_path)
    assert command.startswith("/usr/local/bin/simurgh --home ")
    unit = systemd.render_unit(home, role)
    assert f"ExecStart={command}" in unit


def test_first_run_setup_unit_is_accepted_by_the_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(systemd.shutil, "which",
                        lambda name: "/usr/local/bin/simurgh" if name == "simurgh" else None)
    home = Home(tmp_path)
    command = systemd.exec_start_for(home, "setup")
    args = cli.build_parser().parse_args(shlex.split(command)[1:])
    assert args.command == "setup"
    assert args.home == str(tmp_path)
    unit = systemd.render_unit(home, "setup")
    assert f"ExecStart={command}" in unit
    assert f"StandardOutput=append:{home.logs / 'panel.log'}" in unit


def test_first_run_service_is_written_enabled_and_started(tmp_path, monkeypatch):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    monkeypatch.setattr(systemd, "UNIT_DIR", unit_dir)
    monkeypatch.setattr(systemd, "available", lambda: True)
    calls = []
    monkeypatch.setattr(systemd, "_run", lambda args, timeout=60.0:
                        calls.append(args) or (0, ""))

    report = systemd.install_setup_service(Home(tmp_path))

    path = unit_dir / "simurgh-setup.service"
    assert report["systemd"] and report["write"]
    assert report["units"] == [str(path)]
    assert path.is_file() and "simurgh --home" in path.read_text()
    assert "setup" in path.read_text()
    assert calls == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "simurgh-setup"],
        ["systemctl", "restart", "simurgh-setup"],
    ]


def test_source_checkout_exec_start_is_accepted_by_the_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(systemd.shutil, "which",
                        lambda name: "/usr/bin/python3" if name == "python3" else None)
    command = systemd.exec_start_for(Home(tmp_path), "relay")
    parts = shlex.split(command)

    assert parts[:4] == ["/usr/bin/python3", "-m", "simurgh", "--home"]
    # The first item after `-m simurgh` is --home, so include it in parser input.
    args = cli.build_parser().parse_args(parts[3:])
    assert args.home == str(tmp_path)
    assert args.command == "relay"


@pytest.mark.parametrize("role", ["relay", "exit"])
def test_legacy_post_role_home_is_accepted_during_upgrade(tmp_path, role):
    """Units written by v3.2.0 recover after upgrading, even before regeneration."""
    args = cli.build_parser().parse_args([role, "--home", str(tmp_path)])
    assert args.command == role
    assert args.home == str(tmp_path)

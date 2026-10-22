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

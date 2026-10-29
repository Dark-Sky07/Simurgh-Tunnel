from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rebuild import wizard as setup


def scripted(values):
    iterator = iter(values)
    return lambda prompt: next(iterator)


def test_entry_wizard_supports_udp_and_requires_the_shared_route():
    config = setup.build_config("entry", scripted([
        "2", "", "", "dns", "s" * 40, "203.0.113.8", "", "", "",
    ]), lambda _message: None)

    assert config["role"] == "entry"
    assert config["transport"] == "udp"
    assert config["listen_port"] == 15353
    assert config["route"] == "dns"
    assert config["exit_host"] == "203.0.113.8"
    assert config["secret"] == "s" * 40


def test_exit_wizard_creates_fixed_destination_route(tmp_path):
    config = setup.build_config("exit", scripted([
        "", "", "web", "x" * 40, "", "", "127.0.0.1", "8080",
    ]), lambda _message: None, config_dir=tmp_path)

    assert config["role"] == "exit"
    assert config["cert_file"] == str(tmp_path / "tls/server.crt")
    assert config["key_file"] == str(tmp_path / "tls/server.key")
    assert config["listen_host"] == "0.0.0.0"
    assert config["routes"] == {"web": {"host": "127.0.0.1", "port": 8080}}


def test_setup_menu_preserves_previous_config_with_private_backup(tmp_path):
    path = tmp_path / "exit.json"
    path.write_text('{"role":"old"}\n')
    os.chmod(path, 0o644)
    inputs = scripted([
        "2", "", "", "web", "x" * 40, "", "", "127.0.0.1", "8080", "yes",
    ])
    messages = []

    assert setup.setup_menu(tmp_path, inputs, messages.append) == 0
    new_config = json.loads(path.read_text())
    backup = tmp_path / "exit.json.bak"
    assert new_config["role"] == "exit"
    assert json.loads(backup.read_text()) == {"role": "old"}
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert any("/etc/simurgh" in message for message in messages)


def test_setup_menu_cancel_keeps_existing_bytes(tmp_path):
    path = tmp_path / "entry.json"
    original = b'{"role":"entry","secret":"old"}\n'
    path.write_bytes(original)
    answers = scripted(["1", "1", "", "", "web", "x" * 40,
                        "exit.example.net", "", "", "", "n"])
    assert setup.setup_menu(tmp_path, answers, lambda _message: None) == 1
    assert path.read_bytes() == original
    assert not (tmp_path / "entry.json.bak").exists()


def test_setup_rejects_a_short_secret():
    with pytest.raises(ValueError, match="at least 32"):
        setup.build_config("exit", scripted(["", "", "web", "short"]),
                           lambda _message: None)

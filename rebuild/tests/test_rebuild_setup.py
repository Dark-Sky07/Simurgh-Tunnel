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
from rebuild.supervisor import NodeSupervisor
from rebuild.tunnel import ConfigError


def scripted(values):
    iterator = iter(values)
    return lambda prompt: next(iterator)


def test_client_session_limit_prompt_retries_out_of_range_values():
    messages = []
    assert setup._client_session_limit(scripted(["0", "513", "24"]),
                                      messages.append) == 24
    assert len(messages) == 2


def test_entry_wizard_supports_udp_and_requires_the_shared_route():
    config = setup.build_config("entry", scripted([
        "2", "1", "", "", "dns", "s" * 40, "64", "5000", "203.0.113.8", "", "", "", "",
    ]), lambda _message: None)

    assert config["role"] == "entry"
    assert config["transport"] == "udp"
    assert config["carrier"] == "tls"
    assert config["listen_port"] == 15353
    assert config["route"] == "dns"
    assert config["exit_host"] == "203.0.113.8"
    assert config["secret"] == "s" * 40
    assert config["client_session_limit"] == 64
    assert config["udp_rate_limit_pps"] == 5000


def test_entry_wizard_can_select_wss_carrier():
    config = setup.build_config("entry", scripted([
        "1", "2", "", "", "web", "s" * 40, "128", "exit.example.net", "", "", "", "",
    ]), lambda _message: None)
    assert config["transport"] == "tcp"
    assert config["carrier"] == "wss"


def test_entry_wizard_accepts_monthly_transfer_quota():
    config = setup.build_config("entry", scripted([
        "1", "1", "", "", "web", "s" * 40, "128", "exit.example.net", "", "", "", "256",
    ]), lambda _message: None)
    assert config["monthly_quota_mib"] == 256


def test_reverse_agent_wizard_sets_a_fixed_private_target(tmp_path):
    config = setup.build_config("reverse-agent", scripted([
        "web", "s" * 40, "hub.example.net", "9443", "hub.example.net", "",
        "127.0.0.1", "8080",
    ]), lambda _message: None, config_dir=tmp_path)
    assert config["role"] == "reverse-agent"
    assert config["hub_port"] == 9443
    assert config["server_name"] == "hub.example.net"
    assert config["target_host"] == "127.0.0.1"
    assert config["target_port"] == 8080
    setup.save_config(tmp_path / "reverse-agent.json", config)
    supervisor = NodeSupervisor(tmp_path)
    supervisor.set_autostart("reverse-agent", True)
    assert supervisor.autostart_roles == {"reverse-agent"}
    setup.save_config(tmp_path / "reverse-agent.json", dict(config, target_port=8081))
    backup = setup.list_config_backups(tmp_path, "reverse-agent")[0]
    setup.restore_config(tmp_path, "reverse-agent", backup.name)
    assert json.loads((tmp_path / "reverse-agent.json").read_text())["target_port"] == 8080


def test_reverse_hub_wizard_defaults_to_loopback_public_service(tmp_path):
    config = setup.build_config("reverse-hub", scripted([
        "web", "128", "s" * 40, "", "", "", "", "", "",
    ]), lambda _message: None, config_dir=tmp_path)
    assert config["role"] == "reverse-hub"
    assert config["control_host"] == "0.0.0.0"
    assert config["public_host"] == "127.0.0.1"
    assert config["control_port"] == 9443
    assert config["public_port"] == 8080
    assert config["client_session_limit"] == 128


def test_exit_wizard_creates_fixed_destination_route(tmp_path):
    config = setup.build_config("exit", scripted([
        "1", "", "", "web", "x" * 40, "", "", "127.0.0.1", "8080",
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
        "2", "1", "", "", "web", "x" * 40, "", "", "127.0.0.1", "8080", "yes",
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


def test_restore_config_validates_backup_and_preserves_replaced_file(tmp_path):
    original = {
        "role": "entry", "transport": "tcp", "carrier": "tls",
        "listen_host": "127.0.0.1", "listen_port": 18080,
        "exit_host": "exit.example.net", "exit_port": 8443,
        "server_name": "exit.example.net", "ca_file": "/tmp/ca.crt",
        "secret": "s" * 40, "route": "web",
    }
    current = dict(original, route="new-route")
    path = tmp_path / "entry.json"
    setup.save_config(path, original)
    backup = path.with_name("entry.json.bak")
    setup.save_config(path, current)
    assert backup in setup.list_config_backups(tmp_path, "entry")

    replaced_backup = setup.restore_config(tmp_path, "entry", backup.name)
    assert json.loads(path.read_text())["route"] == "web"
    assert replaced_backup is not None
    assert json.loads(replaced_backup.read_text())["route"] == "new-route"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_config_refuses_to_follow_a_symlink(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret":"external"}\n')
    target = tmp_path / "entry.json"
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="symlinked"):
        setup.save_config(target, {"role": "entry"})
    assert outside.read_text() == '{"secret":"external"}\n'
    assert target.is_symlink()


def test_supervisor_refuses_a_symlinked_node_config(tmp_path):
    outside = tmp_path / "external-entry.json"
    outside.write_text(json.dumps({
        "role": "entry", "listen_host": "127.0.0.1", "listen_port": 18080,
        "exit_host": "127.0.0.1", "exit_port": 8443,
        "server_name": "localhost", "ca_file": "/tmp/ca.crt",
        "secret": "s" * 40, "route": "web",
    }))
    config = tmp_path / "entry.json"
    config.symlink_to(outside)
    supervisor = NodeSupervisor(tmp_path)
    with pytest.raises(ConfigError, match="symlinked node configuration"):
        supervisor.start("entry")
    assert supervisor.nodes == {}


def test_supervisor_refuses_symlinked_log_files(tmp_path):
    config = tmp_path / "entry.json"
    config.write_text(json.dumps({
        "role": "entry", "listen_host": "127.0.0.1", "listen_port": 18080,
        "exit_host": "127.0.0.1", "exit_port": 8443,
        "server_name": "localhost", "ca_file": "/tmp/ca.crt",
        "secret": "s" * 40, "route": "web",
    }))
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    outside = tmp_path / "external.log"
    outside.write_text("preserve external file\n")
    (log_dir / "entry.log").symlink_to(outside)
    supervisor = NodeSupervisor(tmp_path)
    with pytest.raises(OSError):
        supervisor.start("entry")
    assert outside.read_text() == "preserve external file\n"
    assert supervisor.nodes == {}


def test_setup_menu_cancel_keeps_existing_bytes(tmp_path):
    path = tmp_path / "entry.json"
    original = b'{"role":"entry","secret":"old"}\n'
    path.write_bytes(original)
    answers = scripted(["1", "1", "1", "", "", "web", "x" * 40, "128",
                        "exit.example.net", "", "", "", "", "n"])
    assert setup.setup_menu(tmp_path, answers, lambda _message: None) == 1
    assert path.read_bytes() == original
    assert not (tmp_path / "entry.json.bak").exists()


def test_setup_rejects_a_short_secret():
    with pytest.raises(ValueError, match="at least 32"):
        setup.build_config("exit", scripted(["1", "", "", "web", "short"]),
                           lambda _message: None)

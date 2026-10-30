from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rebuild import menu


def test_status_reports_entry_and_exit_routes(tmp_path):
    (tmp_path / "entry.json").write_text(json.dumps({
        "role": "entry", "transport": "udp", "carrier": "wss", "listen_host": "127.0.0.1",
        "listen_port": 5353, "exit_host": "exit.example.net", "exit_port": 8443,
        "server_name": "exit.example.net", "ca_file": "/tmp/ca.crt",
        "secret": "s" * 40, "route": "dns",
    }))
    (tmp_path / "exit.json").write_text(json.dumps({
        "role": "exit", "listen_host": "0.0.0.0", "listen_port": 8443,
        "cert_file": "/tmp/server.crt", "key_file": "/tmp/server.key",
        "secret": "s" * 40, "routes": {"dns": {"host": "127.0.0.1", "port": 53}},
    }))
    lines = []
    roles = menu.show_status(tmp_path, lines.append)

    assert roles == ("entry", "exit")
    assert any("UDP listener" in line and "carrier=WSS" in line and "route=dns" in line
               and "client cap=128/source IP" in line for line in lines)
    assert any("1 fixed route" in line for line in lines)


def test_menu_status_and_runner_support_reverse_roles(tmp_path, monkeypatch):
    hub = {
        "role": "reverse-hub", "control_host": "0.0.0.0", "control_port": 9443,
        "public_host": "127.0.0.1", "public_port": 8080,
        "cert_file": "/tmp/hub.crt", "key_file": "/tmp/hub.key",
        "secret": "h" * 40, "route": "web", "client_session_limit": 17,
    }
    agent = {
        "role": "reverse-agent", "hub_host": "hub.example.net", "hub_port": 9443,
        "server_name": "hub.example.net", "ca_file": "/tmp/ca.crt",
        "secret": "a" * 40, "route": "web", "target_host": "127.0.0.1",
        "target_port": 8080,
    }
    (tmp_path / "reverse-hub.json").write_text(json.dumps(hub))
    (tmp_path / "reverse-agent.json").write_text(json.dumps(agent))
    lines = []
    roles = menu.show_status(tmp_path, lines.append)
    assert roles == ("reverse-hub", "reverse-agent")
    assert any("public TCP 127.0.0.1:8080" in line
               and "client cap=17/source IP" in line for line in lines)

    started = []

    async def fake_agent(config):
        started.append(config.route)

    monkeypatch.setattr(menu, "serve_reverse_agent", fake_agent)
    assert menu.run_role("reverse-agent", tmp_path, lines.append) == 0
    assert started == ["web"]


def test_menu_check_reverse_agent_target_uses_its_fixed_config(tmp_path, monkeypatch):
    (tmp_path / "reverse-agent.json").write_text(json.dumps({
        "role": "reverse-agent", "hub_host": "hub.example.net", "hub_port": 9443,
        "server_name": "hub.example.net", "ca_file": "/tmp/ca.crt",
        "secret": "a" * 40, "route": "web", "target_host": "127.0.0.1",
        "target_port": 8080,
    }))
    checked = []

    async def check(config):
        checked.append((config.target_host, config.target_port))
        return "fixed target reachable"

    monkeypatch.setattr(menu, "check_reverse_agent_target", check)
    lines = []
    assert menu.check_reverse_agent_target_role(tmp_path, lines.append) == 0
    assert checked == [("127.0.0.1", 8080)]
    assert lines == ["fixed target reachable"]


def test_menu_requires_config_before_running_and_can_exit(tmp_path):
    answers = iter(["2", "8"])
    lines = []
    assert menu.menu(tmp_path, lambda _prompt: next(answers), lines.append) == 0
    assert any("Entry is not configured" in line for line in lines)


def test_menu_exposes_release_update_action(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(menu, "interactive_update", lambda config_dir: called.append(config_dir) or 0)
    answers = iter(["7", "8"])
    assert menu.menu(tmp_path, lambda _prompt: next(answers), lambda _message: None) == 0
    assert called == [tmp_path]


def test_run_role_refuses_config_for_the_other_role(tmp_path):
    (tmp_path / "entry.json").write_text(json.dumps({
        "role": "exit", "listen_host": "0.0.0.0", "listen_port": 8443,
        "cert_file": "/tmp/cert", "key_file": "/tmp/key",
        "secret": "s" * 40, "routes": {"x": {"host": "127.0.0.1", "port": 80}},
    }))
    lines = []
    assert menu.run_role("entry", tmp_path, lines.append) == 2
    assert any("does not contain an entry config" in line for line in lines)


def test_menu_checks_entry_path_before_update(tmp_path, monkeypatch):
    (tmp_path / "entry.json").write_text(json.dumps({
        "role": "entry", "transport": "tcp", "listen_host": "127.0.0.1",
        "listen_port": 18080, "exit_host": "exit.example.net", "exit_port": 8443,
        "server_name": "exit.example.net", "ca_file": "/tmp/ca.crt",
        "secret": "s" * 40, "route": "web",
    }))
    async def check(_config):
        return "path looks good"

    monkeypatch.setattr(menu, "check_entry_path", check)
    answers = iter(["5", "8"])
    lines = []
    assert menu.menu(tmp_path, lambda _prompt: next(answers), lines.append) == 0
    assert "path looks good" in lines


def test_menu_runs_speed_test_for_tcp_entry(tmp_path, monkeypatch):
    (tmp_path / "entry.json").write_text(json.dumps({
        "role": "entry", "transport": "tcp", "listen_host": "127.0.0.1",
        "listen_port": 18080, "exit_host": "exit.example.net", "exit_port": 8443,
        "server_name": "exit.example.net", "ca_file": "/tmp/ca.crt",
        "secret": "s" * 40, "route": "web",
    }))

    async def speed_test(_config):
        return "4 MiB measured"

    monkeypatch.setattr(menu, "speed_test_entry", speed_test)
    answers = iter(["6", "8"])
    lines = []
    assert menu.menu(tmp_path, lambda _prompt: next(answers), lines.append) == 0
    assert "4 MiB measured" in lines


def test_run_entry_starts_foreground_service_and_handles_ctrl_c(tmp_path, monkeypatch):
    (tmp_path / "entry.json").write_text(json.dumps({
        "role": "entry", "transport": "tcp", "listen_host": "127.0.0.1",
        "listen_port": 18080, "exit_host": "exit.example.net", "exit_port": 8443,
        "server_name": "exit.example.net", "ca_file": "/tmp/ca.crt",
        "secret": "s" * 40, "route": "web",
    }))
    started = []

    async def serve(config, _usage_path=None):
        started.append(config.route)

    def run(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(menu, "serve_entry", serve)
    monkeypatch.setattr(menu.asyncio, "run", run)
    lines = []
    assert menu.run_role("entry", tmp_path, lines.append) == 0
    assert started == []  # asyncio.run was stubbed and deliberately did not run it
    assert any("Stopped entry" in line for line in lines)

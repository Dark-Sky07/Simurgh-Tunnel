from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rebuild import panel as panel_module
from rebuild.panel import SetupPanel
from rebuild.wizard import list_config_backups, save_config


def started_panel(config_dir):
    panel = SetupPanel(config_dir, port=0)
    panel.start()
    host, port = panel.address
    return panel, f"http://{host}:{port}"


def _csrf(url):
    with urllib.request.urlopen(url + "/") as response:
        body = response.read().decode()
    return re.search(r'name="csrf" value="([^"]+)"', body).group(1)


def _post(url, path, fields, headers=None):
    request_headers = {"Content-Type": "application/x-www-form-urlencoded"}
    request_headers.update(headers or {})
    request = urllib.request.Request(
        url + path,
        data=urlencode(fields).encode(),
        method="POST",
        headers=request_headers,
    )
    return urllib.request.urlopen(request)


def entry_fields(secret="s" * 40):
    return {
        "role": "entry", "transport": "tcp", "listen_host": "127.0.0.1",
        "listen_port": "18080", "route": "web", "secret": secret,
        "exit_host": "exit.example.net", "exit_port": "8443",
        "server_name": "exit.example.net", "ca_file": "/etc/simurgh-next/tls/ca.crt",
        "target_host": "127.0.0.1", "target_port": "8080",
    }


def write_test_entry(config_dir):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    listen_port = probe.getsockname()[1]
    probe.close()
    (config_dir / "entry.json").write_text(json.dumps({
        "role": "entry", "transport": "tcp", "listen_host": "127.0.0.1",
        "listen_port": listen_port, "exit_host": "127.0.0.1", "exit_port": 9,
        "server_name": "localhost", "ca_file": "/tmp/unused-ca.crt",
        "secret": "s" * 40, "route": "test",
    }))


def test_setup_panel_is_loopback_only_and_offers_role_selection(tmp_path):
    panel, url = started_panel(tmp_path)
    try:
        with urllib.request.urlopen(url + "/") as response:
            body = response.read().decode()
            assert response.status == 200
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert 'name="role"' in body
        assert 'value="entry"' in body and 'value="exit"' in body
        assert panel.address[0] == "127.0.0.1"

        request = urllib.request.Request(url + "/", headers={"Host": "attacker.invalid"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 421
    finally:
        panel.close()


def test_panel_rejects_wrong_loopback_port_and_cross_origin_posts(tmp_path):
    panel, url = started_panel(tmp_path)
    try:
        port = panel.address[1]
        request = urllib.request.Request(url + "/", headers={"Host": f"localhost:{port + 1}"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 421

        fields = entry_fields()
        fields["csrf"] = _csrf(url)
        wrong_origin = {"Origin": f"http://localhost:{port + 1}"}
        with pytest.raises(urllib.error.HTTPError) as error:
            _post(url, "/setup", fields, headers=wrong_origin)
        assert error.value.code == 403
        assert not (tmp_path / "entry.json").exists()

        same_origin = {"Origin": f"http://127.0.0.1:{port}"}
        with _post(url, "/setup", fields, headers=same_origin) as response:
            assert response.status == 201
    finally:
        panel.close()


def test_setup_panel_requires_form_token_and_keeps_secret_out_of_response(tmp_path):
    panel, url = started_panel(tmp_path)
    try:
        fields = entry_fields()
        fields["csrf"] = _csrf(url)
        with _post(url, "/setup", fields) as response:
            body = response.read().decode()
            assert response.status == 201
        assert "Saved entry configuration" in body
        assert "s" * 40 not in body
        config = json.loads((tmp_path / "entry.json").read_text())
        assert config["role"] == "entry" and config["transport"] == "tcp"
        assert config["carrier"] == "tls"
        assert config["route"] == "web"
    finally:
        panel.close()


def test_setup_panel_can_save_wss_entry_configuration(tmp_path):
    panel, url = started_panel(tmp_path)
    try:
        fields = entry_fields()
        fields.update({"carrier": "wss", "csrf": _csrf(url)})
        with _post(url, "/setup", fields) as response:
            assert response.status == 201
        config = json.loads((tmp_path / "entry.json").read_text())
        assert config["carrier"] == "wss"
    finally:
        panel.close()


def test_panel_update_check_is_csrf_protected_and_returns_install_command(tmp_path, monkeypatch):
    monkeypatch.setattr(panel_module, "latest_update", lambda: {"_simurgh_version": "0.3.0"})
    panel, url = started_panel(tmp_path)
    try:
        csrf = _csrf(url)
        with _post(url, "/update/check", {"csrf": csrf}) as response:
            body = response.read().decode()
            assert response.status == 200
        assert "Simurgh Next 0.3.0 is available" in body
        assert "simurgh-next-update --apply" in body
        with pytest.raises(urllib.error.HTTPError) as error:
            _post(url, "/update/check", {"csrf": "invalid"})
        assert error.value.code == 403
    finally:
        panel.close()


def test_panel_restores_only_a_validated_backup_after_confirmation(tmp_path):
    old = {
        "role": "entry", "transport": "tcp", "carrier": "tls",
        "listen_host": "127.0.0.1", "listen_port": 18080,
        "exit_host": "exit.example.net", "exit_port": 8443,
        "server_name": "exit.example.net", "ca_file": "/tmp/ca.crt",
        "secret": "s" * 40, "route": "old-route",
    }
    current = dict(old, route="current-route")
    path = tmp_path / "entry.json"
    save_config(path, old)
    save_config(path, current)
    panel, url = started_panel(tmp_path)
    try:
        csrf = _csrf(url)
        with urllib.request.urlopen(url + "/") as response:
            assert "entry.json.bak" in response.read().decode()
        fields = {"csrf": csrf, "role": "entry", "backup": "entry.json.bak"}
        with pytest.raises(urllib.error.HTTPError) as error:
            _post(url, "/config/restore", fields)
        assert error.value.code == 400
        fields["confirm"] = "yes"
        with _post(url, "/config/restore", fields) as response:
            body = response.read().decode()
            assert response.status == 200
        assert "Restored entry configuration" in body
        assert json.loads(path.read_text())["route"] == "old-route"
        preserved = list_config_backups(tmp_path, "entry")
        assert any(json.loads(backup.read_text())["route"] == "current-route"
                   for backup in preserved)
    finally:
        panel.close()


def test_setup_panel_refuses_csrf_and_requires_backup_confirmation(tmp_path):
    panel, url = started_panel(tmp_path)
    existing = tmp_path / "entry.json"
    original = '{"role":"old"}\n'
    existing.write_text(original)
    try:
        fields = entry_fields()
        fields["csrf"] = _csrf(url)
        with pytest.raises(urllib.error.HTTPError) as error:
            _post(url, "/setup", fields)
        assert error.value.code == 409
        assert existing.read_text() == original
        assert not (tmp_path / "entry.json.bak").exists()

        fields["replace"] = "yes"
        with _post(url, "/setup", fields) as response:
            assert response.status == 201
        assert json.loads((tmp_path / "entry.json.bak").read_text()) == {"role": "old"}
        assert json.loads(existing.read_text())["role"] == "entry"
    finally:
        panel.close()


def test_setup_panel_rejects_untrusted_secret_and_non_loopback_binding(tmp_path):
    with pytest.raises(ValueError, match="loopback"):
        SetupPanel(tmp_path, host="0.0.0.0")

    panel, url = started_panel(tmp_path)
    try:
        fields = entry_fields(secret="short")
        fields["csrf"] = _csrf(url)
        with pytest.raises(urllib.error.HTTPError) as error:
            _post(url, "/setup", fields)
        assert error.value.code == 400
        assert not (tmp_path / "entry.json").exists()
    finally:
        panel.close()


def test_panel_starts_and_stops_only_its_own_entry_process(tmp_path):
    write_test_entry(tmp_path)
    panel, url = started_panel(tmp_path)
    try:
        csrf = _csrf(url)
        with _post(url, "/node/start", {"csrf": csrf, "role": "entry"}) as response:
            start_body = response.read().decode()
            assert response.status == 200
        state = panel.supervisor.status()["entry"]
        assert state["state"] == "running"
        assert "Started entry" in start_body
        assert Path(state["log"]).is_file()

        with _post(url, "/node/stop", {"csrf": csrf, "role": "entry"}) as response:
            stop_body = response.read().decode()
            assert response.status == 200
        assert "Stopped entry" in stop_body
        assert panel.supervisor.status()["entry"]["state"] == "stopped"
    finally:
        panel.close()


def test_panel_close_stops_owned_node_process(tmp_path):
    write_test_entry(tmp_path)
    panel, url = started_panel(tmp_path)
    process = None
    try:
        csrf = _csrf(url)
        with _post(url, "/node/start", {"csrf": csrf, "role": "entry"}) as response:
            assert response.status == 200
        process = panel.supervisor.nodes["entry"].process
        assert process.poll() is None
    finally:
        panel.close()
    assert process is not None and process.poll() is not None


def test_panel_autostart_preference_survives_service_restarts(tmp_path):
    write_test_entry(tmp_path)
    state_path = tmp_path / "autostart.json"

    first_panel, first_url = started_panel(tmp_path)
    try:
        csrf = _csrf(first_url)
        with _post(first_url, "/node/autostart", {
            "csrf": csrf, "role": "entry", "enabled": "yes",
        }) as response:
            body = response.read().decode()
            assert response.status == 200
        assert "Auto-start enabled for entry" in body
        assert json.loads(state_path.read_text()) == {"roles": ["entry"]}
        assert first_panel.supervisor.status()["entry"]["state"] == "stopped"
    finally:
        first_panel.close()

    second_panel, second_url = started_panel(tmp_path)
    process = second_panel.supervisor.nodes["entry"].process
    try:
        assert second_panel.supervisor.status()["entry"]["state"] == "running"
        csrf = _csrf(second_url)
        with _post(second_url, "/node/autostart", {
            "csrf": csrf, "role": "entry", "enabled": "no",
        }) as response:
            body = response.read().decode()
            assert response.status == 200
        assert "Auto-start disabled for entry" in body
        assert second_panel.supervisor.status()["entry"]["state"] == "running"
    finally:
        second_panel.close()
    assert process.poll() is not None

    third_panel, _third_url = started_panel(tmp_path)
    try:
        assert third_panel.supervisor.status()["entry"]["state"] == "stopped"
        assert third_panel.supervisor.nodes == {}
    finally:
        third_panel.close()


def test_invalid_autostart_file_is_reported_and_not_overwritten(tmp_path):
    write_test_entry(tmp_path)
    state = tmp_path / "autostart.json"
    original = b"not valid json\n"
    state.write_bytes(original)
    panel, url = started_panel(tmp_path)
    try:
        with urllib.request.urlopen(url + "/") as response:
            body = response.read().decode()
        assert "Auto-start state error" in body
        fields = {"csrf": _csrf(url), "role": "entry", "enabled": "yes"}
        with pytest.raises(urllib.error.HTTPError) as error:
            _post(url, "/node/autostart", fields)
        assert error.value.code == 400
        assert state.read_bytes() == original
    finally:
        panel.close()


def test_sigterm_to_panel_stops_its_child_node(tmp_path):
    write_test_entry(tmp_path)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    panel_port = probe.getsockname()[1]
    probe.close()
    server = subprocess.Popen(
        [sys.executable, "-m", "rebuild.panel", "--config-dir", str(tmp_path),
         "--port", str(panel_port)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_pid = None
    try:
        url = f"http://127.0.0.1:{panel_port}"
        deadline = time.monotonic() + 10
        while True:
            try:
                csrf = _csrf(url)
                break
            except (urllib.error.URLError, ConnectionError):
                if server.poll() is not None:
                    pytest.fail("setup panel exited before becoming ready")
                if time.monotonic() >= deadline:
                    pytest.fail("setup panel did not become ready")
                time.sleep(0.05)
        with _post(url, "/node/start", {"csrf": csrf, "role": "entry"}) as response:
            assert response.status == 200
            body = response.read().decode()
        match = re.search(r"Running · PID (\d+)", body)
        assert match, body
        child_pid = int(match.group(1))

        server.send_signal(signal.SIGTERM)
        assert server.wait(timeout=10) == 0
        deadline = time.monotonic() + 5
        while True:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            if time.monotonic() >= deadline:
                pytest.fail(f"panel child process {child_pid} survived panel SIGTERM")
            time.sleep(0.05)
    finally:
        if server.poll() is None:
            server.send_signal(signal.SIGTERM)
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

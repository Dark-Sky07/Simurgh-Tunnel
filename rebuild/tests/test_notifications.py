from __future__ import annotations

import json
import re
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rebuild import notifications
from rebuild.notifications import WebhookNotifier, _public_address, validate_webhook_url
from rebuild.panel import SetupPanel


def _csrf(url: str) -> str:
    with urllib.request.urlopen(url + "/") as response:
        body = response.read().decode()
    return re.search(r'name="csrf" value="([^"]+)"', body).group(1)


def _post(url: str, path: str, fields: dict[str, str]):
    request = urllib.request.Request(
        url + path,
        data=urlencode(fields).encode(),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return urllib.request.urlopen(request)


def _started_panel(config_dir: Path):
    panel = SetupPanel(config_dir, port=0)
    panel.start()
    host, port = panel.address
    return panel, f"http://{host}:{port}"


def test_webhook_requires_https_and_rejects_embedded_credentials():
    assert validate_webhook_url("https://hooks.example.net/notify?key=abc") == (
        "https://hooks.example.net/notify?key=abc")
    for value in (
        "http://hooks.example.net/notify",
        "https://user:password@hooks.example.net/notify",
        "https://hooks.example.net/notify#fragment",
        "https://127.0.0.1/hook",
    ):
        with pytest.raises(ValueError):
            validate_webhook_url(value)


def test_webhook_dns_resolution_rejects_private_addresses(monkeypatch):
    monkeypatch.setattr(
        "rebuild.notifications.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("127.0.0.1", 443))],
    )
    with pytest.raises(OSError, match="public IP"):
        _public_address("notify.example.net", 443)


def test_existing_webhook_config_permissions_are_tightened(tmp_path):
    config_path = tmp_path / "notifications.json"
    config_path.write_text('{"webhook_url":"https://hooks.example.net/notify"}')
    config_path.chmod(0o644)
    notifier = WebhookNotifier(config_path)
    try:
        assert notifier.configured
        assert config_path.stat().st_mode & 0o777 == 0o600
    finally:
        notifier.close()


def test_webhook_config_is_saved_privately_and_disabled_by_blank_url(tmp_path):
    config_path = tmp_path / "notifications.json"
    notifier = WebhookNotifier(config_path)
    try:
        notifier.set_url("https://hooks.example.net/notify?token=private")
        assert notifier.configured
        assert config_path.read_text() == (
            '{\n  "webhook_url": "https://hooks.example.net/notify?token=private"\n}\n')
        assert config_path.stat().st_mode & 0o777 == 0o600

        notifier.set_url("")
        assert not notifier.configured
    finally:
        notifier.close()


def test_webhook_post_pins_public_ip_and_sends_only_minimal_event(monkeypatch):
    calls = {}

    class Response:
        status = 204

        def read(self, _limit):
            return b""

    class Connection:
        def __init__(self, host, port, address, **_kwargs):
            calls["destination"] = (host, port, address)

        def request(self, method, path, *, body, headers):
            calls["request"] = (method, path, body, headers)

        def getresponse(self):
            return Response()

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr(notifications, "_public_address",
                        lambda host, port: "93.184.216.34")
    monkeypatch.setattr(notifications, "_PinnedHTTPSConnection", Connection)
    notifications._deliver("https://hooks.example.net:8443/alert?token=secret",
                           "node_crashed", "entry")

    method, path, payload, headers = calls["request"]
    assert calls["destination"] == ("hooks.example.net", 8443, "93.184.216.34")
    assert method == "POST" and path == "/alert?token=secret"
    assert b"secret" not in payload
    assert set(json.loads(payload)) == {
        "event", "role", "version", "timestamp",
    }
    assert headers["Host"] == "hooks.example.net:8443"
    assert calls["closed"]


def test_webhook_delivery_runs_on_worker_thread_without_secrets(monkeypatch, tmp_path):
    notifier = WebhookNotifier(tmp_path / "notifications.json")
    delivered = []
    done = threading.Event()

    def fake_deliver(url, event, role):
        delivered.append((url, event, role))
        done.set()

    monkeypatch.setattr(notifications, "_deliver", fake_deliver)
    notifier.set_url("https://hooks.example.net/notify?token=private")
    notifier.notify("node_crashed", "entry")
    try:
        assert done.wait(2)
        assert delivered == [(
            "https://hooks.example.net/notify?token=private", "node_crashed", "entry")]
    finally:
        notifier.close()


def test_panel_can_configure_and_disable_private_webhook(tmp_path):
    panel, url = _started_panel(tmp_path)
    state_path = tmp_path / "notifications.json"
    webhook = "https://hooks.example.net/simurgh?token=private"
    try:
        with _post(url, "/notifications", {
            "csrf": _csrf(url), "webhook_url": webhook,
        }) as response:
            body = response.read().decode()
            assert response.status == 200
        assert "notifications enabled" in body
        assert webhook not in body
        assert webhook in state_path.read_text()
        assert state_path.stat().st_mode & 0o777 == 0o600

        with _post(url, "/notifications", {"csrf": _csrf(url), "webhook_url": ""}) as response:
            assert response.status == 200
        assert not panel.supervisor.notifier.configured
    finally:
        panel.close()


def test_panel_rejects_non_https_webhook(tmp_path):
    panel, url = _started_panel(tmp_path)
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            _post(url, "/notifications", {
                "csrf": _csrf(url), "webhook_url": "http://127.0.0.1/hook",
            })
        assert error.value.code == 400
    finally:
        panel.close()

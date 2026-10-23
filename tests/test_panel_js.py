"""The panel's JavaScript, run against a stubbed DOM.

The panel is one Python file and one JavaScript file that have to agree with
each other.  Python is covered by the rest of the suite; this is the other half:
``tests/js/panel_dom.js`` loads ``assets/app.js`` in a fake document, answers the
API with canned payloads, walks every view and every button, and fails on the
first exception.  It found real bugs (a dead state pill, an empty Ports table, a
button wired to nothing), which is why it is a test and not a scratch file.

Node is required; without it the module is skipped, like the rpm build is.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from simurgh.layout import History, RateTracker, build

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "tests" / "js" / "panel_dom.js"
HAVE_NODE = shutil.which("node") is not None

pytestmark = pytest.mark.skipif(not HAVE_NODE, reason="node is not installed")


def panel_payload(role: str = "relay") -> dict:
    """A payload with everything the page draws, built by the panel's own code."""
    mappings = [
        {"key": "tcp:0.0.0.0:8443/127.0.0.1:8443", "name": "panel-a", "listen": 8443,
         "target": "127.0.0.1:8443", "enabled": True, "bound": True,
         "stats": {"in_bytes": 4 << 20, "out_bytes": 9 << 20, "conns": 12, "active": 4}},
        {"key": "udp:0.0.0.0:2053/127.0.0.1:2053", "name": "dns", "listen": 2053,
         "target": "127.0.0.1:2053", "enabled": False, "bound": True, "udp": True,
         "stats": {"in_bytes": 10, "out_bytes": 20, "conns": 0, "active": 0}},
    ]
    if role == "exit":
        mappings = [{"key": "listen:tls://0.0.0.0:443", "name": "tls", "listen": 0,
                     "target": "tls://0.0.0.0:443", "enabled": True, "bound": True,
                     "listener": True, "stats": {"in_bytes": 0, "out_bytes": 0,
                                                 "conns": 0, "active": 0}}]
    status = {
        "product": "Simurgh Tunnel", "version": "3.0.0",
        "panel": {"role": role, "uptime": 400.0, "requests": 42, "port": 8787,
                  "user": "admin", "error": "", "lockouts": 1, "failed_logins": 6,
                  "paired": True, "pairing_required": False, "reverse": False,
                  "setup_link": role == "exit", "alerts": None},
        "node": {
            "role": role, "name": "ir-tehran" if role == "relay" else "de-frankfurt",
            "connected": True, "dial": "relay", "rtt_ms": 22.5, "uptime": 400.0,
            "reconnects": 2, "last_error": "", "bind_errors": [], "blocked": 1,
            "current_exit": "tls://203.0.113.9:443",
            "mappings": mappings,
            "quotas": {mappings[0]["key"]: {"limit": 10 ** 10, "used": 4 << 20,
                                            "period": "month", "action": "block"}},
            "traffic": [{"key": mappings[0]["key"], "today": 123456,
                         "month": 987654, "total": 10 ** 9}],
            "stats": {"rates": {"in_rate": 1.5e6, "out_rate": 7e5},
                      "totals": {"in": 4 << 20, "out": 9 << 20, "sum": 13 << 20,
                                 "conns": 12}},
            "health": [{"endpoint": "tls://203.0.113.9:443", "up": True, "rtt_ms": 22.5,
                        "fails": 0, "label": "de-frankfurt"},
                       {"endpoint": "tls://203.0.113.10:443", "up": False, "rtt_ms": None,
                        "fails": 3, "failed_until": 8.0, "label": "nl-amsterdam"}],
            "tunnel_targets": ["tls://203.0.113.9:443"],
            "exit_info": {"name": "de-frankfurt", "version": "3.0.0"},
            "listening": [{"endpoint": "tls://0.0.0.0:443", "carrier": "tls",
                           "enabled": True}],
            "tunnels": [{"peer": "203.0.113.9:8443", "streams": 3, "uptime": 90.0}],
        },
    }
    history, tracker = History(), RateTracker()
    payload = {}
    for i in range(60):                       # a chart with half an hour in it
        payload = build(status, role, history, tracker,
                        certificates=[{"endpoint": "tls:8443",
                                       "cert_file": "/etc/fullchain.pem",
                                       "key_file": "/etc/privkey.pem", "exists": True,
                                       "days": 61.0, "fingerprint": "ab" * 32,
                                       "note": ""},
                                      {"endpoint": "wss:8443", "cert_file": "",
                                       "key_file": "", "exists": False, "days": None,
                                       "fingerprint": "", "note": "generated"}],
                        engine="python", now=100.0 + i * 2)
    payload["ok"] = True
    payload["panel"]["tokens"] = {"outstanding": 1, "oldest": 1.0}
    return payload


def run_panel_js(tmp_path: Path, role: str = "relay", payload: dict | None = None,
                 extra_env: dict | None = None):
    path = tmp_path / f"payload-{role}.json"
    path.write_text(json.dumps(payload if payload is not None else panel_payload(role)),
                    encoding="utf-8")
    env = os.environ.copy()
    env.update(extra_env or {})
    return subprocess.run([shutil.which("node"), str(RUNNER), str(path)],
                          capture_output=True, text=True, timeout=120, env=env)


@pytest.mark.parametrize("role", ["relay", "exit"])
def test_the_page_draws_and_every_button_works(tmp_path, role):
    """No view may throw, and every button must be wired to something."""
    proc = run_panel_js(tmp_path, role)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok" in proc.stdout
    drew = proc.stdout.split("drew:", 1)[1].splitlines()[0]
    for element in ("cards", "map", "mapping-body", "mapping-body-2", "wizard-body",
                    "certs-body", "settings-form"):
        assert element in drew, (element, drew)
    called = proc.stdout.split("called:", 1)[1].splitlines()[0]
    for path in ("/api/layout", "/api/config", "/api/tune", "/api/wizard",
                 "/api/login-token", "/api/update", "/api/logs",
                 "/api/certificates/issue"):
        assert path in called, (path, called)
    if role == "relay":                       # an exit has listeners, not ports
        assert "/api/mappings" in called, called
    else:
        assert "/api/setup-link" in called, called
        assert "/api/pair" in called, called


def test_the_page_boots_when_browser_storage_is_disabled(tmp_path):
    """A blocked localStorage must not abort the entire dashboard script."""
    proc = run_panel_js(tmp_path, extra_env={"PANEL_TEST_STORAGE": "blocked"})
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_stale_saved_view_falls_back_to_the_dashboard(tmp_path):
    """An unknown view from an old release must not hide every section."""
    proc = run_panel_js(tmp_path, extra_env={"PANEL_TEST_VIEW": "removed-view"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "stale saved view" not in proc.stdout


def test_unpaired_and_reverse_pairing_controls_call_their_apis(tmp_path):
    unpaired = panel_payload("relay")
    unpaired["panel"].update(paired=False, pairing_required=True, setup_link=False)
    proc = run_panel_js(tmp_path, "relay", payload=unpaired)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "/api/pair" in proc.stdout

    reverse = panel_payload("relay")
    reverse["panel"].update(paired=True, pairing_required=False,
                            reverse=True, setup_link=True)
    proc = run_panel_js(tmp_path, "relay", payload=reverse)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "/api/setup-link" in proc.stdout


def test_the_page_survives_a_panel_that_answers_nothing(tmp_path):
    """A panel that is down is a message, not a stack trace in the console."""
    proc = run_panel_js(tmp_path, "relay", payload={"ok": False, "error": "down"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # and it never calls anything it was not told about
    assert "no stub for" in proc.stdout or "called:" in proc.stdout


def test_the_payload_the_runner_uses_is_json_clean(tmp_path):
    clean = json.loads(json.dumps(panel_payload("relay")))
    assert clean["chart"]["in"] and clean["pool"] and clean["certificates"]
    proc = run_panel_js(tmp_path, "relay", payload=clean)
    assert proc.returncode == 0, proc.stdout + proc.stderr

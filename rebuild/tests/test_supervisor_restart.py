from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rebuild.supervisor import ManagedNode, NodeSupervisor


class ExitedProcess:
    pid = 4321

    def poll(self):
        return 17


def supervisor_with_crashed_entry(tmp_path):
    supervisor = NodeSupervisor(tmp_path)
    supervisor.autostart_roles = {"entry"}
    supervisor.nodes["entry"] = ManagedNode(
        "entry", ExitedProcess(), tmp_path / "entry.log", 0.0)
    return supervisor


def test_crashed_autostart_node_retries_with_backoff_and_opens_circuit(tmp_path, monkeypatch):
    supervisor = supervisor_with_crashed_entry(tmp_path)
    attempts = []

    def failed_start(role):
        attempts.append(role)
        raise RuntimeError("simulated startup failure")

    monkeypatch.setattr(supervisor, "start", failed_start)

    supervisor._monitor_once(now=0.0)
    assert supervisor.status()["entry"]["state"] == "restarting"
    assert supervisor._restart_after["entry"] == 1.0
    supervisor._monitor_once(now=0.99)
    assert attempts == []

    for now, expected_delay in ((1.0, 2.0), (3.0, 4.0), (7.0, 8.0), (15.0, 16.0)):
        supervisor._monitor_once(now=now)
        assert supervisor._restart_after["entry"] == now + expected_delay

    supervisor._monitor_once(now=31.0)
    assert attempts == ["entry"] * 5
    status = supervisor.status()["entry"]
    assert status["state"] == "stopped"
    assert "stopped after 5 attempts" in status["restart_error"]
    assert "entry" not in supervisor._restart_due


def test_successful_restart_clears_pending_state(tmp_path, monkeypatch):
    supervisor = supervisor_with_crashed_entry(tmp_path)
    attempts = []

    def successful_start(role):
        attempts.append(role)
        supervisor._restart_due.discard(role)
        supervisor._restart_after.pop(role, None)
        supervisor._restart_errors.pop(role, None)

    monkeypatch.setattr(supervisor, "start", successful_start)
    supervisor._monitor_once(now=0.0)
    supervisor._monitor_once(now=1.0)

    assert attempts == ["entry"]
    assert supervisor.status()["entry"]["state"] == "stopped"
    assert supervisor.status()["entry"]["restart_error"] is None


def test_failed_panel_autostart_is_retried(tmp_path, monkeypatch):
    supervisor = NodeSupervisor(tmp_path)
    supervisor.autostart_roles = {"entry"}
    attempts = []

    def failed_start(role):
        attempts.append(role)
        raise RuntimeError("simulated startup failure")

    monkeypatch.setattr(supervisor, "start", failed_start)
    assert supervisor.start_autostart() == {"entry": "simulated startup failure"}
    assert "entry" in supervisor._restart_due
    first_retry = supervisor._restart_after["entry"]

    supervisor._monitor_once(now=first_retry)
    assert attempts == ["entry", "entry"]
    assert supervisor.status()["entry"]["state"] == "restarting"


def test_manual_stop_cancels_a_scheduled_restart(tmp_path):
    supervisor = NodeSupervisor(tmp_path)
    supervisor._restart_due.add("entry")
    supervisor._restart_after["entry"] = 10.0
    supervisor._restart_errors["entry"] = "scheduled"

    assert supervisor.stop("entry") is True
    assert "entry" not in supervisor._restart_due
    assert "entry" not in supervisor._restart_after
    assert "entry" not in supervisor._restart_errors
    assert supervisor.stop("entry") is False

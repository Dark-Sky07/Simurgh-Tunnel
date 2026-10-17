"""The compiled engine under the Python operator surface.

The "engine" here is a small script that behaves like the Go one from the
supervisor's point of view: it takes ``role --home <dir>``, writes ``state.json``
and exits when it is asked to.  That is enough to test the parts that matter --
start, follow the state file, alert, restart after a crash, stop cleanly -- with
no compiler in the loop.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys

import pytest

from simurgh.config import RelayConfig
from simurgh.supervise import EngineFacade, EngineSupervisor
from simurgh.util import Home

pytestmark = pytest.mark.asyncio

ENGINE = r'''#!{python}
"""A stand-in for the compiled engine: write state.json, obey SIGTERM."""
import json, os, signal, sys, time

def write(home, role, state):
    path = os.path.join(home, "state.json")
    try:
        data = json.load(open(path))
    except Exception:
        data = {{}}
    data[role] = state
    data["updated"] = time.time()
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)

args = sys.argv[1:]
role = args[0] if args else "relay"
home = args[args.index("--home") + 1] if "--home" in args else "."
plan = {{}}
try:
    plan = json.load(open(os.path.join(home, "plan.json")))
except Exception:
    pass
with open(os.path.join(home, "runs.txt"), "a") as fh:
    fh.write("%d\n" % os.getpid())
print("fake engine: role=%s home=%s pid=%d" % (role, home, os.getpid()), flush=True)

stop = {{"asked": False}}
def on_term(*_):
    stop["asked"] = True
signal.signal(signal.SIGTERM, on_term)

steps = plan.get("steps") or [{{"after": 0.05, "state": plan.get("state", {{"connected": True}})}}]
for step in steps:
    time.sleep(float(step.get("after", 0.05)))
    state = {{"role": role, "engine": "go", "name": "fake", "connected": False,
             "uptime": 1.0}}
    state.update(step.get("state") or {{}})
    write(home, role, state)
    if step.get("exit") is not None:
        sys.exit(int(step["exit"]))
if plan.get("exit_code") is not None:
    sys.exit(int(plan["exit_code"]))
while not stop["asked"]:
    time.sleep(0.05)
with open(os.path.join(home, "stopped"), "w") as fh:
    fh.write("yes")
sys.exit(0)
'''


class FakeAlerts:
    """The recorder the supervisor builds instead of a real alerter."""

    def __init__(self):
        self.events: list[tuple[str, str]] = []
        self.running = False
        self.closed = False

    async def start(self):
        self.running = True

    async def stop(self):
        self.running = False
        self.closed = True

    def notify(self, event, message, level="info", **fields):
        self.events.append((event, message))

    def kinds(self) -> list[str]:
        return [event for event, _ in self.events]

    def text(self, event: str) -> list[str]:
        return [message for kind, message in self.events if kind == event]


@pytest.fixture()
def engine(tmp_path):
    """Write the fake engine and hand back its path."""
    path = tmp_path / "fake-engine"
    path.write_text(ENGINE.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return path


@pytest.fixture()
def alerts(monkeypatch):
    recorder = FakeAlerts()
    monkeypatch.setattr("simurgh.supervise.alerts_from_config",
                        lambda cfg, name="": recorder)
    return recorder


def _home(tmp_path) -> Home:
    home = Home(tmp_path / "simurgh")
    home.ensure()
    return home


def _supervisor(home, engine, cfg, **kw):
    return EngineSupervisor(home, "relay", str(engine), cfg, interval=0.05, **kw)


async def _wait_for(predicate, timeout=8.0):
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_the_engine_runs_and_is_announced(tmp_path, engine, alerts):
    home = _home(tmp_path)
    sup = _supervisor(home, engine, RelayConfig(token="tok", name="ir-1"))
    await sup.start()
    try:
        assert sup.alive and sup.pid > 0
        assert alerts.running is True
        assert alerts.kinds() == ["started"]
        assert "pid" in alerts.text("started")[0]
        assert await _wait_for(lambda: "connected" in alerts.kinds())
        assert "ir-1 is connected" in " ".join(alerts.text("connected"))
    finally:
        await sup.stop()
    assert sup.alive is False
    assert alerts.closed is True
    assert alerts.kinds()[-1] == "stopped"
    assert (home.path / "stopped").exists(), "the engine was not asked to stop"


async def test_the_engine_output_lands_in_the_role_log(tmp_path, engine, alerts):
    home = _home(tmp_path)
    sup = _supervisor(home, engine, RelayConfig(token="tok"))
    await sup.start()
    try:
        assert await _wait_for(lambda: home.relay_log.exists()
                               and "fake engine" in home.relay_log.read_text())
        assert sup.log_path == home.relay_log
    finally:
        await sup.stop()


async def test_a_tunnel_that_drops_and_returns_is_reported(tmp_path, engine,
                                                           alerts):
    home = _home(tmp_path)
    (home.path / "plan.json").write_text(json.dumps({
        "steps": [
            {"after": 0.1, "state": {"connected": True}},
            {"after": 0.2, "state": {"connected": False}},
            {"after": 0.2, "state": {"connected": True}},
        ],
    }))
    sup = _supervisor(home, engine, RelayConfig(token="tok", name="ir-1"))
    await sup.start()
    try:
        assert await _wait_for(lambda: alerts.kinds().count("disconnected") == 1)
        # ...and the third state write brings it back
        assert await _wait_for(lambda: alerts.kinds().count("connected") == 2)
        assert alerts.kinds().count("disconnected") == 1
        assert "lost its tunnel" in alerts.text("disconnected")[0]
    finally:
        await sup.stop()


async def test_a_mapping_that_hits_its_quota_is_reported_once(tmp_path, engine,
                                                              alerts):
    home = _home(tmp_path)
    quota = {"key": "tcp:0.0.0.0:443/127.0.0.1:443"}
    (home.path / "plan.json").write_text(json.dumps({
        "steps": [
            {"after": 0.1, "state": {"connected": True, "quotas": {
                quota["key"]: {"limit": 10485760, "period": "month",
                               "action": "block", "used": 10485760,
                               "exceeded": True}}}},
            {"after": 0.2, "state": {"connected": True, "quotas": {
                quota["key"]: {"limit": 10485760, "period": "month",
                               "action": "block", "used": 11000000,
                               "exceeded": True}}}},
            {"after": 0.2, "state": {"connected": True, "quotas": {
                quota["key"]: {"limit": 10485760, "period": "month",
                               "action": "block", "used": 0,
                               "exceeded": False}}}},
        ],
    }))
    sup = _supervisor(home, engine, RelayConfig(token="tok", name="ir-1"))
    await sup.start()
    try:
        assert await _wait_for(lambda: "quota" in alerts.kinds())
        await asyncio.sleep(0.5)              # two more state writes, no repeat
        assert alerts.kinds().count("quota") == 1
        message = alerts.text("quota")[0]
        assert "10.0 MB/month" in message and "block" in message
        # and after the period rolled over it can fire again next month
        assert sup._exceeded == set()
    finally:
        await sup.stop()


async def test_a_crashed_engine_is_restarted_and_reported(tmp_path, engine,
                                                          alerts):
    home = _home(tmp_path)
    (home.path / "plan.json").write_text(json.dumps({
        "steps": [{"after": 0.05, "state": {"connected": False}, "exit": 3}],
        "exit_code": None,
    }))
    sup = _supervisor(home, engine, RelayConfig(token="tok", name="ir-1"))
    await sup.start()
    first = sup.pid
    try:
        assert await _wait_for(lambda: sup.restarts == 1)
        assert sup.last_exit == 3
        assert await _wait_for(lambda: sup.pid != first and sup.alive)
        errors = alerts.text("error")
        assert errors and "exited (code 3)" in errors[0]
        assert "restarting in 0.5 s" in errors[0]
        # the engine really ran again: every run appends its own pid
        assert await _wait_for(
            lambda: len((home.path / "runs.txt").read_text().split()) >= 2)
    finally:
        await sup.stop()


async def test_an_engine_that_cannot_start_is_an_error(tmp_path, alerts):
    home = _home(tmp_path)
    missing = tmp_path / "not-here"
    sup = EngineSupervisor(home, "relay", str(missing), RelayConfig(token="tok"),
                           interval=0.05)
    with pytest.raises(OSError):
        await sup.start()
    assert alerts.text("error") and "cannot run the go engine" in alerts.text("error")[0]
    await sup.stop()


async def test_status_and_the_panel_facade(tmp_path, engine, alerts):
    home = _home(tmp_path)
    sup = _supervisor(home, engine, RelayConfig(token="tok", name="ir-1"))
    facade = sup.node
    assert isinstance(facade, EngineFacade)
    assert facade is sup.node                       # built once
    await sup.start()
    try:
        assert await _wait_for(lambda: sup.status().get("connected") is True)
        status = facade.status()
        assert status["name"] == "fake" and status["engine"] == "go"
        assert status["supervisor"]["pid"] == sup.pid
        assert status["supervisor"]["alive"] is True
        assert status["supervisor"]["restarts"] == 0
        assert status["supervisor"]["log"].endswith("relay.log")
        assert facade.port_conflicts() == []
        assert facade.cfg is sup.cfg
        assert facade.alerts is sup.alerts
        # an empty state file must not break the panel while the engine boots
        home.state.write_text("{ this is not json")
        assert sup.state() == {}
    finally:
        await sup.stop()


async def test_a_mapping_change_restarts_the_engine(tmp_path, engine, alerts):
    home = _home(tmp_path)
    sup = _supervisor(home, engine, RelayConfig(token="tok"))
    await sup.start()
    first = sup.pid
    try:
        await sup.node.rebind()                     # what the panel calls
        assert sup.pid != first and sup.alive
        assert sup.restarts == 0, "a deliberate restart is not a crash"
    finally:
        await sup.stop()


async def test_reconnect_is_fire_and_forget(tmp_path, engine, alerts):
    home = _home(tmp_path)
    sup = _supervisor(home, engine, RelayConfig(token="tok"))
    await sup.start()
    first = sup.pid
    try:
        sup.node.reconnect()                        # sync, as RelayNode.reconnect
        assert await _wait_for(lambda: sup.pid != first)
    finally:
        await sup.stop()


async def test_argv_and_the_exec_escape_hatch(tmp_path, engine, alerts):
    home = _home(tmp_path)
    sup = _supervisor(home, engine, RelayConfig(token="tok"))
    assert sup.argv() == [str(engine), "relay", "--home", str(home.path)]
    verbose = EngineSupervisor(home, "exit", str(engine), RelayConfig(token="t"),
                               verbose=True)
    assert verbose.argv()[-1] == "--verbose"
    assert verbose.log_path.name == "exit.log"


def test_the_cli_supervises_the_go_engine_by_default():
    """`simurgh relay` must not hand the process over unless asked to."""
    from simurgh.cli import build_parser

    args = build_parser().parse_args(["--home", "/tmp/x", "relay"])
    assert args.exec_engine is False
    args = build_parser().parse_args(["--home", "/tmp/x", "relay", "--exec"])
    assert args.exec_engine is True


async def test_stopping_twice_is_harmless(tmp_path, engine, alerts):
    home = _home(tmp_path)
    sup = _supervisor(home, engine, RelayConfig(token="tok"))
    await sup.start()
    await sup.stop()
    await sup.stop()
    assert sup.alive is False
    assert alerts.kinds().count("stopped") == 1

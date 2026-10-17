"""Run the compiled engine while the Python side keeps the operator surface.

``engine = "go"`` makes the Go binary the data plane.  Without supervision the
Python process simply ``exec``\\s it, and everything an operator lives on -- the
panel, ``/metrics``, the alerts -- goes away with it: the Go engine opens no
HTTP listener, it only writes ``state.json``.

This module keeps the engine as a *child* process instead:

* the panel, the Prometheus endpoint and the alert channels stay up,
* ``state.json`` (written every three seconds by the engine) is followed, so a
  tunnel that goes down and comes back is announced, and a mapping that hits its
  quota is reported once,
* a crashed engine is restarted with a growing backoff, and the exit code is
  sent to the operator instead of being buried in a journal nobody reads.

The child gets an ``EngineFacade`` in front of it, which is what the panel sees:
it looks like a node (``cfg``, ``status()``, ``rebind()``), so mapping edits and
the live numbers keep working exactly as they do with the Python engine.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from .alerts import alerts_from_config
from .util import get_logger, human_bytes

log = get_logger("simurgh.supervise")

#: a crash is retried sooner the first time and slower every time after
BACKOFF = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0)
#: an engine that stayed up this long is not a crash loop: forget the count
SETTLED = 60.0
#: how often the state file is read (the engine writes it every 3 s)
POLL = 2.0
#: grace period between SIGTERM and SIGKILL
STOP_TIMEOUT = 5.0


class EngineSupervisor:
    """Keep one compiled engine alive and tell the operator what it does."""

    def __init__(self, home, role: str, binary: str, cfg, *, verbose: bool = False,
                 interval: float = POLL):
        self.home = Path(getattr(home, "path", home))
        self.role = role
        self.binary = str(binary)
        self.cfg = cfg
        self.verbose = verbose
        self.interval = interval
        self.alerts = alerts_from_config(cfg, name=cfg.name or role)
        self.proc: asyncio.subprocess.Process | None = None
        self.pid = 0
        self.restarts = 0
        self.last_exit: int | None = None
        self.started_at = time.time()
        self._spawned_at = 0.0
        self._connected: bool | None = None
        self._exceeded: set[str] = set()
        self._task: asyncio.Task | None = None
        self._log_file = None
        self._stopping = False
        self._stopped = False

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        await self.alerts.start()
        self._log_file = open(self.log_path, "ab", buffering=0)   # noqa: SIM115
        await self._spawn()
        self._task = asyncio.ensure_future(self.watch())

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            self._task = None
        await self._terminate()
        self.alerts.notify("stopped",
                           f"{self.role} {self.cfg.name or ''} stopped "
                           f"(engine: go)".strip())
        await self.alerts.stop()
        await asyncio.sleep(0)
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:                                     # pragma: no cover
                pass
            self._log_file = None

    async def restart(self) -> None:
        """Stop and start the engine (the panel calls this after a change)."""
        await self._terminate()
        if self._stopping:
            return
        await self._spawn()

    @property
    def log_path(self) -> Path:
        return self.home / "logs" / f"{self.role}.log"

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def argv(self) -> list[str]:
        argv = [self.binary, self.role, "--home", str(self.home)]
        if self.verbose:
            argv.append("--verbose")
        return argv

    async def _spawn(self) -> None:
        if self._log_file is None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(self.log_path, "ab", buffering=0)   # noqa: SIM115
        argv = self.argv()
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *argv, stdout=self._log_file, stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL)
        except OSError as exc:
            self.last_exit = -1
            self.alerts.notify("error",
                               f"cannot run the go engine ({exc})", level="error")
            log.error("cannot run %s: %s", " ".join(argv), exc)
            raise
        self.pid = self.proc.pid or 0
        self._spawned_at = time.time()
        log.info("go engine started (pid %s): %s", self.pid, " ".join(argv))
        self.alerts.notify(
            "started", f"{self.role} {self.cfg.name or ''} started "
                       f"(engine: go, pid {self.pid})".strip(),
            pid=self.pid)

    async def _terminate(self) -> None:
        proc = self.proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()                      # the engine closes its sockets
        except ProcessLookupError:                # pragma: no cover - raced us
            return
        try:
            await asyncio.wait_for(proc.wait(), STOP_TIMEOUT)
        except asyncio.TimeoutError:              # pragma: no cover - hung engine
            log.warning("the go engine did not stop in %.0f s; killing it",
                        STOP_TIMEOUT)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
        self.last_exit = proc.returncode
        log.info("go engine stopped (pid %s, code %s)", self.pid, proc.returncode)

    # ------------------------------------------------------------ the watcher
    async def watch(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.interval)
            self._read_state()
            if self.proc is not None and self.proc.returncode is not None:
                await self._crashed()

    async def _crashed(self) -> None:
        proc = self.proc
        code = proc.returncode if proc is not None else -1
        if time.time() - self._spawned_at > SETTLED:
            self.restarts = 0
        self.restarts += 1
        self.last_exit = code
        delay = BACKOFF[min(self.restarts - 1, len(BACKOFF) - 1)]
        self.alerts.notify(
            "error", f"the go {self.role} engine exited (code {code}); "
                     f"restarting in {delay:g} s",
            level="error", exit_code=code, restarts=self.restarts)
        log.warning("the go engine exited with %s; restart %d in %g s",
                    code, self.restarts, delay)
        await asyncio.sleep(delay)
        if self._stopping:
            return
        await self._spawn()

    # ---------------------------------------------------------------- the state
    def state(self) -> dict:
        """What the engine wrote about itself (empty while it is starting)."""
        try:
            data = json.loads((self.home / "state.json").read_text())
        except (OSError, ValueError):
            return {}
        payload = data.get(self.role)
        return payload if isinstance(payload, dict) else {}

    def _read_state(self) -> None:
        state = self.state()
        if not state:
            return
        connected = bool(state.get("connected"))
        if self._connected is None:
            self._connected = connected
            if connected:
                self._announce_up(state)
        elif connected != self._connected:
            self._connected = connected
            if connected:
                self._announce_up(state)
            else:
                self.alerts.notify(
                    "disconnected",
                    f"{self.role} {self.cfg.name or ''} lost its tunnel".strip())
        for key, quota in (state.get("quotas") or {}).items():
            if not isinstance(quota, dict):
                continue
            exceeded = bool(quota.get("exceeded"))
            if exceeded and key not in self._exceeded:
                self._exceeded.add(key)
                limit = human_bytes(float(quota.get("limit") or 0))
                period = quota.get("period") or "month"
                action = quota.get("action") or "block"
                self.alerts.notify(
                    "quota", f"mapping {key} reached its {limit}/{period} quota "
                             f"({action})", level="warning", mapping=key,
                    used=quota.get("used"), limit=quota.get("limit"))
            elif not exceeded and key in self._exceeded:
                self._exceeded.discard(key)       # the period rolled over

    def _announce_up(self, state: dict) -> None:
        where = state.get("current_exit") or state.get("name") or ""
        rtt = state.get("rtt_ms")
        extra = f" (rtt {float(rtt):.0f} ms)" if rtt else ""
        self.alerts.notify(
            "connected", f"{self.role} {self.cfg.name or ''} is connected "
                         f"to {where}{extra}".strip(), rtt_ms=rtt)

    # ------------------------------------------------------------- panel view
    def status(self) -> dict:
        """State file payload plus what only the supervisor knows."""
        state = dict(self.state())
        state["engine"] = state.get("engine") or "go"
        state["supervisor"] = {
            "pid": self.pid,
            "alive": self.alive,
            "restarts": self.restarts,
            "last_exit": self.last_exit,
            "uptime": time.time() - self.started_at,
            "log": str(self.log_path),
            "engine": "go",
        }
        return state

    @property
    def node(self) -> "EngineFacade":
        """A node-shaped object for the panel (constructed once)."""
        facade = getattr(self, "_facade", None)
        if facade is None:
            facade = EngineFacade(self)
            self._facade = facade
        return facade


class EngineFacade:
    """What ``Panel`` sees when the data plane is the compiled engine.

    The panel edits ``cfg`` and calls ``rebind()`` after a change; here that
    means "restart the engine so the new mapping is live", which is exactly
    what the same click does with the Python engine.
    """

    def __init__(self, supervisor: EngineSupervisor):
        self.supervisor = supervisor
        self.cfg = supervisor.cfg
        self.alerts = supervisor.alerts

    def status(self) -> dict:
        return self.supervisor.status()

    async def rebind(self) -> None:
        await self.supervisor.restart()

    def reconnect(self) -> None:
        asyncio.ensure_future(self.supervisor.restart())

    def port_conflicts(self) -> list[str]:
        return []

    @property
    def connected(self):
        """Whether the tunnel is up, in the shape the rest of the code expects."""
        return bool(self.supervisor.state().get("connected"))

"""In-memory supervision and persistent panel-start preferences."""

from __future__ import annotations

import json
import logging
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .notifications import WebhookNotifier
from .tunnel import ConfigError, EntryConfig, ExitConfig, load_config
from .wizard import save_config

LOG = logging.getLogger("simurgh.rebuild.supervisor")
RESTART_BASE_DELAY = 1.0
RESTART_MAX_DELAY = 30.0
RESTART_WINDOW = 300.0
RESTART_MAX_ATTEMPTS = 5


@dataclass
class ManagedNode:
    role: str
    process: subprocess.Popen
    log_path: Path
    started_at: float


class NodeSupervisor:
    """Own only child processes launched by this instance; never trust PID files."""

    def __init__(self, config_dir: Path, log_dir: Path | None = None):
        self.config_dir = Path(config_dir)
        self.log_dir = Path(log_dir) if log_dir else self.config_dir / "logs"
        self.autostart_path = self.config_dir / "autostart.json"
        self.nodes: dict[str, ManagedNode] = {}
        self._lock = threading.RLock()
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._restart_due: set[str] = set()
        self._restart_after: dict[str, float] = {}
        self._restart_history: dict[str, list[float]] = {}
        self._restart_errors: dict[str, str] = {}
        self.notifier = WebhookNotifier(self.config_dir / "notifications.json")
        try:
            self.autostart_roles = self._load_autostart()
            self.autostart_error: str | None = None
        except ConfigError as exc:
            self.autostart_roles = set()
            self.autostart_error = str(exc)
            LOG.error("cannot load node auto-start preferences: %s", exc)

    def _load_autostart(self) -> set[str]:
        try:
            raw = json.loads(self.autostart_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot read auto-start state {self.autostart_path}: {exc}") from exc
        roles = raw.get("roles") if isinstance(raw, dict) else None
        if (not isinstance(roles, list)
                or any(role not in ("entry", "exit") for role in roles)
                or len(set(roles)) != len(roles)):
            raise ConfigError("auto-start state must contain a unique list of entry/exit roles")
        return set(roles)

    def _validate(self, role: str) -> Path:
        if role not in ("entry", "exit"):
            raise ValueError("role must be entry or exit")
        candidate = self.config_dir / f"{role}.json"
        if candidate.is_symlink():
            raise ConfigError(f"refusing symlinked node configuration: {candidate}")
        path = candidate.resolve()
        config = load_config(path)
        expected = EntryConfig if role == "entry" else ExitConfig
        if not isinstance(config, expected):
            raise ConfigError(f"{path} does not contain an {role} config")
        return path

    def start(self, role: str) -> ManagedNode:
        with self._lock:
            config_path = self._validate(role)
            existing = self.nodes.get(role)
            if existing and existing.process.poll() is None:
                raise RuntimeError(f"{role} is already running (pid {existing.process.pid})")
            if existing:
                self.nodes.pop(role, None)

            if self.log_dir.is_symlink():
                raise OSError(f"refusing symlinked node log directory: {self.log_dir}")
            self.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.log_dir.is_symlink() or not self.log_dir.is_dir():
                raise OSError(f"node log path is not a safe directory: {self.log_dir}")
            os.chmod(self.log_dir, 0o700)
            log_path = self.log_dir / f"{role}.log"
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            fd = os.open(log_path, flags, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError(f"refusing non-regular node log file: {log_path}")
                os.fchmod(fd, 0o600)
            except BaseException:
                os.close(fd)
                raise
            output = os.fdopen(fd, "ab", buffering=0)
            try:
                process = subprocess.Popen(
                    [sys.executable, "-m", "rebuild.tunnel", "--config", str(config_path)],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    start_new_session=(os.name == "posix"),
                )
            finally:
                output.close()
            node = ManagedNode(role, process, log_path, time.time())
            self.nodes[role] = node
            time.sleep(0.05)
            if process.poll() is not None:
                self.nodes.pop(role, None)
                raise RuntimeError(f"{role} exited during startup; inspect {log_path}")
            self._restart_due.discard(role)
            self._restart_after.pop(role, None)
            self._restart_errors.pop(role, None)
            self.notifier.notify("node_started", role)
            return node

    def set_webhook(self, url: str) -> None:
        self.notifier.set_url(url)

    def set_autostart(self, role: str, enabled: bool) -> None:
        if role not in ("entry", "exit"):
            raise ValueError("role must be entry or exit")
        with self._lock:
            if self.autostart_error:
                raise ConfigError(self.autostart_error)
            updated = set(self.autostart_roles)
            if enabled:
                self._validate(role)
                updated.add(role)
            else:
                updated.discard(role)
            if updated == self.autostart_roles:
                return
            backup = save_config(self.autostart_path, {"roles": sorted(updated)})
            self.autostart_roles = updated
            self.autostart_error = None
            if not enabled:
                self._restart_due.discard(role)
                self._restart_after.pop(role, None)
                self._restart_history.pop(role, None)
                self._restart_errors.pop(role, None)
            if backup:
                LOG.info("previous auto-start preferences backed up to %s", backup)

    def start_autostart(self) -> dict[str, str]:
        failures = {}
        with self._lock:
            roles = sorted(self.autostart_roles)
        for role in roles:
            try:
                self.start(role)
            except (ConfigError, RuntimeError, OSError) as exc:
                failures[role] = str(exc)
                with self._lock:
                    self._restart_due.add(role)
                    self._restart_after[role] = time.monotonic() + RESTART_BASE_DELAY
                    self._restart_errors[role] = (
                        f"auto-start failed: {exc}; retrying in {RESTART_BASE_DELAY:g} seconds")
                self.notifier.notify("autostart_failed", role)
                LOG.error("could not auto-start %s: %s", role, exc)
        return failures

    def _monitor_once(self, now: float | None = None) -> None:
        """Restart auto-start nodes after crashes, using bounded exponential backoff."""
        current = time.monotonic() if now is None else now
        with self._lock:
            for role in sorted(self.autostart_roles):
                node = self.nodes.get(role)
                if node is not None:
                    code = node.process.poll()
                    if code is None:
                        if time.time() - node.started_at >= RESTART_WINDOW:
                            self._restart_history.pop(role, None)
                            self._restart_errors.pop(role, None)
                        continue
                    if role not in self._restart_due:
                        history = [timestamp for timestamp in self._restart_history.get(role, [])
                                   if current - timestamp < RESTART_WINDOW]
                        self._restart_history[role] = history
                        if len(history) >= RESTART_MAX_ATTEMPTS:
                            self.nodes.pop(role, None)
                            self._restart_errors[role] = (
                                f"auto-restart stopped after {RESTART_MAX_ATTEMPTS} attempts "
                                f"in {int(RESTART_WINDOW)} seconds (exit code {code})")
                            self.notifier.notify("restart_circuit_open", role)
                            LOG.error("%s", self._restart_errors[role])
                            continue
                        delay = min(RESTART_BASE_DELAY * (2 ** len(history)),
                                    RESTART_MAX_DELAY)
                        self._restart_due.add(role)
                        self._restart_after[role] = current + delay
                        self._restart_errors[role] = (
                            f"exited with code {code}; retrying in {delay:g} seconds")
                        self.notifier.notify("node_crashed", role)
                        LOG.warning("%s node exited unexpectedly; retry scheduled in %g seconds",
                                    role, delay)

                if role not in self._restart_due or current < self._restart_after.get(role, current):
                    continue
                history = [timestamp for timestamp in self._restart_history.get(role, [])
                           if current - timestamp < RESTART_WINDOW]
                if len(history) >= RESTART_MAX_ATTEMPTS:
                    self._restart_due.discard(role)
                    self._restart_after.pop(role, None)
                    self._restart_errors[role] = (
                        f"auto-restart stopped after {RESTART_MAX_ATTEMPTS} attempts "
                        f"in {int(RESTART_WINDOW)} seconds")
                    self.notifier.notify("restart_circuit_open", role)
                    LOG.error("%s", self._restart_errors[role])
                    continue
                history.append(current)
                self._restart_history[role] = history
                delay = min(RESTART_BASE_DELAY * (2 ** len(history)), RESTART_MAX_DELAY)
                self._restart_after[role] = current + delay
                try:
                    self.start(role)
                except (ConfigError, RuntimeError, OSError) as exc:
                    if len(history) >= RESTART_MAX_ATTEMPTS:
                        self._restart_due.discard(role)
                        self._restart_after.pop(role, None)
                        self._restart_errors[role] = (
                            f"auto-restart stopped after {RESTART_MAX_ATTEMPTS} attempts "
                            f"in {int(RESTART_WINDOW)} seconds; last error: {exc}")
                    else:
                        self._restart_errors[role] = f"restart attempt {len(history)} failed: {exc}"
                    self.notifier.notify("restart_failed", role)
                    LOG.error("could not restart %s: %s", role, exc)

    def _monitor_loop(self) -> None:
        while not self._monitor_stop.wait(0.5):
            try:
                self._monitor_once()
            except Exception:
                LOG.exception("node supervision check failed")

    def start_monitor(self) -> None:
        with self._lock:
            if self._monitor_thread and self._monitor_thread.is_alive():
                return
            self._monitor_stop.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, name="simurgh-node-supervisor", daemon=True)
            self._monitor_thread.start()

    def status(self) -> dict[str, dict]:
        with self._lock:
            result = {}
            for role in ("entry", "exit"):
                node = self.nodes.get(role)
                if node is None:
                    result[role] = {
                        "state": "restarting" if role in self._restart_due else "stopped",
                        "pid": None, "log": None,
                        "autostart": role in self.autostart_roles,
                        "restart_error": self._restart_errors.get(role),
                    }
                    continue
                code = node.process.poll()
                result[role] = {
                    "state": ("running" if code is None else
                              "restarting" if role in self._restart_due else "stopped"),
                    "pid": node.process.pid if code is None else None,
                    "exit_code": code,
                    "started_at": node.started_at,
                    "log": str(node.log_path),
                    "autostart": role in self.autostart_roles,
                    "restart_error": self._restart_errors.get(role),
                }
            if self.autostart_error:
                result["error"] = {"state": "invalid-autostart", "message": self.autostart_error}
            return result

    def stop(self, role: str, timeout: float = 5.0) -> bool:
        if role not in ("entry", "exit"):
            raise ValueError("role must be entry or exit")
        with self._lock:
            node = self.nodes.get(role)
            if node is None:
                pending = role in self._restart_due
                self._restart_due.discard(role)
                self._restart_after.pop(role, None)
                self._restart_history.pop(role, None)
                self._restart_errors.pop(role, None)
                return pending
            process = node.process
            if process.poll() is None:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:  # pragma: no cover - this product currently targets Linux
                        process.terminate()
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:  # pragma: no cover
                        process.kill()
                    process.wait(timeout=timeout)
                except ProcessLookupError:
                    process.wait(timeout=timeout)
            self.nodes.pop(role, None)
            self._restart_due.discard(role)
            self._restart_after.pop(role, None)
            self._restart_history.pop(role, None)
            self._restart_errors.pop(role, None)
            return True

    def stop_all(self) -> None:
        self._monitor_stop.set()
        thread = self._monitor_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._monitor_thread = None
        with self._lock:
            for role in tuple(self.nodes):
                try:
                    self.stop(role)
                except (OSError, subprocess.SubprocessError):
                    LOG.exception("could not stop managed %s process", role)
        self.notifier.close()

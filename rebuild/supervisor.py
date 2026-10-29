"""In-memory supervision and persistent panel-start preferences."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .tunnel import ConfigError, EntryConfig, ExitConfig, load_config
from .wizard import save_config

LOG = logging.getLogger("simurgh.rebuild.supervisor")


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
        path = (self.config_dir / f"{role}.json").resolve()
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

            self.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.chmod(self.log_dir, 0o700)
            except OSError:
                pass
            log_path = self.log_dir / f"{role}.log"
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.chmod(log_path, 0o600)
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
            return node

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
                LOG.error("could not auto-start %s: %s", role, exc)
        return failures

    def status(self) -> dict[str, dict]:
        with self._lock:
            result = {}
            for role in ("entry", "exit"):
                node = self.nodes.get(role)
                if node is None:
                    result[role] = {
                        "state": "stopped", "pid": None, "log": None,
                        "autostart": role in self.autostart_roles,
                    }
                    continue
                code = node.process.poll()
                result[role] = {
                    "state": "running" if code is None else "stopped",
                    "pid": node.process.pid if code is None else None,
                    "exit_code": code,
                    "started_at": node.started_at,
                    "log": str(node.log_path),
                    "autostart": role in self.autostart_roles,
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
                return False
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
            return True

    def stop_all(self) -> None:
        with self._lock:
            for role in tuple(self.nodes):
                try:
                    self.stop(role)
                except (OSError, subprocess.SubprocessError):
                    LOG.exception("could not stop managed %s process", role)

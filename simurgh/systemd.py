"""systemd glue: unit files, service control and status.

Everything here degrades gracefully: on a machine without systemd the
functions simply report that, and the caller falls back to running the node in
the foreground (``simurgh relay`` / ``simurgh exit``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .util import Home

SERVICES = ("simurgh-exit", "simurgh-relay")

UNIT_DIR = Path("/etc/systemd/system")
USER_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

_UNIT_TEMPLATE = """\
[Unit]
Description=Simurgh Tunnel ({role}){name}
Documentation=https://github.com/Dark-Sky07/Simurgh-Tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
{user_line}Environment=SIMURGH_HOME={home}
Environment=PYTHONUNBUFFERED=1
ExecStart={exec_start}
Restart=always
RestartSec=3
LimitNOFILE=1048576
TimeoutStopSec=15
KillMode=mixed
# A busy relay keeps one session per user, so memory grows with the user
# count: 1G/2G keeps a thousand users comfortable without hiding a leak.
MemoryHigh=1G
MemoryMax=2G
TasksMax=8192
StandardOutput=append:{log}
StandardError=append:{log}

[Install]
WantedBy=multi-user.target
"""


def available() -> bool:
    """True when a usable systemd is present (and we may talk to it)."""
    if not shutil.which("systemctl"):
        return False
    try:
        probe = subprocess.run(
            ["systemctl", "is-system-running"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    out = (probe.stdout + probe.stderr).strip().lower()
    # "running" / "degraded" mean we can use it; "offline"/"unknown" mean not.
    return out in ("running", "degraded", "maintenance")


def _run(args: list[str], timeout: float = 60.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def exec_start_for(home: Home, role: str) -> str:
    """The ExecStart line: prefer the installed console script."""
    exe = shutil.which("simurgh")
    if not exe:
        # running from a source checkout: `python3 -m simurgh ...`
        py = shutil.which("python3") or "python3"
        return f"{py} -m simurgh {role} --home {home.path}"
    return f"{exe} {role} --home {home.path}"


def render_unit(home: Home, role: str, name: str = "", user: str | None = None) -> str:
    user_line = f"User={user}\n" if user else ""
    log = home.logs / ("exit.log" if role == "exit" else "relay.log")
    title = f" - {name}" if name else ""
    return _UNIT_TEMPLATE.format(
        role=role, name=title, home=home.path,
        exec_start=exec_start_for(home, role),
        user_line=user_line, log=log,
    )


def unit_path(service: str) -> Path:
    return UNIT_DIR / f"{service}.service"


def write_units(home: Home, roles: tuple[str, ...] = ("exit", "relay"),
                user: str | None = None) -> list[Path]:
    """Write unit files for the given roles.  Returns the files written."""
    written: list[Path] = []
    for role in roles:
        service = f"simurgh-{role}"
        text = render_unit(home, role, name=home.path.name, user=user)
        path = unit_path(service)
        path.write_text(text)
        os.chmod(path, 0o644)
        written.append(path)
    return written


def daemon_reload() -> bool:
    code, _ = _run(["systemctl", "daemon-reload"])
    return code == 0


def control(action: str, services: tuple[str, ...] = SERVICES) -> dict[str, str]:
    """Run ``systemctl <action>`` for the services; returns {service: output}."""
    out: dict[str, str] = {}
    for svc in services:
        code, text = _run(["systemctl", action, svc])
        out[svc] = "ok" if code == 0 else text or f"exit {code}"
    return out


def service_state(service: str) -> dict:
    """Live state of one unit (empty dict when systemd cannot tell us)."""
    code, text = _run([
        "systemctl", "show", service,
        "--property=ActiveState,SubState,UnitFileState,MainPID,ActiveEnterTimestamp",
    ], timeout=15)
    if code != 0:
        return {}
    state: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            state[key] = value
    return state


def install_services(home: Home, roles: tuple[str, ...] = ("exit", "relay"),
                     enable: bool = True, start: bool = True,
                     user: str | None = None) -> dict:
    """Write, enable and start the units.  Returns a human-readable report."""
    report: dict = {"systemd": available(), "units": [], "actions": {}}
    if not report["systemd"]:
        return report
    for path in write_units(home, roles, user=user):
        report["units"].append(str(path))
    daemon_reload()
    services = tuple(f"simurgh-{r}" for r in roles)
    if enable:
        report["actions"].update(control("enable", services))
    if start:
        report["actions"].update(control("restart", services))
    return report


def uninstall_services(roles: tuple[str, ...] = ("exit", "relay")) -> dict:
    services = tuple(f"simurgh-{r}" for r in roles)
    report: dict = {"actions": {}}
    report["actions"].update(control("stop", services))
    report["actions"].update(control("disable", services))
    for svc in services:
        path = unit_path(svc)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover
            report["actions"][svc] = f"remove failed: {exc}"
    try:
        daemon_reload()
    except Exception:  # pragma: no cover
        pass
    return report


def journal_tail(service: str, lines: int = 80) -> str:
    if not shutil.which("journalctl"):
        return ""
    code, text = _run(["journalctl", "-u", service, "-n", str(lines), "--no-pager"], timeout=20)
    return text if code == 0 else ""

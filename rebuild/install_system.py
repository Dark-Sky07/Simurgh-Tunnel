"""Root-owned, non-destructive system installer used by install.sh --system."""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path

ACCOUNT = "simurgh-next"
SERVICE = "simurgh-next-panel.service"
MANAGED = "Managed by Simurgh Tunnel independent rebuild"
MODULES = ("__init__.py", "menu.py", "panel.py", "paths.py", "supervisor.py", "tunnel.py", "wizard.py")
COMMANDS = ("simurgh-next", "simurgh-next-panel", "simurgh-next-setup", "simurgh-next-node")


class InstallError(RuntimeError):
    pass


def run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(argv, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise InstallError(f"cannot run {argv[0]}: {exc}") from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise InstallError(f"{argv[0]} failed ({result.returncode}): {detail}")
    return result


def _version(source: Path) -> str:
    text = (source / "tunnel.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*"([0-9]+(?:\.[0-9]+){2})"\s*$', text, re.MULTILINE)
    if not match:
        raise InstallError("cannot determine a valid three-part rebuild version")
    return match.group(1)


def _copy_release(source: Path, release: Path, python: str, app_root: Path,
                  config_dir: Path) -> None:
    package = release / "rebuild"
    package.mkdir(parents=True, mode=0o755)
    for name in MODULES:
        shutil.copyfile(source / name, package / name)
        (package / name).chmod(0o644)
    shutil.copytree(source / "examples", package / "examples")
    for example in (package / "examples").glob("*.json"):
        example.chmod(0o644)
    for name in ("README.md", "SETUP.md", "PROTOCOL.md"):
        shutil.copyfile(source / name, release / name)
    bin_dir = release / "bin"
    bin_dir.mkdir(mode=0o755)
    modules = {
        "simurgh-next": "rebuild.menu",
        "simurgh-next-panel": "rebuild.panel",
        "simurgh-next-setup": "rebuild.wizard",
        "simurgh-next-node": "rebuild.tunnel",
    }
    for name, module in modules.items():
        script = ("#!/bin/sh\n"
                  f"export PYTHONPATH=\"{app_root}/current${{PYTHONPATH:+:$PYTHONPATH}}\"\n"
                  f"export SIMURGH_NEXT_CONFIG_DIR=\"{config_dir}\"\n"
                  f"exec \"{python}\" -m {module} \"$@\"\n")
        path = bin_dir / name
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)


def _backup_tree(config_dir: Path, backup_dir: Path) -> Path | None:
    if not config_dir.exists() or not any(config_dir.iterdir()):
        return None
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_dir, 0o700)
    name = dt.datetime.now(dt.timezone.utc).strftime("config-%Y%m%dT%H%M%S")
    archive = backup_dir / f"{name}-{uuid.uuid4().hex[:8]}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for child in config_dir.iterdir():
            if child.name != "logs":
                tar.add(child, arcname=child.name, recursive=True)
    archive.chmod(0o600)
    return archive


def _has_symlinks(directory: Path) -> bool:
    return any(path.is_symlink() for path in directory.rglob("*"))


def _systemd_unit(python: str, config_dir: Path, app_root: Path, state_dir: Path) -> str:
    return f"""# {MANAGED}; local edits are backed up on update.
[Unit]
Description=Simurgh Tunnel independent rebuild panel and managed nodes
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={ACCOUNT}
Group={ACCOUNT}
WorkingDirectory={app_root}/current
Environment=SIMURGH_NEXT_CONFIG_DIR={config_dir}
ExecStart={python} -m rebuild.panel --config-dir={config_dir}
Restart=always
RestartSec=3
TimeoutStopSec=45
KillMode=control-group
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={config_dir} {state_dir}
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
"""


def _restore_link(python: str, current: Path, target: str | None) -> None:
    if target is None:
        current.unlink(missing_ok=True)
        return
    temporary = current.with_name(f"current.rollback.{os.getpid()}")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, current)


def install(source: Path, *, test_root: Path | None = None) -> int:
    source = source.resolve()
    if test_root is None and os.geteuid() != 0:
        raise InstallError("system installation requires root; use the user installer otherwise")
    if test_root is not None and os.environ.get("SIMURGH_NEXT_TEST_INSTALLER") != "1":
        raise InstallError("test root is permitted only for installer tests")

    def target(path: str) -> Path:
        return (test_root / path.lstrip("/")) if test_root else Path(path)

    app_root = target("/opt/simurgh-next")
    config_dir = target("/etc/simurgh-next")
    unit_file = target(f"/etc/systemd/system/{SERVICE}")
    backup_root = target("/var/backups/simurgh-next")
    state_dir = target("/var/lib/simurgh-next")
    bin_dir = target("/usr/local/bin")
    systemd_root = target("/etc/systemd/system")

    for directory in (app_root, app_root / "releases", config_dir, unit_file,
                      unit_file.parent, backup_root, state_dir, bin_dir):
        if directory.is_symlink():
            raise InstallError(f"refusing symlinked install path: {directory}")
    if app_root.exists() and not app_root.is_dir():
        raise InstallError(f"application path is not a directory: {app_root}")
    if app_root.exists() and not test_root:
        app_info = app_root.stat()
        if app_info.st_uid != 0 or app_info.st_mode & 0o022:
            raise InstallError(f"existing application directory must be root-owned and not group/world writable: {app_root}")
    if config_dir.exists() and not config_dir.is_dir():
        raise InstallError(f"configuration path is not a directory: {config_dir}")
    if unit_file.exists() and MANAGED not in unit_file.read_text(encoding="utf-8"):
        raise InstallError(f"refusing to overwrite unmanaged unit: {unit_file}")

    app_root.mkdir(parents=True, exist_ok=True, mode=0o755)
    releases = app_root / "releases"
    releases.mkdir(mode=0o755, exist_ok=True)
    current = app_root / "current"
    previous = None
    if current.exists() or current.is_symlink():
        if not current.is_symlink():
            raise InstallError(f"refusing to replace non-symlink release pointer: {current}")
        previous = os.readlink(current)
        expected = str(releases) + os.sep
        if not previous.startswith(expected):
            raise InstallError(f"refusing unmanaged release pointer: {current} -> {previous}")

    python = shutil.which("python3")
    if not python:
        raise InstallError("Python 3 is required")
    if sys.version_info < (3, 10):  # noqa: UP036
        raise InstallError("Python 3.10 or newer is required")
    version = _version(source)
    release = releases / f"{version}-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"

    for name in COMMANDS:
        wrapper = bin_dir / name
        managed_target = str(app_root / "current" / "bin" / name)
        if wrapper.exists() and not wrapper.is_symlink():
            raise InstallError(f"refusing to overwrite command: {wrapper}")
        if wrapper.is_symlink() and os.readlink(wrapper) != managed_target:
            raise InstallError(f"refusing to replace unrelated command symlink: {wrapper}")

    for command in ("systemctl", "getent", "chown"):
        if not shutil.which(command):
            raise InstallError(f"required system command is missing: {command}")
    if not test_root and not Path("/run/systemd/system").exists():
        raise InstallError("systemd is not running; system installation currently requires systemd")

    if run(["getent", "passwd", ACCOUNT], check=False).returncode:
        run(["useradd", "--system", "--user-group", "--create-home",
             "--home-dir", str(state_dir), "--shell", "/usr/sbin/nologin", ACCOUNT])
    group_result = run(["getent", "group", ACCOUNT], check=False)
    if group_result.returncode:
        raise InstallError(f"system group {ACCOUNT} is missing after checking the account")

    backup = _backup_tree(config_dir, backup_root)
    if config_dir.exists() and _has_symlinks(config_dir):
        suffix = f"; backup: {backup}" if backup else ""
        raise InstallError(f"config contains symlinks; copy certificate files into {config_dir}/tls before retrying{suffix}")
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    (config_dir / "tls").mkdir(mode=0o750, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    if not test_root:
        run(["chown", "-R", f"{ACCOUNT}:{ACCOUNT}", str(config_dir)])
        run(["chown", f"{ACCOUNT}:{ACCOUNT}", str(state_dir)])
    for path in (config_dir, config_dir / "tls", state_dir):
        path.chmod(0o750)
    for path in config_dir.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o750)
        elif path.is_file() and not path.is_symlink():
            path.chmod(0o600)

    release.mkdir(mode=0o755)
    _copy_release(source, release, python, app_root, config_dir)
    temporary = app_root / f"current.new.{os.getpid()}"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(release)
    os.replace(temporary, current)

    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in COMMANDS:
        wrapper = bin_dir / name
        if not wrapper.is_symlink():
            wrapper.symlink_to(app_root / "current" / "bin" / name)
    systemd_root.mkdir(parents=True, exist_ok=True)
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_root.chmod(0o700)
    unit_backup = None
    if unit_file.exists():
        unit_backup = backup_root / f"{SERVICE}.{uuid.uuid4().hex[:10]}.bak"
        shutil.copy2(unit_file, unit_backup)
        unit_backup.chmod(0o600)
    unit_temp = unit_file.with_name(f".{SERVICE}.{os.getpid()}.tmp")
    unit_temp.write_text(_systemd_unit(python, config_dir, app_root, state_dir), encoding="utf-8")
    unit_temp.chmod(0o644)
    os.replace(unit_temp, unit_file)

    def rollback() -> None:
        if unit_backup:
            shutil.copy2(unit_backup, unit_file)
        else:
            unit_file.unlink(missing_ok=True)
        _restore_link(python, current, previous)
        run(["systemctl", "daemon-reload"], check=False)
        if previous:
            run(["systemctl", "restart", SERVICE], check=False)
        else:
            run(["systemctl", "disable", "--now", SERVICE], check=False)

    try:
        run(["systemctl", "daemon-reload"])
        active = run(["systemctl", "is-active", "--quiet", SERVICE], check=False).returncode == 0
        run(["systemctl", "restart", SERVICE] if active
            else ["systemctl", "enable", "--now", SERVICE])
    except InstallError:
        rollback()
        raise

    print(f"Installed Simurgh rebuild {version} for both roles.")
    print(f"Config directory: {config_dir} (existing files backed up before permission changes)")
    if backup:
        print(f"Configuration archive: {backup}")
    print("Role selection remains in the loopback panel or terminal menu; no role was inferred.")
    print(f"Panel service: systemctl status {SERVICE}")
    print("The legacy /etc/simurgh directory was not accessed or modified.")
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: install_system.py SOURCE_DIR", file=sys.stderr)
        return 2
    test_root = os.environ.get("SIMURGH_NEXT_SYSTEM_TEST_ROOT")
    try:
        install(Path(sys.argv[1]), test_root=Path(test_root) if test_root else None)
    except (InstallError, OSError, tarfile.TarError) as exc:
        print(f"system install failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

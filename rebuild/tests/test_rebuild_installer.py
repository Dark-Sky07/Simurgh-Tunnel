from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rebuild.paths import default_config_dir

INSTALLER = ROOT / "install.sh"


def _systemd_escape(value: str) -> str:
    return value.replace("%", "%%").replace("\\", "\\x5c").replace('"', "\\x22").replace(" ", "\\x20")


def _installer_env(tmp_path, *, with_systemctl=False):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    prefix = tmp_path / "install area" / "simurgh-next"
    bindir = tmp_path / "bin"
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "SIMURGH_NEXT_PREFIX": str(prefix),
        "SIMURGH_NEXT_BIN_DIR": str(bindir),
    })
    if with_systemctl:
        fake_bin = tmp_path / "fake-bin"
        fake_bin.mkdir()
        active_flag = tmp_path / "service-active"
        log = tmp_path / "systemctl.log"
        fake_systemctl = fake_bin / "systemctl"
        fake_systemctl.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n"
            "case \"$*\" in\n"
            "  '--user show-environment') exit 0 ;;\n"
            "  '--user is-active --quiet simurgh-next-panel.service') [ -e \"$SYSTEMCTL_ACTIVE_FLAG\" ] ;;\n"
            "  '--user enable --now simurgh-next-panel.service') touch \"$SYSTEMCTL_ACTIVE_FLAG\" ;;\n"
            "  '--user restart simurgh-next-panel.service') [ \"${SYSTEMCTL_FAIL_RESTART:-0}\" = 1 ] && exit 1; touch \"$SYSTEMCTL_ACTIVE_FLAG\" ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n"
        )
        fake_systemctl.chmod(0o755)
        env.update({
            "PATH": f"{fake_bin}:{env['PATH']}",
            "SYSTEMCTL_LOG": str(log),
            "SYSTEMCTL_ACTIVE_FLAG": str(active_flag),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "SIMURGH_NEXT_CONFIG_DIR": str(home / ".config/config dir/simurgh-next"),
        })
    return env


def test_config_directory_default_and_override(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SIMURGH_NEXT_CONFIG_DIR", raising=False)
    assert default_config_dir() == home / ".config/simurgh-next"
    override = tmp_path / "custom-config"
    monkeypatch.setenv("SIMURGH_NEXT_CONFIG_DIR", str(override))
    assert default_config_dir() == override


def test_installer_refuses_to_overwrite_user_command(tmp_path):
    home = tmp_path / "home"
    bindir = home / ".local/bin"
    bindir.mkdir(parents=True)
    command = bindir / "simurgh-next"
    command.write_text("preserve this launcher\n")
    env = _installer_env(tmp_path)
    env["SIMURGH_NEXT_BIN_DIR"] = str(bindir)

    result = subprocess.run(["sh", str(INSTALLER)], cwd=ROOT, env=env,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert command.read_text() == "preserve this launcher\n"
    assert "Refusing to overwrite" in result.stderr


def test_installer_refuses_to_replace_an_unmanaged_user_service(tmp_path):
    env = _installer_env(tmp_path, with_systemctl=True)
    service = Path(env["XDG_CONFIG_HOME"]) / "systemd/user/simurgh-next-panel.service"
    service.parent.mkdir(parents=True)
    service.write_text("# local administrator unit\n")

    result = subprocess.run(["sh", str(INSTALLER), "--enable-service"], cwd=ROOT,
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert service.read_text() == "# local administrator unit\n"
    assert "Refusing to overwrite an unmanaged service" in result.stderr


def test_user_installer_is_role_agnostic_repeatable_and_keeps_old_release(tmp_path):
    env = _installer_env(tmp_path)
    home = Path(env["HOME"])
    prefix = Path(env["SIMURGH_NEXT_PREFIX"])
    bindir = Path(env["SIMURGH_NEXT_BIN_DIR"])
    config_dir = home / ".config" / "simurgh-next"
    config_dir.mkdir(parents=True)
    config = config_dir / "entry.json"
    original_config = '{"role":"entry","secret":"keep-me"}\n'
    config.write_text(original_config)

    subprocess.run(["sh", str(INSTALLER)], cwd=ROOT, env=env, check=True,
                   capture_output=True, text=True)
    first_release = (prefix / "current").resolve()
    assert config.read_text() == original_config

    subprocess.run(["sh", str(INSTALLER)], cwd=ROOT, env=env, check=True,
                   capture_output=True, text=True)
    second_release = (prefix / "current").resolve()
    assert second_release != first_release
    assert first_release.is_dir()
    assert second_release.is_dir()
    assert config.read_text() == original_config
    commands = ("simurgh-next", "simurgh-next-panel", "simurgh-next-setup",
                "simurgh-next-node")
    assert all((bindir / command).is_symlink() for command in commands)
    for command in commands:
        result = subprocess.run([str(bindir / command), "--help"], env=env,
                                check=True, capture_output=True, text=True)
        assert "usage:" in result.stdout.lower()
    assert "loopback-only" in subprocess.run(
        [str(bindir / "simurgh-next-panel"), "--help"], env=env,
        check=True, capture_output=True, text=True).stdout
    assert not (home / ".config" / "simurgh-next" / "exit.json").exists()


def test_installer_generates_user_service_and_restarts_it_on_update(tmp_path):
    env = _installer_env(tmp_path, with_systemctl=True)
    prefix = Path(env["SIMURGH_NEXT_PREFIX"])
    unit = Path(env["XDG_CONFIG_HOME"]) / "systemd/user/simurgh-next-panel.service"
    log = Path(env["SYSTEMCTL_LOG"])

    subprocess.run(["sh", str(INSTALLER), "--enable-service"], cwd=ROOT,
                   env=env, check=True, capture_output=True, text=True)
    unit_text = unit.read_text()
    assert "[Unit]\n" in unit_text and "[Service]\n" in unit_text
    assert f"WorkingDirectory={_systemd_escape(str(prefix / 'current'))}" in unit_text
    assert f"ExecStart={_systemd_escape(str(prefix / 'current/bin/simurgh-next-panel'))}" in unit_text
    config_env = f"SIMURGH_NEXT_CONFIG_DIR={env['SIMURGH_NEXT_CONFIG_DIR']}"
    assert _systemd_escape(config_env) in unit_text
    assert "KillMode=control-group" in unit_text
    analyzer = shutil.which("systemd-analyze")
    if analyzer:
        checked = subprocess.run([analyzer, "verify", str(unit)],
                                 capture_output=True, text=True, check=False)
        assert checked.returncode == 0, checked.stderr
    assert "--user enable --now simurgh-next-panel.service" in log.read_text()

    subprocess.run(["sh", str(INSTALLER), "--enable-service"], cwd=ROOT,
                   env=env, check=True, capture_output=True, text=True)
    assert "--user restart simurgh-next-panel.service" in log.read_text()
    assert (prefix / "current").resolve().is_dir()


def _system_installer_env(tmp_path):
    root = tmp_path / "system-root"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    active = tmp_path / "system-service-active"
    log = tmp_path / "system-service.log"
    commands = {
        "getent": "#!/bin/sh\nexit 0\n",
        "chown": "#!/bin/sh\nexit 0\n",
        "systemctl": (
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n"
            "case \"$*\" in\n"
            "'daemon-reload') exit 0 ;;\n"
            "'is-active --quiet simurgh-next-panel.service') [ -e \"$SYSTEMCTL_ACTIVE_FLAG\" ] ;;\n"
            "'enable --now simurgh-next-panel.service') touch \"$SYSTEMCTL_ACTIVE_FLAG\" ;;\n"
            "'restart simurgh-next-panel.service') [ \"${SYSTEMCTL_FAIL_SYSTEM_RESTART:-0}\" = 1 ] && exit 1; touch \"$SYSTEMCTL_ACTIVE_FLAG\" ;;\n"
            "*) exit 0 ;;\nesac\n"
        ),
    }
    for name, content in commands.items():
        command = fake_bin / name
        command.write_text(content)
        command.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env['PATH']}",
        "SIMURGH_NEXT_SYSTEM_TEST_ROOT": str(root),
        "SIMURGH_NEXT_TEST_INSTALLER": "1",
        "SYSTEMCTL_ACTIVE_FLAG": str(active),
        "SYSTEMCTL_LOG": str(log),
    })
    return env, root, log


def test_system_installer_preserves_legacy_config_and_upgrades_release(tmp_path):
    env, root, log = _system_installer_env(tmp_path)
    legacy = root / "etc/simurgh"
    legacy.mkdir(parents=True)
    (legacy / "preserve.txt").write_text("legacy stays\n")
    config_dir = root / "etc/simurgh-next"
    config_dir.mkdir(parents=True)
    existing = config_dir / "entry.json"
    existing.write_text('{"role":"entry","secret":"preserve"}\n')
    app_root = root / "opt/simurgh-next"
    service = root / "etc/systemd/system/simurgh-next-panel.service"

    subprocess.run(["sh", str(INSTALLER), "--system"], cwd=ROOT, env=env,
                   check=True, capture_output=True, text=True)
    first_release = (app_root / "current").resolve()
    assert existing.read_text() == '{"role":"entry","secret":"preserve"}\n'
    assert (legacy / "preserve.txt").read_text() == "legacy stays\n"
    assert service.exists() and "User=simurgh-next" in service.read_text()
    assert "ExecStart=" in service.read_text() and "CapabilityBoundingSet=CAP_NET_BIND_SERVICE" in service.read_text()
    backups = list((root / "var/backups/simurgh-next").glob("config-*.tar.gz"))
    assert backups
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert existing.stat().st_mode & 0o777 == 0o600
    assert config_dir.stat().st_mode & 0o777 == 0o750
    analyzer = shutil.which("systemd-analyze")
    if analyzer:
        checked = subprocess.run([analyzer, "verify", str(service)],
                                 capture_output=True, text=True, check=False)
        assert checked.returncode == 0, checked.stderr

    subprocess.run(["sh", str(INSTALLER), "--system"], cwd=ROOT, env=env,
                   check=True, capture_output=True, text=True)
    second_release = (app_root / "current").resolve()
    assert second_release != first_release and first_release.is_dir()
    assert existing.read_text() == '{"role":"entry","secret":"preserve"}\n'
    assert "restart simurgh-next-panel.service" in log.read_text()
    assert (legacy / "preserve.txt").read_text() == "legacy stays\n"


def test_system_update_failure_restores_old_release_and_service_unit(tmp_path):
    env, root, _log = _system_installer_env(tmp_path)
    subprocess.run(["sh", str(INSTALLER), "--system"], cwd=ROOT, env=env,
                   check=True, capture_output=True, text=True)
    app_root = root / "opt/simurgh-next"
    unit = root / "etc/systemd/system/simurgh-next-panel.service"
    previous = (app_root / "current").resolve()
    previous_unit = unit.read_text()

    env["SYSTEMCTL_FAIL_SYSTEM_RESTART"] = "1"
    result = subprocess.run(["sh", str(INSTALLER), "--system"], cwd=ROOT,
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert (app_root / "current").resolve() == previous
    assert unit.read_text() == previous_unit
    assert "systemctl failed" in result.stderr.lower()


def test_system_installer_refuses_config_symlinks_after_backup(tmp_path):
    env, root, _log = _system_installer_env(tmp_path)
    config_dir = root / "etc/simurgh-next"
    config_dir.mkdir(parents=True)
    outside = tmp_path / "external-key"
    outside.write_text("private key stays external\n")
    (config_dir / "key.pem").symlink_to(outside)

    result = subprocess.run(["sh", str(INSTALLER), "--system"], cwd=ROOT,
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert outside.read_text() == "private key stays external\n"
    assert list((root / "var/backups/simurgh-next").glob("config-*.tar.gz"))


def test_failed_service_update_restores_previous_release_and_unit(tmp_path):
    env = _installer_env(tmp_path, with_systemctl=True)
    prefix = Path(env["SIMURGH_NEXT_PREFIX"])
    unit = Path(env["XDG_CONFIG_HOME"]) / "systemd/user/simurgh-next-panel.service"
    subprocess.run(["sh", str(INSTALLER), "--enable-service"], cwd=ROOT,
                   env=env, check=True, capture_output=True, text=True)
    good_release = (prefix / "current").resolve()
    good_unit = unit.read_text()

    env["SYSTEMCTL_FAIL_RESTART"] = "1"
    result = subprocess.run(["sh", str(INSTALLER), "--enable-service"], cwd=ROOT,
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert (prefix / "current").resolve() == good_release
    assert unit.read_text() == good_unit
    assert "restoring previous" in result.stderr.lower()

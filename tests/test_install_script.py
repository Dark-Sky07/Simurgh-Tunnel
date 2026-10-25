"""Regression tests for the easy installer (``install.sh``).

The installer is the first thing people run, and it runs in two very different
ways:

* ``sudo bash install.sh …`` — a file, so ``BASH_SOURCE`` exists;
* ``curl -fsSL …/install.sh | sudo bash …`` — stdin is the script, so
  ``BASH_SOURCE`` is empty. Fresh role selection belongs to the setup panel or
  terminal menu, never to an installer prompt.

Both used to abort with ``…: unbound variable`` (a variable that was read but
never set, under ``set -u``), and ``--listen`` was silently dropped. These
tests keep both holes closed.

They never touch the network: ``SIMURGH_SRC`` points at this checkout, and
``--no-go`` skips the compiled engine.
"""

from __future__ import annotations

import os
import pty
import re
import select
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"

# a non-root install is enough to test the script: it then skips systemd and
# puts everything below the temporary home directory
BASE_ENV = {
    "SIMURGH_ALLOW_NONROOT": "1",
    "SIMURGH_SRC": str(ROOT),
    # the tests never answer questions: pretend there is no terminal
    "SIMURGH_NO_TTY": "1",
}

TIMEOUT = 300


def _env(tmp_path: Path) -> dict:
    env = dict(os.environ)
    env.update(BASE_ENV)
    env["HOME"] = str(tmp_path / "home")
    env["SIMURGH_VENV"] = str(tmp_path / "venv")
    env["SIMURGH_HOME"] = str(tmp_path / "etc")
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    return env


def _config(tmp_path: Path, name: str = "exit.toml") -> str:
    return (tmp_path / "etc" / name).read_text(encoding="utf-8")


def _non_git_source(tmp_path: Path) -> Path:
    """A plain cached tree, for pipe tests that are not testing git updates."""
    source = tmp_path / "plain-source"
    source.mkdir()
    shutil.copytree(ROOT / "simurgh", source / "simurgh",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("pyproject.toml", "setup.cfg", "README.md"):
        shutil.copy2(ROOT / name, source / name)
    return source


def _assert_installed_panel_assets(tmp_path: Path, env: dict) -> None:
    """Inspect the installed wheel, not the source tree next to the tests.

    Running ``python -c`` from the repository would import the checkout and
    hide a missing package-data rule.  The temporary cwd and explicit venv-root
    assertion keep this regression tied to the artifact users actually run.
    """
    code = r"""
import sys
from pathlib import Path
from simurgh import panel_ui
root = Path(sys.argv[1]).resolve()
assert root in panel_ui.ASSETS_DIR.resolve().parents, panel_ui.ASSETS_DIR
names = ("index.html", "app.css", "app.js", "login.js",
         "manifest.webmanifest", "favicon.png", "simurgh.png")
missing = [name for name in names if panel_ui.read_asset(name) is None]
assert not missing, f"panel assets missing from installed package: {missing}"
html = panel_ui.dashboard_html("relay", "test", "Simurgh Tunnel")
assert "<!DOCTYPE html>" in html and "__META__" not in html
print("installed panel assets OK")
"""
    proc = subprocess.run(
        [str(tmp_path / "venv" / "bin" / "python"), "-c", code,
         str(tmp_path / "venv")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "installed panel assets OK" in proc.stdout


def test_install_script_has_no_syntax_errors():
    subprocess.run(["bash", "-n", str(INSTALL)], check=True, timeout=60)


def test_bad_options_fail_with_a_sentence_not_a_trace():
    """A typo must name the option, not print a cryptic unbound variable."""
    worst = "unbound variable"
    for args in (["--role", "bogus"], ["--engine", "foo"], ["--dial", "maybe"],
                 ["--exit", "1.2.3.4"], ["--connections", "x"]):
        proc = subprocess.run(["bash", str(INSTALL), *args, "--yes"],
                              env={**os.environ, **BASE_ENV},
                              capture_output=True, text=True, timeout=60)
        output = proc.stdout + proc.stderr
        assert proc.returncode != 0
        assert worst not in output, output
        assert "unknown" in output or "must be" in output or "wants" in output, output


def test_install_from_a_file_forwards_every_option(tmp_path):
    """``sudo bash install.sh --role exit …`` with no --connections flag.

    ``--connections`` used to be read without ever being set, which aborted the
    install right before the config was written.  The final probe also requires
    every panel asset inside the installed venv: running the tests only from a
    source checkout previously hid their omission from the Python wheel.
    """
    env = _env(tmp_path)
    proc = subprocess.run(
        ["bash", str(INSTALL), "--yes", "--no-go", "--role", "exit",
         "--name", "omega-de", "--token", "test-token",
         "--listen", "8443,9443", "--panel-port", "9999"],
        env=env, capture_output=True, text=True, timeout=TIMEOUT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "unbound variable" not in (proc.stdout + proc.stderr)
    cfg = _config(tmp_path)
    assert "test-token" in cfg
    assert "omega-de" in cfg
    for port in ("8443", "9443"):
        assert port in cfg, f"--listen {port} was dropped"
    _assert_installed_panel_assets(tmp_path, env)


def test_install_when_the_script_is_piped_into_bash(tmp_path):
    """``curl … | sudo bash`` — no BASH_SOURCE, no terminal, no questions."""
    script = INSTALL.read_text(encoding="utf-8")
    env = _env(tmp_path)
    env["SIMURGH_SRC"] = str(_non_git_source(tmp_path))
    proc = subprocess.run(
        ["bash", "-s", "--", "--yes", "--no-go", "--role", "exit", "--token", "piped-token"],
        input=script, env=env, capture_output=True, text=True, timeout=TIMEOUT,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "unbound variable" not in output, output
    assert "BASH_SOURCE" not in output, output
    assert (tmp_path / "etc" / "exit.toml").exists()


def test_installs_without_reaching_pypi(tmp_path):
    """An Iranian server often cannot reach PyPI. The install must still end
    with a working ``simurgh`` command (the build falls back to whatever
    setuptools the machine has, and in the worst case to the source itself)."""
    env = _env(tmp_path)
    env["PIP_NO_INDEX"] = "1"
    proc = subprocess.run(
        ["bash", str(INSTALL), "--yes", "--no-go", "--role", "exit", "--token", "offline"],
        env=env, capture_output=True, text=True, timeout=TIMEOUT,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "local build tools (no PyPI needed)" in output, output
    assert "normal install failed" not in output, output
    assert not list((tmp_path / "venv").rglob("simurgh-src.pth"))
    assert "include-system-site-packages = true" in (
        tmp_path / "venv" / "pyvenv.cfg").read_text(encoding="utf-8")
    binary = tmp_path / "venv" / "bin" / "simurgh"
    assert binary.exists(), output
    version = subprocess.run([str(binary), "--version"], capture_output=True,
                             text=True, timeout=60)
    assert version.returncode == 0, version.stdout + version.stderr
    _assert_installed_panel_assets(tmp_path, env)
    metadata = subprocess.run([
        str(tmp_path / "venv" / "bin" / "python"), "-I", "-c",
        "import importlib.metadata as m; "
        "print(m.version('simurgh-tunnel')); "
        "print(m.distribution('simurgh-tunnel').read_text('entry_points.txt'))",
    ], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert metadata.returncode == 0, metadata.stdout + metadata.stderr
    assert "simurgh = simurgh.cli:main" in metadata.stdout


def test_install_survives_a_systemctl_without_root(tmp_path):
    """A container or a CI runner has `systemctl` but nobody may write to
    `/etc/systemd/system` — the install has to finish anyway.

    The installer used to call `simurgh install --no-start --no-enable`, which
    still writes the unit files, so it died with
    `PermissionError: '/etc/systemd/system/simurgh-exit.service'` as soon as
    `systemctl is-system-running` answered (the CI runner does).
    """
    env = _env(tmp_path)
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_systemctl = fake_bin / "systemctl"
    fake_systemctl.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  is-system-running) echo running ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    proc = subprocess.run(
        ["bash", str(INSTALL), "--yes", "--no-go", "--role", "exit",
         "--token", "no-root", "--no-systemd"],
        env=env, capture_output=True, text=True, timeout=TIMEOUT,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "PermissionError" not in output, output
    assert "Traceback" not in output, output
    assert (tmp_path / "etc" / "exit.toml").exists(), output


def test_a_relay_without_a_token_is_installed_and_paired_later(tmp_path):
    """The relay can be installed before its exit exists.

    Nothing may be asked (or demanded) up front: the config is written as
    `unpaired`, the panel is installed, and the summary says exactly how to
    finish it.  Piping the installer is the common case, so it must not abort.
    """
    script = INSTALL.read_text(encoding="utf-8")
    env = _env(tmp_path)
    env["SIMURGH_SRC"] = str(_non_git_source(tmp_path))
    proc = subprocess.run(
        ["bash", "-s", "--", "--yes", "--no-go", "--role", "relay"],
        input=script, env=env, capture_output=True, text=True, timeout=TIMEOUT,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "unbound variable" not in output, output
    relay = _config(tmp_path, "relay.toml")
    assert "unpaired = true" in relay, relay
    assert "token = \"\"" in relay, relay
    # the normal next step is the panel, not a second role-specific CLI flow
    assert "not paired yet" in output, output
    assert "web panel:" in output and "Pair" in output, output
    assert "http://<SERVER-IP>:8787/login" in output, output
    assert "sudo simurgh link" not in output and "sudo simurgh join" not in output, output


def test_piped_installer_refreshes_a_complete_but_stale_git_cache(tmp_path):
    """A valid ``/opt/simurgh-src`` may still contain an older CLI.

    The one-liner downloads a new install.sh on every run.  Reusing an old git
    checkout just because pyproject.toml exists used to make that new script
    pass --pair-later to a CLI which did not know the option yet.
    """
    cache = tmp_path / "git-source"
    subprocess.run(["git", "clone", "--quiet", str(ROOT), str(cache)],
                   check=True, timeout=60)
    cli = cache / "simurgh" / "cli.py"
    source = cli.read_text(encoding="utf-8")
    option = 'p.add_argument("--pair-later", "--unpaired", dest="pair_later",'
    assert option in source
    cli.write_text(source.replace(
        option, 'p.add_argument("--unpaired", dest="pair_later",'),
        encoding="utf-8",
    )
    subprocess.run([
        "git", "-C", str(cache), "-c", "user.name=Installer Test",
        "-c", "user.email=installer@example.invalid", "commit", "--quiet",
        "--all", "--message", "simulate a stale cached CLI",
    ], check=True, timeout=60)

    env = _env(tmp_path)
    env["SIMURGH_SRC"] = str(cache)
    env["SIMURGH_REF"] = "HEAD"       # the local origin; no network is used
    proc = subprocess.run(
        ["bash", "-s", "--", "--yes", "--no-go", "--role", "relay"],
        input=INSTALL.read_text(encoding="utf-8"), env=env,
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "updating the source" in output, output
    assert "unrecognized arguments: --pair-later" not in output, output
    assert "unpaired = true" in _config(tmp_path, "relay.toml")
    assert option in cli.read_text(encoding="utf-8")


def test_role_both_writes_both_configs(tmp_path):
    """``--role both`` is documented; it must produce a relay that dials the
    local exit (and a shared token), not a half-configured server."""
    proc = subprocess.run(
        ["bash", str(INSTALL), "--yes", "--no-go", "--role", "both", "--name", "dual"],
        env=_env(tmp_path), capture_output=True, text=True, timeout=TIMEOUT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    relay = _config(tmp_path, "relay.toml")
    exit_cfg = _config(tmp_path)
    assert "127.0.0.1" in relay
    token_line = [line for line in exit_cfg.splitlines() if line.startswith("token")][0]
    assert token_line in relay, "the relay and the exit must share the token"


def _both_configs(tmp_path):
    from simurgh.config import load_exit, load_relay

    home = tmp_path / "etc"
    return load_exit(home / "exit.toml"), load_relay(home / "relay.toml")


@pytest.mark.parametrize("extra", [
    [],                                        # defaults: 443, 2053, 2083
    ["--tunnel-port", "8443"],
    ["--listen", "9443,9444"],
    ["--listen", "9443-9445", "--tunnel-port", "8443"],
    ["--carrier", "wss"],                      # a local exit always serves tls
])
def test_both_mode_dials_a_port_and_carrier_the_local_exit_serves(tmp_path, extra):
    """``--role both`` puts the relay and the exit on one machine, so the relay
    must dial 127.0.0.1 on a port the exit actually listens on -- with the same
    carrier. Otherwise the tunnel can never come up (the installer used to dial
    443 no matter what ``--tunnel-port`` said)."""
    proc = subprocess.run(
        ["bash", str(INSTALL), "--yes", "--no-go", "--role", "both", "--name", "dual", *extra],
        env=_env(tmp_path), capture_output=True, text=True, timeout=TIMEOUT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    exit_cfg, relay_cfg = _both_configs(tmp_path)
    assert relay_cfg.exit.address == "127.0.0.1"
    served = {(spec.port, spec.carrier) for spec in exit_cfg.listen}
    assert (relay_cfg.exit.port, relay_cfg.exit.carrier) in served, (
        f"the exit serves {sorted(served)} but the relay dials "
        f"{relay_cfg.exit.port}/{relay_cfg.exit.carrier}")
    assert exit_cfg.token == relay_cfg.token

    # the exit's certificate is self-signed and there is no setup link to carry
    # its fingerprint to the relay here, so the relay has to pin it -- without
    # the pin every handshake fails with "cannot reach exit".
    from simurgh.certs import fingerprint_of

    assert relay_cfg.exit.fingerprint == fingerprint_of(exit_cfg.cert_file)

    # the exit listens on the tunnel port on this same machine, so mirroring
    # its port list would make the relay fight the exit for those ports.
    assert relay_cfg.accept_push is False


def test_both_mode_in_reverse_finishes_or_points_to_the_panel(tmp_path):
    """Reverse + both: pair locally or explain the panel-only fallback."""
    proc = subprocess.run(
        ["bash", str(INSTALL), "--yes", "--no-go", "--role", "both", "--dial", "exit",
         "--tunnel-port", "8443", "--name", "dual"],
        env=_env(tmp_path), capture_output=True, text=True, timeout=TIMEOUT,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "<link>" not in output, output
    exit_cfg, relay_cfg = _both_configs(tmp_path)
    assert relay_cfg.dial == "exit"
    assert relay_cfg.tunnel.port == 8443
    # either the exit dials now, or the user is sent to the two local panels
    assert ("the local exit dials this relay now" in output
            or "in their panels" in output), output


def _install_as(tmp_path, role, country=None):
    """Run the universal installer with an explicit operator-selected role."""
    env = _env(tmp_path)
    if country is not None:
        # Retained solely as a regression trap: role must not depend on this.
        env["SIMURGH_COUNTRY"] = country
    return subprocess.run(
        ["bash", str(INSTALL), "--yes", "--no-go", "--role", role,
         "--name", "selected-box"],
        env=env, capture_output=True, text=True, timeout=TIMEOUT,
    )


def test_explicit_relay_role_is_used_even_if_geo_environment_says_elsewhere(tmp_path):
    proc = _install_as(tmp_path, "relay", "DE")
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "using explicit server role: relay" in output, output
    assert (tmp_path / "etc" / "relay.toml").exists()
    assert not (tmp_path / "etc" / "exit.toml").exists()
    assert "unpaired = true" in _config(tmp_path, "relay.toml")


def test_explicit_exit_role_is_used_even_if_geo_environment_says_iran(tmp_path):
    proc = _install_as(tmp_path, "exit", "IR")
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "using explicit server role: exit" in output, output
    assert (tmp_path / "etc" / "exit.toml").exists()
    assert not (tmp_path / "etc" / "relay.toml").exists()


def test_fresh_noninteractive_install_defers_role_to_menu_or_panel(tmp_path):
    env = _env(tmp_path)
    env["SIMURGH_COUNTRY"] = "IR"  # must not silently decide relay
    proc = subprocess.run(
        ["bash", str(INSTALL), "--yes", "--no-go"],
        env=env, capture_output=True, text=True, timeout=TIMEOUT,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "no role has been selected yet" in output
    assert "simurgh menu" in output
    assert not (tmp_path / "etc" / "relay.toml").exists()
    assert not (tmp_path / "etc" / "exit.toml").exists()


def test_custom_install_options_without_role_are_rejected(tmp_path):
    env = _env(tmp_path)
    proc = subprocess.run(
        ["bash", str(INSTALL), "--yes", "--no-go", "--exit", "203.0.113.9:443"],
        env=env, capture_output=True, text=True, timeout=TIMEOUT,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert "choose a role" in output
    assert not (tmp_path / "etc" / "relay.toml").exists()
    assert not (tmp_path / "etc" / "exit.toml").exists()


@pytest.mark.skipif(not hasattr(pty, "fork"), reason="requires a POSIX pseudo-terminal")
def test_interactive_installer_does_not_prompt_for_a_role(tmp_path):
    env = _env(tmp_path)
    env.pop("SIMURGH_NO_TTY", None)
    command = ["bash", str(INSTALL), "--yes", "--no-go"]
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(command[0], command, env)

    output = bytearray()
    status = None
    deadline = time.monotonic() + TIMEOUT
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([fd], [], [], 0.5)
            if readable:
                try:
                    chunk = os.read(fd, 8192)
                except OSError:  # Linux ptys report EIO after the child exits
                    chunk = b""
                if chunk:
                    output.extend(chunk)
            waited, child_status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                status = child_status
                break
        if status is None:
            os.kill(pid, signal.SIGTERM)
            _, status = os.waitpid(pid, 0)
    finally:
        os.close(fd)

    text = output.decode("utf-8", errors="replace")
    assert status is not None and os.waitstatus_to_exitcode(status) == 0, text
    assert "Select 1 or 2" not in text
    assert "choose Exit or Relay" in text
    assert not (tmp_path / "etc" / "relay.toml").exists()
    assert not (tmp_path / "etc" / "exit.toml").exists()


def test_a_rerun_keeps_the_role_that_is_already_installed(tmp_path):
    """Existing configuration remains authoritative on noninteractive reruns."""
    assert _install_as(tmp_path, "exit", "DE").returncode == 0
    env = _env(tmp_path)
    env["SIMURGH_COUNTRY"] = "IR"
    proc = subprocess.run(["bash", str(INSTALL), "--yes", "--no-go"],
                          env=env, capture_output=True, text=True, timeout=TIMEOUT)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "keeping the existing exit config" in output, output
    assert not (tmp_path / "etc" / "relay.toml").exists()


def test_rerun_is_an_upgrade_and_preserves_pairing_ports_and_token(tmp_path):
    """The universal command must never silently turn a paired relay into a
    fresh, unpaired one.  That used to happen because the shell forced config
    regeneration on every rerun."""
    env = _env(tmp_path)
    first = subprocess.run([
        "bash", str(INSTALL), "--yes", "--no-go", "--role", "relay",
        "--name", "keep-me", "--token", "keep-this-secret",
        "--exit", "203.0.113.9:9443", "--mapping", "8443:9000",
    ], env=env, capture_output=True, text=True, timeout=TIMEOUT)
    assert first.returncode == 0, first.stdout + first.stderr
    before = _config(tmp_path, "relay.toml")
    assert "keep-this-secret" in before and "unpaired = true" not in before

    # No role, token or endpoint on the second run: existing configuration is
    # authoritative, just as it is when the panel or menu installs an update.
    env["SIMURGH_COUNTRY"] = "DE"
    second = subprocess.run(["bash", str(INSTALL), "--yes", "--no-go"],
                            env=env, capture_output=True, text=True, timeout=TIMEOUT)
    output = second.stdout + second.stderr
    assert second.returncode == 0, output
    assert "keeping the existing relay config" in output
    assert _config(tmp_path, "relay.toml") == before


def test_exit_summary_points_to_the_panel_not_a_token_workflow(tmp_path):
    proc = _install_as(tmp_path, "exit")
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "web panel:" in output and "Pair" in output, output
    assert "http://<SERVER-IP>:8787/login" in output, output
    assert "auto-token" not in output, output
    assert "relay setup link" not in output, output


@pytest.mark.parametrize("filename,start,end", [
    ("README.md", "### 1 — Install every server with the same command",
     "### Packages and containers"),
    ("README.fa.md", "### ۱ — نصب همهٔ سرورها با یک فرمان واحد",
     "### بسته‌ها و کانتینر"),
])
def test_quick_start_has_one_universal_install_command(filename, start, end):
    text = (ROOT / filename).read_text(encoding="utf-8")
    quick = text.split(start, 1)[1].split(end, 1)[0]
    commands = re.findall(r"```bash\n(.*?)```", quick, flags=re.S)
    assert commands == [
        "curl -fsSL https://raw.githubusercontent.com/Dark-Sky07/Simurgh-Tunnel/main/install.sh | sudo bash\n"
    ]
    assert "--role exit" in quick and "--role relay" in quick  # advanced automation only
    assert "simurgh join" not in quick and "simurgh link" not in quick
    assert "Pair" in quick and "Settings" in quick


def test_help_does_not_execute_commands_in_backticks(tmp_path):
    """``install.sh --help`` must print ``simurgh join '<link>'``, never run it."""
    env = _env(tmp_path)
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    marker = tmp_path / "ran-simurgh"
    fake = fake_bin / "simurgh"
    fake.write_text(f"#!/bin/sh\necho \"$*\" > '{marker}'\nexit 99\n",
                    encoding="utf-8")
    fake.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    proc = subprocess.run(["bash", str(INSTALL), "--help"],
                          env=env, capture_output=True, text=True, timeout=30)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert not marker.exists(), f"--help executed simurgh: {marker.read_text()}"
    assert "simurgh join '<link>'" in output, output


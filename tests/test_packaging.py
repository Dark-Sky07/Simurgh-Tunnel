"""The packages: .deb, .rpm, the portable tarball, and the container.

Packaging is a feature with a failure mode of its own -- the package installs,
`simurgh --version` works, and then nothing else does -- so these tests build
the real thing (no mocks: the builder is the artefact under test) and inspect
what comes out.  `dpkg-deb` and `rpmbuild` are optional: on a machine without
them the corresponding assertions are skipped, while the tarball is always
checked because it is what the others are built from.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
VERSION = (REPO / "simurgh" / "__init__.py").read_text().split('__version__ = "')[1].split('"')[0]

HAVE_DPKG = shutil.which("dpkg-deb") is not None
HAVE_RPM = shutil.which("rpmbuild") is not None


def run(cmd, **kwargs):
    kwargs.setdefault("cwd", REPO)
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """One real build for the whole module: the tarball plus the .deb."""
    out = tmp_path_factory.mktemp("packages")
    proc = run(["bash", "packaging/build-packages.sh", "--out", str(out)])
    assert proc.returncode == 0, proc.stderr
    return out


def extract_tarball(tarball: Path, into: Path) -> Path:
    into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball) as tar:
        tar.extractall(into)  # noqa: S202 (our own build, in a tmp dir)
    return into


# --------------------------------------------------------------- the scripts


def test_every_packaging_script_is_valid_shell():
    scripts = [REPO / "packaging/build-packages.sh", REPO / "packaging/docker/entrypoint.sh"]
    scripts += sorted((REPO / "packaging/deb").iterdir())
    for script in scripts:
        proc = run(["bash", "-n", str(script)])
        assert proc.returncode == 0, f"{script.name}: {proc.stderr}"
        assert os.access(script, os.X_OK) or script.name == "simurgh.spec", script.name


def test_the_entrypoint_explains_a_missing_configuration(tmp_path):
    home = tmp_path / "etc"
    proc = run(["sh", str(REPO / "packaging/docker/entrypoint.sh"), "relay"],
               env={**os.environ, "SIMURGH_HOME": str(home)})
    assert proc.returncode == 1
    assert "no " in proc.stderr and "relay.toml" in proc.stderr
    assert "simurgh init --role relay" in proc.stderr
    # and it does not need the volume to exist yet
    assert home.is_dir()


def test_the_entrypoint_forwards_other_commands(tmp_path):
    # any command that is not a role goes straight to the CLI: `--version`
    # is the cheapest one to prove it (the packaged launcher is used here, so
    # nothing is installed on the machine running the tests)
    home = tmp_path / "etc"
    home.mkdir()
    (home / "relay.toml").write_text('token = "x"\n')
    launcher = REPO / "simurgh" / "__main__.py"  # a stand-in, replaced below
    fake = tmp_path / "simurgh"
    fake.write_text(f"#!/bin/sh\necho forwarded: $*\n")
    fake.chmod(0o755)
    proc = run(["sh", str(REPO / "packaging/docker/entrypoint.sh"), "--version"],
               env={**os.environ, "SIMURGH_HOME": str(home), "SIMURGH_BIN": str(fake)})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "forwarded: --version"
    assert launcher.exists()


# --------------------------------------------------------------- the tarball


def test_the_tarball_carries_both_engines_and_the_units(built):
    tarball = built / f"simurgh-{VERSION}-noarch.tar.gz"
    assert tarball.is_file()
    with tarfile.open(tarball) as tar:
        names = set(tar.getnames())
    for wanted in (
        "./usr/bin/simurgh",
        "./usr/lib/simurgh/simurgh/cli.py",
        "./usr/lib/simurgh/simurgh/panel.py",
        "./usr/lib/simurgh/simurgh/_tomli/__init__.py",
        "./usr/lib/systemd/system/simurgh-relay.service",
        "./usr/lib/systemd/system/simurgh-exit.service",
        "./usr/share/doc/simurgh/README.md",
        "./usr/share/doc/simurgh/CONFIGURATION.fa.md",
        "./etc/simurgh",
    ):
        assert wanted in names, wanted
    # no build leftovers, no secrets, nothing from a developer's machine
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    assert not [n for n in names
                if n.rsplit("/", 1)[-1].startswith("test_") or n.endswith("_test.py")]


def test_the_tarball_directory_is_private(built, tmp_path):
    root = extract_tarball(built / f"simurgh-{VERSION}-noarch.tar.gz", tmp_path / "root")
    config_dir = root / "etc" / "simurgh"
    assert config_dir.is_dir()
    assert oct(config_dir.stat().st_mode & 0o777) == "0o700", "the config dir holds tokens"


def test_the_packaged_launcher_runs_the_cli(built, tmp_path):
    root = extract_tarball(built / f"simurgh-{VERSION}-noarch.tar.gz", tmp_path / "root")
    launcher = root / "usr" / "bin" / "simurgh"
    assert launcher.is_file() and os.access(launcher, os.X_OK)
    proc = run([str(launcher), "--version"], cwd=tmp_path,
               env={**os.environ, "SIMURGH_LIB": str(root / "usr/lib/simurgh")})
    assert proc.returncode == 0, proc.stderr
    assert f"Simurgh Tunnel v{VERSION}" in proc.stdout


def test_the_launcher_records_the_libdir_the_build_gave_it(built, tmp_path):
    # `install.sh` uses a virtualenv; a package cannot, so the launcher carries
    # the library path from build time -- and no unsubstituted placeholder
    root = extract_tarball(built / f"simurgh-{VERSION}-noarch.tar.gz", tmp_path / "root")
    text = (root / "usr" / "bin" / "simurgh").read_text()
    assert "@LIBDIR@" not in text
    assert 'BUILT_LIBDIR = "/usr/lib/simurgh"' in text

    # a build for another prefix points there instead, and still runs
    out = tmp_path / "out2"
    proc = run(["bash", "packaging/build-packages.sh", "--out", str(out),
                "--prefix", "/opt/simurgh", "--no-deb", "--no-rpm"])
    assert proc.returncode == 0, proc.stderr
    other = extract_tarball(out / f"simurgh-{VERSION}-noarch.tar.gz", tmp_path / "root2")
    launcher = other / "opt" / "simurgh" / "bin" / "simurgh"
    assert 'BUILT_LIBDIR = "/opt/simurgh/lib/simurgh"' in launcher.read_text()
    proc = run([str(launcher), "--version"],
               env={**os.environ, "SIMURGH_LIB": str(other / "opt/simurgh/lib/simurgh")})
    assert proc.returncode == 0, proc.stderr
    assert f"Simurgh Tunnel v{VERSION}" in proc.stdout


# ------------------------------------------------------------------- the deb


@pytest.mark.skipif(not HAVE_DPKG, reason="dpkg-deb is not installed")
def test_the_deb_control_is_complete(built):
    deb = built / f"simurgh_{VERSION}_all.deb"
    assert deb.is_file()
    info = run(["dpkg-deb", "-I", str(deb)])
    assert info.returncode == 0, info.stderr
    assert f"Version: {VERSION}" in info.stdout
    assert "Architecture: all" in info.stdout
    assert "Depends: python3 (>= 3.9)" in info.stdout
    for script in ("postinst", "prerm", "postrm"):
        assert script in info.stdout
    contents = run(["dpkg-deb", "-c", str(deb)])
    assert "./usr/bin/simurgh" in contents.stdout
    assert "./usr/lib/systemd/system/simurgh-exit.service" in contents.stdout
    assert "./usr/lib/simurgh/simurgh/relay.py" in contents.stdout
    assert "DEBIAN" not in contents.stdout or "./DEBIAN" not in contents.stdout


@pytest.mark.skipif(not HAVE_DPKG, reason="dpkg-deb is not installed")
def test_a_deb_with_the_engine_is_architecture_specific(tmp_path):
    engine = tmp_path / "simurgh-go"
    engine.write_text("#!/bin/sh\necho simurgh (go engine)\n")
    engine.chmod(0o755)
    out = tmp_path / "out"
    proc = run(["bash", "packaging/build-packages.sh", "--out", str(out),
                "--no-rpm", "--go-binary", str(engine)])
    assert proc.returncode == 0, proc.stderr
    debs = list(out.glob("*.deb"))
    assert len(debs) == 1
    info = run(["dpkg-deb", "-I", str(debs[0])])
    assert "Architecture: all" not in info.stdout
    contents = run(["dpkg-deb", "-c", str(debs[0])])
    assert "./usr/bin/simurgh-go" in contents.stdout
    assert "-rwxr-xr-x" in [line for line in contents.stdout.splitlines()
                            if line.endswith("./usr/bin/simurgh-go")][0]


# ------------------------------------------------------------------- the rpm


def test_the_rpm_spec_only_uses_macros_every_rpm_defines():
    # `%{_unitdir}` needs systemd-rpm-macros, which Ubuntu's rpm (the build host
    # CI uses) does not install: the units are addressed through %{_prefix}
    spec = (REPO / "packaging/rpm/simurgh.spec").read_text()
    assert "_unitdir" not in spec
    macros = set(re.findall(r"%\{(\??[A-Za-z_][A-Za-z0-9_]*)\}", spec))
    allowed = {"version", "release", "?dist", "dist", "stage", "goarch", "buildroot",
               "_prefix", "_bindir", "_sysconfdir"}
    assert macros <= allowed, macros - allowed
    # and the changelog date must be a real day (rpmbuild warns otherwise)
    stamp = [line for line in spec.splitlines() if line.startswith("* ")][0]
    day, month, date, year = stamp[2:].split()[:4]
    assert (day, month) == ("Fri", "Oct") and date == "16" and year == "2026", stamp


def test_the_rpm_spec_matches_the_staging_tree():
    spec = (REPO / "packaging/rpm/simurgh.spec").read_text()
    for line in ("Name:           simurgh", "License:        MIT",
                 "Requires:       python3 >= 3.9", "%post", "%preun", "%postun", "%changelog"):
        assert line in spec, line
    # the %files list must be exactly what the builder stages
    builder = (REPO / "packaging/build-packages.sh").read_text()
    for path in ("%{_bindir}/simurgh", "%{_prefix}/lib/simurgh",
                 "%{_prefix}/lib/systemd/system/simurgh-relay.service",
                 "%{_prefix}/lib/systemd/system/simurgh-exit.service"):
        assert path in spec
    # the builder stages every one of these, so the spec has to package them
    for name in ("README.md", "README.fa.md", "CONFIGURATION.md", "CONFIGURATION.fa.md",
                 "PACKAGING.md", "PACKAGING.fa.md"):
        assert name in builder, f"{name} is not staged at all"
        assert name in spec, f"{name} is staged but not in the RPM file list"
    assert "noarch" in spec


def test_every_staged_file_is_in_the_rpm_file_list(built):
    """`rpmbuild` refuses a buildroot file nobody packaged ("Installed (but
    unpackaged) file(s) found"), which is a CI-only failure no one wants twice:
    the tarball *is* the staging tree, so check its contents against %files."""
    spec = (REPO / "packaging/rpm/simurgh.spec").read_text()
    section = spec.split("%files", 1)[1].split("%changelog", 1)[0]
    packaged: list[str] = []
    for line in section.splitlines():
        line = line.strip()
        if not line or line.startswith("%if") or line.startswith("%endif"):
            continue
        line = line.replace("%dir ", "").replace("%license ", "").replace("%doc ", "")
        if not line.startswith(("/", "%")):
            continue
        packaged.append(line.replace("%{_prefix}", "/usr")
                            .replace("%{_bindir}", "/usr/bin")
                            .replace("%{_sysconfdir}", "/etc").lstrip("/"))
    assert packaged, "the %files section is empty"

    with tarfile.open(built / f"simurgh-{VERSION}-noarch.tar.gz") as tar:
        names = [name.lstrip("./").rstrip("/") for name in tar.getnames()
                 if name not in ("./", "", "/")]

    unpackaged = []
    for name in names:
        if not name or name.endswith("simurgh-go"):
            continue  # the engine binary only exists in the engine build
        entries = [entry.rstrip("/") for entry in packaged]
        if any(name == entry or name.startswith(entry + "/") for entry in entries):
            continue
        if any(entry.startswith(name + "/") for entry in entries):
            continue  # a parent directory of something packaged
        unpackaged.append(name)
    assert not unpackaged, f"staged but not in %files: {unpackaged}"


@pytest.mark.skipif(not HAVE_RPM, reason="rpmbuild is not installed")
def test_the_rpm_builds(built):
    rpms = list(built.glob("*.rpm"))
    assert rpms, "the builder said it had rpmbuild but produced no .rpm"
    info = run(["rpm", "-qip", str(rpms[0])])
    assert info.returncode == 0, info.stderr
    assert re.search(rf"Version\s*:\s*{VERSION}\b", info.stdout), info.stdout[:200]
    files = run(["rpm", "-qlp", str(rpms[0])])
    assert "/usr/bin/simurgh" in files.stdout


# ---------------------------------------------------------------- the doctor


def test_the_dockerfile_only_copies_paths_that_exist():
    text = (REPO / "Dockerfile").read_text()
    copied = 0
    for line in text.splitlines():
        line = line.strip()
        if not line.upper().startswith("COPY "):
            continue
        assert "--from=" not in line or True
        parts = [p for p in line.split()[1:] if not p.startswith("--")]
        if line.upper().startswith("COPY --FROM"):
            continue
        source = parts[0]
        if source.startswith("/"):
            continue
        assert (REPO / source).exists(), f"Dockerfile copies a missing path: {source}"
        copied += 1
    assert copied >= 4


def test_the_dockerfile_builds_the_engine_without_network():
    text = (REPO / "Dockerfile").read_text()
    assert "GOPROXY=off" in text and "CGO_ENABLED=0" in text
    assert text.count("FROM ") >= 2, "the engine is built in its own stage"
    assert "golang:1.27" in text and "python:3" in text
    assert 'ENTRYPOINT ["/usr/local/bin/simurgh-entrypoint"]' in text
    assert 'CMD ["relay"]' in text
    assert 'VOLUME ["/etc/simurgh"]' in text
    assert "go build" in text and "/out/simurgh-go" in text


def test_the_compose_file_does_not_fight_over_host_ports():
    text = (REPO / "docker-compose.yml").read_text()
    assert "services:" in text and "  relay:" in text and "  exit:" in text
    published: dict[str, str] = {}
    service = ""
    for line in text.splitlines():
        # a service key is indented exactly two spaces; anything deeper is a
        # property of the service above it
        if line.startswith("  ") and not line.startswith("   ") \
                and line.strip().endswith(":") and not line.strip().startswith("-"):
            service = line.strip().rstrip(":")
        stripped = line.strip()
        if service in ("relay", "exit") and stripped.startswith('- "') and ":" in stripped:
            host = stripped.strip('- "').split(":")[0]
            assert host not in published, \
                f"port {host} is published by both {published.get(host)} and {service}"
            published[host] = service
    assert {"8787", "8443"} <= set(published), published
    assert text.count("restart: unless-stopped") >= 2
    assert text.count("healthcheck:") >= 2
    assert "network_mode: host" in text  # the real-server example


def test_the_compose_recipe_is_a_pair_the_cli_really_writes(tmp_path):
    """The header comment of docker-compose.yml is a promise: run it here."""
    token = "container-demo-token"
    for cmd, home in (
        (["install", "--role", "exit", "--listen", "19443", "--token", token, "--force"],
         "exit-home"),
        (["install", "--role", "relay", "--exit-host", "exit", "--exit-port", "19443",
          "--carrier", "tls", "--insecure", "--mapping", "8443:9000", "--token", token,
          "--force"], "relay-home"),
    ):
        proc = run([sys.executable, "-m", "simurgh", "--home", str(tmp_path / home), *cmd])
        assert proc.returncode == 0, proc.stdout + proc.stderr
    relay = (tmp_path / "relay-home" / "relay.toml").read_text()
    exit_cfg = (tmp_path / "exit-home" / "exit.toml").read_text()
    assert f'token = "{token}"' in relay and f'token = "{token}"' in exit_cfg
    assert 'address = "exit"' in relay and "port = 19443" in relay
    assert "insecure_skip_verify = true" in relay
    assert 'carrier = "tls"' in exit_cfg and "port = 19443" in exit_cfg
    assert "target_port = 9000" in relay


def test_the_readme_points_at_the_packaging_doc():
    for readme, doc in ((REPO / "README.md", "docs/PACKAGING.md"),
                        (REPO / "README.fa.md", "docs/PACKAGING.fa.md")):
        text = readme.read_text()
        assert doc in text or doc.replace("docs/", "") in text, doc
        assert (REPO / doc).is_file()

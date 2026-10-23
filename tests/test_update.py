"""``simurgh update``: version maths, asset choice, the git path and the binary path.

Nothing here talks to the internet: the release API is stubbed with the same
JSON shape GitHub sends, and the downloads are replaced with local files.  The
git path does use a real repository, but one built in ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from simurgh import update as updater
from simurgh import __version__

RELEASE = {
    "tag_name": "v9.9.9",
    "name": "🕊️ Simurgh Tunnel v9.9.9",
    "published_at": "2026-10-16T00:00:00Z",
    "html_url": "https://github.com/Dark-Sky07/Simurgh-Tunnel/releases/tag/v9.9.9",
    "assets": [
        {"name": "simurgh-go-linux-amd64",
         "browser_download_url": "https://example.invalid/simurgh-go-linux-amd64"},
        {"name": "SHA256SUMS.txt",
         "browser_download_url": "https://example.invalid/SHA256SUMS.txt"},
    ],
}


class FakeResponse:
    """The tiny slice of ``urlopen`` this code uses."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.pos = 0

    # `read()` alone would be enough for the API calls; the download path goes
    # through shutil.copyfileobj, which wants a real file-like cursor
    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self.payload[self.pos:]
            self.pos = len(self.payload)
            return chunk
        chunk = self.payload[self.pos:self.pos + size]
        self.pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_parse_version_orders_releases():
    assert updater.parse_version("v2.3.0") > updater.parse_version("2.2.0")
    assert updater.parse_version("2.3.0-go") == updater.parse_version("v2.3.0")
    assert updater.parse_version("2.10.0") > updater.parse_version("2.9.9")
    assert updater.parse_version("2.3") == updater.parse_version("2.3.0")


def test_asset_name_follows_the_release_matrix():
    assert updater.asset_name("x86_64", "Linux") == "simurgh-go-linux-amd64"
    assert updater.asset_name("aarch64", "Linux") == "simurgh-go-linux-arm64"
    assert updater.asset_name("armv7l", "Linux") == "simurgh-go-linux-armv7"
    assert updater.asset_name("x86_64", "Darwin") == "simurgh-go-darwin-amd64"
    assert updater.asset_name("arm64", "Darwin") == "simurgh-go-darwin-arm64"
    assert updater.asset_name("AMD64", "Windows") == "simurgh-go-windows-amd64.exe"
    with pytest.raises(updater.UpdateError):
        updater.asset_name("mips", "Linux")


def test_check_reports_an_available_update(monkeypatch):
    monkeypatch.setattr(updater, "_open",
                        lambda url, timeout=updater.TIMEOUT:
                        FakeResponse(json.dumps(RELEASE).encode()))
    info = updater.check()
    assert info["current"] == __version__
    assert info["latest"] == "9.9.9" and info["tag"] == "v9.9.9"
    assert info["available"] is True
    assert "simurgh-go-linux-amd64" in info["assets"]
    assert info["asset_urls"]["SHA256SUMS.txt"].endswith("SHA256SUMS.txt")


def test_check_survives_no_network(monkeypatch):
    def boom(url, timeout=updater.TIMEOUT):
        raise OSError("network is down")

    monkeypatch.setattr(updater, "_open", boom)
    with pytest.raises(updater.UpdateError) as err:
        updater.check()
    assert "network is down" in str(err.value)


def test_update_says_nothing_to_do_on_the_newest_release(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "_open",
                        lambda url, timeout=updater.TIMEOUT:
                        FakeResponse(json.dumps({**RELEASE, "tag_name": "v2.0.0"})
                                     .encode()))
    result = updater.update(tmp_path, verbose=False)
    assert result["action"] == "up to date"
    assert result["available"] is False


def test_binary_install_is_verified_and_keeps_a_backup(monkeypatch, tmp_path):
    """The packaged path: checksum checked, old binary kept as .bak."""
    payload = b"#!/bin/sh\necho new engine\n"
    import hashlib
    digest = hashlib.sha256(payload).hexdigest()
    name = updater.asset_name()
    urls = {name: "https://example.invalid/" + name,
            "SHA256SUMS.txt": "https://example.invalid/SHA256SUMS.txt"}

    def fake_open(url, timeout=updater.TIMEOUT):
        if url.endswith("SHA256SUMS.txt"):
            return FakeResponse(f"{digest}  {name}\n".encode())
        return FakeResponse(payload)

    monkeypatch.setattr(updater, "_open", fake_open)
    home = tmp_path / "home"
    dest = home / "bin" / "simurgh-go"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"old engine")
    os.chmod(dest, 0o755)

    report = updater.update_binary(dest, "v9.9.9", urls)
    assert "installed" in report
    assert dest.read_bytes() == payload
    backup = dest.parent / (dest.name + ".bak")
    assert backup.read_bytes() == b"old engine"
    assert oct(dest.stat().st_mode & 0o777) == oct(0o755)

    # a corrupted download must not touch the install
    monkeypatch.setattr(updater, "_open",
                        lambda url, timeout=updater.TIMEOUT:
                        FakeResponse(b"tampered" if url != urls["SHA256SUMS.txt"]
                                     else f"{digest}  {name}\n".encode()))
    with pytest.raises(updater.UpdateError) as err:
        updater.update_binary(dest, "v9.9.9", urls)
    assert "checksum" in str(err.value)
    assert dest.read_bytes() == payload


def test_update_finds_a_packaged_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "_open",
                        lambda url, timeout=updater.TIMEOUT:
                        FakeResponse(json.dumps(RELEASE).encode()))
    name = updater.asset_name()
    urls = {name: "https://example.invalid/" + name,
            "SHA256SUMS.txt": "https://example.invalid/SHA256SUMS.txt"}
    monkeypatch.setattr(updater, "check",
                        lambda: {"current": "1.0.0", "latest": "9.9.9",
                                 "tag": "v9.9.9", "name": "x", "published": "",
                                 "notes_url": "", "assets": [name],
                                 "asset_urls": urls, "available": True})
    monkeypatch.setattr(updater, "source_checkout", lambda: None)
    monkeypatch.setattr(updater, "reinstall_release",
                        lambda tag: f"Python release {tag} installed")

    home = tmp_path / "home"
    dest = home / "bin" / "simurgh-go"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"old")
    monkeypatch.setattr(updater, "_download", lambda url, target: target.write_bytes(b"new"))
    import hashlib
    digest = hashlib.sha256(b"new").hexdigest()
    monkeypatch.setattr(updater, "checksums_for", lambda urls: {name: digest})
    result = updater.update(home, verbose=False)
    assert result["action"] == "package install updated to v9.9.9"
    assert result["steps"][0] == "Python release v9.9.9 installed"
    assert dest.read_bytes() == b"new"


@pytest.mark.parametrize("url", [
    "http://example.invalid/release",
    "file:///tmp/engine",
    "https://[broken-ipv6/release",
])
def test_update_transport_refuses_a_non_https_or_malformed_url(url):
    with pytest.raises(updater.UpdateError, match="HTTPS"):
        updater._open(url)


def test_binary_update_refuses_a_missing_checksum(monkeypatch, tmp_path):
    """No checksum means no installation, never a silent security downgrade."""
    name = updater.asset_name()
    dest = tmp_path / "simurgh-go"
    dest.write_bytes(b"known-good")
    monkeypatch.setattr(updater, "checksums_for", lambda urls: {})
    monkeypatch.setattr(updater, "_download",
                        lambda url, target: target.write_bytes(b"untrusted"))
    with pytest.raises(updater.UpdateError, match="checksum"):
        updater.update_binary(dest, "v9.9.9", {name: "https://example.invalid/" + name})
    assert dest.read_bytes() == b"known-good"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.mark.skipif(not updater.shutil.which("git"), reason="git is not installed")
def test_git_install_moves_the_checkout_to_the_tag(tmp_path):
    """The checkout path: fetch, checkout the tag, and say what it did."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-q")

    work = tmp_path / "work"
    work.mkdir()
    (work / "tools").mkdir()
    (work / "tools" / "build-go.sh").write_text("#!/bin/sh\nexit 0\n")
    (work / "simurgh").mkdir()
    (work / "simurgh" / "cli.py").write_text("# cli\n")
    _git(work, "init", "-q")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "one")
    _git(work, "tag", "-a", "v1.0.0", "-m", "one")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "HEAD:v1.0.0")

    # a second commit, tagged v2.0.0, that only exists on the remote
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True,
                   capture_output=True)
    (other / "README.md").write_text("two\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "two")
    _git(other, "tag", "-a", "v2.0.0", "-m", "two")
    _git(other, "push", "-q", "--tags", "origin", "HEAD:main")

    assert updater.git_root(work / "simurgh") == work
    steps = updater.update_git(work, "v2.0.0", verbose=False)
    assert (work / "README.md").exists(), "the checkout did not move to the tag"
    assert any("checked out v2.0.0" in step for step in steps)
    assert any("fetched" in step for step in steps)

    with pytest.raises(updater.UpdateError):
        updater.update_git(work, "v404.0.0", verbose=False)


def test_cli_update_check_stays_offline(capsys, monkeypatch, tmp_path):
    """The command exists, is wired to argparse, and reports a network failure."""
    from simurgh import cli

    def boom(url, timeout=updater.TIMEOUT):
        raise OSError("no route to host")

    monkeypatch.setattr(updater, "_open", boom)
    args = cli.build_parser().parse_args(["--home", str(tmp_path), "update", "--check"])
    assert args.func(args) == 1
    assert "no route to host" in capsys.readouterr().err

    # ...and with a stubbed release it reports the newest version
    monkeypatch.setattr(updater, "_open",
                        lambda url, timeout=updater.TIMEOUT:
                        FakeResponse(json.dumps(RELEASE).encode()))
    args = cli.build_parser().parse_args(["--home", str(tmp_path), "update",
                                          "--check", "--json"])
    assert args.func(args) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["latest"] == "9.9.9" and payload["available"] is True


def test_cli_json_update_still_restarts_without_corrupting_json(
        tmp_path, monkeypatch, capsys):
    from simurgh import cli

    monkeypatch.setattr(updater, "update", lambda path, force=False: {
        "current": "1.0.0", "latest": "9.9.9", "tag": "v9.9.9",
        "available": True, "action": "updated", "restart_required": True,
    })
    calls = []

    def restart(home, expected_version=None):
        calls.append((home.path, expected_version))
        print("human restart output that must be captured")
        return 0

    monkeypatch.setattr(cli, "_restart_updated_service", restart)
    args = cli.build_parser().parse_args([
        "--home", str(tmp_path), "update", "--restart", "--json",
    ])
    assert args.func(args) == 0
    assert calls == [(tmp_path, "9.9.9")]
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["restart_ok"] is True
    assert "human restart output" not in output


def test_direct_url_recovers_the_installers_source_checkout(tmp_path, monkeypatch):
    """site-packages is not inside /opt/simurgh-src; pip's direct_url is the
    reliable link back to the checkout used by install.sh."""
    root = tmp_path / "simurgh-src"
    (root / ".git").mkdir(parents=True)
    (root / "simurgh").mkdir()
    (root / "simurgh" / "cli.py").write_text("# cli")

    class Distribution:
        def read_text(self, name):
            assert name == "direct_url.json"
            return json.dumps({"url": root.as_uri()})

    monkeypatch.setattr(updater.importlib.metadata, "distribution",
                        lambda name: Distribution())
    assert updater._direct_url_checkout() == root


def test_source_update_reinstalls_python_and_the_real_engine(tmp_path, monkeypatch):
    """The default install has three locations: source, venv and /usr/local.
    A successful update must address all applicable ones and accept util.Home
    without crashing on path operations."""
    from simurgh.util import Home

    root = tmp_path / "source"
    root.mkdir()
    home = Home(tmp_path / "etc")
    engine = home.path / "bin" / "simurgh-go"
    engine.parent.mkdir(parents=True)
    engine.write_bytes(b"old")
    urls = {"simurgh-go-linux-amd64": "https://example.invalid/engine"}
    monkeypatch.setattr(updater, "check", lambda: {
        "current": "1.0.0", "latest": "9.9.9", "tag": "v9.9.9",
        "name": "new", "published": "", "notes_url": "", "assets": [],
        "asset_urls": urls, "available": True,
    })
    monkeypatch.setattr(updater, "source_checkout", lambda: root)
    monkeypatch.setattr(updater, "update_git",
                        lambda root, tag, verbose=True: [f"checked out {tag}"])
    calls = []
    monkeypatch.setattr(updater, "reinstall_package",
                        lambda source: calls.append(("python", source)) or "reinstalled")
    monkeypatch.setattr(updater, "update_binary",
                        lambda dest, tag, assets:
                        calls.append(("go", dest, tag, assets)) or "engine updated")

    result = updater.update(home, verbose=False)

    assert result["restart_required"] is True
    assert result["steps"] == ["checked out v9.9.9", "reinstalled", "engine updated"]
    assert calls[0] == ("python", root)
    assert calls[1] == ("go", engine, "v9.9.9", urls)


def test_package_reinstall_uses_the_running_venv_first_offline(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(updater, "_run",
                        lambda cmd, cwd=None: commands.append((cmd, cwd)) or (0, ""))
    root = tmp_path / "source"
    assert "reinstalled" in updater.reinstall_package(root)
    command, cwd = commands[0]
    assert command[:3] == [updater.sys.executable, "-m", "pip"]
    assert "--no-build-isolation" in command
    assert command[-1] == str(root) and cwd == root


def test_non_git_python_update_uses_the_same_release_tag(monkeypatch):
    commands = []
    monkeypatch.setattr(updater, "_run",
                        lambda cmd, cwd=None: commands.append(cmd) or (0, ""))
    assert "v9.9.9" in updater.reinstall_release("v9.9.9")
    assert len(commands) == 1
    assert commands[0][-1] == (
        "https://github.com/Dark-Sky07/Simurgh-Tunnel/archive/refs/tags/v9.9.9.tar.gz")
    assert commands[0][:3] == [updater.sys.executable, "-m", "pip"]

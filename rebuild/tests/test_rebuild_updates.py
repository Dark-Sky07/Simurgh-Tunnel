from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rebuild import updates


def _release(version="0.3.0", archive_url="https://github.com/Dark-Sky07/Simurgh-Tunnel/releases/download/x/archive"):
    archive_name = f"simurgh-tunnel-{version}.tar.gz"
    return {
        "tag_name": f"simurgh-next-v{version}",
        "draft": False,
        "prerelease": False,
        "_simurgh_version": version,
        "assets": [
            {"name": archive_name, "browser_download_url": archive_url, "size": 64},
            {"name": "SHA256SUMS.txt",
             "browser_download_url": "https://github.com/Dark-Sky07/Simurgh-Tunnel/releases/download/x/SHA256SUMS.txt",
             "size": 100},
            {"name": f"{archive_name}.sigstore.json",
             "browser_download_url": f"https://github.com/Dark-Sky07/Simurgh-Tunnel/releases/download/x/{archive_name}.sigstore.json",
             "size": 1024},
        ],
    }


def test_release_filter_ignores_old_project_tags_and_prereleases(monkeypatch):
    releases = [
        {"tag_name": "v3.2.15", "draft": False, "prerelease": False, "assets": []},
        {"tag_name": "simurgh-next-v9.0.0", "draft": True, "prerelease": False, "assets": []},
        {"tag_name": "simurgh-next-v8.0.0", "draft": False, "prerelease": True, "assets": []},
        _release("0.3.0"),
        _release("0.2.0"),
    ]
    monkeypatch.setattr(updates, "_read_url", lambda *_args, **_kwargs: json.dumps(releases).encode())
    latest = updates.latest_update("0.2.0")
    assert latest is not None
    assert latest["_simurgh_version"] == "0.3.0"


def test_release_listing_accepts_setuptools_normalized_source_asset(monkeypatch):
    release = _release("0.3.0")
    release["assets"][0]["name"] = "simurgh_tunnel-0.3.0.tar.gz"
    release["assets"][2]["name"] = "simurgh_tunnel-0.3.0.tar.gz.sigstore.json"
    monkeypatch.setattr(updates, "_read_url", lambda *_args, **_kwargs: json.dumps([release]).encode())
    latest = updates.latest_update("0.2.0")
    assert latest is not None and latest["_simurgh_version"] == "0.3.0"


def test_url_allowlist_rejects_credentials_and_nonstandard_ports():
    assert not updates._approved_https_url("https://user:pass@github.com/release")
    assert not updates._approved_https_url("https://github.com:444/release")
    assert updates._approved_https_url("https://api.github.com/releases", api=True)


def test_release_listing_rejects_an_unapproved_asset_host(monkeypatch):
    release = _release(archive_url="https://attacker.invalid/source.tar.gz")
    monkeypatch.setattr(updates, "_read_url", lambda *_args, **_kwargs: json.dumps([release]).encode())
    with pytest.raises(updates.UpdateError, match="unapproved download URL"):
        updates.latest_update("0.2.0")


def test_latest_update_returns_none_for_current_version(monkeypatch):
    releases = [_release("0.2.0")]
    monkeypatch.setattr(updates, "_read_url", lambda *_args, **_kwargs: json.dumps(releases).encode())
    assert updates.latest_update("0.2.0") is None


def test_download_accepts_matching_sha256(tmp_path, monkeypatch):
    archive_name = "simurgh-tunnel-0.3.0.tar.gz"
    payload = b"release source archive"
    digest = hashlib.sha256(payload).hexdigest()
    checksums = f"{digest}  {archive_name}\n".encode()
    values = {"archive": payload, "SHA256SUMS.txt": checksums,
              "simurgh-tunnel-0.3.0.tar.gz.sigstore.json": b"bundle"}
    monkeypatch.setattr(updates, "_read_url", lambda url, **_kwargs: values[url.rsplit('/', 1)[-1]])
    monkeypatch.setattr(updates, "_verify_github_signature", lambda *_args: None)
    archive = updates._download_verified_source(_release(), tmp_path)
    assert archive.read_bytes() == payload


def test_download_accepts_setuptools_normalized_source_filename(tmp_path, monkeypatch):
    archive_name = "simurgh_tunnel-0.3.0.tar.gz"
    payload = b"setuptools source archive"
    digest = hashlib.sha256(payload).hexdigest()
    values = {"archive": payload,
              "SHA256SUMS.txt": f"{digest}  {archive_name}\n".encode(),
              f"{archive_name}.sigstore.json": b"bundle"}
    monkeypatch.setattr(updates, "_read_url", lambda url, **_kwargs: values[url.rsplit('/', 1)[-1]])
    monkeypatch.setattr(updates, "_verify_github_signature", lambda *_args: None)
    release = _release()
    release["assets"][0]["name"] = archive_name
    release["assets"][2]["name"] = f"{archive_name}.sigstore.json"
    release["assets"][2]["browser_download_url"] = f"https://github.com/Dark-Sky07/Simurgh-Tunnel/releases/download/x/{archive_name}.sigstore.json"
    archive = updates._download_verified_source(release, tmp_path)
    assert archive.name == archive_name and archive.read_bytes() == payload


def test_download_rejects_ambiguous_duplicate_checksum_lines(tmp_path, monkeypatch):
    archive_name = "simurgh-tunnel-0.3.0.tar.gz"
    payload = b"release source archive"
    digest = hashlib.sha256(payload).hexdigest()
    checksums = f"{digest}  {archive_name}\n{digest}  {archive_name}\n".encode()
    values = {"archive": payload, "SHA256SUMS.txt": checksums,
              "simurgh-tunnel-0.3.0.tar.gz.sigstore.json": b"bundle"}
    monkeypatch.setattr(updates, "_read_url", lambda url, **_kwargs: values[url.rsplit('/', 1)[-1]])
    with pytest.raises(updates.UpdateError, match="ambiguous source digests"):
        updates._download_verified_source(_release(), tmp_path)


def test_download_requires_matching_sha256(tmp_path, monkeypatch):
    archive_name = "simurgh-tunnel-0.3.0.tar.gz"
    payload = b"release source archive"
    checksums = f"{'0' * 64}  {archive_name}\n".encode()
    values = {"archive": payload, "SHA256SUMS.txt": checksums,
              "simurgh-tunnel-0.3.0.tar.gz.sigstore.json": b"bundle"}
    monkeypatch.setattr(updates, "_read_url", lambda url, **_kwargs: values[url.rsplit('/', 1)[-1]])
    release = _release()
    with pytest.raises(updates.UpdateError, match="checksum mismatch"):
        updates._download_verified_source(release, tmp_path)


def test_signature_verifier_pins_expected_github_workflow_claims(tmp_path, monkeypatch):
    archive = tmp_path / "source.tar.gz"
    bundle = tmp_path / "source.tar.gz.sigstore.json"
    calls = []
    monkeypatch.setattr(updates.subprocess, "run",
                        lambda command, **kwargs: calls.append((command, kwargs)))
    updates._verify_github_signature(archive, bundle, "0.3.0")
    command, kwargs = calls[0]
    assert command[:4] == [sys.executable, "-m", "sigstore", "verify"]
    assert command[4:6] == ["github", str(archive)]
    assert "--bundle" in command and str(bundle) in command
    assert "--offline" in command
    assert command[command.index("--repository") + 1] == "Dark-Sky07/Simurgh-Tunnel"
    assert command[command.index("--cert-identity") + 1] == (
        "https://github.com/Dark-Sky07/Simurgh-Tunnel/.github/workflows/"
        "release-binaries.yml@refs/tags/simurgh-next-v0.3.0"
    )
    assert command[command.index("--name") + 1] == "Release Python package"
    assert command[command.index("--trigger") + 1] == "release"
    assert command[command.index("--ref") + 1] == "refs/tags/simurgh-next-v0.3.0"
    assert kwargs["check"] and kwargs["capture_output"]


def test_signature_verification_failure_is_fail_closed(tmp_path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "sigstore")

    monkeypatch.setattr(updates.subprocess, "run", fail)
    with pytest.raises(updates.UpdateError, match="provenance verification failed"):
        updates._verify_github_signature(tmp_path / "source.tar.gz",
                                        tmp_path / "bundle.json", "0.3.0")


def test_latest_update_rejects_release_without_provenance_bundle(monkeypatch):
    release = _release()
    release["assets"].pop()
    monkeypatch.setattr(updates, "_read_url", lambda *_args, **_kwargs: json.dumps([release]).encode())
    with pytest.raises(updates.UpdateError, match="missing required asset"):
        updates.latest_update("0.2.0")


def _make_source_archive(path: Path, *, version="0.3.0", unsafe=False,
                         stray=False, underscore=False):
    separator = "_" if underscore else "-"
    project = f"simurgh{separator}tunnel-{version}"
    with tarfile.open(path, "w:gz") as archive:
        files = {
            f"{project}/install.sh": b"#!/bin/sh\nexit 0\n",
            f"{project}/rebuild/tunnel.py": f'VERSION = "{version}"\n'.encode(),
        }
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o755 if name.endswith("install.sh") else 0o644
            archive.addfile(member, io.BytesIO(data))
        if unsafe:
            member = tarfile.TarInfo(f"{project}/../../outside")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
        if stray:
            member = tarfile.TarInfo("unscoped-file.txt")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))


def test_safe_extract_accepts_expected_source_layout(tmp_path):
    archive = tmp_path / "source.tar.gz"
    _make_source_archive(archive)
    project = updates._safe_extract(archive, tmp_path / "extract")
    assert project.name == "simurgh-tunnel-0.3.0"
    assert (project / "install.sh").stat().st_mode & 0o777 == 0o755


def test_safe_extract_accepts_setuptools_normalized_project_root(tmp_path):
    archive = tmp_path / "normalized.tar.gz"
    _make_source_archive(archive, underscore=True)
    project = updates._safe_extract(archive, tmp_path / "extract")
    assert project.name == "simurgh_tunnel-0.3.0"


def test_safe_extract_rejects_entries_outside_project_root(tmp_path):
    archive = tmp_path / "stray.tar.gz"
    _make_source_archive(archive, stray=True)
    with pytest.raises(updates.UpdateError, match="outside its project directory"):
        updates._safe_extract(archive, tmp_path / "extract")


def test_safe_extract_rejects_path_traversal(tmp_path):
    archive = tmp_path / "unsafe.tar.gz"
    _make_source_archive(archive, unsafe=True)
    with pytest.raises(updates.UpdateError, match="unsafe path"):
        updates._safe_extract(archive, tmp_path / "extract")
    assert not (tmp_path / "outside").exists()


def test_user_update_reuses_custom_install_and_managed_service_paths(tmp_path, monkeypatch):
    prefix = tmp_path / "custom install"
    bin_dir = tmp_path / "custom bin"
    config_dir = tmp_path / "custom config"
    service_dir = tmp_path / "custom systemd/user"
    service_dir.mkdir(parents=True)
    (service_dir / "simurgh-next-panel.service").touch()
    monkeypatch.setenv("SIMURGH_NEXT_PREFIX", str(prefix))
    monkeypatch.setenv("SIMURGH_NEXT_BIN_DIR", str(bin_dir))
    monkeypatch.setenv("SIMURGH_NEXT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("SIMURGH_NEXT_SERVICE_DIR", str(service_dir))
    command = updates._install_command(tmp_path, mode="user")
    assert command == ["sh", str(tmp_path / "install.sh"), "--enable-service"]


def test_system_update_uses_privilege_escalation_and_preserves_config_path(tmp_path, monkeypatch):
    archive = tmp_path / "source.tar.gz"
    archive.write_bytes(b"verified")
    project = tmp_path / "source"
    project.mkdir()
    calls = []
    monkeypatch.setattr(updates, "_download_verified_source", lambda _r, _d: archive)
    monkeypatch.setattr(updates, "_safe_extract", lambda _a, _d: project)
    monkeypatch.setattr(updates.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(updates.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    config_dir = tmp_path / "config"
    updates.install_release(_release(), config_dir=config_dir, mode="system")
    command, options = calls[0]
    assert command[:2] == ["sudo", "sh"]
    assert command[-1] == "--system"
    assert options["env"]["SIMURGH_NEXT_CONFIG_DIR"] == str(config_dir)

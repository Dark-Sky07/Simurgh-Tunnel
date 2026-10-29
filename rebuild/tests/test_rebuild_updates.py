from __future__ import annotations

import hashlib
import io
import json
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
    values = {"archive": payload, "SHA256SUMS.txt": checksums}
    monkeypatch.setattr(updates, "_read_url", lambda url, **_kwargs: values[url.rsplit('/', 1)[-1]])
    archive = updates._download_verified_source(_release(), tmp_path)
    assert archive.read_bytes() == payload


def test_download_accepts_setuptools_normalized_source_filename(tmp_path, monkeypatch):
    archive_name = "simurgh_tunnel-0.3.0.tar.gz"
    payload = b"setuptools source archive"
    digest = hashlib.sha256(payload).hexdigest()
    values = {"archive": payload,
              "SHA256SUMS.txt": f"{digest}  {archive_name}\n".encode()}
    monkeypatch.setattr(updates, "_read_url", lambda url, **_kwargs: values[url.rsplit('/', 1)[-1]])
    release = _release()
    release["assets"][0]["name"] = archive_name
    archive = updates._download_verified_source(release, tmp_path)
    assert archive.name == archive_name and archive.read_bytes() == payload


def test_download_requires_matching_sha256(tmp_path, monkeypatch):
    archive_name = "simurgh-tunnel-0.3.0.tar.gz"
    payload = b"release source archive"
    checksums = f"{'0' * 64}  {archive_name}\n".encode()
    values = {"archive": payload, "SHA256SUMS.txt": checksums}
    monkeypatch.setattr(updates, "_read_url", lambda url, **_kwargs: values[url.rsplit('/', 1)[-1]])
    release = _release()
    with pytest.raises(updates.UpdateError, match="checksum mismatch"):
        updates._download_verified_source(release, tmp_path)


def _make_source_archive(path: Path, *, version="0.3.0", unsafe=False):
    project = f"simurgh-tunnel-{version}"
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


def test_safe_extract_accepts_expected_source_layout(tmp_path):
    archive = tmp_path / "source.tar.gz"
    _make_source_archive(archive)
    project = updates._safe_extract(archive, tmp_path / "extract")
    assert project.name == "simurgh-tunnel-0.3.0"
    assert (project / "install.sh").stat().st_mode & 0o777 == 0o755


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

"""Verified update discovery and installation from namespaced GitHub releases."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

from .paths import default_config_dir
from .tunnel import VERSION

_REPOSITORY = "Dark-Sky07/Simurgh-Tunnel"
_RELEASES_API = f"https://api.github.com/repos/{_REPOSITORY}/releases"
_TAG_PREFIX = "simurgh-next-v"
_MAX_METADATA = 2 * 1024 * 1024
_MAX_ASSET = 20 * 1024 * 1024
_MAX_EXTRACTED = 64 * 1024 * 1024
_ALLOWED_ASSET_HOSTS = {"github.com", "release-assets.githubusercontent.com",
                        "objects.githubusercontent.com"}


class UpdateError(RuntimeError):
    """Release metadata or update assets failed validation."""


def _version_key(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise UpdateError(f"invalid release version {value!r}")
    return tuple(int(part) for part in value.split("."))


def _approved_https_url(url: str, *, api: bool = False) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    allowed = _ALLOWED_ASSET_HOSTS | ({"api.github.com"} if api else set())
    return (parsed.scheme == "https" and parsed.hostname in allowed
            and parsed.username is None and parsed.password is None
            and port in (None, 443) and not parsed.fragment)


class _GitHubRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _approved_https_url(newurl, api=True):
            raise UpdateError("GitHub redirected to an unapproved download host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_url(url: str, *, limit: int, timeout: float = 10.0) -> bytes:
    if not _approved_https_url(url, api=True):
        raise UpdateError("release URL is not an approved GitHub HTTPS address")
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "Simurgh-Tunnel-Independent-Updater",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        opener = urllib.request.build_opener(_GitHubRedirectHandler())
        with opener.open(request, timeout=timeout) as response:
            if not _approved_https_url(response.geturl(), api=True):
                raise UpdateError("GitHub redirected to an unapproved download host")
            data = response.read(limit + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"could not retrieve release information: {exc}") from exc
    if len(data) > limit:
        raise UpdateError("download exceeds its configured size limit")
    return data


def list_project_releases() -> list[dict[str, Any]]:
    """Return matching published, non-draft releases, newest semantic version first."""
    matching = []
    for page in range(1, 6):
        url = f"{_RELEASES_API}?per_page=100&page={page}"
        try:
            releases = json.loads(_read_url(url, limit=_MAX_METADATA))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise UpdateError("GitHub returned malformed release metadata") from exc
        if not isinstance(releases, list):
            raise UpdateError("GitHub release metadata has an unexpected shape")
        for release in releases:
            if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
                continue
            tag = release.get("tag_name")
            if not isinstance(tag, str) or not tag.startswith(_TAG_PREFIX):
                continue
            version = tag[len(_TAG_PREFIX):]
            try:
                key = _version_key(version)
            except UpdateError:
                continue
            if isinstance(release.get("assets"), list):
                matching.append((key, version, release))
        if len(releases) < 100:
            break
    matching.sort(key=lambda item: item[0], reverse=True)
    return [dict(release, _simurgh_version=version) for _, version, release in matching]


def _source_archive_name(release: dict[str, Any], version: str) -> str:
    expected = {
        f"simurgh-tunnel-{version}.tar.gz",
        f"simurgh_tunnel-{version}.tar.gz",
    }
    matches = [asset.get("name") for asset in release.get("assets", [])
               if isinstance(asset, dict) and asset.get("name") in expected]
    if len(matches) != 1:
        if not matches:
            raise UpdateError("release is missing its versioned source archive")
        raise UpdateError("release contains ambiguous versioned source archives")
    return matches[0]


def _signature_bundle_name(archive_name: str) -> str:
    return f"{archive_name}.sigstore.json"


def latest_update(current_version: str = VERSION) -> dict[str, Any] | None:
    current = _version_key(current_version)
    for release in list_project_releases():
        version = release["_simurgh_version"]
        if _version_key(version) <= current:
            continue
        archive_name = _source_archive_name(release, version)
        _asset(release, archive_name)
        _asset(release, "SHA256SUMS.txt")
        _asset(release, _signature_bundle_name(archive_name))
        return release
    return None


def _asset(release: dict[str, Any], name: str) -> dict[str, Any]:
    assets = release.get("assets", [])
    for asset in assets:
        if isinstance(asset, dict) and asset.get("name") == name:
            url = asset.get("browser_download_url")
            if isinstance(url, str) and _approved_https_url(url):
                return asset
            raise UpdateError(f"release asset {name} has an unapproved download URL")
    raise UpdateError(f"release is missing required asset {name}")


def _verify_github_signature(archive: Path, bundle: Path, version: str) -> None:
    """Verify Sigstore identity, GitHub workflow claims, and the archive subject digest."""
    vendor = Path(__file__).resolve().parent.parent / "vendor"
    environment = os.environ.copy()
    pythonpath = [str(vendor), environment.get("PYTHONPATH", "")]
    environment["PYTHONPATH"] = os.pathsep.join(path for path in pythonpath if path)
    command = [
        sys.executable, "-m", "sigstore", "verify", "github", str(archive),
        "--bundle", str(bundle), "--offline", "--repository", _REPOSITORY,
        "--cert-identity",
        f"https://github.com/{_REPOSITORY}/.github/workflows/release-binaries.yml@refs/tags/{_TAG_PREFIX}{version}",
        "--name", "Release Python package", "--trigger", "release",
        "--ref", f"refs/tags/{_TAG_PREFIX}{version}",
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, env=environment,
                       timeout=120)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(
            "source archive Sigstore provenance verification failed; refusing to install"
        ) from exc


def _download_verified_source(release: dict[str, Any], directory: Path) -> Path:
    version = release["_simurgh_version"]
    archive_name = _source_archive_name(release, version)
    archive_asset = _asset(release, archive_name)
    sums_asset = _asset(release, "SHA256SUMS.txt")
    bundle_asset = _asset(release, _signature_bundle_name(archive_name))
    archive_size = archive_asset.get("size")
    if isinstance(archive_size, int) and archive_size > _MAX_ASSET:
        raise UpdateError("source archive is larger than the configured download limit")
    sums_size = sums_asset.get("size")
    if isinstance(sums_size, int) and sums_size > 128 * 1024:
        raise UpdateError("release checksum file is too large")
    bundle_size = bundle_asset.get("size")
    if isinstance(bundle_size, int) and bundle_size > _MAX_METADATA:
        raise UpdateError("Sigstore provenance bundle is too large")
    archive_data = _read_url(archive_asset["browser_download_url"], limit=_MAX_ASSET)
    sums_data = _read_url(sums_asset["browser_download_url"], limit=128 * 1024)
    bundle_data = _read_url(bundle_asset["browser_download_url"], limit=_MAX_METADATA)
    matching_digests = []
    for line in sums_data.decode("ascii", "strict").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", line)
        if match and match.group(2) == archive_name:
            matching_digests.append(match.group(1).lower())
    if len(matching_digests) != 1:
        if not matching_digests:
            raise UpdateError("release checksum file does not contain the source archive")
        raise UpdateError("release checksum file contains ambiguous source digests")
    expected = matching_digests[0]
    actual = hashlib.sha256(archive_data).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise UpdateError("source archive SHA-256 checksum mismatch")
    archive = directory / archive_name
    bundle = directory / _signature_bundle_name(archive_name)
    archive.write_bytes(archive_data)
    bundle.write_bytes(bundle_data)
    _verify_github_signature(archive, bundle, version)
    return archive


def _safe_extract(archive: Path, destination: Path) -> Path:
    total = 0
    root_candidates: set[str] = set()
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if len(members) > 5000:
            raise UpdateError("source archive contains too many entries")
        validated = []
        for member in members:
            name = PurePosixPath(member.name)
            if (name.is_absolute() or not name.parts
                    or any(part in ("", ".", "..") for part in name.parts)):
                raise UpdateError("source archive contains an unsafe path")
            if len(name.parts) == 1 and not member.isdir():
                raise UpdateError("source archive contains a file outside its project directory")
            root_candidates.add(name.parts[0])
            if not (member.isdir() or member.isfile()) or member.issym() or member.islnk():
                raise UpdateError("source archive contains a non-regular file")
            total += member.size
            if total > _MAX_EXTRACTED:
                raise UpdateError("source archive expands beyond the configured limit")
            validated.append((member, name))
        if len(root_candidates) != 1:
            raise UpdateError("source archive must contain one top-level project directory")
        for member, name in validated:
            path = destination.joinpath(*name.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                path.mkdir(exist_ok=True)
                continue
            source = tar.extractfile(member)
            if source is None:
                raise UpdateError("could not read source archive member")
            with source, path.open("xb") as output:
                shutil.copyfileobj(source, output, length=64 * 1024)
            path.chmod(0o755 if member.mode & 0o111 else 0o644)
    project = destination / next(iter(root_candidates))
    if not (project / "install.sh").is_file() or not (project / "rebuild/tunnel.py").is_file():
        raise UpdateError("verified source archive is missing the unified installer or runtime")
    try:
        source_version = (project / "rebuild/tunnel.py").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise UpdateError("could not inspect the source version") from exc
    project_version = re.fullmatch(r"simurgh[-_]tunnel-([0-9]+\.[0-9]+\.[0-9]+)",
                                   project.name)
    if (project_version is None
            or not re.search(rf'^VERSION = "{re.escape(project_version.group(1))}"$',
                             source_version, re.MULTILINE)):
        raise UpdateError("source archive project name/version does not match its runtime")
    return project


def _install_command(project: Path, *, mode: str) -> list[str]:
    installer = project / "install.sh"
    if mode == "system":
        command = ["sh", str(installer), "--system"]
        if os.geteuid() != 0:
            command.insert(0, "sudo")
        return command
    if mode != "user":
        raise UpdateError("install mode must be user or system")
    service_dir = Path(os.environ.get(
        "SIMURGH_NEXT_SERVICE_DIR",
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user",
    ))
    enable_service = (service_dir / "simurgh-next-panel.service").is_file()
    command = ["sh", str(installer)]
    if enable_service:
        command.append("--enable-service")
    return command


def install_release(release: dict[str, Any], *, config_dir: Path, mode: str) -> None:
    with tempfile.TemporaryDirectory(prefix="simurgh-next-update-") as temp:
        staging = Path(temp)
        archive = _download_verified_source(release, staging)
        try:
            project = _safe_extract(archive, staging / "source")
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise UpdateError(f"source archive could not be safely extracted: {exc}") from exc
        command = _install_command(project, mode=mode)
        environment = os.environ.copy()
        environment["SIMURGH_NEXT_CONFIG_DIR"] = str(config_dir)
        try:
            subprocess.run(command, check=True, env=environment)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise UpdateError(f"installer failed; current installation was not confirmed updated: {exc}") from exc


def interactive_update(config_dir: Path) -> int:
    mode = os.environ.get("SIMURGH_NEXT_INSTALL_MODE", "user")
    try:
        release = latest_update()
        if release is None:
            print(f"Simurgh Tunnel {VERSION} is up to date (no newer stable rebuild release found).")
            return 0
        version = release["_simurgh_version"]
        print(f"A source release, Simurgh Tunnel {version}, is available for provenance verification.")
        answer = input("Download, verify and install it now? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            return 0
        install_release(release, config_dir=config_dir, mode=mode)
        print(f"Simurgh Tunnel {version} installed; previous code releases are retained.")
        return 0
    except (UpdateError, OSError, ValueError) as exc:
        print(f"Update failed safely: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check or install a verified Simurgh release")
    parser.add_argument("--check", action="store_true", help="check only; do not install")
    parser.add_argument("--apply", action="store_true", help="download, verify and install")
    parser.add_argument("--config-dir", type=Path, default=default_config_dir())
    parser.add_argument("--mode", choices=("user", "system"), default="user")
    args = parser.parse_args(argv)
    if args.apply:
        if args.check:
            parser.error("choose only one of --check or --apply")
        os.environ["SIMURGH_NEXT_INSTALL_MODE"] = args.mode
        return interactive_update(args.config_dir)
    try:
        release = latest_update()
    except UpdateError as exc:
        print(f"Update check failed: {exc}", file=sys.stderr)
        return 2
    if release is None:
        print(f"Simurgh Tunnel {VERSION} is up to date.")
    else:
        print(f"Simurgh Tunnel {release['_simurgh_version']} is available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""``simurgh update`` — bring this install to the newest release.

Two shapes of install have to be handled, and the command works out which one
it is looking at:

* **a git checkout** (what ``install.sh`` does by default): the newest tag is
  fetched and checked out, the Python package is reinstalled into the running
  venv, and the actual installed Go engine is updated too;
* **a packaged install** (pip/wheel, or someone who copied the files): Python
  is installed from the same release tag, then the release asset for this
  OS/architecture is downloaded, its SHA-256 is checked against the release's
  ``SHA256SUMS.txt``, and the running binary is replaced atomically — the old
  one is kept as ``.bak``.

The web panel and text menu call this same implementation.  None of these
paths opens, rewrites or deletes the relay/exit TOML configuration.

Nothing here is needed to *run* Simurgh, and every network step is optional:
when the release cannot be checked the command exits before touching the
install.  A later failure is raised instead of being reported as a complete
update.  ``--check`` only reports.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from . import __product__, __version__
from .util import get_logger

log = get_logger("simurgh.update")

REPO = "Dark-Sky07/Simurgh-Tunnel"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
WEB_LATEST = f"https://github.com/{REPO}/releases/latest"
DOWNLOAD = f"https://github.com/{REPO}/releases/download"
#: where install.sh puts the compiled engine
BINARIES = {"linux": "simurgh-go-linux-{arch}", "darwin": "simurgh-go-darwin-{arch}",
            "windows": "simurgh-go-windows-amd64.exe"}
ARCHES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
          "armv7l": "armv7", "armv6l": "armv7"}
#: the redirect fallback can construct these immutable release asset names from
#: its tag; it never has to scrape HTML or trust an API inventory.
ENGINE_ASSETS = (
    "simurgh-go-darwin-amd64", "simurgh-go-darwin-arm64",
    "simurgh-go-linux-amd64", "simurgh-go-linux-arm64",
    "simurgh-go-linux-armv7", "simurgh-go-windows-amd64.exe",
)
RELEASE_ASSETS = ("SHA256SUMS.txt", *ENGINE_ASSETS)
TIMEOUT = 20.0


class UpdateError(RuntimeError):
    """Anything that means "cannot update from here, and here is why"."""


def _open(url: str, *, timeout: float = TIMEOUT):
    try:
        parsed = urlparse(url)
        valid = parsed.scheme == "https" and bool(parsed.hostname)
    except ValueError:
        valid = False
    if not valid:
        raise UpdateError("update downloads must use a valid HTTPS URL")
    request = urllib.request.Request(url, headers={
        "User-Agent": f"{__product__.replace(' ', '-')}/{__version__}",
        "Accept": ("application/vnd.github+json" if parsed.hostname == "api.github.com"
                   else "*/*"),
    })
    return urllib.request.urlopen(request, timeout=timeout)   # noqa: S310


def parse_version(text: str) -> tuple:
    """``v2.3.0`` / ``2.3.0-go`` / ``2.3.0rc1`` -> a sortable tuple."""
    cleaned = str(text).strip().lstrip("vV")
    for suffix in ("-go", "+go"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
    numbers: list[int] = []
    rest = ""
    for part in cleaned.replace("-", ".").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if digits != part:
            rest = part
        try:
            numbers.append(int(digits or 0))
        except ValueError:                                    # pragma: no cover
            numbers.append(0)
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers[:4]) + (rest,)


def _release_from_tag(tag: str) -> dict:
    """Build API-shaped release metadata from a trusted GitHub release tag.

    GitHub's asset download URLs are deterministic.  The release API is handy
    for dates and notes, but it is not required for a safe update: the engine
    and its SHA-256 manifest can always be fetched directly from the tag.
    """
    raw = str(tag)
    tag = raw.strip()
    if (not tag or raw != tag or len(tag) > 200
            or any(ch.isspace() or ch in "/\\?#" for ch in tag)):
        raise UpdateError("GitHub sent an invalid latest-release tag")
    encoded = quote(tag, safe="")
    base = f"{DOWNLOAD}/{encoded}"
    return {
        "tag_name": tag,
        "name": f"{__product__} {tag}",
        "html_url": f"https://github.com/{REPO}/releases/tag/{encoded}",
        "assets": [
            {"name": name, "browser_download_url": f"{base}/{quote(name, safe='')}"}
            for name in RELEASE_ASSETS
        ],
    }


def _newest_release_from_redirect() -> dict:
    """Discover the latest tag without using GitHub's rate-limited API.

    ``/releases/latest`` is GitHub's public, canonical redirect to the newest
    non-prerelease tag.  Validate the final origin and path before using any
    part of it; a redirect must never choose where update artifacts come from.
    """
    with _open(WEB_LATEST) as response:
        final_url = str(response.geturl() or "")
    parsed = urlparse(final_url)
    prefix = f"/{REPO}/releases/tag/"
    if (parsed.scheme != "https" or parsed.netloc.lower() != "github.com"
            or not parsed.path.startswith(prefix)):
        raise UpdateError("GitHub's latest-release redirect was not a trusted release tag")
    return _release_from_tag(unquote(parsed.path[len(prefix):]))


def newest_release() -> dict:
    """The newest published release, with a non-API fallback.

    Shared hosting addresses frequently exhaust GitHub's anonymous API quota,
    and some networks block only ``api.github.com``.  Neither case should make
    an otherwise reachable release impossible to install.
    """
    try:
        with _open(API_LATEST) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("the release response is not an object")
            return payload
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
            OSError, ValueError) as api_exc:
        try:
            return _newest_release_from_redirect()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                OSError, ValueError, UpdateError) as web_exc:
            raise UpdateError(
                "cannot discover the latest GitHub release "
                f"(API: {api_exc}; release page: {web_exc}); this install is unchanged"
            ) from web_exc


def asset_name(machine: str | None = None, system: str | None = None) -> str:
    """The release asset that fits this machine."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    if system not in BINARIES:
        raise UpdateError(f"no prebuilt engine for {system}")
    if system == "windows":
        return BINARIES["windows"]
    arch = ARCHES.get(machine)
    if arch is None:
        raise UpdateError(f"no prebuilt engine for {machine}")
    if arch == "arm64" and system == "linux":
        # linux arm64 covers both spellings the assets use
        return BINARIES["linux"].format(arch="arm64")
    return BINARIES[system].format(arch=arch)


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, target: Path) -> None:
    try:
        with _open(url, timeout=60.0) as response, open(target, "wb") as out:
            shutil.copyfileobj(response, out, 1 << 20)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        raise UpdateError(f"cannot download {url} ({exc})") from exc


def checksums_for(asset_urls: dict) -> dict[str, str]:
    """Read ``SHA256SUMS.txt`` from the release (missing file -> empty map)."""
    url = str(asset_urls.get("SHA256SUMS.txt") or "")
    if not url:
        return {}
    out: dict[str, str] = {}
    try:
        with _open(url) as response:
            for line in response.read().decode("utf-8", "replace").splitlines():
                parts = line.split()
                if len(parts) == 2:
                    out[parts[1].lstrip("*")] = parts[0]
    except Exception as exc:                                  # pragma: no cover
        log.warning("could not read SHA256SUMS.txt (%s)", exc)
    return out


def git_root(path: Path | None = None) -> Path | None:
    """The checkout this package lives in, when it is one."""
    start = (path or Path(__file__).resolve().parent).resolve()
    for folder in (start, *start.parents):
        if (folder / ".git").exists() and (folder / "simurgh" / "cli.py").exists():
            return folder
    return None


def _direct_url_checkout() -> Path | None:
    """Find the source a pip-installed local checkout came from.

    ``install.sh`` installs ``/opt/simurgh-src`` into its venv.  Once installed,
    the module lives in site-packages, so walking its parents cannot discover
    that checkout.  pip records the local source in ``direct_url.json``; use it
    instead of pretending the installation is a stand-alone binary package.
    """
    try:
        distribution = importlib.metadata.distribution("simurgh-tunnel")
        raw = distribution.read_text("direct_url.json")
        payload = json.loads(raw or "{}")
        parsed = urlparse(str(payload.get("url") or ""))
        if parsed.scheme == "file" and parsed.path:
            return git_root(Path(unquote(parsed.path)))
    except (importlib.metadata.PackageNotFoundError, OSError, ValueError, TypeError):
        pass
    return None


def source_checkout() -> Path | None:
    """The checkout used by the universal installer, if this is one."""
    root = git_root()
    if root is not None:
        return root
    root = _direct_url_checkout()
    if root is not None:
        return root
    candidates = []
    if os.environ.get("SIMURGH_SRC"):
        candidates.append(Path(os.environ["SIMURGH_SRC"]))
    candidates.append(Path("/opt/simurgh-src"))
    for candidate in candidates:
        root = git_root(candidate)
        if root is not None:
            return root
    return None


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True,
                              text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def check() -> dict:
    """Current version, newest release and whether an update is available."""
    release = newest_release()
    tag = str(release.get("tag_name") or "")
    if not tag:
        raise UpdateError("the latest release has no tag")
    assets = release.get("assets") or []
    return {
        "current": __version__,
        "latest": tag.lstrip("v"),
        "tag": tag,
        "name": str(release.get("name") or tag),
        "published": str(release.get("published_at") or ""),
        "notes_url": str(release.get("html_url") or ""),
        "assets": [str(a.get("name", "")) for a in assets],
        "asset_urls": {str(a.get("name", "")): str(a.get("browser_download_url", ""))
                       for a in assets},
        "available": parse_version(tag) > parse_version(__version__),
    }


def update_git(root: Path, tag: str, *, verbose: bool = True) -> list[str]:
    """Move a checkout to *tag* without touching anything under ``/etc``.

    The Python package and compiled engine are separate artifacts and are
    updated explicitly below.  In particular, do not silently leave an old Go
    engine behind merely because the server has no Go compiler installed.
    """
    steps: list[str] = []
    code, out = _run(["git", "fetch", "--tags", "--force", "origin"], root)
    if code != 0:
        raise UpdateError(f"git fetch failed: {out.strip()[:200]}")
    steps.append("fetched the tags")
    code, out = _run(["git", "checkout", "--force", tag], root)
    if code != 0:
        raise UpdateError(f"git checkout {tag} failed: {out.strip()[:200]}")
    steps.append(f"checked out {tag}")
    if verbose:
        for step in steps:
            print(f"  - {step}")
    return steps


def reinstall_package(root: Path) -> str:
    """Reinstall the updated checkout into the *running* Python environment.

    The first form is offline-friendly and uses the build tools the installer
    already put in the venv.  The fallback retains support for environments
    which need pip's isolated build environment instead.
    """
    base = [sys.executable, "-m", "pip", "install", "--no-deps",
            "--force-reinstall"]
    offline_cmd = [*base, "--no-build-isolation", str(root)]
    code, out = _run(offline_cmd, root)
    if code == 0:
        return f"reinstalled the Python package in {sys.prefix}"
    online_cmd = [*base, str(root)]
    fallback_code, fallback_out = _run(online_cmd, root)
    if fallback_code == 0:
        return f"reinstalled the Python package in {sys.prefix}"
    detail = (fallback_out or out).strip()[:300]
    raise UpdateError(f"the source was fetched but pip could not reinstall it: {detail}")


def reinstall_release(tag: str) -> str:
    """Install Python code from the same release tag on non-git installs.

    Updating only simurgh-go would leave the panel, CLI and protocol supervisor
    on the old version.  Use pip's direct source URL rather than assuming this
    project exists on PyPI; an externally-managed system Python can still
    refuse the operation, in which case the user gets an error, not a false
    "everything was updated" success.
    """
    url = (f"https://github.com/{REPO}/archive/refs/tags/"
           f"{quote(tag, safe='')}.tar.gz")
    base = [sys.executable, "-m", "pip", "install", "--no-deps",
            "--force-reinstall"]
    code, out = _run([*base, "--no-build-isolation", url])
    if code == 0:
        return f"installed the Python release {tag} in {sys.prefix}"
    fallback_code, fallback_out = _run([*base, url])
    if fallback_code == 0:
        return f"installed the Python release {tag} in {sys.prefix}"
    detail = (fallback_out or out).strip()[:300]
    raise UpdateError(f"pip could not install the Python release {tag}: {detail}")


def update_binary(dest: Path, tag: str, asset_urls: dict) -> str:
    """Download this machine's asset, verify it and install it."""
    name = asset_name()
    url = str(asset_urls.get(name) or "")
    if not url:
        raise UpdateError(f"release {tag} has no asset called {name}")
    sums = checksums_for(asset_urls)
    wanted = sums.get(name)
    if not wanted:
        # A network or release-layout problem must not silently turn a verified
        # update into an unverified binary installation.
        raise UpdateError(f"release {tag} has no usable SHA-256 checksum for {name}; "
                          "the existing engine was left unchanged")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Stage beside the destination so os.replace is an atomic, same-filesystem
    # swap: a stopped service must never observe a half-copied engine.
    with tempfile.TemporaryDirectory(prefix=".simurgh-update-", dir=dest.parent) as tmp:
        staged = Path(tmp) / name
        _download(url, staged)
        got = _sha256(staged)
        if got.lower() != wanted.lower():
            raise UpdateError(f"{name} does not match its checksum "
                              f"(expected {wanted[:12]}…, got {got[:12]}…)")
        os.chmod(staged, 0o755)
        if dest.exists():
            shutil.copy2(dest, dest.with_suffix(dest.suffix + ".bak"))
        os.replace(staged, dest)
        os.chmod(dest, 0o755)
    return f"installed {name} to {dest}" + (" (the old one is next to it as .bak)"
                                            if dest.with_suffix(dest.suffix + ".bak").exists()
                                            else "")


def _home_path(home) -> Path:
    """Accept both ``pathlib.Path`` and the CLI's ``util.Home`` safely."""
    value = getattr(home, "path", home)
    return Path(value) if value is not None else Path(sys.prefix)


def installed_binary(home) -> Path | None:
    """The compiled engine the running install would actually execute."""
    base = _home_path(home)
    for candidate in (base / "bin" / "simurgh-go",
                      base / "bin" / "simurgh-go.exe",
                      Path(__file__).resolve().parent.parent / "bin" / "simurgh-go"):
        if candidate.exists():
            return candidate
    # The universal installer puts the engine in /usr/local/bin.  Reuse the
    # runtime lookup instead of maintaining a second, incomplete path list.
    from .engines import find_binary
    from .util import Home

    found = find_binary(Home(base))
    return Path(found) if found else None


def update(home: Path | None = None, *, check_only: bool = False, force: bool = False,
           rebuild: bool = True, verbose: bool = True) -> dict:
    """Do the whole thing; returns what happened.

    ``rebuild`` remains in the signature for callers from older releases.  A
    release update never requires a compiler: the engine comes from the same
    checksummed release assets on every installation shape.
    """
    info = check()
    result = dict(info)
    if not info["available"] and not force:
        result["action"] = "up to date"
        result["restart_required"] = False
        return result
    if check_only:
        result["action"] = "check only"
        result["restart_required"] = False
        return result

    home_path = _home_path(home)
    root = source_checkout()
    if root is not None:
        steps = update_git(root, info["tag"], verbose=verbose)
        # Checking out /opt/simurgh-src alone does not update the copy already
        # installed in /opt/simurgh/venv.  This was the main reason `update`
        # reported success while `simurgh --version` stayed on the old release.
        steps.append(reinstall_package(root))
        dest = installed_binary(home_path)
        if dest is not None:
            steps.append(update_binary(dest, info["tag"], info["asset_urls"]))
        result["action"] = f"source install updated to {info['tag']}"
        result["steps"] = steps
        result["restart_required"] = True
        return result

    steps = [reinstall_release(info["tag"])]
    dest = installed_binary(home_path)
    if dest is not None:
        steps.append(update_binary(dest, info["tag"], info["asset_urls"]))
    result["action"] = f"package install updated to {info['tag']}"
    result["steps"] = steps
    result["restart_required"] = True
    return result

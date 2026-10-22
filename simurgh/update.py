"""``simurgh update`` — bring this install to the newest release.

Two shapes of install have to be handled, and the command works out which one
it is looking at:

* **a git checkout** (what ``install.sh`` does by default): the newest tag is
  fetched and checked out, and the Go engine is rebuilt from that source;
* **a packaged install** (pip/wheel, or someone who copied the files):
  the release asset for this OS/architecture is downloaded, its SHA-256 is
  checked against the release's ``SHA256SUMS.txt``, and the running binary is
  replaced atomically — the old one is kept as ``.bak``.

Nothing here is needed to *run* Simurgh, and every network step is optional:
with no network the command says so and exits with a non-zero status instead of
touching the install.  ``--check`` only reports.
"""

from __future__ import annotations

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
from urllib.parse import urlparse

from . import __product__, __version__
from .util import get_logger

log = get_logger("simurgh.update")

REPO = "Dark-Sky07/Simurgh-Tunnel"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD = f"https://github.com/{REPO}/releases/download"
#: where install.sh puts the compiled engine
BINARIES = {"linux": "simurgh-go-linux-{arch}", "darwin": "simurgh-go-darwin-{arch}",
            "windows": "simurgh-go-windows-amd64.exe"}
ARCHES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
          "armv7l": "armv7", "armv6l": "armv7"}
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
        "Accept": "application/vnd.github+json",
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


def newest_release() -> dict:
    """The newest published release, as the GitHub API describes it."""
    try:
        with _open(API_LATEST) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"GitHub answered {exc.code} for the latest release") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"cannot reach GitHub ({exc}); this install is unchanged") from exc
    except ValueError as exc:
        raise UpdateError(f"GitHub sent something unreadable ({exc})") from exc


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
    """Move a checkout to *tag* (fetch, checkout, rebuild the engine)."""
    steps: list[str] = []
    code, out = _run(["git", "fetch", "--tags", "--force", "origin"], root)
    if code != 0:
        raise UpdateError(f"git fetch failed: {out.strip()[:200]}")
    steps.append("fetched the tags")
    code, out = _run(["git", "checkout", "--force", tag], root)
    if code != 0:
        raise UpdateError(f"git checkout {tag} failed: {out.strip()[:200]}")
    steps.append(f"checked out {tag}")
    builder = root / "tools" / "build-go.sh"
    if builder.exists() and shutil.which("go"):
        code, out = _run(["bash", str(builder)], root)
        if code != 0:
            steps.append(f"the Go engine did not rebuild ({out.strip()[:120]})")
        else:
            steps.append("rebuilt the Go engine")
    elif builder.exists():
        steps.append("no Go toolchain here: the engine stays as it is")
    if verbose:
        for step in steps:
            print(f"  - {step}")
    return steps


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


def installed_binary(home: Path) -> Path | None:
    """The compiled engine the running install would use."""
    for candidate in (home / "bin" / "simurgh-go",
                      home / "bin" / "simurgh-go.exe",
                      Path(__file__).resolve().parent.parent / "bin" / "simurgh-go"):
        if candidate.exists():
            return candidate
    return None


def update(home: Path | None = None, *, check_only: bool = False, force: bool = False,
           rebuild: bool = True, verbose: bool = True) -> dict:
    """Do the whole thing; returns what happened."""
    info = check()
    result = dict(info)
    if not info["available"] and not force:
        result["action"] = "up to date"
        return result
    if check_only:
        result["action"] = "check only"
        return result

    root = git_root()
    if root is not None:
        steps = update_git(root, info["tag"], verbose=verbose) if rebuild else []
        result["action"] = f"git checkout updated to {info['tag']}"
        result["steps"] = steps
        return result

    dest = installed_binary(home or Path(sys.prefix))
    if dest is None:
        pip = shutil.which("pip") or shutil.which("pip3")
        if not pip:
            raise UpdateError("this install is neither a git checkout nor a packaged "
                              "one, and no pip is available to upgrade it")
        code, out = _run([pip, "install", "-U", "simurgh-tunnel"])
        if code != 0:
            raise UpdateError(f"pip could not upgrade the package: {out.strip()[:200]}")
        result["action"] = "installed the newest package with pip"
        return result

    result["action"] = update_binary(dest, info["tag"], info["asset_urls"])
    return result

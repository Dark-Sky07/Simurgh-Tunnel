"""Locating (and describing) the data plane engines.

Simurgh ships two engines that speak the same wire protocol:

* ``python`` — the reference engine, pure standard library, works everywhere.
* ``go``     — the compiled engine: same protocol, many cores, much faster per
  gigabyte, which is what busy servers want.

The panel, the menu, the CLI and the installer all stay Python; only the data
plane (the process that moves the bytes) is delegated to the compiled binary
when the config says ``engine = "go"``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .util import Home

#: where an installation may keep the compiled engine, in search order
_EXTRA_PATHS = (
    "/usr/local/bin/simurgh-go",
    "/usr/bin/simurgh-go",
    "/opt/simurgh/simurgh-go",
)


def find_binary(home: Home | None = None) -> str | None:
    """Absolute path of the compiled engine, or ``None`` when it is missing."""
    env = os.environ.get("SIMURGH_GO_BIN")
    candidates: list[str] = []
    if env:
        candidates.append(env)
    if home is not None:
        candidates.append(str(home.path / "simurgh-go"))
    candidates.extend(_EXTRA_PATHS)
    # a source checkout keeps its build in ./bin
    repo = Path(__file__).resolve().parent.parent
    candidates.append(str(repo / "bin" / "simurgh-go"))
    candidates.extend(shutil.which("simurgh-go") and [shutil.which("simurgh-go")] or [])
    for candidate in candidates:
        if candidate and os.access(candidate, os.X_OK) and os.path.isfile(candidate):
            return candidate
    return None


def version_of(binary: str) -> str:
    """``simurgh-go version`` output, or an empty string when it misbehaves."""
    try:
        out = subprocess.run([binary, "version"], capture_output=True, text=True,
                             timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.stdout or "").strip()


def describe(home: Home | None = None) -> dict:
    """What the menu, doctor and panel show about the data plane."""
    binary = find_binary(home)
    return {
        "binary": binary or "",
        "version": version_of(binary) if binary else "",
        "engine": "go" if binary else "python",
    }

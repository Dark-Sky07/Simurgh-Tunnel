"""Shared defaults for a user-space development installation."""

from __future__ import annotations

import os
from pathlib import Path


def default_config_dir() -> Path:
    """Honor an explicit deployment path; otherwise keep configs in the user home."""
    configured = os.environ.get("SIMURGH_NEXT_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".config/simurgh-next"

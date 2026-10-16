# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2021 Taneli Hukkinen
# Licensed to PSF under a Contributor Agreement.
#
# Vendored, unmodified copy of tomli 2.5.0 (https://pypi.org/project/tomli/,
# `pip download tomli`) so that Simurgh runs on Python 3.9 and 3.10, where the
# standard library has no `tomllib`. simurgh/config.py imports it as
# `tomllib`; the API is identical (`load`, `loads`, `TOMLDecodeError`).
# The licence is next to this file, in LICENSE.

__all__ = ("loads", "load", "TOMLDecodeError")
__version__ = "2.5.0"  # DO NOT EDIT THIS LINE MANUALLY. LET bump2version UTILITY DO IT

from ._parser import TOMLDecodeError, load, loads

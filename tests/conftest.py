"""Shared fixtures for the Simurgh test suite."""

from __future__ import annotations

import asyncio
import inspect
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simurgh.certs import ensure_certificate  # noqa: E402
from simurgh.util import Home  # noqa: E402


def free_port() -> int:
    """Ask the kernel for an unused TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def home(tmp_path) -> Home:
    home = Home(tmp_path / "simurgh")
    home.ensure()
    return home


@pytest.fixture()
def certs(home):
    return ensure_certificate(home, "localhost")


def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: run this test inside an event loop")


# The project has no test-only dependencies on purpose (it must install
# offline), so coroutine tests are driven by this tiny hook instead of
# pytest-asyncio.
@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(func):
        return None
    kwargs = {name: pyfuncitem.funcargs[name]
              for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(func(**kwargs))
    return True

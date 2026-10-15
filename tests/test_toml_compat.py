"""Python 3.9/3.10 have no `tomllib`, and the installer must work there.

Ubuntu 22.04 ships Python 3.10, where the config layer used to die with
``ModuleNotFoundError: No module named 'tomllib'`` *after* the python
environment had been built — the worst possible moment. simurgh/config.py now
falls back to the bundled copy of tomli (`simurgh._tomli`), because reaching
PyPI at install time is exactly what cannot be relied on there.

These tests keep the fallback honest: it must be importable, it must read
everything the writer produces, and it must keep the same API and error type.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from simurgh import _tomli  # noqa: E402
from simurgh import config as config_mod  # noqa: E402
from simurgh.config import toml_dumps  # noqa: E402


def _sample() -> dict:
    """Everything the config writer can put into a file."""
    return {
        "token": "abcd-1234",
        "name": "omega-de",
        "engine": "go",
        "connections": 4,
        "keepalive": 25,
        "stream_window": 262144,
        "split": 3,
        "verbose": False,
        "enabled": True,
        "ratio": 1.5,
        "tags": ["a", "b"],
        "ports": [443, 2053, 2083],
        "empty": [],
        "listen": [
            {"carrier": "tls", "host": "0.0.0.0", "port": 443, "path": "/ws"},
            {"carrier": "wss", "host": "0.0.0.0", "port": 2083, "path": "/ws"},
        ],
        "mapping": [
            {"name": "panel", "listen": 8443, "target_port": 8443, "udp": False},
        ],
        "exit": {"carrier": "tls", "address": "203.0.113.9", "port": 443,
                 "domain": None, "insecure_skip_verify": False},
    }


def test_the_bundled_parser_reads_what_the_writer_writes():
    text = toml_dumps(_sample())
    assert _tomli.loads(text) == config_mod.tomllib.loads(text)


def test_the_bundled_parser_agrees_with_the_standard_library(tmp_path):
    pytest.importorskip("tomllib")
    import tomllib

    text = toml_dumps(_sample())
    path = tmp_path / "sample.toml"
    path.write_text(text, encoding="utf-8")
    with open(path, "rb") as fh:
        from_bundle = _tomli.load(fh)
    with open(path, "rb") as fh:
        from_stdlib = tomllib.load(fh)
    assert from_bundle == from_stdlib


def test_the_bundled_parser_reports_broken_toml_the_same_way():
    with pytest.raises(_tomli.TOMLDecodeError):
        _tomli.loads("token = \n")            # a value is missing
    with pytest.raises(ValueError):           # TOMLDecodeError is a ValueError
        _tomli.loads("token = \n")


def test_config_imports_without_tomllib_at_all():
    """Simulate Python 3.9/3.10: `tomllib` must be absent, yet config must work."""
    code = "\n".join([
        "import sys",
        "class Blocker:",
        "    def find_spec(self, name, path=None, target=None):",
        "        if name == 'tomllib':",
        "            raise ModuleNotFoundError(\"No module named 'tomllib'\")",
        "        return None",
        "sys.meta_path.insert(0, Blocker())",
        "import simurgh.config as cfg",
        "assert cfg.tomllib.__name__ == 'simurgh._tomli', cfg.tomllib.__name__",
        "assert cfg.tomllib.loads('a = 1') == {'a': 1}",
        "print('fallback ok')",
    ])
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "fallback ok" in proc.stdout


def test_a_real_config_round_trips_through_the_bundled_parser(tmp_path):
    """The writer and both readers have to agree on a file on disk, not just
    on a hand-written dictionary."""
    from simurgh.config import (ExitConfig, ExitEndpoint, ListenSpec, Mapping,
                                RelayConfig, load_exit, load_relay, save_exit,
                                save_relay)
    cfg = ExitConfig(token="tok", name="de-1", connections=3)
    cfg.listen = [ListenSpec(carrier="tls", port=443, path="/ws")]
    path = tmp_path / "exit.toml"
    save_exit(cfg, path)
    assert load_exit(path).token == "tok"
    assert load_exit(path).connections == 3

    relay = RelayConfig(token="tok", name="ir-1")
    relay.exit = ExitEndpoint(carrier="tls", address="203.0.113.9", port=443)
    relay.mappings = [Mapping(name="panel", listen=8443, target_port=8443)]
    rpath = tmp_path / "relay.toml"
    save_relay(relay, rpath)
    loaded = load_relay(rpath)
    assert loaded.exit.address == "203.0.113.9"
    assert loaded.mappings[0].listen == 8443

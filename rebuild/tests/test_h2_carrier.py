from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rebuild import wizard
from rebuild.tunnel import EntryConfig, ExitConfig, load_config


def scripted(values):
    iterator = iter(values)
    return lambda _prompt: next(iterator)


def test_h2_carrier_is_available_in_setup_wizard():
    config = wizard.build_config("entry", scripted([
        "1", "3", "", "", "web", "s" * 40, "128", "exit.example.net", "", "", "", "",
    ]), lambda _message: None)
    assert config["carrier"] == "h2"


def test_h2_carrier_loads_for_both_roles(tmp_path):
    entry = {
        "role": "entry", "carrier": "h2", "listen_host": "127.0.0.1",
        "listen_port": 18080, "exit_host": "localhost", "exit_port": 8443,
        "server_name": "localhost", "ca_file": "/tmp/ca.crt",
        "secret": "s" * 40, "route": "web",
    }
    path = tmp_path / "entry.json"
    wizard.save_config(path, entry)
    loaded = load_config(path)
    assert isinstance(loaded, EntryConfig)
    assert loaded.carrier == "h2"

    exit_config = {
        "role": "exit", "carrier": "h2", "listen_host": "0.0.0.0",
        "listen_port": 8443, "cert_file": "/tmp/server.crt",
        "key_file": "/tmp/server.key", "secret": "s" * 40,
        "routes": {"web": {"host": "127.0.0.1", "port": 8080}},
    }
    exit_path = tmp_path / "exit.json"
    wizard.save_config(exit_path, exit_config)
    loaded_exit = load_config(exit_path)
    assert isinstance(loaded_exit, ExitConfig)
    assert loaded_exit.carrier == "h2"

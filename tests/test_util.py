"""Small helpers: paths, formatting, ports and logging."""

from __future__ import annotations

import logging
import socket

import pytest

from simurgh.util import (Home, clamp, free_port, get_logger, human_bytes,
                          human_duration, human_rate, is_ip, local_ips,
                          parse_host_port, parse_port_list, setup_logging)


def test_home_layout(tmp_path, monkeypatch):
    home = Home(tmp_path / "s").ensure()
    assert home.exit_cfg.name == "exit.toml"
    assert home.relay_cfg.name == "relay.toml"
    assert home.state.name == "state.json"
    assert home.logs.is_dir() and home.run.is_dir()


def test_home_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMURGH_HOME", str(tmp_path / "custom"))
    assert Home().path == tmp_path / "custom"


@pytest.mark.parametrize("value,text", [
    (0, "0 B"), (1023, "1023 B"), (1024, "1.0 KB"),
    (1536, "1.5 KB"), (1024 ** 3, "1.0 GB"),
])
def test_human_bytes(value, text):
    assert human_bytes(value) == text


def test_human_rate_and_duration():
    assert human_rate(1024).endswith("/s")
    assert human_duration(59) == "59s"
    assert human_duration(3600) == "1h 0m"
    assert human_duration(86400 + 3600) == "1d 1h"


@pytest.mark.parametrize("text,valid", [
    ("1.2.3.4", True), ("::1", True), ("2001:db8::1", True),
    ("example.com", False), ("999.1.1.1", False), ("", False),
])
def test_is_ip(text, valid):
    assert is_ip(text) is valid


def test_parse_port_list_forms():
    assert parse_port_list("443,2053") == [443, 2053]
    assert parse_port_list("443 2053") == [443, 2053]
    assert parse_port_list([8443, 443]) == [443, 8443]
    assert parse_port_list("") == []


def test_parse_host_port():
    assert parse_host_port("example.com:443") == ("example.com", 443)
    assert parse_host_port("example.com", 80) == ("example.com", 80)
    assert parse_host_port("[::1]:443") == ("::1", 443)
    with pytest.raises(ValueError):
        parse_host_port("example.com:notaport")


def test_free_port_detects_a_listener():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        used = sock.getsockname()[1]
        assert free_port(used, "127.0.0.1") is False
    assert free_port(used, "127.0.0.1") is True


def test_clamp():
    assert clamp(5, 0, 10) == 5
    assert clamp(-1, 0, 10) == 0
    assert clamp(99, 0, 10) == 10


def test_local_ips_returns_valid_addresses():
    ips = local_ips()
    assert ips and all(is_ip(ip) for ip in ips)


def test_setup_logging_writes_a_file(tmp_path):
    logfile = tmp_path / "x.log"
    setup_logging(False, logfile=str(logfile))
    get_logger("simurgh.test").info("hello-from-test")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "hello-from-test" in logfile.read_text()
    setup_logging(False)

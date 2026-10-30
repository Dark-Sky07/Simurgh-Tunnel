"""Small interactive setup wizard for the independent rebuild."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from .paths import default_config_dir
from .tunnel import (
    DEFAULT_CLIENT_SESSION_LIMIT,
    DEFAULT_UDP_RATE_LIMIT_PPS,
    MAX_CLIENT_SESSION_LIMIT,
    MAX_UDP_RATE_LIMIT_PPS,
    NODE_CONFIG_TYPES,
    NODE_ROLES,
    load_config,
)

Input = Callable[[str], str]
Output = Callable[[str], None]


def _choice(prompt: str, values: dict[str, str], input_fn: Input, output: Output) -> str:
    while True:
        answer = input_fn(prompt).strip()
        if answer in values:
            return values[answer]
        output("Choose one of: " + ", ".join(values))


def _value(prompt: str, default: str, input_fn: Input) -> str:
    answer = input_fn(f"{prompt} [{default}]: ").strip()
    return answer or default


def _port(prompt: str, default: int, input_fn: Input) -> int:
    while True:
        answer = _value(prompt, str(default), input_fn)
        try:
            value = int(answer)
        except ValueError:
            continue
        if 1 <= value <= 65535:
            return value


def _quota_mib(input_fn: Input, output: Output) -> int | None:
    while True:
        answer = input_fn("Entry monthly transfer quota in MiB (blank = unlimited): ").strip()
        if not answer:
            return None
        try:
            quota = int(answer)
        except ValueError:
            output("Enter a whole number of MiB, or leave blank for unlimited.")
            continue
        if 1 <= quota <= 1_048_576:
            return quota
        output("Quota must be between 1 and 1,048,576 MiB.")


def _client_session_limit(input_fn: Input, output: Output) -> int:
    while True:
        answer = _value(
            "Maximum concurrent client sessions per source IP (1–512)",
            str(DEFAULT_CLIENT_SESSION_LIMIT), input_fn)
        try:
            value = int(answer)
        except ValueError:
            output("Enter a whole number from 1 to 512.")
            continue
        if 1 <= value <= MAX_CLIENT_SESSION_LIMIT:
            return value
        output(f"Enter a whole number from 1 to {MAX_CLIENT_SESSION_LIMIT}.")


def _udp_rate_limit_pps(input_fn: Input, output: Output) -> int:
    while True:
        answer = _value(
            "Maximum UDP datagrams per second per source IP (1–100000)",
            str(DEFAULT_UDP_RATE_LIMIT_PPS), input_fn)
        try:
            value = int(answer)
        except ValueError:
            output("Enter a whole number from 1 to 100000.")
            continue
        if 1 <= value <= MAX_UDP_RATE_LIMIT_PPS:
            return value
        output(f"Enter a whole number from 1 to {MAX_UDP_RATE_LIMIT_PPS}.")


def _secret(input_fn: Input, output: Output) -> str:
    value = input_fn("Shared secret (leave blank to generate a new one): ").strip()
    if not value:
        value = secrets.token_urlsafe(36)
        output("Generated a secret. Copy it securely to the other server; it will not be shown again.")
        output(value)
    if len(value.encode("utf-8")) < 32:
        raise ValueError("the shared secret must contain at least 32 UTF-8 bytes")
    return value


def build_config(
    role: str,
    input_fn: Input = input,
    output: Output = print,
    config_dir: Path | None = None,
) -> dict:
    """Prompt for one node config without reading or altering other files."""
    tls_dir = (config_dir or default_config_dir()) / "tls"
    if role == "reverse-hub":
        route = _value("Reverse route name", "web", input_fn)
        client_session_limit = _client_session_limit(input_fn, output)
        secret = _secret(input_fn, output)
        return {
            "role": role,
            "control_host": _value("Control bind address (0.0.0.0 accepts remote agents)",
                                    "0.0.0.0", input_fn),  # nosec B104
            "control_port": _port("TLS control port", 9443, input_fn),
            "public_host": _value("Public TCP service bind address", "127.0.0.1", input_fn),
            "public_port": _port("Public TCP service port", 8080, input_fn),
            "cert_file": _value("TLS certificate path", str(tls_dir / "server.crt"), input_fn),
            "key_file": _value("TLS private-key path", str(tls_dir / "server.key"), input_fn),
            "secret": secret,
            "route": route,
            "client_session_limit": client_session_limit,
        }
    if role == "reverse-agent":
        route = _value("Reverse route name (must match the hub)", "web", input_fn)
        secret = _secret(input_fn, output)
        return {
            "role": role,
            "hub_host": _value("Public reverse hub address", "hub.example.net", input_fn),
            "hub_port": _port("Hub TLS control port", 9443, input_fn),
            "server_name": _value("Expected hub TLS DNS name", "hub.example.net", input_fn),
            "ca_file": _value("Trusted CA certificate path", str(tls_dir / "ca.crt"), input_fn),
            "secret": secret,
            "route": route,
            "target_host": _value("Fixed service address on this private host", "127.0.0.1", input_fn),
            "target_port": _port("Fixed service port", 8080, input_fn),
        }
    if role not in ("entry", "exit"):
        raise ValueError("unsupported node role")
    transport = "tcp"
    if role == "entry":
        transport = _choice("Local entry transport: 1) TCP  2) UDP: ",
                            {"1": "tcp", "2": "udp"}, input_fn, output)
    carrier = _choice("Tunnel carrier: 1) TLS stream  2) WebSocket over TLS (WSS)  "
                      "3) HTTP/2 over TLS (H2): ",
                      {"1": "tls", "2": "wss", "3": "h2"}, input_fn, output)
    # Exit listeners must accept remote entries; the next prompt states the exposure explicitly.
    bind_default = "0.0.0.0" if role == "exit" else "127.0.0.1"  # nosec B104
    bind_prompt = ("Exit tunnel bind address (0.0.0.0 exposes all interfaces)"
                   if role == "exit" else "Local entry bind address")
    listen_host = _value(bind_prompt, bind_default, input_fn)
    listen_port = _port("Local listen port", 8443 if role == "exit" else
                        (15353 if transport == "udp" else 18080), input_fn)
    route = _value("Route name (must match on both sides)", "web", input_fn)
    secret = _secret(input_fn, output)

    if role == "exit":
        return {
            "role": "exit",
            "carrier": carrier,
            "listen_host": listen_host,
            "listen_port": listen_port,
            "cert_file": _value("TLS certificate path", str(tls_dir / "server.crt"), input_fn),
            "key_file": _value("TLS private-key path", str(tls_dir / "server.key"), input_fn),
            "secret": secret,
            "routes": {
                route: {
                    "host": _value("Destination address", "127.0.0.1", input_fn),
                    "port": _port("Destination port", 80, input_fn),
                }
            },
        }
    if role != "entry":
        raise ValueError("role must be entry or exit")
    client_session_limit = _client_session_limit(input_fn, output)
    udp_rate_limit_pps = (_udp_rate_limit_pps(input_fn, output)
                          if transport == "udp" else None)
    config = {
        "role": "entry",
        "transport": transport,
        "carrier": carrier,
        "listen_host": listen_host,
        "listen_port": listen_port,
        "exit_host": _value("Exit server address", "exit.example.net", input_fn),
        "exit_port": _port("Exit TLS port", 8443, input_fn),
        "server_name": _value("Expected TLS DNS name", "exit.example.net", input_fn),
        "ca_file": _value("CA certificate path", str(tls_dir / "ca.crt"), input_fn),
        "secret": secret,
        "route": route,
        "client_session_limit": client_session_limit,
    }
    if udp_rate_limit_pps is not None:
        config["udp_rate_limit_pps"] = udp_rate_limit_pps
    monthly_quota_mib = _quota_mib(input_fn, output)
    if monthly_quota_mib is not None:
        config["monthly_quota_mib"] = monthly_quota_mib
    return config


def save_config(path: Path, config: dict) -> Path | None:
    """Write mode-0600 JSON atomically, first preserving an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if path.is_symlink():
        raise ValueError(f"refusing to replace a symlinked configuration path: {path}")
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        if backup.exists():
            backup = path.with_suffix(path.suffix + f".{time.time_ns()}.bak")
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    return backup


def list_config_backups(config_dir: Path, role: str) -> tuple[Path, ...]:
    if role not in NODE_ROLES:
        raise ValueError("unsupported node role")
    pattern = re.compile(rf"^{re.escape(role)}\.json(?:\.[0-9]+)?\.bak$")
    directory = Path(config_dir)
    if not directory.is_dir():
        return ()
    files = [path for path in directory.iterdir()
             if pattern.fullmatch(path.name) and path.is_file() and not path.is_symlink()]
    return tuple(sorted(files, key=lambda path: path.stat().st_mtime_ns, reverse=True))


def restore_config(config_dir: Path, role: str, backup_name: str) -> Path | None:
    """Restore a validated adjacent backup, first saving the current config again."""
    backups = list_config_backups(config_dir, role)
    source = next((path for path in backups if path.name == backup_name), None)
    if source is None:
        raise ValueError("selected config backup is missing or not managed by this installer")
    parsed = load_config(source)
    expected_type = NODE_CONFIG_TYPES[role]
    if not isinstance(parsed, expected_type):
        raise ValueError(f"backup does not contain a valid {role} configuration")  # noqa: TRY004
    config = json.loads(source.read_text(encoding="utf-8"))
    return save_config(Path(config_dir) / f"{role}.json", config)


def setup_menu(config_dir: Path, input_fn: Input = input, output: Output = print) -> int:
    output("Simurgh Tunnel — independent setup")
    role = _choice("Choose role: 1) Entry  2) Exit  3) Reverse hub  "
                   "4) Reverse agent  5) Quit: ",
                   {"1": "entry", "2": "exit", "3": "reverse-hub",
                    "4": "reverse-agent", "5": "quit"}, input_fn, output)
    if role == "quit":
        return 0
    config = build_config(role, input_fn, output, config_dir)
    path = config_dir / f"{role}.json"
    if path.exists():
        confirm = input_fn(f"{path} exists. Back it up and replace it? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes"):
            output("No configuration was changed.")
            return 1
    backup = save_config(path, config)
    output(f"Saved {role} configuration to {path}")
    if backup:
        output(f"Previous file preserved at {backup}")
    output("The existing /etc/simurgh directory is not read, changed, or removed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Choose a role and configure a rebuild node")
    parser.add_argument("--config-dir", type=Path, default=default_config_dir())
    args = parser.parse_args(argv)
    try:
        return setup_menu(args.config_dir)
    except (OSError, ValueError) as exc:
        print(f"setup failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

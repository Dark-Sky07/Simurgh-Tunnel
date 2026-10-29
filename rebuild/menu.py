"""Terminal role menu for the independent rebuild."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from .paths import default_config_dir
from .tunnel import (
    ConfigError,
    EntryConfig,
    ExitConfig,
    check_entry_path,
    load_config,
    serve_entry,
    serve_exit,
)
from .updates import interactive_update
from .wizard import setup_menu

LOG = logging.getLogger("simurgh.rebuild.menu")
Input = Callable[[str], str]
Output = Callable[[str], None]


def _configured_roles(config_dir: Path) -> tuple[str, ...]:
    return tuple(role for role in ("entry", "exit")
                 if (config_dir / f"{role}.json").is_file())


def show_status(config_dir: Path, output: Output = print) -> tuple[str, ...]:
    roles = _configured_roles(config_dir)
    output(f"Configuration directory: {config_dir}")
    if not roles:
        output("No rebuild role is configured yet.")
    for role in roles:
        try:
            config = load_config(config_dir / f"{role}.json")
        except ConfigError as exc:
            output(f"{role}: invalid config ({exc})")
            continue
        if role == "entry" and isinstance(config, EntryConfig):
            output(f"entry: configured, {config.transport.upper()} listener "
                   f"{config.host}:{config.port} -> {config.exit_host}:{config.exit_port} "
                   f"carrier={config.carrier.upper()} route={config.route}")
        elif role == "exit" and isinstance(config, ExitConfig):
            output(f"exit: configured, {config.carrier.upper()} listener "
                   f"{config.host}:{config.port}; {len(config.routes)} fixed route(s)")
        else:
            output(f"{role}: config role does not match its filename")
    return roles


def run_role(role: str, config_dir: Path, output: Output = print) -> int:
    if role not in ("entry", "exit"):
        output("Choose entry or exit.")
        return 2
    path = config_dir / f"{role}.json"
    try:
        config = load_config(path)
    except ConfigError as exc:
        output(f"Cannot start {role}: {exc}")
        return 2
    if role == "entry" and not isinstance(config, EntryConfig):
        output("The selected entry file does not contain an entry config.")
        return 2
    if role == "exit" and not isinstance(config, ExitConfig):
        output("The selected exit file does not contain an exit config.")
        return 2
    output(f"Starting {role} in the foreground. Press Ctrl+C to stop it.")
    try:
        asyncio.run(serve_entry(config) if isinstance(config, EntryConfig)
                    else serve_exit(config))
    except KeyboardInterrupt:
        output(f"Stopped {role}.")
    except OSError as exc:
        LOG.error("cannot start %s: %s", role, exc)
        output(f"Could not start {role}: {exc}")
        return 1
    return 0


def check_entry(config_dir: Path, output: Output = print) -> int:
    path = config_dir / "entry.json"
    try:
        config = load_config(path)
    except (ConfigError, OSError) as exc:
        output(f"Cannot check entry path: {exc}")
        return 2
    if not isinstance(config, EntryConfig):
        output("Path checks must be run from a configured entry node.")
        return 2
    try:
        result = asyncio.run(check_entry_path(config))
    except (ConnectionError, OSError, TimeoutError) as exc:
        output(f"Entry path check failed: {exc}")
        return 1
    output(result)
    return 0


def menu(config_dir: Path, input_fn: Input = input, output: Output = print) -> int:
    while True:
        output("\nSimurgh Tunnel — independent rebuild")
        output("1) Configure/select a role")
        output("2) Run entry in foreground")
        output("3) Run exit in foreground")
        output("4) Show configured roles")
        output("5) Check the entry-to-exit path")
        output("6) Check/apply release update")
        output("7) Quit")
        choice = input_fn("Choose: ").strip()
        if choice == "1":
            setup_menu(config_dir, input_fn, output)
        elif choice == "2":
            if not (config_dir / "entry.json").is_file():
                output("Entry is not configured; choose option 1 first.")
            else:
                run_role("entry", config_dir, output)
        elif choice == "3":
            if not (config_dir / "exit.json").is_file():
                output("Exit is not configured; choose option 1 first.")
            else:
                run_role("exit", config_dir, output)
        elif choice == "4":
            show_status(config_dir, output)
        elif choice == "5":
            check_entry(config_dir, output)
        elif choice == "6":
            interactive_update(config_dir)
        elif choice == "7":
            return 0
        else:
            output("Choose a number from 1 to 7.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Choose and run a rebuild node role")
    parser.add_argument("--config-dir", type=Path, default=default_config_dir())
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return menu(args.config_dir)
    except (OSError, ValueError) as exc:
        LOG.error("menu failed: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

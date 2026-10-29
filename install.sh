#!/bin/sh
# User-space development installer for the independent rebuild.
set -eu

REPO_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
SOURCE_DIR="$REPO_DIR/rebuild"
ENABLE_SERVICE=0
SYSTEM_INSTALL=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --system) SYSTEM_INSTALL=1 ;;
        --enable-service) ENABLE_SERVICE=1 ;;
        --help)
            echo "Usage: sh install.sh [--enable-service | --system]"
            echo "Default: install for the current user; --enable-service manages a user unit."
            echo "--system: install the panel service for all users (requires root)."
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done
if [ "$SYSTEM_INSTALL" -eq 1 ]; then
    if [ "$ENABLE_SERVICE" -eq 1 ]; then
        echo "Choose either --system or --enable-service, not both." >&2
        exit 2
    fi
    SYSTEM_PYTHON=$(command -v python3 || true)
    if [ -z "$SYSTEM_PYTHON" ]; then
        echo "Python 3.10 or newer is required." >&2
        exit 2
    fi
    exec "$SYSTEM_PYTHON" "$SOURCE_DIR/install_system.py" "$SOURCE_DIR"
fi
: "${HOME:?HOME must be set}"
CONFIG_DIR=${SIMURGH_NEXT_CONFIG_DIR:-"$HOME/.config/simurgh-next"}
case "$CONFIG_DIR" in /*) ;; *) echo "Config directory must be an absolute path: $CONFIG_DIR" >&2; exit 2 ;; esac
PREFIX=${SIMURGH_NEXT_PREFIX:-"$HOME/.local/share/simurgh-next"}
BIN_DIR=${SIMURGH_NEXT_BIN_DIR:-"$HOME/.local/bin"}
XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
SERVICE_DIR=${SIMURGH_NEXT_SERVICE_DIR:-"$XDG_CONFIG_HOME/systemd/user"}
case "$SERVICE_DIR" in /*) ;; *) echo "Service directory must be an absolute path: $SERVICE_DIR" >&2; exit 2 ;; esac
SERVICE_FILE="$SERVICE_DIR/simurgh-next-panel.service"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this development installer as the account that will operate the panel; do not use sudo." >&2
    exit 2
fi
case "$PREFIX" in /*) ;; *) echo "Install prefix must be an absolute path: $PREFIX" >&2; exit 2 ;; esac
case "$BIN_DIR" in /*) ;; *) echo "Command directory must be an absolute path: $BIN_DIR" >&2; exit 2 ;; esac
if [ -L "$PREFIX" ] || [ -L "$BIN_DIR" ]; then
    echo "Refusing to install through a symlinked prefix or command directory." >&2
    exit 2
fi
if [ -e "$PREFIX/current" ] && [ ! -L "$PREFIX/current" ]; then
    echo "Refusing to replace a non-symlink current release: $PREFIX/current" >&2
    exit 2
fi
PREVIOUS_CURRENT=
if [ -L "$PREFIX/current" ]; then
    PREVIOUS_CURRENT=$(readlink "$PREFIX/current")
    case "$PREVIOUS_CURRENT" in "$PREFIX"/releases/*) ;; *)
        echo "Refusing to replace an unmanaged current-release symlink." >&2
        exit 2
    esac
fi

PYTHON=$(command -v python3 || true)
if [ -z "$PYTHON" ]; then
    echo "Python 3.10 or newer is required." >&2
    exit 2
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    echo "Python 3.10 or newer is required." >&2
    exit 2
fi

VERSION=$(sed -n 's/^VERSION = "\([0-9][^"]*\)"/\1/p' "$SOURCE_DIR/tunnel.py")
if [ -z "$VERSION" ]; then
    echo "Could not determine the rebuild version." >&2
    exit 2
fi
COMMANDS="simurgh-next simurgh-next-panel simurgh-next-setup simurgh-next-node simurgh-next-update"
if [ "$ENABLE_SERVICE" -eq 1 ]; then
    SYSTEMCTL=$(command -v systemctl || true)
    if [ -z "$SYSTEMCTL" ]; then
        echo "systemctl is required for --enable-service." >&2
        exit 2
    fi
    if ! "$SYSTEMCTL" --user show-environment >/dev/null 2>&1; then
        echo "No usable systemd user manager. Install without --enable-service or start your user manager first." >&2
        exit 2
    fi
    case "$XDG_CONFIG_HOME" in /*) ;; *)
        echo "XDG_CONFIG_HOME must be an absolute path." >&2
        exit 2
    esac
    if [ -L "$SERVICE_DIR" ] || [ -L "$SERVICE_FILE" ]; then
        echo "Refusing to install through a symlinked user service path." >&2
        exit 2
    fi
    if [ -e "$SERVICE_FILE" ] && ! grep -q 'Managed by Simurgh Tunnel independent rebuild' "$SERVICE_FILE"; then
        echo "Refusing to overwrite an unmanaged service file: $SERVICE_FILE" >&2
        exit 2
    fi
fi
mkdir -p "$BIN_DIR"
for command_name in $COMMANDS; do
    target="$BIN_DIR/$command_name"
    expected="$PREFIX/current/bin/$command_name"
    if [ -e "$target" ] && [ ! -L "$target" ]; then
        echo "Refusing to overwrite a non-symlink command: $target" >&2
        exit 2
    fi
    if [ -L "$target" ] && [ "$(readlink "$target")" != "$expected" ]; then
        echo "Refusing to replace an unrelated symlink: $target" >&2
        exit 2
    fi
done

mkdir -p "$PREFIX/releases" "$PREFIX/bin"
RELEASE=$(mktemp -d "$PREFIX/releases/$VERSION-XXXXXXXX")
mkdir -p "$RELEASE/rebuild" "$RELEASE/bin"
for module in __init__.py h2stream.py menu.py notifications.py panel.py paths.py reverse.py supervisor.py tunnel.py updates.py usage.py wizard.py; do
    cp "$SOURCE_DIR/$module" "$RELEASE/rebuild/$module"
done
"$PYTHON" -m pip install --quiet --disable-pip-version-check --no-warn-script-location \
    --no-compile --target "$RELEASE/vendor" 'h2>=4.1,<5' 'sigstore>=4.0,<5'
cp -R "$SOURCE_DIR/examples" "$RELEASE/rebuild/examples"
cp "$SOURCE_DIR/README.md" "$SOURCE_DIR/SETUP.md" "$SOURCE_DIR/PROTOCOL.md" "$RELEASE/"

make_launcher() {
    name=$1
    module=$2
    "$PYTHON" - "$RELEASE/bin/$name" "$PREFIX" "$BIN_DIR" "$CONFIG_DIR" \
        "$SERVICE_DIR" "$PYTHON" "$module" <<'PY'
import shlex
import sys
from pathlib import Path

path, prefix, bin_dir, config_dir, service_dir, python, module = sys.argv[1:]
lines = [
    "#!/bin/sh",
    "export PYTHONPATH=" + shlex.quote(f"{prefix}/current:{prefix}/current/vendor") + '"${PYTHONPATH:+:$PYTHONPATH}"',
    "export SIMURGH_NEXT_INSTALL_MODE=user",
    "export SIMURGH_NEXT_PREFIX=" + shlex.quote(prefix),
    "export SIMURGH_NEXT_BIN_DIR=" + shlex.quote(bin_dir),
    "export SIMURGH_NEXT_CONFIG_DIR=" + shlex.quote(config_dir),
    "export SIMURGH_NEXT_SERVICE_DIR=" + shlex.quote(service_dir),
    "exec " + shlex.quote(python) + " -m " + shlex.quote(module) + ' "$@"',
    "",
]
Path(path).write_text(chr(10).join(lines), encoding="utf-8")
PY
    chmod 755 "$RELEASE/bin/$name"
}
make_launcher simurgh-next rebuild.menu
make_launcher simurgh-next-panel rebuild.panel
make_launcher simurgh-next-setup rebuild.wizard
make_launcher simurgh-next-node rebuild.tunnel
make_launcher simurgh-next-update rebuild.updates

current_tmp="$PREFIX/current.new.$$"
ln -s "$RELEASE" "$current_tmp"
"$PYTHON" -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' \
    "$current_tmp" "$PREFIX/current"
for command_name in $COMMANDS; do
    target="$BIN_DIR/$command_name"
    if [ ! -L "$target" ]; then
        ln -s "$PREFIX/current/bin/$command_name" "$target"
    fi
done

if [ "$ENABLE_SERVICE" -eq 1 ]; then
    mkdir -p "$SERVICE_DIR"
    SERVICE_BACKUP=
    if [ -e "$SERVICE_FILE" ]; then
        SERVICE_BACKUP="$SERVICE_FILE.bak.$(date -u +%Y%m%d%H%M%S)-$$"
        cp -p "$SERVICE_FILE" "$SERVICE_BACKUP"
        chmod 600 "$SERVICE_BACKUP"
    fi
    unit_tmp=$(mktemp "$SERVICE_DIR/.simurgh-next-panel.XXXXXXXX")
    "$PYTHON" - "$unit_tmp" "$PREFIX" "$CONFIG_DIR" <<'PY'
import os
import sys
from pathlib import Path

unit_path, prefix, config_dir = sys.argv[1:]

def escape(value: str) -> str:
    return (value.replace("%", "%%").replace("\\", "\\x5c")
            .replace('"', "\\x22").replace(" ", "\\x20"))

root = Path(prefix)
lines = [
    "# Managed by Simurgh Tunnel independent rebuild; local edits are backed up on update.",
    "[Unit]",
    "Description=Simurgh Tunnel independent rebuild control panel",
    "After=network.target",
    "",
    "[Service]",
    "Type=simple",
    f"WorkingDirectory={escape(str(root / 'current'))}",
    f"Environment={escape('SIMURGH_NEXT_CONFIG_DIR=' + config_dir)}",
    f"ExecStart={escape(str(root / 'current' / 'bin' / 'simurgh-next-panel'))}",
    "Restart=always",
    "RestartSec=3",
    "TimeoutStopSec=45",
    "KillMode=control-group",
    "UMask=0077",
    "NoNewPrivileges=true",
    "PrivateTmp=true",
    "",
    "[Install]",
    "WantedBy=default.target",
    "",
]
Path(unit_path).write_text("\n".join(lines), encoding="utf-8")
PY
    chmod 644 "$unit_tmp"
    "$PYTHON" - "$unit_tmp" "$SERVICE_FILE" <<'PY'
import os
import sys
os.replace(sys.argv[1], sys.argv[2])
PY
    service_name=simurgh-next-panel.service
    rollback_service() {
        if [ -n "$SERVICE_BACKUP" ]; then
            cp -p "$SERVICE_BACKUP" "$SERVICE_FILE" || true
        else
            rm -f "$SERVICE_FILE"
        fi
        if [ -n "$PREVIOUS_CURRENT" ]; then
            restore_tmp="$PREFIX/current.rollback.$$"
            rm -f "$restore_tmp"
            if ln -s "$PREVIOUS_CURRENT" "$restore_tmp"; then
                "$PYTHON" -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' \
                    "$restore_tmp" "$PREFIX/current" || true
            fi
            "$SYSTEMCTL" --user daemon-reload >/dev/null 2>&1 || true
            "$SYSTEMCTL" --user restart "$service_name" >/dev/null 2>&1 || true
        else
            "$SYSTEMCTL" --user disable --now "$service_name" >/dev/null 2>&1 || true
        fi
    }
    if ! "$SYSTEMCTL" --user daemon-reload; then
        echo "Could not reload systemd; restoring previous release/unit where available." >&2
        rollback_service
        exit 1
    fi
    if "$SYSTEMCTL" --user is-active --quiet "$service_name"; then
        if ! "$SYSTEMCTL" --user restart "$service_name"; then
            echo "Panel service restart failed; restoring previous release/unit where available." >&2
            rollback_service
            exit 1
        fi
    elif ! "$SYSTEMCTL" --user enable --now "$service_name"; then
        echo "Panel service failed to start; restoring previous release/unit where available." >&2
        rollback_service
        exit 1
    fi
fi

cat <<EOF
Simurgh Tunnel independent rebuild installed for this user.
Choose a role after installation; none was selected by the installer.

Terminal menu: $BIN_DIR/simurgh-next
Loopback web panel: $BIN_DIR/simurgh-next-panel
Default config directory: $CONFIG_DIR
The existing /etc/simurgh directory was not accessed or changed.

For a remote host, run the panel and connect through an SSH local forward.
Ensure $BIN_DIR is on PATH before using the short command names.
Previous code releases are retained under $PREFIX/releases.
EOF
if [ "$ENABLE_SERVICE" -eq 1 ]; then
    echo "User systemd service enabled: simurgh-next-panel.service"
    echo "It starts the panel, not a tunnel role; choose/configure roles in the panel."
    echo "For start-at-boot without login, systemd may require: loginctl enable-linger $(id -un)"
else
    echo "No system service was created. Use --enable-service to enable the user-level panel service."
fi

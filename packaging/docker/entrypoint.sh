#!/bin/sh
# Container entrypoint: make sure the configuration volume exists, explain
# what is missing instead of starting a role that cannot work, and hand the
# process to the CLI (so `simurgh` in the container is exactly `simurgh` on a
# server: same subcommands, same flags).
set -eu

HOME_DIR="${SIMURGH_HOME:-/etc/simurgh}"
# the CLI to exec: /usr/bin/simurgh in the image, overridable so the same
# entrypoint works for a tarball unpacked at another prefix (and in tests)
SIMURGH_BIN="${SIMURGH_BIN:-/usr/bin/simurgh}"
mkdir -p "$HOME_DIR"

case "${1:-}" in
    relay|exit)
        role="$1"
        if [ ! -f "$HOME_DIR/$role.toml" ]; then
            cat >&2 <<MSG
simurgh: no $HOME_DIR/$role.toml yet, so there is nothing to run.

  write it once (it stays in the volume):

    docker run --rm -v <volume>:$HOME_DIR simurgh init --role $role ...
    docker run --rm -v <volume>:$HOME_DIR simurgh join '<setup link>'

  or run any other command in this same container:

    docker run --rm -it -v <volume>:$HOME_DIR simurgh menu
MSG
            exit 1
        fi
        ;;
esac

exec "$SIMURGH_BIN" "$@"

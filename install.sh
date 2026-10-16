#!/usr/bin/env bash
# Simurgh Tunnel — one-command installer for the Iranian relay and the
# foreign exit server.  Tested on Debian/Ubuntu; works on any systemd distro.
#
#   curl -fsSL https://raw.githubusercontent.com/Dark-Sky07/Simurgh-Tunnel/main/install.sh | sudo bash
#
# or, from a checkout:   sudo bash install.sh --role relay --exit 203.0.113.9:443 --token XXX
set -euo pipefail

BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'
CYAN=$'\033[36m'; DIM=$'\033[2m'; RESET=$'\033[0m'

REPO_URL="${SIMURGH_REPO:-https://github.com/Dark-Sky07/Simurgh-Tunnel.git}"
# which git ref to fetch when this script has to download the source itself
# (e.g. when you pipe it into bash).  Override with SIMURGH_REF or --ref.
REF="${SIMURGH_REF:-main}"
SRC_DIR="${SIMURGH_SRC:-/opt/simurgh-src}"
if [ "$(id -u)" = "0" ]; then
  VENV_DIR="${SIMURGH_VENV:-/opt/simurgh/venv}"
  BIN_DIR="/usr/local/bin"
else
  VENV_DIR="${SIMURGH_VENV:-$HOME/.local/share/simurgh/venv}"
  BIN_DIR="$HOME/.local/bin"
fi

# every variable the script reads must exist before it is read: the script runs
# with `set -u`, so a missing one aborts the install with "unbound variable".
ROLE=""
DIAL=""
TUNNEL_PORT=""
CONNECTIONS=""
NAME=""
TOKEN=""
EXIT_ADDR=""
EXIT_HOST=""
EXIT_PORT=""
LISTEN=""
MAPPING=""
PANEL_PORT=""
ASSUME_YES=0
NO_SYSTEMD=0
UNINSTALL=0
PURGE=0
BOTH=0
# a re-run refreshes the configuration; that is what people expect from
# "run the installer again" (back up /etc/simurgh first if you edited it by hand)
FORCE=1
CARRIER=""
SPLIT=""
DOMAIN=""
FINGERPRINT=""
INSECURE=""
# data plane: auto = build the compiled engine when a toolchain is available
ENGINE="${SIMURGH_ENGINE:-auto}"

say()  { printf '%s\n' "${CYAN}•${RESET} $*"; }
ok()   { printf '%s\n' "${GREEN}✔${RESET} $*"; }
warn() { printf '%s\n' "${YELLOW}!${RESET} $*"; }
die()  { printf '%s\n' "${RED}✘${RESET} $*" >&2; exit 1; }

banner() {
  printf '%s' "$CYAN"
  cat <<'ART'

   ███████╗██╗███╗   ███╗██╗   ██╗██████╗  ██████╗ ██╗  ██╗
   ██╔════╝██║████╗ ████║██║   ██║██╔══██╗██╔════╝ ██║  ██║
   ███████╗██║██╔████╔██║██║   ██║██████╔╝██║  ███╗███████║
   ╚════██║██║██║╚██╔╝██║██║   ██║██╔══██╗██║   ██║██╔══██║
   ███████║██║██║ ╚═╝ ██║╚██████╔╝██║  ██║╚██████╔╝██║  ██║
   ╚══════╝╚═╝╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝
ART
  printf '%s' "$RESET"
printf '   %s\n\n' "${DIM}transparent Iran <-> Kharej tunnel  •  Simurgh Tunnel${RESET}"
}

usage() {
  cat <<EOF
${BOLD}Simurgh Tunnel installer${RESET}

  bash install.sh [options]

main options:
  --role exit|relay|both     role of this server (foreign / Iranian / both)
                             without --role the installer asks (or picks the
                             foreign/exit role when there is no terminal)
  --both                     run both services on one server (same as --role both)
  --name NAME                a name for this server
  --token TOKEN              shared secret token (generated/asked if missing)
  --exit HOST:PORT           foreign server address and port (for the relay)
  --dial relay|exit          who dials the tunnel (default relay = direct mode;
                             "exit" = reverse: this Iranian server listens)
  --carrier tls|wss|raw|plain  tunnel carrier
  --domain DOMAIN            domain name for the SNI / certificate
  --fingerprint HEX          certificate fingerprint of the foreign server (optional)
  --insecure                 skip verifying the foreign certificate (testing)
  --listen PORTS             exit listening ports, e.g. 443,2053
  --tunnel-port PORT         the tunnel port: in reverse mode (--dial exit) the
                             port THIS server listens on; with --role both the
                             port the local exit is reached on (it is added to
                             --listen, first, so both sides agree)
  --mapping L1:T1,L2:T2      relay port forwardings, e.g. 443:443
  --panel-port PORT          web panel port on the relay
  --split N                  anti-DPI: let the first N bytes of the tunnel
                             handshake travel in a TCP packet of their own, so a
                             filter that reads only the first packet of a
                             connection sees a fragment (0 = off, default 3)
  --connections N            tunnel connections to keep open (2-16, default 1):
                             better speed and resilience on long paths
  --engine auto|go|python    data plane: "go" is the compiled engine (many cores,
                             much less cpu per gigabyte), "python" the portable
                             one. auto = go when it can be built (default)
  --no-go                    shortcut for --engine python
  --yes                      accept everything without asking
  --no-systemd               do not create a systemd service (run manually)
  --ref REF                  git ref to download when the source is missing
                             (branch or tag, default: main)
  --uninstall [--purge]      remove the services (--purge also deletes the
                             config, the python environment and the source)
  --force                    overwrite the existing config (this is the default)
  --help                     this text
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --role|-r)     ROLE="${2:-}"
                   case "$ROLE" in both|all) ROLE="auto"; BOTH=1 ;; esac
                   shift 2 ;;
    --both)        BOTH=1; shift ;;
    --name|-n)     NAME="${2:-}"; shift 2 ;;
    --token|-t)    TOKEN="${2:-}"; shift 2 ;;
    --exit|-e)     EXIT_ADDR="${2:-}"; shift 2 ;;
    --dial)        DIAL="${2:-}"; shift 2 ;;
    --tunnel-port) TUNNEL_PORT="${2:-}"; shift 2 ;;
    --carrier)     CARRIER="${2:-}"; shift 2 ;;
    --domain|-d)   DOMAIN="${2:-}"; shift 2 ;;
    --fingerprint) FINGERPRINT="${2:-}"; shift 2 ;;
    --insecure)    INSECURE="--insecure"; shift ;;
    --listen|-l)   LISTEN="${2:-}"; shift 2 ;;
    --mapping|-m)  MAPPING="${2:-}"; shift 2 ;;
    --panel-port)  PANEL_PORT="${2:-}"; shift 2 ;;
    --connections) CONNECTIONS="${2:-}"; shift 2 ;;
    --split)       SPLIT="${2:-}"; shift 2 ;;
    --engine)      ENGINE="${2:-auto}"; shift 2 ;;
    --no-go)       ENGINE="python"; shift ;;
    --yes|-y)      ASSUME_YES=1; shift ;;
    --no-systemd)  NO_SYSTEMD=1; shift ;;
    --ref)         REF="${2:-main}"; shift 2 ;;
    --uninstall)   UNINSTALL=1; shift ;;
    --purge)       PURGE=1; shift ;;
    --force|-f)    FORCE=1; shift ;;
    -h|--help)     usage; exit 0 ;;
*) die "unknown option: $1  (see --help)";;
  esac
done

# ------------------------------------------------------- validate the options
# fail here, with a clear sentence, instead of inside the CLI later.
case "$ROLE" in
  ""|exit|relay|auto) ;;
  *) die "unknown role: $ROLE  (exit | relay | both)" ;;
esac
case "$DIAL" in
  ""|relay|exit) ;;
  *) die "unknown --dial: $DIAL  (relay | exit)" ;;
esac
case "$ENGINE" in
  auto|go|python) ;;
  *) die "unknown --engine: $ENGINE  (auto | go | python)" ;;
esac
if [ -n "$CARRIER" ]; then
  case "$CARRIER" in
    tls|wss|raw|plain) ;;
    *) die "unknown --carrier: $CARRIER  (tls | wss | raw | plain)" ;;
  esac
fi
for _var in "tunnel port:$TUNNEL_PORT" "panel port:$PANEL_PORT" \
            "connections:$CONNECTIONS" "split:$SPLIT"; do
  _val="${_var#*:}"
  [ -n "$_val" ] || continue
  case "$_val" in
    *[!0-9]*) die "the ${_var%%:*} must be a number (got: $_val)" ;;
  esac
done
if [ -n "$EXIT_ADDR" ]; then
  EXIT_ADDR="${EXIT_ADDR#*://}"            # accept a pasted tls://host:port
  case "$EXIT_ADDR" in
    *:*) ;;
    *) die "--exit wants HOST:PORT, e.g.  --exit 203.0.113.9:443" ;;
  esac
  EXIT_HOST="${EXIT_ADDR%:*}"
  EXIT_PORT="${EXIT_ADDR##*:}"
  [ -n "$EXIT_HOST" ] || die "--exit has no address: $EXIT_ADDR"
  # ``[2001:db8::1]:443`` -> the config wants the address without its brackets
  case "$EXIT_HOST" in
    \[*\]*) EXIT_HOST="${EXIT_HOST#[}"; EXIT_HOST="${EXIT_HOST%]}" ;;
    *:*) die "--exit with an IPv6 address needs brackets:  --exit [2001:db8::1]:443" ;;
  esac
  case "$EXIT_PORT" in
    *[!0-9]*|"") die "--exit wants HOST:PORT with a numeric port (got: $EXIT_ADDR)" ;;
  esac
  { [ "$EXIT_PORT" -ge 1 ] && [ "$EXIT_PORT" -le 65535 ]; } ||
    die "the --exit port is outside 1-65535 (got: $EXIT_PORT)"
fi

if [ "$(id -u)" != "0" ]; then
  # Containers, WSL and "install into my home" setups: allow it explicitly,
  # but never touch systemd then.
  if [ "${SIMURGH_ALLOW_NONROOT:-0}" = "1" ]; then
warn "running without root — no systemd service is created and the install goes into the user home."
    NO_SYSTEMD=1
  else
die "this script must run as root (sudo)."
  fi
fi

banner

# ---------------------------------------------------------------- system pkgs
PKG=""
if command -v apt-get >/dev/null 2>&1; then PKG="apt"
elif command -v dnf >/dev/null 2>&1; then PKG="dnf"
elif command -v yum >/dev/null 2>&1; then PKG="yum"
elif command -v apk >/dev/null 2>&1; then PKG="apk"
fi

install_pkgs() {
  local pkgs="$*"
  if [ "$(id -u)" != "0" ]; then
warn "cannot install packages without root (skipped: $pkgs)."
    return 0
  fi
  # the package list is meant to be split into separate arguments here
  # shellcheck disable=SC2086
  case "$PKG" in
    apt) DEBIAN_FRONTEND=noninteractive apt-get update -qq || true
         DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $pkgs ;;
    dnf) dnf install -y -q $pkgs ;;
    yum) yum install -y -q $pkgs ;;
    apk) apk add --no-cache $pkgs ;;
*)   warn "unknown package manager; please make sure python3 is installed." ;;
  esac
}

if [ "$UNINSTALL" = "1" ]; then
say "removing the Simurgh services…"
  # prefer the simurgh that this installer put here, then whatever is in PATH
  if [ -x "$VENV_DIR/bin/simurgh" ]; then
    if [ "$PURGE" = "1" ]; then
      "$VENV_DIR/bin/simurgh" uninstall --yes --purge || true
    else
      "$VENV_DIR/bin/simurgh" uninstall --yes || true
    fi
  elif command -v simurgh >/dev/null 2>&1; then
    if [ "$PURGE" = "1" ]; then
      simurgh uninstall --yes --purge || true
    else
      simurgh uninstall --yes || true
    fi
  else
    systemctl disable --now simurgh-exit simurgh-relay 2>/dev/null || true
    rm -f /etc/systemd/system/simurgh-*.service
    systemctl daemon-reload 2>/dev/null || true
  fi
  if [ "$PURGE" = "1" ]; then
    rm -rf /etc/simurgh "$VENV_DIR" "$SRC_DIR" /etc/sysctl.d/99-simurgh.conf
    rm -f "$BIN_DIR/simurgh" "$BIN_DIR/simurgh-go"
ok "everything removed (config, environment and source)."
  fi
ok "done."
  exit 0
fi

say "checking the prerequisites…"
if ! command -v python3 >/dev/null 2>&1; then
say "installing python…"; install_pkgs "python3 python3-venv python3-pip"
fi
PY=python3
PYVER="$($PY -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
say "python $PYVER found."
case "$PYVER" in
3.8|3.7|3.6|2.*) die "python 3.9 or newer is required (found: $PYVER)." ;;
esac
if ! $PY -c 'import venv' 2>/dev/null; then
say "installing the venv module…"; install_pkgs "python3-venv"
fi
if ! $PY -c 'import cryptography' 2>/dev/null; then
say "installing cryptography (needed for the raw carrier and certificates)…"
  install_pkgs "python3-cryptography" || true
fi
command -v openssl >/dev/null 2>&1 || install_pkgs "openssl" || true

# ------------------------------------------------------------------ source
# The documented one-liner pipes this script into bash, and then BASH_SOURCE is
# empty — reading ${BASH_SOURCE[0]} unconditionally aborts with `set -u`.
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
fi
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ] && [ -d "$SCRIPT_DIR/simurgh" ]; then
  SRC="$SCRIPT_DIR"
say "using the source next to this script: $SRC"
elif [ -f "$SRC_DIR/pyproject.toml" ] && [ -d "$SRC_DIR/simurgh" ]; then
  # a checkout that was put there by hand (or by a previous run)
  SRC="$SRC_DIR"
say "using the source in $SRC"
else
  if [ -d "$SRC_DIR/.git" ]; then
say "updating the source in $SRC_DIR ($REF)…"
git -C "$SRC_DIR" fetch --depth 1 origin "$REF" --quiet &&
git -C "$SRC_DIR" checkout --quiet FETCH_HEAD ||
warn "could not update the source; continuing with the existing copy."
  else
    command -v git >/dev/null 2>&1 || install_pkgs "git"
say "downloading the source from GitHub ($REF)…"
git -c advice.detachedHead=false clone --depth 1 --branch "$REF" "$REPO_URL" "$SRC_DIR" ||
die "could not download the source ($REF)."
  fi
  SRC="$SRC_DIR"
fi

# --------------------------------------------------------------- virtualenv
say "creating the python environment in $VENV_DIR…"
mkdir -p "$(dirname "$VENV_DIR")" "$BIN_DIR"
$PY -m venv "$VENV_DIR" 2>/dev/null || {
  install_pkgs "python3-venv"; $PY -m venv "$VENV_DIR"; }
"$VENV_DIR/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
say "installing the simurgh package…"
install_pkg() {
  "$VENV_DIR/bin/pip" install --quiet "$@" && return 0
  return 1
}
if ! install_pkg "$SRC"; then
  # No PyPI (usual on an Iranian server): build with the distribution's own
  # setuptools and wheel, which the venv is then allowed to see.
warn "the normal install failed; retrying without downloading build tools…"
  install_pkgs "python3-setuptools python3-wheel python3-pip" || true
  if ! "$VENV_DIR/bin/python" -c 'import setuptools, wheel' >/dev/null 2>&1; then
say "recreating the environment with access to the system packages…"
    case "$VENV_DIR" in ""|"/") die "refusing to remove '$VENV_DIR'." ;; esac
    rm -rf "$VENV_DIR"
    $PY -m venv --system-site-packages "$VENV_DIR" ||
      die "could not create the python environment in $VENV_DIR."
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
  fi
  if install_pkg --no-build-isolation "$SRC"; then
    :
  else
    # Last resort, and it needs no build tooling at all: point the interpreter
    # at this checkout and start the CLI with `python -m simurgh`.
warn "the package could not be built; running it from $SRC instead."
    SITE="$("$VENV_DIR/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null || true)"
    [ -n "$SITE" ] && printf '%s\n' "$SRC" > "$SITE/simurgh-src.pth"
    "$VENV_DIR/bin/python" -c 'import simurgh' >/dev/null 2>&1 ||
      die "package installation failed (and $SRC cannot be imported either)."
    printf '#!/bin/sh\nexec "%s/bin/python" -m simurgh "$@"\n' "$VENV_DIR" > "$VENV_DIR/bin/simurgh"
    chmod +x "$VENV_DIR/bin/simurgh"
  fi
fi

# `cryptography` is optional for the package but needed by the raw carrier and
# by the certificate helper, and the system package above does not reach into
# this venv — so try the venv's own pip, and carry on when it is offline.
if "$VENV_DIR/bin/python" -c 'import cryptography' >/dev/null 2>&1; then
  :
elif install_pkg --timeout 20 --retries 2 "cryptography>=41" 2>/dev/null; then
ok "cryptography installed (raw carrier + certificates)."
else
warn "cryptography is not installed: the 'raw' carrier is unavailable and"
warn "certificates fall back to openssl. Add it later with:"
warn "  $VENV_DIR/bin/pip install cryptography"
fi

[ -x "$VENV_DIR/bin/simurgh" ] && ln -sf "$VENV_DIR/bin/simurgh" "$BIN_DIR/simurgh"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) export PATH="$BIN_DIR:$PATH"
warn "$BIN_DIR is not in your PATH; use $BIN_DIR/simurgh or add it to PATH." ;;
esac
ok "the simurgh command is installed: $(command -v simurgh)"

# ------------------------------------------------------------- go engine
# The compiled engine speaks the same protocol and keeps many users fast on
# one core-poor box; the python engine stays as the portable fallback.
ENGINE_CHOSEN="python"
GO_BUILD_LOG="${TMPDIR:-/tmp}/simurgh-go-build.log"
if [ "$ENGINE" != "python" ]; then
  say "building the compiled engine (this takes a few seconds)…"
  if SIMURGH_GO_BIN="$BIN_DIR/simurgh-go" bash "$SRC/tools/build-go.sh" >"$GO_BUILD_LOG" 2>&1; then
    ENGINE_CHOSEN="go"
    ok "the go engine is installed: $BIN_DIR/simurgh-go"
  else
    # no toolchain, no network, or an unsupported cpu: the python engine always
    # works, so this is only a warning unless the user asked for --engine go.
    if [ "$ENGINE" = "go" ]; then
warn "the go engine could not be built here (log: $GO_BUILD_LOG)."
    else
say "no compiled engine here; the portable python engine will be used."
    fi
    # the last lines of the log are the reason (see tools/build-go.sh)
    tail -n 3 "$GO_BUILD_LOG" 2>/dev/null | while IFS= read -r line; do
      printf '  %s%s%s\n' "$DIM" "$line" "$RESET"
    done
say "build it later with:  sudo bash $SRC/tools/build-go.sh  &&  simurgh engine go"
  fi
else
  say "using the python engine (--engine python)."
fi

# ------------------------------------------------------------ kernel tuning
# A relay with a thousand users needs more sockets than a stock kernel opens,
# and BBR keeps a long, lossy path full instead of backing off on every loss.
if [ "$(id -u)" = "0" ] && [ -d /etc/sysctl.d ] && command -v sysctl >/dev/null 2>&1; then
  SYSCTL_FILE=/etc/sysctl.d/99-simurgh.conf
  say "tuning kernel limits for many simultaneous users…"
  {
    echo "# Simurgh Tunnel: sockets and buffers for many simultaneous users"
    echo "fs.file-max = 1048576"
    echo "net.core.somaxconn = 65535"
    echo "net.ipv4.tcp_max_syn_backlog = 65535"
    echo "net.ipv4.ip_local_port_range = 10240 65535"
    echo "net.ipv4.tcp_tw_reuse = 1"
    echo "net.core.rmem_max = 16777216"
    echo "net.core.wmem_max = 16777216"
    echo "net.ipv4.tcp_rmem = 4096 87380 16777216"
    echo "net.ipv4.tcp_wmem = 4096 65536 16777216"
  } > "$SYSCTL_FILE"
  if sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null | grep -qw bbr; then
    printf '%s\n' "net.core.default_qdisc = fq" \
                  "net.ipv4.tcp_congestion_control = bbr" >> "$SYSCTL_FILE"
    ok "bbr congestion control enabled (keeps long paths fast)"
  fi
  sysctl -p "$SYSCTL_FILE" >/dev/null 2>&1 && ok "kernel limits applied ($SYSCTL_FILE)" \
    || warn "some kernel values could not be applied"
fi

# --------------------------------------------------------------- configure
# Ask first, build the argument list afterwards: the answers decide what
# `simurgh install` is told (asking about the tunnel port after the argument
# list was already built is how --tunnel-port used to get lost).
if [ -z "$ROLE" ] && [ "$ASSUME_YES" != "1" ] && [ -t 0 ]; then
printf '\n%s\n' "${BOLD}What is the role of this server?${RESET}"
printf '  %s1)%s Foreign server (exit)  — your panel and services live here\n' "$CYAN" "$RESET"
printf '  %s2)%s Iranian server (relay) — your users connect to this server\n' "$CYAN" "$RESET"
read -r -p "  choice [1]: " _role_choice
  case "${_role_choice:-1}" in
2|relay) ROLE="relay" ;;
    *) ROLE="exit" ;;
  esac
  if [ "$ROLE" = "relay" ] && [ -z "$EXIT_ADDR" ]; then
    printf '  %s1)%s direct: this server dials the foreign server (normal)\n' "$CYAN" "$RESET"
    printf '  %s2)%s reverse: the foreign server dials this one (no open port needed there)\n' "$CYAN" "$RESET"
    read -r -p "  choice [1]: " _dial_choice
    if [ "${_dial_choice:-1}" = "2" ]; then
      DIAL="exit"
      read -r -p "  tunnel port on this server [8443]: " _tp
      TUNNEL_PORT="${_tp:-8443}"
    fi
  fi
fi
if [ -z "$ROLE" ]; then
  # no answer needed: a relay dials a foreign server, the exit is what is left
  if [ -n "$EXIT_ADDR" ] || [ "$DIAL" = "exit" ]; then ROLE="relay"; else ROLE="exit"; fi
fi

# ------------------------------------------------- where is the exit server?
# first listen port (``443,2053`` / ``443-445`` / ``443 2053`` all work)
first_listen_port() {
  printf '%s' "$1" | sed -e 's/^[[:space:]]*//' -e 's/[, ].*$//' -e 's/-.*$//'
}
# Both services on one server: the relay dials the exit that runs next to it,
# and the exit has to listen on exactly the port the relay dials -- served with
# tls, which is the carrier `simurgh install` gives the first listen port.
if [ -z "$EXIT_HOST" ] && [ "$BOTH" = "1" ] && [ "$DIAL" != "exit" ]; then
  if [ -n "$TUNNEL_PORT" ]; then
    case ",$LISTEN," in
      *",$TUNNEL_PORT,"*) ;;
      # first in the list on purpose: `simurgh install` serves the *first*
      # listen port with tls and only switches the third one to wss
      *) LISTEN="$TUNNEL_PORT${LISTEN:+,$LISTEN}" ;;
    esac
  else
    TUNNEL_PORT="$(first_listen_port "$LISTEN")"
    [ -n "$TUNNEL_PORT" ] || TUNNEL_PORT="443"
  fi
  EXIT_HOST="127.0.0.1"
  EXIT_PORT="$TUNNEL_PORT"
say "both roles on this server: the relay dials the local exit on $EXIT_HOST:$EXIT_PORT"
fi
if [ "$BOTH" = "1" ]; then
  if [ -n "$CARRIER" ] && [ "$CARRIER" != "tls" ]; then
warn "a local exit serves tls on its first port; '$CARRIER' is ignored there."
  fi
  CARRIER="tls"
fi
if [ -n "$EXIT_HOST" ]; then
  [ -n "$EXIT_PORT" ] || EXIT_PORT="443"
elif [ -n "$TUNNEL_PORT" ]; then
  # in reverse mode the tunnel port is the port THIS server listens on, which
  # is what `--exit-port` means there. In direct mode it would silently replace
  # the foreign port, so we say so instead of doing that.
  if [ "$DIAL" = "exit" ]; then
    EXIT_PORT="$TUNNEL_PORT"
  else
warn "--tunnel-port only applies in reverse mode (--dial exit); ignoring it."
  fi
fi

ARGS=()
[ "$FORCE" = "1" ]     && ARGS+=(--force)
ARGS+=(--role "$ROLE")
[ "$BOTH" = "1" ]      && ARGS+=(--both)
[ -n "$NAME" ]         && ARGS+=(--name "$NAME")
[ -n "$EXIT_HOST" ]    && ARGS+=(--exit-host "$EXIT_HOST")
[ -n "$EXIT_PORT" ]    && ARGS+=(--exit-port "$EXIT_PORT")
[ -n "$DIAL" ]         && ARGS+=(--dial "$DIAL")
[ -n "$CARRIER" ]      && ARGS+=(--carrier "$CARRIER")
[ -n "$DOMAIN" ]       && ARGS+=(--domain "$DOMAIN")
[ -n "$FINGERPRINT" ]  && ARGS+=(--fingerprint "$FINGERPRINT")
[ -n "$INSECURE" ]     && ARGS+=("$INSECURE")
[ -n "$LISTEN" ]       && ARGS+=(--listen "$LISTEN")
[ -n "$PANEL_PORT" ]   && ARGS+=(--panel-port "$PANEL_PORT")
[ -n "$CONNECTIONS" ]  && ARGS+=(--connections "$CONNECTIONS")
[ -n "$SPLIT" ]        && ARGS+=(--split "$SPLIT")
ARGS+=(--engine "$ENGINE_CHOSEN")
if [ -n "$MAPPING" ]; then
  IFS=',' read -ra _maps <<< "$MAPPING"
  for m in "${_maps[@]}"; do ARGS+=(--mapping "$m"); done
fi
[ "$NO_SYSTEMD" = "1" ] && ARGS+=(--no-units)

SIMURGH_BIN="$VENV_DIR/bin/simurgh"
if [ -z "$TOKEN" ]; then
  if [ "$BOTH" = "1" ]; then
    # one token for the two local services, generated here and shown at the end
    TOKEN="$(od -An -tx1 -N16 /dev/urandom 2>/dev/null | tr -d ' \n')"
    [ -n "$TOKEN" ] || TOKEN="tok$(date +%s)$RANDOM"
    ARGS+=(--token "$TOKEN")
ok "generated a shared token: $TOKEN"
  elif [ "$ROLE" = "relay" ] && [ ! -t 0 ]; then
die "the relay needs the shared token:  --token XXX  (copy it from the exit,
     or install the exit first and run:  sudo simurgh join '<setup link>')"
  fi
else
  ARGS+=(--token "$TOKEN")
fi

say "writing the config…"
"$SIMURGH_BIN" install "${ARGS[@]}" || die "writing the config failed."

# ------------------------------------------------------------------- finish
echo
ok "installation complete."
if [ -n "${SIMURGH_HOME:-}" ]; then SIMHOME="$SIMURGH_HOME"
elif [ "$(id -u)" = "0" ]; then SIMHOME=/etc/simurgh
else SIMHOME="$HOME/.simurgh"; fi
PORTS="$(grep -E '^port = ' "$SIMHOME/exit.toml" 2>/dev/null | awk '{print $3}' | paste -sd, - || true)"
# `auto` means "the relay when there is a foreign server, otherwise the exit";
# resolve it here only so the summary shows the right instructions.
EFFECTIVE_ROLE="$ROLE"
if [ "$ROLE" = "auto" ]; then
  if [ -n "$EXIT_HOST" ] || [ "$DIAL" = "exit" ]; then EFFECTIVE_ROLE="relay"; else EFFECTIVE_ROLE="exit"; fi
fi
SHOW_PANEL=0; SHOW_LINK=0
[ "$EFFECTIVE_ROLE" = "relay" ] && SHOW_PANEL=1
[ "$EFFECTIVE_ROLE" = "exit" ] && SHOW_LINK=1
if [ "$BOTH" = "1" ]; then SHOW_PANEL=1; SHOW_LINK=1; fi
if [ "$SHOW_PANEL" = "1" ]; then
  PANEL="$(SIMHOME="$SIMHOME" "$VENV_DIR/bin/python" - <<'PY' 2>/dev/null || echo 8787
import os
from simurgh.config import load_relay
try:
    print(load_relay(os.path.join(os.environ["SIMHOME"], "relay.toml")).panel_port or 8787)
except Exception:
    print(8787)
PY
)"
  CREDS="$(SIMHOME="$SIMHOME" "$VENV_DIR/bin/python" - <<'PY' 2>/dev/null || true
import json, os
try:
    data = json.load(open(os.path.join(os.environ["SIMHOME"], "state.json")))
    auth = data.get("panel_auth", {})
    print(f"{auth.get('user','admin')}:{auth.get('password','')}")
except Exception:
    pass
PY
)"
printf '%s\n' "  ${BOLD}web panel:${RESET} ${CYAN}http://<IRAN-SERVER-IP>:$PANEL${RESET}"
[ -n "$CREDS" ] && printf '%s\n' "  ${BOLD}login:${RESET} $CREDS"
printf '%s\n' "  ${DIM}add a port: simurgh mapping add 443 443${RESET}"
printf '%s\n' "  ${DIM}text menu: simurgh menu${RESET}"
fi
if [ "$SHOW_LINK" = "1" ]; then
  LINK="$("$SIMURGH_BIN" link --show 2>/dev/null || true)"
  LINK_LABEL="relay setup link"
  [ "$DIAL" = "exit" ] && LINK_LABEL="exit setup link"
[ -n "$LINK" ] && printf '%s\n' "  ${BOLD}$LINK_LABEL:${RESET} ${CYAN}$LINK${RESET}"
printf '%s\n' "  ${DIM}active ports: ${PORTS:-443}${RESET}"
  if [ "$BOTH" = "1" ] && [ "$DIAL" = "exit" ]; then
    # reverse + both: the local exit has to be told to dial this relay. The
    # link is fetched from the local panel, so this only works once the relay
    # is running; when it is not, we print the exact command to run later.
    LOCAL_LINK="$("$SIMURGH_BIN" link --show --host 127.0.0.1 2>/dev/null || true)"
    if [ -n "$LOCAL_LINK" ] &&
       "$SIMURGH_BIN" join "$LOCAL_LINK" --force --host 127.0.0.1 >/dev/null 2>&1; then
      systemctl restart simurgh-exit 2>/dev/null || true
      ok "reverse mode: the local exit dials this relay now (127.0.0.1)"
    else
      printf '%s\n' "  ${DIM}reverse mode: the local exit still has to be pointed at this relay:${RESET}"
      printf '%s\n' "  ${DIM}  sudo simurgh link --host 127.0.0.1 --show    # then:${RESET}"
      printf '%s\n' "  ${DIM}  sudo simurgh join '<that link>' --host 127.0.0.1 --force${RESET}"
    fi
  fi
fi
printf '%s\n' "  ${DIM}status: simurgh status   •   log: simurgh logs -f${RESET}"
echo
if [ "$NO_SYSTEMD" = "1" ]; then
warn "installed without systemd. To run it:  simurgh exit   or   simurgh relay"
fi

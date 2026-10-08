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
SRC_DIR="${SIMURGH_SRC:-/opt/simurgh-src}"
if [ "$(id -u)" = "0" ]; then
  VENV_DIR="${SIMURGH_VENV:-/opt/simurgh/venv}"
  BIN_DIR="/usr/local/bin"
else
  VENV_DIR="${SIMURGH_VENV:-$HOME/.local/share/simurgh/venv}"
  BIN_DIR="$HOME/.local/bin"
fi

ROLE=""
NAME=""
TOKEN=""
EXIT_ADDR=""
LISTEN=""
MAPPING=""
PANEL_PORT=""
ASSUME_YES=0
NO_SYSTEMD=0
UNINSTALL=0
FORCE=0
CARRIER=""
DOMAIN=""
FINGERPRINT=""
INSECURE=""

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
  --name NAME                a name for this server
  --token TOKEN              shared secret token (generated/asked if missing)
  --exit HOST:PORT           foreign server address and port (for the relay)
  --carrier tls|wss|raw      tunnel carrier
  --domain DOMAIN            domain name for the SNI / certificate
  --fingerprint HEX          certificate fingerprint of the foreign server (optional)
  --insecure                 skip verifying the foreign certificate (testing)
  --listen PORTS             exit listening ports, e.g. 443,2053
  --mapping L1:T1,L2:T2      relay port forwardings, e.g. 443:443
  --panel-port PORT          web panel port on the relay
  --yes                      accept everything without asking
  --no-systemd               do not create a systemd service (run manually)
  --uninstall [--purge]      remove the services
  --force                    overwrite the existing config
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --role|-r)     ROLE="${2:-}"; shift 2 ;;
    --name|-n)     NAME="${2:-}"; shift 2 ;;
    --token|-t)    TOKEN="${2:-}"; shift 2 ;;
    --exit|-e)     EXIT_ADDR="${2:-}"; shift 2 ;;
    --carrier)     CARRIER="${2:-}"; shift 2 ;;
    --domain|-d)   DOMAIN="${2:-}"; shift 2 ;;
    --fingerprint) FINGERPRINT="${2:-}"; shift 2 ;;
    --insecure)    INSECURE="--insecure"; shift ;;
    --listen|-l)   LISTEN="${2:-}"; shift 2 ;;
    --mapping|-m)  MAPPING="${2:-}"; shift 2 ;;
    --panel-port)  PANEL_PORT="${2:-}"; shift 2 ;;
    --yes|-y)      ASSUME_YES=1; shift ;;
    --no-systemd)  NO_SYSTEMD=1; shift ;;
    --uninstall)   UNINSTALL=1; shift ;;
    --purge)       PURGE=1; shift ;;
    --force|-f)    FORCE=1; shift ;;
    -h|--help)     usage; exit 0 ;;
*) die "unknown option: $1  (see --help)";;
  esac
done

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
  if command -v simurgh >/dev/null 2>&1; then
    simurgh uninstall --yes ${PURGE:+--purge} || true
  else
    systemctl disable --now simurgh-exit simurgh-relay 2>/dev/null || true
    rm -f /etc/systemd/system/simurgh-*.service
    systemctl daemon-reload 2>/dev/null || true
  fi
  [ "${PURGE:-0}" = "1" ] && rm -rf /etc/simurgh "$VENV_DIR"
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
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/pyproject.toml" ] && [ -d "$SCRIPT_DIR/simurgh" ]; then
  SRC="$SCRIPT_DIR"
say "using the source next to this script: $SRC"
else
  if [ -d "$SRC_DIR/.git" ]; then
say "updating the source in $SRC_DIR…"
git -C "$SRC_DIR" pull --quiet --ff-only || warn "could not update the source; continuing with the existing copy."
  else
    command -v git >/dev/null 2>&1 || install_pkgs "git"
say "downloading the source from GitHub…"
git clone --depth 1 "$REPO_URL" "$SRC_DIR" || die "could not download the source."
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
warn "the normal install failed; trying offline (no-build-isolation)…"
  install_pkgs "python3-setuptools python3-wheel" || true
install_pkg --no-build-isolation "$SRC" || die "package installation failed."
fi
ln -sf "$VENV_DIR/bin/simurgh" "$BIN_DIR/simurgh"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) export PATH="$BIN_DIR:$PATH" ;; esac
ok "the simurgh command is installed: $(command -v simurgh)"

# --------------------------------------------------------------- configure
ARGS=(--force)
[ -n "$ROLE" ]        && ARGS+=(--role "$ROLE")
[ -n "$NAME" ]        && ARGS+=(--name "$NAME")
[ -n "$TOKEN" ]       && ARGS+=(--token "$TOKEN")
[ -n "$EXIT_ADDR" ]   && ARGS+=(--exit-host "${EXIT_ADDR%%:*}" --exit-port "${EXIT_ADDR##*:}")
[ -n "$CARRIER" ]     && ARGS+=(--carrier "$CARRIER")
[ -n "$DOMAIN" ]      && ARGS+=(--domain "$DOMAIN")
[ -n "$FINGERPRINT" ] && ARGS+=(--fingerprint "$FINGERPRINT")
[ -n "$INSECURE" ]    && ARGS+=("$INSECURE")
[ -n "$LISTEN" ]      && ARGS+=(--listen "$LISTEN")
[ -n "$PANEL_PORT" ]  && ARGS+=(--panel-port "$PANEL_PORT")
if [ -n "$MAPPING" ]; then
  IFS=',' read -ra _maps <<< "$MAPPING"
  for m in "${_maps[@]}"; do ARGS+=(--mapping "$m"); done
fi
[ "$NO_SYSTEMD" = "1" ] && ARGS+=(--no-start --no-enable)

if [ -z "$ROLE" ] && [ "$ASSUME_YES" = "1" ]; then
  [ -n "$EXIT_ADDR" ] && ROLE="relay" || ROLE="exit"
  ARGS+=(--role "$ROLE")
fi
if [ -z "$ROLE" ] && [ -t 0 ]; then
printf '\n%s\n' "${BOLD}What is the role of this server?${RESET}"
printf '  %s1)%s Foreign server (exit)  — your panel and services live here\n' "$CYAN" "$RESET"
printf '  %s2)%s Iranian server (relay) — your users connect to this server\n' "$CYAN" "$RESET"
read -r -p "  choice [1]: " _role_choice
  case "${_role_choice:-1}" in
2|relay) ROLE="relay" ;;
    *) ROLE="exit" ;;
  esac
  ARGS+=(--role "$ROLE")
fi
[ -n "$ROLE" ] || ROLE="auto"

say "writing the config…"
simurgh install "${ARGS[@]}" || die "writing the config failed."

# ------------------------------------------------------------------- finish
echo
ok "installation complete."
if [ -n "${SIMURGH_HOME:-}" ]; then SIMHOME="$SIMURGH_HOME"
elif [ "$(id -u)" = "0" ]; then SIMHOME=/etc/simurgh
else SIMHOME="$HOME/.simurgh"; fi
PORTS="$(grep -E '^port = ' "$SIMHOME/exit.toml" 2>/dev/null | awk '{print $3}' | paste -sd, - || true)"
if [ "$ROLE" = "relay" ]; then
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
else
  LINK="$(simurgh link --show 2>/dev/null || true)"
[ -n "$LINK" ] && printf '%s\n' "  ${BOLD}relay setup link:${RESET} ${CYAN}$LINK${RESET}"
printf '%s\n' "  ${DIM}active ports: ${PORTS:-443}${RESET}"
fi
printf '%s\n' "  ${DIM}status: simurgh status   •   log: simurgh logs -f${RESET}"
echo
if [ "$NO_SYSTEMD" = "1" ]; then
warn "installed without systemd. To run it:  simurgh exit   or   simurgh relay"
fi

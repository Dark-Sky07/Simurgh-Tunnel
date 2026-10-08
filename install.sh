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
  printf '   %s\n\n' "${DIM}تونل شفاف ایران ↔ خارج  •  Simurgh Tunnel${RESET}"
}

usage() {
  cat <<EOF
${BOLD}نصب سیمورغ${RESET}

  bash install.sh [گزینه‌ها]

گزینه‌های اصلی:
  --role exit|relay|both     نقش این سرور (خارج / ایران / هر دو)
  --name NAME                نام این سرور
  --token TOKEN              توکن مشترک (اگر ندهید ساخته/پرسیده می‌شود)
  --exit HOST:PORT           آدرس و پورت سرور خارج (برای رله)
  --carrier tls|wss|raw      نوع حامل تونل
  --domain DOMAIN            دامنه برای SNI/گواهی
  --fingerprint HEX          اثر انگشت گواهی سرور خارج (اختیاری)
  --insecure                 تایید گواهی سرور خارج را رد کن (تست)
  --listen PORTS             پورت‌های گوش‌دادن اگزیت، مثل 443,2053
  --mapping L1:T1,L2:T2      پورت‌های فوروارد رله، مثل 443:443
  --panel-port PORT          پورت پنل وب روی رله
  --yes                      بدون پرسش، همه‌چیز را بپذیر
  --no-systemd               سرویس systemd نساز (اجرای دستی)
  --uninstall [--purge]      حذف سرویس‌ها
  --force                    بازنویسی تنظیمات موجود
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
    *) die "گزینه ناشناخته: $1  (با --help راهنما را ببینید)" ;;
  esac
done

if [ "$(id -u)" != "0" ]; then
  # Containers, WSL and "install into my home" setups: allow it explicitly,
  # but never touch systemd then.
  if [ "${SIMURGH_ALLOW_NONROOT:-0}" = "1" ]; then
    warn "اجرای بدون root — سرویس systemd ساخته نمی‌شود و مسیر نصب در خانهٔ کاربر است."
    NO_SYSTEMD=1
  else
    die "این اسکریپت باید با root اجرا شود (sudo)."
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
    warn "بدون root نمی‌توان بسته نصب کرد (رد شد: $pkgs)."
    return 0
  fi
  case "$PKG" in
    apt) DEBIAN_FRONTEND=noninteractive apt-get update -qq || true
         DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $pkgs ;;
    dnf) dnf install -y -q $pkgs ;;
    yum) yum install -y -q $pkgs ;;
    apk) apk add --no-cache $pkgs ;;
    *)   warn "مدیر بسته شناخته نشد؛ مطمئن شوید پایتون نصب است." ;;
  esac
}

if [ "$UNINSTALL" = "1" ]; then
  say "حذف سرویس‌های سیمورغ…"
  if command -v simurgh >/dev/null 2>&1; then
    simurgh uninstall --yes ${PURGE:+--purge} || true
  else
    systemctl disable --now simurgh-exit simurgh-relay 2>/dev/null || true
    rm -f /etc/systemd/system/simurgh-*.service
    systemctl daemon-reload 2>/dev/null || true
  fi
  [ "${PURGE:-0}" = "1" ] && rm -rf /etc/simurgh "$VENV_DIR"
  ok "تمام شد."
  exit 0
fi

say "بررسی پیش‌نیازها…"
if ! command -v python3 >/dev/null 2>&1; then
  say "نصب پایتون…"; install_pkgs "python3 python3-venv python3-pip"
fi
PY=python3
PYVER="$($PY -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
say "پایتون $PYVER پیدا شد."
case "$PYVER" in
  3.8|3.7|3.6|2.*) die "پایتون ۳.۹ یا بالاتر لازم است (نسخه فعلی: $PYVER)." ;;
esac
if ! $PY -c 'import venv' 2>/dev/null; then
  say "نصب ماژول venv…"; install_pkgs "python3-venv"
fi
if ! $PY -c 'import cryptography' 2>/dev/null; then
  say "نصب cryptography (برای حامل raw و گواهی)…"
  install_pkgs "python3-cryptography" || true
fi
command -v openssl >/dev/null 2>&1 || install_pkgs "openssl" || true

# ------------------------------------------------------------------ source
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/pyproject.toml" ] && [ -d "$SCRIPT_DIR/simurgh" ]; then
  SRC="$SCRIPT_DIR"
  say "استفاده از سورس کنار اسکریپت: $SRC"
else
  if [ -d "$SRC_DIR/.git" ]; then
    say "به‌روزرسانی سورس در $SRC_DIR…"
    git -C "$SRC_DIR" pull --quiet --ff-only || warn "به‌روزرسانی سورس ناموفق بود؛ ادامه با نسخه موجود."
  else
    command -v git >/dev/null 2>&1 || install_pkgs "git"
    say "دریافت سورس از گیت‌هاب…"
    git clone --depth 1 "$REPO_URL" "$SRC_DIR" || die "دریافت سورس ناموفق بود."
  fi
  SRC="$SRC_DIR"
fi

# --------------------------------------------------------------- virtualenv
say "ساخت محیط پایتون در $VENV_DIR…"
mkdir -p "$(dirname "$VENV_DIR")" "$BIN_DIR"
$PY -m venv "$VENV_DIR" 2>/dev/null || {
  install_pkgs "python3-venv"; $PY -m venv "$VENV_DIR"; }
"$VENV_DIR/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
say "نصب بسته سیمورغ…"
install_pkg() {
  "$VENV_DIR/bin/pip" install --quiet "$@" && return 0
  return 1
}
if ! install_pkg "$SRC"; then
  warn "نصب عادی ناموفق بود؛ تلاش بدون شبکه (no-build-isolation)…"
  install_pkgs "python3-setuptools python3-wheel" || true
  install_pkg --no-build-isolation "$SRC" || die "نصب بسته ناموفق بود."
fi
ln -sf "$VENV_DIR/bin/simurgh" "$BIN_DIR/simurgh"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) export PATH="$BIN_DIR:$PATH" ;; esac
ok "فرمان simurgh نصب شد: $(command -v simurgh)"

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
  printf '\n%s\n' "${BOLD}این سرور چه نقشی دارد؟${RESET}"
  printf '  %s1)%s سرور خارج (Exit)  — پنل اصلی و سرویس‌ها اینجا هستند\n' "$CYAN" "$RESET"
  printf '  %s2)%s سرور ایران (Relay) — کاربران به این سرور وصل می‌شوند\n' "$CYAN" "$RESET"
  read -r -p "  انتخاب [1]: " _role_choice
  case "${_role_choice:-1}" in
    2|relay|ایران) ROLE="relay" ;;
    *) ROLE="exit" ;;
  esac
  ARGS+=(--role "$ROLE")
fi
[ -n "$ROLE" ] || ROLE="auto"

say "ساخت تنظیمات…"
simurgh install "${ARGS[@]}" || die "نصب تنظیمات ناموفق بود."

# ------------------------------------------------------------------- finish
echo
ok "نصب کامل شد."
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
  printf '%s\n' "  ${BOLD}پنل مدیریت:${RESET} ${CYAN}http://<IP-سرور-ایران>:$PANEL${RESET}"
  [ -n "$CREDS" ] && printf '%s\n' "  ${BOLD}ورود:${RESET} $CREDS"
  printf '%s\n' "  ${DIM}افزودن پورت: simurgh mapping add 443 443${RESET}"
  printf '%s\n' "  ${DIM}منوی متنی: simurgh menu${RESET}"
else
  LINK="$(simurgh link --show 2>/dev/null || true)"
  [ -n "$LINK" ] && printf '%s\n' "  ${BOLD}لینک اتصال رله:${RESET} ${CYAN}$LINK${RESET}"
  printf '%s\n' "  ${DIM}پورت‌های فعال: ${PORTS:-443}${RESET}"
fi
printf '%s\n' "  ${DIM}وضعیت: simurgh status   •   لاگ: simurgh logs -f${RESET}"
echo
if [ "$NO_SYSTEMD" = "1" ]; then
  warn "بدون systemd نصب شد. برای اجرا:  simurgh exit   یا   simurgh relay"
fi

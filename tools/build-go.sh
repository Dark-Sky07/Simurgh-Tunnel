#!/usr/bin/env bash
# Get the compiled (Go) data plane of Simurgh Tunnel onto this machine.
#
#   sudo bash tools/build-go.sh                  # download the release binary, else build
#   sudo bash tools/build-go.sh --from-source    # always build locally
#   bash tools/build-go.sh --release dist/       # cross-compile every platform (maintainers)
#
# The engine speaks exactly the same protocol as the Python one, so a relay and
# an exit may each run whichever engine they have; the installer uses this
# script and falls back to Python when nothing works here.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GO_DIR="$SRC_DIR/go"
OUT="${SIMURGH_GO_BIN:-$SRC_DIR/bin/simurgh-go}"
GO_VERSION="${SIMURGH_GO_VERSION:-1.27.1}"
REPO_SLUG="${SIMURGH_REPO_SLUG:-Dark-Sky07/Simurgh-Tunnel}"
FROM_SOURCE=0
RELEASE_DIR=""

say()  { printf '  %s\n' "$*"; }
die()  { printf 'build-go: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --from-source) FROM_SOURCE=1; shift ;;
    --release)     RELEASE_DIR="${2:-}"; shift 2 ;;
    -h|--help)     sed -n '2,12p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[ -d "$GO_DIR" ] || die "no go/ directory in $SRC_DIR"

arch_name() {
  case "$(uname -m)" in
    x86_64|amd64) echo amd64 ;;
    aarch64|arm64) echo arm64 ;;
    armv7l) echo armv7 ;;
    *) echo "" ;;
  esac
}

fetch_prebuilt() {
  local arch url tmp
  arch="$(arch_name)"
  [ -n "$arch" ] || return 1
  [ "$(uname -s)" = "Linux" ] || return 1
  command -v curl >/dev/null 2>&1 || return 1
  url="${SIMURGH_GO_URL:-https://github.com/${REPO_SLUG}/releases/latest/download/simurgh-go-linux-${arch}}"
  tmp="$(mktemp)"
  say "looking for a prebuilt engine ($url)…"
  if curl -fsSL --max-time 60 "$url" -o "$tmp" 2>/dev/null && [ -s "$tmp" ]; then
    mkdir -p "$(dirname "$OUT")"
    install -m 0755 "$tmp" "$OUT"
    rm -f "$tmp"
    say "downloaded: $OUT"
    return 0
  fi
  rm -f "$tmp"
  return 1
}

find_go() {
  if [ -n "${GO_BIN:-}" ] && [ -x "${GO_BIN}" ]; then echo "${GO_BIN}"; return; fi
  if command -v go >/dev/null 2>&1; then command -v go; return; fi
  for candidate in /usr/local/go/bin/go /usr/lib/go/bin/go /opt/go/bin/go \
                   "$HOME/.local/go/bin/go" "$HOME/.cache/goroot/bin/go"; do
    [ -x "$candidate" ] && { echo "$candidate"; return; }
  done
  return 1
}

install_toolchain() {
  # 1. the distribution package (Debian/Ubuntu: golang-go, RHEL: golang)
  if command -v apt-get >/dev/null 2>&1; then
    say "installing golang-go (apt)…"
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends golang-go >/dev/null 2>&1 || true
  elif command -v dnf >/dev/null 2>&1; then
    say "installing golang (dnf)…"
    dnf install -y golang >/dev/null 2>&1 || true
  elif command -v yum >/dev/null 2>&1; then
    say "installing golang (yum)…"
    yum install -y golang >/dev/null 2>&1 || true
  fi
  find_go && return 0

  # 2. the official tarball (needs a working outbound HTTPS)
  local arch url tmp
  arch="$(arch_name)"
  [ -n "$arch" ] || return 1
  url="https://go.dev/dl/go${GO_VERSION}.linux-${arch}.tar.gz"
  tmp="$(mktemp -d)"
  say "downloading the Go toolchain (${GO_VERSION})…"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$tmp/go.tgz" || { rm -rf "$tmp"; return 1; }
  elif command -v wget >/dev/null 2>&1; then
    wget -q "$url" -O "$tmp/go.tgz" || { rm -rf "$tmp"; return 1; }
  else
    rm -rf "$tmp"; return 1
  fi
  mkdir -p "$HOME/.cache/goroot"
  tar -xzf "$tmp/go.tgz" -C "$HOME/.cache/goroot" --strip-components=1 || { rm -rf "$tmp"; return 1; }
  rm -rf "$tmp"
  find_go
}

# ------------------------------------------------------------------ release mode
if [ -n "$RELEASE_DIR" ]; then
  GO="$(find_go || install_toolchain || true)"
  [ -n "$GO" ] || die "no Go toolchain available"
  mkdir -p "$RELEASE_DIR"
  cd "$GO_DIR"
  for target in linux/amd64 linux/arm64 linux/arm/v7 darwin/amd64 darwin/arm64 windows/amd64; do
    os_name="${target%%/*}"; rest="${target#*/}"
    goarch="${rest%%/*}"                 # what GOARCH wants: amd64, arm64, arm
    asset_arch="$goarch"                 # what the installer looks up
    # 32 bit Arm is GOARCH=arm / GOARM=7 here and "armv7" in the asset name
    [ "$goarch" = "arm" ] && asset_arch="armv7"
    [ "$os_name$goarch" = "windowsamd64" ] && ext=".exe" || ext=""
    out="$RELEASE_DIR/simurgh-go-${os_name}-${asset_arch}${ext}"
    say "building $out"
    CGO_ENABLED=0 GOOS="$os_name" GOARCH="$goarch" GOARM=7 GOFLAGS= GOPROXY=off GOTOOLCHAIN=local \
      "$GO" build -trimpath -ldflags "-s -w" -o "$out" ./cmd/simurgh
  done
  ls -1 "$RELEASE_DIR"
  exit 0
fi

# ------------------------------------------------------------------ normal mode
if [ "$FROM_SOURCE" = "0" ]; then
  if fetch_prebuilt; then
    "$OUT" version || true
    exit 0
  fi
  say "no prebuilt binary for this platform; building from source"
fi

GO="$(find_go || true)"
if [ -z "$GO" ]; then
  GO="$(install_toolchain || true)"
fi
[ -n "$GO" ] || die "no Go toolchain available (install golang-go, or run with --engine python)"

say "building with $("$GO" version 2>/dev/null || echo "$GO")"
mkdir -p "$(dirname "$OUT")"
cd "$GO_DIR"
# standard library only: no module downloads, no network, no cgo needed
CGO_ENABLED=0 GOFLAGS=-mod=mod GOPROXY=off GOTOOLCHAIN=local \
  "$GO" build -trimpath -ldflags "-s -w" -o "$OUT" ./cmd/simurgh

chmod 0755 "$OUT"
say "built: $OUT"
"$OUT" version

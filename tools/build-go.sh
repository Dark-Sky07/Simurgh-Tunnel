#!/usr/bin/env bash
# Build the compiled (Go) data plane of Simurgh Tunnel.
#
#   sudo bash tools/build-go.sh              # builds ./bin/simurgh-go
#   SIMURGH_GO_BIN=/usr/local/bin/simurgh-go bash tools/build-go.sh
#
# The engine speaks exactly the same protocol as the Python one, so the relay
# and the exit may each run whichever engine is available; the installer uses
# this script and falls back to Python when a toolchain cannot be obtained.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GO_DIR="$SRC_DIR/go"
OUT="${SIMURGH_GO_BIN:-$SRC_DIR/bin/simurgh-go}"
# the version downloaded when no toolchain is present (any recent one works)
GO_VERSION="${SIMURGH_GO_VERSION:-1.23.4}"

say()  { printf '  %s\n' "$*"; }
die()  { printf 'build-go: %s\n' "$*" >&2; exit 1; }

[ -d "$GO_DIR" ] || die "no go/ directory in $SRC_DIR"

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
  case "$(uname -m)" in
    x86_64|amd64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    armv7l) arch=armv6l ;;
    *) return 1 ;;
  esac
  url="https://go.dev/dl/go${GO_VERSION}.linux-${arch}.tar.gz"
  tmp="$(mktemp -d)"
  say "downloading the Go toolchain from go.dev…"
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

GO="$(find_go || true)"
if [ -z "$GO" ]; then
  GO="$(install_toolchain || true)"
fi
[ -n "$GO" ] || die "no Go toolchain available (install golang-go, or run with --engine python)"

say "building with $("$GO" version 2>/dev/null || echo "$GO")"
mkdir -p "$(dirname "$OUT")"
cd "$GO_DIR"
# stdlib only: no module downloads, no network, no cgo needed
CGO_ENABLED=0 GOFLAGS=-mod=mod GOPROXY=off GOTOOLCHAIN=local \
  "$GO" build -trimpath -ldflags "-s -w" -o "$OUT" ./cmd/simurgh

chmod 0755 "$OUT"
say "built: $OUT"
"$OUT" version

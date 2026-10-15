# third_party — vendored dependencies

Everything here is third-party source that is committed to this repository on
purpose: the data plane must build with **no module downloads and no network**
(`GOFLAGS=-mod=vendor`-free, `GOPROXY=off`), because a tunnel server is often a
machine in Iran where `proxy.golang.org` may be unreachable.

## `xcrypto/` — ChaCha20-Poly1305

* Upstream: `golang.org/x/crypto` (`chacha20poly1305`, `chacha20`,
  `internal/poly1305`, `internal/alias`), tag **v0.58.0**,
  commit `8fdf07b3114278fb3b78c1cf1814e08eb851eb4c`.
* License: BSD-3-Clause — see `xcrypto/LICENSE`.
* Why: the `raw` carrier encrypts with ChaCha20-Poly1305 exactly like the
  Python engine does (`cryptography`'s `ChaCha20Poly1305`), and the Go standard
  library has no ChaCha20-Poly1305. Vendoring keeps one wire format for both
  engines instead of inventing a second one.
* What changed: only the import paths (`golang.org/x/crypto/...` →
  `github.com/Dark-Sky07/Simurgh-Tunnel/go/third_party/xcrypto/...`), so the
  packages build as part of this module. The upstream RFC 8439 test vectors are
  kept, so `go test ./...` proves the cipher still matches the standard.

## `xsys/` — CPU feature detection

* Upstream: `golang.org/x/sys/cpu`, tag **v0.49.0**,
  commit `b6b8557e8e2974925aec7a136ba0e323eedc5f43`.
* License: BSD-3-Clause — see `xsys/LICENSE`.
* Why: `xcrypto/chacha20poly1305/chacha20poly1305_amd64.go` asks this package
  whether the CPU has AVX2/BMI2 before it uses the assembly implementation.
* What changed: import paths only.

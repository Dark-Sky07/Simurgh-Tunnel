# Simurgh vs. the projects it sits next to

**English** · [فارسی](COMPARISON.fa.md)

This page is an honest feature comparison between Simurgh and the tools an
Iranian operator would otherwise use for the same job (a relay in Iran that
carries user traffic to a foreign exit). It lists **what Simurgh does better**,
**what it is missing**, and roughly what each missing piece would cost.

Everything marked *(verified here)* was read in this repository's source or run
in this checkout on 2026-10-16 — not copied from a README.

## The projects compared

| Project | Language | Shape | Transports | Notes for the Iranian use case |
|---|---|---|---|---|
| **Simurgh** (this repo) | Python + Go | relay (Iran) ⇄ exit (foreign), both directions | `tls` (decoy site), `wss`, `raw` (X25519+ChaCha20), `plain` | transparent port forwarding for unchanged customer configs, one mux per tunnel, pool of tunnels, bilingual panel |
| [rathole](https://github.com/rathole-org/rathole) | Rust | server ⇄ client | `tcp`, `tls`, `noise`, `websocket` | per-service tokens, dial through SOCKS5/HTTP proxy, `nodelay`/keepalive tuning, `prefer_ipv6`, nix flake |
| [frp](https://github.com/fatedier/frp) | Go | frps ⇄ frpc | `tcp`, `kcp`, `quic`, `websocket`, `xtcp`, `tcpmux` + vhost HTTP/HTTPS | the kitchen sink: port ranges, bandwidth limit, load-balancing groups, service health checks, port reuse, PROXY protocol, plugins, admin API + dashboard, SSH gateway, VirtualNet |
| [wstunnel](https://github.com/erebe/wstunnel) | Rust | client ⇄ server | WebSocket/TLS, HTTP/2, WebTransport (QUIC) | `tcp`/`udp`/`socks5`/`http`/`tproxy`/`unix`/`stdio` remotes, reverse tunnels, dial through HTTP proxy, connection pooling, SNI override/ECH/mTLS, `SO_MARK` |
| [chisel](https://github.com/jpillora/chisel) | Go | server ⇄ client (SSH over HTTP) | HTTP/WebSocket + TLS | `socks`/`udpsocks` remotes, reverse `R:` remotes, `--backend` camouflage proxy, automatic Let's Encrypt, `authfile` with per-user address regexes |
| [gost](https://github.com/go-gost/gost) | Go | chained proxy / tunnel toolkit | very many protocols | multi-port listen, forwarding chains, transparent proxy, TUN/TAP, load balancing, routing/bypass, admission control, limiters, plugins, **Prometheus metrics**, REST API, GUI/WebUI, DEB/RPM/Docker |
| [Kariz](https://github.com/Erfan-XRay/Kariz) | Rust | entry (Iran) ⇄ exit (Kharej) | `tcp`, `tcpmux`, `ws`, `wss`, `quic`, `kcp` | the closest Iranian competitor: 6 transports, UDP without head-of-line blocking, gaming profile (FEC, packet duplication, DSCP), speed test with latency-under-load + UDP loss, multi-server panel with wizard/undo/charts/backups, CDN-ready, single static binary, manager script with `update`, EN/FA docs + video |
| [FRPulse](https://github.com/Erfan-XRay/FRPulse) | Bash | management front-end for frp | frp's | IPv4/IPv6, TCP/UDP/HTTP/HTTPS, SSL or plain, certbot certificates, cron jobs, per-client logs, Persian-friendly menu |
| Azumi67 templates ([Rathole](https://github.com/Azumi67/Rathole_reverseTunnel), [Chisel](https://github.com/Azumi67/Chisel_multipleServers), [frp](https://github.com/Azumi67/FRP_Reverse_Loadbalance)) | Bash | recipes, not a program | rathole/chisel/frp | "10 Kharej servers ⟷ 1 Iran server" topologies, transport over IPv6 or private IPs, load balancing |
| [bazarghan/stunnel](https://github.com/bazarghan/stunnel) | Bash | minimal reverse TCP tunnel | none of its own | menu-driven, one port per tunnel, no crypto (relies on the service behind it) |

## Where Simurgh is ahead

These are not small things — they are the reason the project exists:

* **Actually looks like a website.** A real TLS handshake, a real certificate, a
  decoy nginx page (or `fallback = "site:host:port"` → the port really serves
  that site), and `split = n` so the first TCP segment on the wire carries only
  a few bytes *(verified: `simurgh/carriers.py`, `decoy.py`, `tlsio.py`, and the
  `split` tests)*. rathole, frp, gost and wstunnel answer probers with a
  handshake error, a reset or a bare "this is a tunnel"
  (chisel's `--backend` is the only comparable camouflage in the list, and it is
  coarser). Kariz has CDN-shaped `ws`/`wss` and sealed QUIC packets, but no
  decoy site.
* **Auth without a fingerprint to recognise.** 25-byte handshake header,
  HMAC-SHA256[:16], ±180 s window, 300 s replay cache *(verified:
  `simurgh/protocol.py`)*; wrong token → decoy page.
* **Window autotuning (bandwidth-delay product).** Per-stream window that grows
  under demand plus a shared credit budget per tunnel connection *(verified:
  `simurgh/mux.py`)* — this is what turns 2 MB/s into 40 MB/s on a 60 ms path
  and it is the single biggest difference against rathole/frp, which use fixed
  windows.
* **Two engines, one wire protocol, offline-friendly installs.** Python 3.9+
  with a bundled TOML reader and no PyPI need, and a compiled Go engine with
  every dependency vendored under `go/third_party/` *(verified:
  `tools/build-go.sh` builds with `GOPROXY=off`)*. On a machine where pip or the
  Go proxy is blocked, this is the difference between "works" and "no install".
* **Verified claims instead of vibes.** 176 tests pass in this checkout
  *(the number this page found was 153; every iteration since carries its own
  proof in the table below)*, plus the Go test suite and a live carrier matrix.
* **Operator ergonomics:** one command that works out the role by itself, a
  setup link / `simurgh join`, a text menu, `simurgh doctor`, a bilingual panel,
  live mapping changes without restart, engine switch without touching configs.

## What is missing (prioritised)

Effort is a rough estimate for someone who knows this codebase:
**S** = hours, **M** = a few days, **L** = a week or more.

### P0 — touches the core job

**Status: all five landed** (see *What has been done* at the end of this file).
The table stays as the record of what was missing and why.

| Gap | Who has it | Why it matters here | Effort |
|---|---|---|---|
| **A UDP-native carrier (QUIC, KCP-style, or "raw over UDP" with ARQ+FEC)** | Kariz (`quic`, `kcp`), frp (`kcp`, `quic`), gost, wstunnel (WebTransport) | Today UDP rides the TCP mux, and `simurgh/udp.py` says it honestly: *"tunnelling UDP over a TCP tunnel adds head-of-line blocking, so latency-sensitive protocols (QUIC, WireGuard) do not love it"*(verified). WireGuard, Hysteria2, TUIC and QUIC-based Xray inbounds are a large share of Iranian traffic. This is the one functional gap a competitor's README can beat Simurgh with. | **L** for QUIC (vendoring a QUIC stack breaks the offline-build promise unless it is vendored like `third_party/xcrypto`), **M** for a hand-rolled UDP carrier with FEC built from the existing `raw` crypto + `protocol.py` framing |
| **Port ranges and bulk mapping** | Kariz (`443,8080-8090`), frp (port ranges) | Every real setup forwards 5–50 ports (`443, 2053, 2083, 2087, 2096, 8443, 1000-1100`). `simurgh mapping add` takes exactly one port per call *(verified: `cli.py`, single-port validation)*. | **S** |
| **Dial the exit through an upstream proxy (SOCKS5 / HTTP)** | rathole (`transport.tcp.proxy`), wstunnel (`-p`), frp (`transport.proxyURL`) | Lets the tunnel leave the box through an existing local proxy/VPN/routing rule, and makes the exit reachable when it sits behind a NAT or a provider that only permits an outbound proxy. Also the building block for "tunnel over tunnel" chains that Iranian operators use a lot. | **S–M** |
| **Real multi-exit: per-mapping exit assignment + active load balancing + target health checks** | frp (proxy groups + `healthCheck`), gost (selectors), Azumi67 templates | `[[pool]]` is **failover only** — `_pick()` returns the first healthy endpoint, so every mapping rides the same foreign server and the others sit idle until it dies *(verified: `simurgh/relay.py`)*. Operators with 3 foreign servers want panel-A on exit-A, panel-B on exit-B, or want the load split, and want a dead target detected instead of a refused stream. | **M** |
| **Per-mapping / per-token bandwidth limit and traffic quota** | frp (`bandwidthLimit`), gost (limiters) | Needed to sell or share a pipe without one customer eating it, and to cap a shared/backup tunnel. Simurgh's credit system gives *fairness between streams*, not a *cap per port* *(verified: `mux.py`, `stats.py` — counters only)*. | **M** |

### P1 — operational maturity

| Gap | Who has it | Why it matters here | Effort |
|---|---|---|---|
| **Prometheus `/metrics`, a token-authenticated API, `--json` output** | gost (metrics + REST API), frp (admin API/dashboard), Kariz (`status --json`) | The panel already exposes `/api/status`, `/api/config`, `/api/mappings`, `/api/action`, `/api/logs`, `/api/join` *(verified: `panel.py` docstring)*, but they are basic-auth only and undocumented as an interface — nothing can scrape or automate them. | **S–M** |
| **Multi-tenant tokens** | frp (`[user]` + `allowPorts`), rathole (per-service tokens), chisel (`authfile` with address regexes) | Simurgh has **one token per tunnel** and one panel password *(verified)*. Two admins, or a reseller who must be limited to `443` and `8443`, cannot be expressed. | **M** |
| **Persistent traffic accounting** | frp dashboard, Kariz charts | Stats are in memory: a 60-second `RateMeter` ring and counters that reset with the service *(verified: `stats.py`)*. After a restart the panel's month-to-date is gone, and there is no per-day/per-month per-mapping history. | **S–M** |
| **`simurgh update` + backup/restore** | Kariz (`kariz-manager update`, panel backups) | Today: re-run `install.sh`, keep your own copy of `/etc/simurgh`. A version check against the GitHub release, config+state snapshot and rollback is cheap and removes a support class. | **S** |
| **Alerts (Telegram/webhook on tunnel down, exit up, quota hit)** | frp (server-manage plugin / HTTP callbacks) | The operator finds out the tunnel is down from a customer. A webhook on the events the code already knows about (`_on_ctrl`, reconnect, errors) is a small change. | **S** |
| **Packaging: Docker/compose, DEB/RPM, nix** | gost (DEB/RPM/Docker), frp (Docker/k8s/brew), rathole (nix), Kariz (single static binary) | `install.sh` + systemd is excellent for a VPS, but container users and package-manager users are simply not served. | **M** |
| **Certificate automation** | chisel (`--tls-domain` → Let's Encrypt), FRPulse (certbot) | Simurgh *uses* an existing Let's Encrypt file fine (`cert_file`/`key_file`) and `certs.py` mentions acme.sh, but nothing obtains or renews a certificate for you *(verified)*. | **M** |
| **Panel hardening** | Kariz (one-time login link) | The panel is plain HTTP with a shared password, has a `?k=<password>` URL form (password ends up in logs/history), and has an IP allow-list but no failed-attempt lockout and no 2FA *(verified: `panel.py`)*. Options: TLS, rate-limit login, one-time links. | **S–M** |
| **Fuller speed test** | Kariz (download/upload, latency under load, jitter, UDP loss) | **Done in 2.7.0.** `simurgh speedtest` measures throughput (one or more streams), the idle *and* under-load round trip with jitter and a bufferbloat grade A–F, and — with `--udp` — datagram loss, RTT and jitter through the tunnel. The exit's localhost-only endpoint answers all three probes and now exists in **both** engines, and the exit announces its port in the connection hello so moving `speedtest_port` does not break the measurement. *(verified: `tests/test_speedtest.py` through a real relay+exit pair, and a live Python client against a Go exit)* | **done** |
| **Preset tuning profiles** | Kariz (`balanced` / `ultraspeed` / `gaming`) | **Done in 2.8.0.** `simurgh tune gaming\|balanced\|speed` writes a whole combination at once — `stream_window`, `max_stream_window`, `chunk`, `connections`, the tunnel ping interval and the socket options (`tcp_nodelay`, `tcp_keepalive`) — reporting only the keys that really differ, so `balanced` is also the undo. `--dry-run` prints the difference, `--json` is for scripts, `--role` picks the file, a bare `simurgh tune` documents the three choices without needing an installation, and the menu has the same page as **[16] Performance profile**. The compiled engine now reads `tcp_nodelay`/`tcp_keepalive` everywhere the Python engine does, including on the connections it accepts, so a mixed pair is tuned by one file. *(verified: `tests/test_tune.py` — 14 tests, incl. gaming < balanced < speed for every key, that `balanced` equals the shipped defaults, that relay-only keys never touch `exit.toml`, and every CLI path)* | **done** |

### P2 — worth having, out of the critical path

| Gap | Who has it | Note | Effort |
|---|---|---|---|
| SOCKS5 / HTTP-proxy *listener* on the Iranian side | chisel, wstunnel, gost | Simurgh v0.1.0 was exactly this and was replaced on purpose; a `socks5` mapping type would bring it back inside the current architecture without weakening the tunnel. | M |
| TUN/TAP or transparent-proxy mode | gost (TUN/TAP), wstunnel (`tproxy+tcp/udp`), Azumi67 (LocalTun) | Different product shape (VPN instead of port forwarding); only build it if users ask for "all traffic", not "these ports". | L |
| HTTP/vhost routing, subdomains, URL routing, web-service basic auth, port reuse | frp | Not in Simurgh's mission. Port reuse / SNI-based demultiplexing is the only piece that could matter (one public port, several customers) | M–L |
| SSH tunnel gateway, stdio/unix remotes | frp (`pkg/ssh`), wstunnel (`stdio:`, `unix:`) | Niche; useful for `ProxyCommand` style access and for admins who live in SSH. | M |
| Plugin / hook system | frp (client + server plugins), gost (plugins) | A full framework is overkill — a webhook/exec hook on events covers the real demand (see the alerts row above). | — |
| Multi-server panel with a tunnel wizard and undo | Kariz | Simurgh's panel manages the node it runs on; Kariz's manages many servers with a live map, backups and a rollback on failure. This is the largest *product* gap, and the most expensive one to close. | L |
| Documentation site + Persian video walkthrough | Kariz (site + video), FRPulse (tutorial) | Simurgh's markdown docs (EN/FA) are good; a site and a 3-minute video are marketing, not engineering — and they convert users. | S–M |
| First-class IPv6 | Kariz, frp, FRPulse, rathole (`prefer_ipv6`) | Bracket form works (`install.sh` accepts `--exit [2001:db8::1]:443`, `util.py` picks `AF_INET6`), but there is no documented IPv6 listening story and no test — while IPv6 is often the cheaper, less filtered path between Iran and a foreign server *(verified: support exists, coverage does not)*. | S |
| Socket tuning knobs | rathole (`nodelay`, `keepalive_secs`, `keepalive_interval`), wstunnel (`SO_MARK`, worker threads) | `TCP_NODELAY` is always on and cannot be turned off *(verified: `bridge.py`, `carriers.py`)*; `SO_MARK`/keepalive tuning is useful for policy routing on multi-homed Iranian boxes. | S |
| Windows/macOS service install | — | Go binaries are already cross-compiled for darwin/windows, but only Linux has an installer. Low value for the two-server use case. | S |

## Deliberately not a gap

* **One tunnel protocol instead of six transports** is a *feature*, not a
  deficiency: fewer code paths is why Simurgh can promise (and test) the same
  behaviour on every carrier and why Go ⇄ Python interop works. The missing
  thing is one **UDP** option, not five new transports.
* **No SOCKS5 in the tunnel** keeps the "your customers change one address"
  promise honest.
* **No plugin framework**: a webhook on events gets 90% of the value for 5% of
  the maintenance.

## Doc drift found while comparing

* `README.md` said the suite was **126 passed** while this checkout runs **153**
  on Python 3.11 — corrected in both READMEs while writing this page (the count
  moves with the interpreter, so the tables now say which one was measured).
* `docs/CONFIGURATION.md` documents `speedtest_port` in the `relay.toml` key
  table, but the sample `relay.toml` block above that table does not show it
  (the `exit.toml` sample does show it) — a one-line symmetry fix.
* `simurgh/certs.py` mentions acme.sh but no document explains how to point
  `cert_file`/`key_file` at an acme.sh or certbot directory — that page is the
  single easiest documentation win (see the P1 certificate row).

## If you only do three things

1. **A UDP-native carrier** (P0) — it is the one place a competitor can
   legitimately claim a better product.
2. **Port ranges + bulk mapping + `simurgh update` + backups** (P0/P1, all
   small) — daily operator pain, hours of work.
3. **`/metrics` + token API + per-mapping limits** (P1/P0) — turns the panel
   from a display into something a business can bill on.

---

## What has been done since this comparison

Work lands on `arena/a9b6a860-simurgh-tunnel` (PR #5).  Only what a test or a
measurement proves is called done.

### P0 — complete

| Item | How it works now | Proof |
|---|---|---|
| **UDP-native carrier** | `carrier = "udp"`: a reliable, ordered byte stream over datagrams with ChaCha20-Poly1305, selective acks, congestion control and optional XOR-FEC (`fec = "8:2"`). Configurable per listener/endpoint (`mtu`, `fec`, `window`). The advertised window is capped by the socket queue the kernel actually granted, sends are reported (`BlockingIOError`) instead of swallowed, loss is detected from SACK gaps, Karn keeps retransmits out of the RTT estimate, FEC parity is assembled from what was *sent*. | 4 MiB in 0.21 s per direction on loopback (≈20 MB/s per stream, **zero** retransmits, **zero** drops); a lossy middlebox at 0/5/10 % loss recovers at 6:1 and 8:2 FEC; e2e tests parametrised over `udp` |
| **Port ranges and bulk mapping** | `ports = ["443", "8080-8090", "9000:9100"]` expands into one mapping per port that keeps the block's name, group and limits; the panel shows one row per port | `tests/test_config.py` (ranges, `range=target` forms, lists) |
| **Upstream SOCKS5 / HTTP dial** | `proxy = "socks5://user:pass@host:port"` (relay-wide, per-exit and per reverse-mode listener) for the outgoing tunnel; `mode = "socks5"` / `"http"` gives the *users* a proxy on the Iranian side | `tests/test_proxyout.py` (a real SOCKS5 and a real HTTP CONNECT server in the test, plus a whole `plain` handshake through the proxy) |
| **Real multi-exit** | `[[pool]]` now dials **every** endpoint (one tunnel each, extras round robin); streams go to the least-loaded tunnel; a mapping can pin itself with `exit = "kharej-2"`; an active health loop pings every tunnel, records RTT and takes an endpoint out of the candidate list after three misses | `tests/test_pool.py` + `tests/test_e2e.py` |
| **Per-mapping bandwidth and quota** | `rate_limit = "20M"` or `{rate = "10M", burst = "1M"}` is one shared token bucket per mapping, applied by *pausing the socket* (never dropping); `quota = "200G"` with `quota_action = block|throttle|warn` is enforced from a persistent ledger (`traffic.json`, day/month buckets, survives restarts) | `tests/test_limits.py` (limit is honoured, raising it in the config speeds the next connection up, an exhausted quota turns the next user away, `throttle` keeps them online, the ledger reloads after a restart) |

### Go engine parity — complete

The compiled engine used to speak four of the five carriers and to ignore the
limits; both are done now.

| Item | How it works now | Proof |
|---|---|---|
| **The `udp` carrier in Go** | `go/internal/carrier/udp.go` is a byte-for-byte reimplementation of `simurgh/udpcarrier.py`: the hello HMAC, ChaCha20-Poly1305 with the packet id as nonce, the SACK/stream core, XOR-FEC and the socket tuning. `carrier = "udp"` works on both sides, including the reverse mode (`[tunnel]` with `carrier = "udp"`). | `go/internal/carrier/udp_test.go`: wire-format vectors checked against the Python constants, `ParseFEC`, a round trip, a 512 KiB stream at **80 MB/s** on loopback, mux over UDP, and FEC repairing a real drop behind a lossy middlebox (`server received 4 parity packets … recovered 2`) |
| **Limits in Go** | `go/internal/limits/` is the Go copy of `ratelimit.py` + `accounting.py`: one byte bucket per mapping drawing from both directions, quotas with `block` / `throttle` / `warn`, `max_conns` slots, and a ledger that **is** `traffic.json` — same version, same key names (`tcp:host:port/target:port`, `token:<name>`), same day/month buckets, so the panel and `simurgh traffic` read one history whichever engine ran. | `go/internal/limits/limits_test.go` (sizes, bucket rate within 60 %, ledger round trip) and `go/internal/node/limits_test.go`: 192 KiB through a `rate_limit = "64k"` mapping takes 5.0 s and every byte arrives; a mapping over `quota = "4k"` refuses the next user (`blocked` counted, ledger written); `quota_action = "throttle"` keeps serving at 128 KiB/s; `max_conns = 1` refuses the second user and accepts again once the first leaves |
| **One fix the limits exposed** | A user who closed before the far side finished left the socket half open forever, holding its `max_conns` slot. The bridge now closes for real once both halves are done. | `tests/test_limits.py::test_max_conns_turns_extra_users_away` (the port accepts again after the first user leaves) |
| **Cross-engine proof** | One engine as the relay, the other as the exit, over `plain` and `udp`, in both roles. | `tests/test_udp_cross_engine.py` (4 tests, a real HTTP answer byte for byte) |

### P1 — the operator surface is landing

| Item | How it works now | Proof |
|---|---|---|
| **`/metrics`** | The panel port serves Prometheus text: node and tunnel state, per-mapping bytes/connections/errors, the persistent ledger (`today`/`month`/`total`), quota limit + used + exhausted, and pool health with RTT. Authentication is the panel's (Basic auth, session cookie or the `?k=` link). It reads the same status the panel shows, so it works with the Python engine *and* with the Go one (from `state.json`). | `tests/test_panel.py` (the exposition is parsed line by line, label values are checked, an unauthenticated scrape is refused, and a Go-engine `state.json` produces the same series); `tests/test_limits.py` (a live relay's own counters: the mapping that just moved 32 KiB shows it, and the quota gauges are there) |
| **`simurgh update`** | Works out the install shape: a git checkout fetches and checks out the newest tag (and rebuilds the engine), a packaged install downloads this machine's asset, verifies its SHA-256 against `SHA256SUMS.txt` and swaps it in atomically (old binary kept as `.bak`), a pip install upgrades through pip. `--check [--json]` reports only (exit code 3 when an update exists), `--force` reinstalls, and with no network nothing is touched. | `tests/test_update.py`: version ordering, asset choice per OS/arch, an unavailable network, "up to date", a real git repository that really moves to a newer tag, and a verified binary swap that keeps the old one as `.bak` — plus a corrupted download that must leave the install alone |
| **Loud numbers in the panel** | `count_bytes()` now feeds the live per-mapping counters as well as the ledger, so the panel's mapping table and `/metrics` agree with `simurgh traffic` instead of only the latter being right. | the same two test files |
| **Alerts (Telegram + webhook)** | Both roles emit `started`/`stopped`/`connected`/`disconnected`/`quota`/`throttle`/`error`, both watch the certificates they actually serve and warn **14 days** before expiry (once a day per file), and the panel sends `login` when it locks an address out. Telegram (`telegram_token` + `telegram_chat`, `telegram_api` overridable for a local Bot API server) and a generic JSON webhook, on a queue drained by a worker thread with a 20-second per-event cooldown; the queue is flushed on shutdown, so a "stopped" message is never lost. `simurgh alert status\|test\|events` shows and edits it. | `tests/test_alerts.py`: 36 tests — the exact URL and JSON body a real alert would send (a recording `urlopen`), the event filter, the cooldown, queue drain on stop, real X.509 certificates for the expiry maths (10-day, 200-day, broken, missing), `_check_certs()` on both nodes fire once a day per file, a live relay + exit + target pair whose alerts really arrive at a local HTTP endpoint, and every `simurgh alert` subcommand |
| **Panel hardening** | Five wrong passwords from one address lock it out for 300 s (`429`) and fire the `login` alert; the counter is per address and is cleared by a success or by the window ending. `/metrics` exposes `simurgh_panel_lockouts_total`, `simurgh_panel_failed_logins` and the alerter's `simurgh_alerts_*` series. Tokens are masked wherever they are printed. | `tests/test_panel.py` (23 tests: the lockout, that a correct password does not help while blocked, that `/healthz` stays open for monitoring, that the window expires, that `/api/join` is guarded too, and the new metric series) and `tests/test_alerts.py` (`simurgh alert status` never prints the token) |
| **Certificate automation** | `simurgh cert issue` drives whichever ACME client is installed (`acme.sh` first — one shell script, installs where pip and snap are filtered — then `certbot`), supports the webroot and standalone flows, staging and a per-listener target, finds the files the client produced, wires them into the right config with `cert_auto` off, and installs the renewal hook (`acme.sh --install-cert --reloadcmd …`, or certbot's `--deploy-hook`). `simurgh cert` lists every certificate in use with days left and fingerprint; `simurgh cert renew` runs the client's own cron. | `tests/test_acme.py`: 24 tests against stand-in clients that are really executed — client detection (including an unreadable `/root/.acme.sh`), the exact argv for webroot/standalone/staging, a failing client reported as a message and not a traceback, a client that claims success without writing files, wiring into the exit, the relay's `[tunnel]` and one `[[listen]]`, a listener that keeps its own certificate, `renew`, and every CLI path (status, issue, renew, `--no-write`, no client installed, no domain) |
| **Packaging (.deb / .rpm / tarball / container)** | `packaging/build-packages.sh` stages the tree and builds a `.deb` (`dpkg-deb`), an `.rpm` (`rpmbuild`, same layout through the spec's `%install`) and a portable tarball that unpacks at `/` — the compiled engine included when `--go-binary` is passed (`all`/`noarch` variants without it). The launcher carries its library path (`$SIMURGH_LIB` overrides), the units are the same two roles, the package creates `/etc/simurgh` with mode `0700` and never writes it (`remove` keeps it, `purge` drops it). `Dockerfile` is a two-stage build that compiles the engine with `GOPROXY=off`, `docker-compose.yml` runs the pair, and CI builds every artefact plus the image on each push. | `tests/test_packaging.py`: 15 tests — a real build, the tarball's contents and modes, the packaged launcher running the CLI, the build-time libdir substitution, `dpkg-deb -I/-c` (control fields, the three maintainer scripts, no `DEBIAN/` in the payload, `amd64` when the engine is inside), the RPM spec against the staging tree, the Dockerfile's `COPY` paths and offline engine build, the compose port map, and the compose recipe really run through `simurgh install` |
| **The Go engine, supervised** | With `engine = "go"` the compiled engine runs as a child process instead of replacing the Python one, so the panel, `/metrics` and the alerts survive next to it (the engine opens no HTTP port of its own). `state.json` is followed for up/down and quota events, and a crashed engine is restarted with a growing backoff (0.5 s → 30 s) with the exit code in the alert. `simurgh relay --exec` keeps the old hand-off behaviour for the tiny VPS. | `tests/test_supervise.py`: 12 tests against a stand-in engine — start/announce, log redirection, up→down→up, quota fired once per period, crash → restart with a new pid and an `error` alert, clean SIGTERM shutdown, the panel facade, deliberate restarts not counting as crashes. Plus a live smoke run: real Go engine, real panel (25 metric series), real traffic through the tunnel, SIGKILL → restart → traffic again |

### The panel (in progress)

The operator surface is complete in the CLI, but the web panel is the one place
Simurgh is still visibly behind Kariz: theirs is a real single-page app with a
live map, per-server pages, charts, a wizard that rolls back and a one-time
login link. **This is the next release's work**, and it is being built to the
same standard the rest of the repo is held to: no CDN, no build step for the
user, one self-contained page served by `simurgh/panel.py`, bilingual (EN/FA,
RTL) like the README, and it must work with **both** engines (the Python panel
already reads `state.json`, so a Go engine reports into it).

| Kariz has | Simurgh will have | State |
|---|---|---|
| live map of servers and tunnels with traffic | a topology view: Iranian relay ⇄ foreign exit(s), animated with the real throughput, per-mapping spurs, quota rings — the same data `/metrics` already exposes | planned |
| per-server pages, charts, logs in the browser | dashboard with rolling throughput charts (from the `traffic.json` ledger, which survives restarts — data Kariz keeps in memory), log tail, and a per-mapping card for limits/quota/state | planned |
| wizard that checks both servers and rolls back | a mapping/role wizard that validates the config, applies it, and restores the previous file if the first traffic probe fails | planned |
| one-time login link | single-use, expiring login tokens next to the current password lockout (`simurgh web --link`) | planned |
| light/dark themes, phone layout | two themes named after the project (night/dawn), responsive down to a phone, and no JavaScript framework to fetch | planned |
| speed test in the panel | the existing `simurgh speedtest` results (throughput, latency under load, UDP loss) rendered as a page, per run | planned |

What stays Simurgh: everything is still one Python process, the panel is
read-mostly and never in the data path, and a server with no panel
(`simurgh relay --exec`) keeps working exactly as it does today.

### What is still open

**P1 is complete.**  Packaging (Docker/DEB/RPM/tarball), the tuning profiles
(`simurgh tune gaming\|balanced\|speed`) and the socket-option keys behind them
have all landed, and the operator surface is complete
on the Python side and works *with* the Go engine (supervised child: panel,
`/metrics`, alerts, restarts).

P2 (SOCKS5 listener, TUN/TAP, vhost reuse, SSH gateway, plugins, multi-server
panel, first-class IPv6, socket knobs, Windows/macOS services).  On the Go side
the remaining parity work is the per-token data-path policy (port/target
allowlists, per-token buckets and quotas) and native proxy-out — the rest of the
config is already read identically by both engines (the per-listener
`cert_file`/`key_file` keys included).

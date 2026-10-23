<div align="center">
  <img src="assets/logo.png" width="184" alt="Simurgh Tunnel logo">

  # Simurgh Tunnel

  **A resilient, transparent path between Iran and your upstream infrastructure.**

  TCP & UDP forwarding · multiplexed tunnels · Python + Go · English/Persian panel

  [![CI](https://github.com/Dark-Sky07/Simurgh-Tunnel/actions/workflows/ci.yml/badge.svg)](https://github.com/Dark-Sky07/Simurgh-Tunnel/actions/workflows/ci.yml) [![Release](https://img.shields.io/github/v/release/Dark-Sky07/Simurgh-Tunnel?display_name=tag)](https://github.com/Dark-Sky07/Simurgh-Tunnel/releases/latest) [![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

  **English** · [فارسی](README.fa.md)<br>
  [Quick start](#quick-start) · [Iran playbook](#iran-operations-playbook) · [Panel](#web-panel) · [Security](#security) · [Documentation](#documentation)
</div>

---

Simurgh moves traffic from user-facing ports on an **Iranian relay** to services on a **foreign exit**, without turning client applications into tunnel-aware clients. Users normally keep their ports, credentials, TLS settings and subscriptions; they only connect to the relay address instead of the foreign address.

```text
users ──► Iranian relay :443 ══ authenticated, multiplexed tunnel ══► foreign exit ──► 127.0.0.1:443
             public address                  TLS / WSS / raw / UDP                 your panel or service
```

> **Operational note:** filtering and routing in Iran vary by ISP, province and time. Simurgh provides several carriers, failover and measurement tools; no setting can promise connectivity during a shutdown or guarantee that a path is undetectable.

## Why Simurgh

| Capability | What you get |
|---|---|
| **Transparent forwarding** | TCP and UDP port mappings, optional SOCKS5/HTTP modes, PROXY protocol support and per-port quotas/rate limits. |
| **Built for imperfect paths** | Credit-based flow control, automatic stream windows, reconnects, a tunnel pool and multiple exit endpoints. |
| **Several wire formats** | TLS, secure WebSocket, raw encrypted transport, UDP with retransmission/FEC, and plain TCP for trusted networks. |
| **Two interoperable engines** | A compiled Go data plane and a portable Python 3.9+ implementation can run on opposite sides of the same tunnel. |
| **Operable, not just installed** | Pairing links, live port management, guided changes with backup/rollback, doctor, speed tests, logs, alerts and Prometheus metrics. |
| **Self-contained** | No database, Node runtime, CDN, external font or panel asset service. Python 3.9/3.10 includes a bundled TOML reader. |

## Architecture

**Direct mode (default):** the relay dials the exit. Open the selected tunnel port on the foreign server.

```text
Iran relay ──dial──► foreign exit :443
Iran relay :8443 ──user traffic──► exit target 127.0.0.1:8443
```

**Reverse mode (`dial = "exit"`):** the exit dials the relay. Use it when the foreign server cannot accept inbound connections or you prefer to expose the tunnel listener in Iran. User-facing mappings remain on the relay in both modes.

## Quick start

### Requirements

- A Linux server for each role; root/sudo for the one-line installer.
- Python **3.9+**. `systemd` is used when available; manual and container runs are supported too.
- One reachable tunnel port and, for production TLS, either a pinned certificate fingerprint or a CA-signed certificate.

### 1 — Install every server with the same command

Run **this exact command on both the foreign server and the Iranian server**. Do not add a role-specific option:

```bash
curl -fsSL https://raw.githubusercontent.com/Dark-Sky07/Simurgh-Tunnel/main/install.sh | sudo bash
```

The installer detects an existing installation and otherwise uses the public IP's country to select the role; if that is unavailable, it asks on the terminal. A direct relay is deliberately installed **unpaired**, so its panel can start before the exit address or token exists. Re-running the installer refreshes its source checkout rather than mixing a new script with an old cached CLI.

### 2 — Open both web panels

Open `http://<server-ip>:8787` on each server (or the panel port printed by the installer). The installer prints the login credentials. Keep the panels behind a firewall or SSH tunnel as described in [Web panel](#web-panel).

### 3 — Pair and forward from the panel (direct mode)

1. On the **foreign exit**, open **Pair → Create a setup link**. Enter its public panel IP/domain and port if the browser address is not reachable from the relay, then create and copy the link.
2. On the **Iranian relay**, open **Pair**, paste the link and press **Pair**. The relay securely fetches the token, exit address, listener pool, certificate fingerprint and advertised ports. Existing panel settings and mappings are preserved.
3. Open **Ports** on the relay to add or remove user-facing forwards. Changes to mappings are applied live.
4. Use **Settings** for the engine, tunnel direction, reverse listener, flow control, quotas and rate limits. Use **Wizard** for carrier or public tunnel endpoint changes. Use **Tools → Restart** when a saved change says it needs a restart.
5. Confirm the result on **Dashboard**. The tunnel is connected when its state is up and RTT/throughput are reported.

The setup link carries panel credentials and the fetched payload contains the tunnel token. Treat both as secrets and send the link only over a private channel.

For the complete firewall, Direct/Reverse and independent end-to-end testing walkthrough, follow **[Connect the servers and test the tunnel](docs/CONNECTING.md)**.

### Reverse mode (optional, also panel-only)

Use reverse mode when the foreign exit should dial the Iranian relay:

1. On the relay, open **Settings**, set **Who dials the tunnel** to `exit`, choose the reverse listener carrier/port, save, and restart from **Tools**.
2. On the relay's **Pair** page, create a setup link using the relay's publicly reachable panel address.
3. On the foreign exit, open **Pair → Connect to a reverse relay**, paste the link and save. Restart the exit from **Tools** when prompted.

Open the selected tunnel listener port on the relay instead of the exit. User-facing mappings remain on the relay in both modes.

### Packages and containers

Release assets include `.deb`, `.rpm`, portable `.tar.gz` packages and compiled engines. A `Dockerfile` and `docker-compose.yml` are included as well.

```bash
sudo dpkg -i simurgh_3.2.8_amd64.deb
sudo dnf install ./simurgh-3.2.8-1.x86_64.rpm
sudo tar -xzf simurgh-3.2.8-noarch.tar.gz -C /
docker compose up -d                   # optional two-node development lab, not server installation
```

See **[Packages & containers](docs/PACKAGING.md)** for offline installation, package contents and upgrades.

## Iran operations playbook

Start conservatively, measure on the actual relay → exit route, and change one variable at a time.

| Condition | Recommended approach |
|---|---|
| **General production start** | `tls` on TCP 443 with certificate pinning, `split = 3`, one tunnel connection. Confirm the baseline with `status`, `doctor` and `speedtest`. |
| **TLS handshake resets or first-packet interference** | Test `split = 0`, `1` and `3` separately. Splitting is a compatibility heuristic, not a guarantee; `wss` is the next controlled experiment. |
| **A route favours WebSocket/HTTPS-like flows** | Try `wss` with a normal path such as `/ws`, while retaining the same token and certificate verification. |
| **Long RTT or a single TCP flow is limiting throughput** | Try `connections = 2`, then `4`, and compare loaded latency as well as throughput. More connections are not automatically better. |
| **Datagram loss on the tunnel path** | Test `udp` only after confirming that UDP passes on that route; use `speedtest --udp` and consider the documented FEC settings. Keep a TCP carrier as fallback. |
| **Foreign inbound is unavailable** | Use reverse mode and expose the tunnel listener on the relay instead. |
| **An exit or route becomes unreliable** | Configure multiple exit endpoints/listeners and inspect pool health in the panel. Do not force UDP or aggressive splitting everywhere. |

`raw` requires the `cryptography` extra on the Python engine; the installer attempts to provide it. `plain` has no TLS outer layer and belongs on trusted/internal paths, not as the default public Internet carrier.

More detail: **[Configuration](docs/CONFIGURATION.md)** and **[Troubleshooting](docs/TROUBLESHOOTING.md)**.

## Everyday operations

Configuration is panel-first on both roles:

| Task | Panel location |
|---|---|
| Create or import a setup link | **Pair** |
| Add, remove, enable or disable forwards | **Ports** |
| Preview, apply or roll back a configuration plan | **Wizard** |
| Change engine, tunnel direction, reverse listener, performance, quotas or rate limits | **Settings** |
| Run speed tests, request/inspect certificates and logs | **Speed test / Certificates / Logs** |
| Restart, start, stop or reconnect; check and install updates; export config | **Tools** |

The CLI remains useful for read-only diagnostics and automation:

```bash
simurgh status --detail            # service, pairing, mappings and tunnel state
simurgh doctor                     # configuration and connectivity checks
simurgh logs -f                    # follow the active role's log
simurgh update --check             # check without changing the installation
simurgh update --restart           # install, restart, then verify /healthz
```

For a fully interactive update, use **Tools → Install update** in the panel or option **17** in `simurgh menu`. Updates reinstall the Python package and the checksummed Go engine without rewriting pairing, mappings, credentials or other configuration. If GitHub's API is rate-limited, release discovery falls back to the validated `/releases/latest` redirect without bypassing checksum verification. Re-running the universal installer likewise preserves an existing config; only an explicit `--force` replaces it, with a backup. It also verifies the installed panel assets, not just the source checkout or `/healthz`.

Run `simurgh <command> --help` for command-specific options.

## Web panel

Open `http://<server-ip>:8787` on either role (or the configured panel port). The panel is bilingual **English / فارسی**, supports RTL/LTR and night/dawn themes, and serves every asset locally under a strict Content Security Policy.

- **Dashboard:** live tunnel map, throughput chart, reconnects, RTT and exit-pool health.
- **Pair:** create or import setup links on either role, including reverse mode, without reinstalling or using the CLI.
- **Ports:** add, remove, enable and disable mappings without restarting the live node.
- **Wizard:** preview configuration changes, apply them with a backup and roll back.
- **Operations:** speed tests, multi-domain ACME certificate issuance and status, filterable logs, service tools and one-click updates with a post-restart health check.
- **Security:** failed-login state and single-use login links; five failed attempts from one address trigger a 300-second lockout.
- **Monitoring:** authenticated `/metrics` and unauthenticated liveness-only `/healthz`.

Prefer the login form or a **single-use, 10-minute link** from **Security** / `simurgh web --link`. Legacy `?k=<password>` links now redirect to a clean URL after setting the session cookie.

> The built-in panel uses HTTP. On a public server, restrict its source addresses with a firewall or use an SSH tunnel: `ssh -L 8787:127.0.0.1:8787 root@server`, then browse to `http://127.0.0.1:8787`.

Full reference: **[Panel guide](docs/PANEL.md)**.

## Engines and performance

| Engine | Best use |
|---|---|
| **Go** | High-throughput/multi-core data plane; preferred when a matching compiled binary is available. |
| **Python** | Portable fallback and protocol reference; no third-party dependency for TLS/WSS/plain operation. |

Both engines read the same configuration and interoperate. Switch in **Settings → Engine**, then restart from **Tools**. The panel rejects Go if its binary is not installed. Actual throughput depends on CPU, RTT, loss, carrier, flow count and upstream service—not just the engine.

Reproduce measurements on your own path instead of treating loopback benchmarks as Internet results:

```bash
python tools/live_smoke.py
python tools/latency_bench.py 60 40 tls --connections 4
simurgh speedtest --streams 4 --udp
```

## Security

- Versioned, timestamped **HMAC-SHA-256** tunnel hello with replay protection.
- **SHA-256 certificate pinning** for self-signed TLS; CA verification also supported. Avoid `insecure_skip_verify` in production.
- AEAD protection for raw/UDP carriers; WebSocket SHA-1 appears only where RFC 6455 requires it and is not used as a password hash.
- Atomic configuration writes with mode `0600`; tokens are masked in panel/config views.
- Panel authentication, per-address lockouts, `HttpOnly`/`SameSite` cookies, single-use login tokens, strict CSP and no third-party assets.
- Binary updates are installed **only when their SHA-256 checksum is present and matches**; the previous binary is retained as `.bak`.

A tunnel is not a replacement for securing the destination panel. Keep services patched, restrict management interfaces and protect setup links like passwords.

## Verification and development

```bash
git clone https://github.com/Dark-Sky07/Simurgh-Tunnel.git
cd Simurgh-Tunnel
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[secure]' pytest ruff

python -m pytest -q
ruff check --select F,E9 simurgh tests
python tools/live_smoke.py

# Go data plane (Go 1.27+; third-party source is vendored)
cd go
GOPROXY=off go test ./...
GOPROXY=off go test -race ./...
```

The suite covers protocol/mux behaviour, all carriers, direct and reverse pairing, reconnects, UDP, limits/quotas, installer regressions, configuration, panel APIs and JavaScript, packaging, and Python ↔ Go interoperability when a Go toolchain is available. Some packaging and cross-engine tests skip cleanly when their external tool is absent.

## Documentation

| Guide | Contents |
|---|---|
| [Connecting & testing](docs/CONNECTING.md) | Direct/Reverse pairing, firewalls, disposable end-to-end response, speed and UDP tests |
| [Configuration](docs/CONFIGURATION.md) | Every setting, carriers, mappings, pools, performance profiles and systemd |
| [Panel](docs/PANEL.md) | Views, authentication, API, security and monitoring |
| [Packages](docs/PACKAGING.md) | deb/rpm/tarball, Docker, offline installation and upgrades |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Pairing, certificates, filtering, ports, performance and logs |
| [Comparison](docs/COMPARISON.md) | Technical comparison with related approaches |

Persian editions are linked at the top of each guide.

## License

MIT — see [LICENSE](LICENSE). Bundled third-party licences are retained beside their source, including `simurgh/_tomli/` and `go/third_party/`.

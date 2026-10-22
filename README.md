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

### 1 — Install the foreign exit

Explicit roles avoid relying on geolocation when a server's IP database is inaccurate:

```bash
curl -fsSL https://raw.githubusercontent.com/Dark-Sky07/Simurgh-Tunnel/main/install.sh \
  | sudo bash /dev/stdin --role exit --name de-frankfurt
```

Keep the generated token and setup link private. To use a specific tunnel listener:

```bash
curl -fsSL https://raw.githubusercontent.com/Dark-Sky07/Simurgh-Tunnel/main/install.sh \
  | sudo bash /dev/stdin --role exit --listen 443,2053 --name de-frankfurt
```

### 2 — Install the Iranian relay

The relay can be installed before pairing. Its panel starts in **not paired** state instead of failing because no exit has been supplied yet.

```bash
curl -fsSL https://raw.githubusercontent.com/Dark-Sky07/Simurgh-Tunnel/main/install.sh \
  | sudo bash /dev/stdin --role relay --name ir-tehran
```

Omit `--role` to let the installer ask or auto-detect. Re-running the installer refreshes an existing source checkout before invoking the CLI, so an old cached CLI is not mixed with a new installer.

### 3 — Pair, forward and verify

```bash
# On the exit: print only the setup link
sudo simurgh link --show

# On the relay: paste the complete link between quotes
sudo simurgh join 'simurgh://<exit-ip>:8787/setup?u=admin&p=…#de-frankfurt'

# Add another mapping when needed (join imports ports advertised by the exit)
sudo simurgh mapping add 8443 8443 --name my-service
sudo simurgh restart

sudo simurgh status --detail
sudo simurgh doctor
```

You can also paste the setup link into **Panel → Pair**. Pairing preserves the relay's existing panel settings, engine choice and mappings.

<details>
<summary><strong>Direct install without a setup link</strong></summary>

If you already manage the token and certificate verification yourself, install in one step:

```bash
curl -fsSL https://raw.githubusercontent.com/Dark-Sky07/Simurgh-Tunnel/main/install.sh \
  | sudo bash /dev/stdin --role relay \
      --exit 203.0.113.9:443 --token 'SHARED-TOKEN' --fingerprint 'SHA256-FINGERPRINT'
```

Run `simurgh cert` on the exit to inspect its fingerprint. The default exit certificate is self-signed: **a token and address alone do not authenticate it**. Use the setup-link flow, a fingerprint, or a properly configured CA-signed certificate. `--insecure` is for temporary testing only.

</details>

### Packages and containers

Release assets include `.deb`, `.rpm`, portable `.tar.gz` packages and compiled engines. A `Dockerfile` and `docker-compose.yml` are included as well.

```bash
sudo dpkg -i simurgh_3.1.1_amd64.deb
sudo dnf install ./simurgh-3.1.1-1.x86_64.rpm
sudo tar -xzf simurgh-3.1.1-noarch.tar.gz -C /
docker compose up -d
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

## Everyday commands

```bash
simurgh menu                       # interactive operator menu
simurgh status --detail            # service, pairing, mappings and tunnel state
simurgh doctor                     # configuration and connectivity checks
simurgh mapping list               # list user-facing forwards
simurgh mapping add 443 443        # relay port -> exit target port
simurgh speedtest --udp            # throughput, loaded latency and UDP loss
simurgh logs -f                    # follow the active role's log
simurgh tune balanced              # restore the baseline performance profile
simurgh update --check             # check without changing the installation
simurgh link --show                # setup link for the other server
simurgh start | stop | restart     # service control
```

Run `simurgh <command> --help` for command-specific options.

## Web panel

Open `http://<relay-ip>:8787` (or the configured panel port). The panel is bilingual **English / فارسی**, supports RTL/LTR and night/dawn themes, and serves every asset locally under a strict Content Security Policy.

- **Dashboard:** live tunnel map, throughput chart, reconnects, RTT and exit-pool health.
- **Pair:** finish an unpaired relay without reinstalling.
- **Ports:** add, remove, enable and disable mappings without restarting the live node.
- **Wizard:** preview configuration changes, apply them with a backup and roll back.
- **Operations:** speed tests, certificates, filterable logs, service tools and update checks.
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

Both engines read the same configuration and interoperate. Switch with `simurgh engine go` or `simurgh engine python`, then restart. Actual throughput depends on CPU, RTT, loss, carrier, flow count and upstream service—not just the engine.

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
| [Configuration](docs/CONFIGURATION.md) | Every setting, carriers, mappings, pools, performance profiles and systemd |
| [Panel](docs/PANEL.md) | Views, authentication, API, security and monitoring |
| [Packages](docs/PACKAGING.md) | deb/rpm/tarball, Docker, offline installation and upgrades |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Pairing, certificates, filtering, ports, performance and logs |
| [Comparison](docs/COMPARISON.md) | Technical comparison with related approaches |

Persian editions are linked at the top of each guide.

## License

MIT — see [LICENSE](LICENSE). Bundled third-party licences are retained beside their source, including `simurgh/_tomli/` and `go/third_party/`.

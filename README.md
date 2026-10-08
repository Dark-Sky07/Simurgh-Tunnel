# 🕊️ Simurgh Tunnel

**English** · [فارسی](README.fa.md) &nbsp;|&nbsp; [Install](#install) · [Configuration](docs/CONFIGURATION.md) · [Troubleshooting](docs/TROUBLESHOOTING.md) · [Security](#security)

A transparent, multiplexed **TCP/UDP port-forwarding tunnel** between one or more
Iranian servers (relay) and one or more foreign servers (exit) — the same class
of tool as Karez / Rathole / frpulse, built for the Iranian network.

Your customers keep **the exact same configuration** they have today (3x-ui,
OMEGA, X-UI, Xray, WireGuard over TCP, anything): they only change the server
address to your **Iranian** IP. Traffic crosses one disguised tunnel to the real
foreign server, which serves it exactly as before.

```
customer ──same config, Iran IP──► [Iran relay] ══one disguised tunnel══► [foreign exit] ──► 127.0.0.1:panel
```

## Why it fits the Iranian network

| Requirement | How Simurgh handles it |
|---|---|
| The foreign IP/domain must not be filtered | Customers only ever use the **Iranian** IP; the foreign address never appears in any config, QR code or subscription. |
| DPI must not detect the tunnel | A real TLS connection to a real domain: valid certificate, SNI, and a **decoy website** (nginx page) for anything without the token. |
| Speed and stability | One multiplexed connection (`mux`) serves every user and every port; no re-encryption of user traffic, credit-based flow control instead of buffering. The per-stream window **grows itself** on long paths (bandwidth-delay product), and `connections = 2..16` keeps a pool of tunnels so one TCP flow — or one dropped tunnel — is never the ceiling for everyone. |
| Easy install | One command per server, one link to move the config across. |
| Minimal resources | Pure Python 3 + asyncio, no Node/Go/database. Idle memory ≈ 25–40 MB per role. |
| Text menu + web panel | Full-featured **English text menu** (`simurgh menu`) and a **bilingual (EN/FA) web panel** with live graphs. |

## Two directions

* **Direct (default)** — the Iranian relay dials the foreign exit. The exit needs
  one open inbound port (e.g. TCP 443).
* **Reverse** (`dial = "exit"`) — the **foreign exit dials the Iranian relay**.
  Use this when the foreign server has no reachable inbound port (NAT, blocked
  ports, changing IP) or when you do not want to expose one. User-facing ports
  stay on the Iranian side in both modes.

## Install

### Foreign server (exit)

```bash
curl -fsSL https://raw.githubusercontent.com/Dark-Sky07/Simurgh-Tunnel/main/install.sh \
  | sudo bash /dev/stdin --role exit --name omega-de --listen 443,2053,2083
```

The installer creates the venv, writes `/etc/simurgh/exit.toml`, generates a
self-signed certificate, installs the `simurgh-exit` systemd service and prints
a **relay setup link**.

### Iranian server (relay), direct mode

```bash
sudo bash install.sh --role relay --exit 203.0.113.9:443        # or the link:
sudo simurgh join 'simurgh://203.0.113.9:8787/setup?u=admin&p=…#omega-de'
sudo simurgh mapping add 443 443 --name panel
sudo simurgh restart
```

### Iranian server (relay), reverse mode

```bash
sudo bash install.sh --role relay --dial exit --tunnel-port 8443 --token XXX
# then, on the foreign server, import the link that `simurgh link` printed:
sudo simurgh join 'simurgh://<iran-ip>:8787/setup?u=admin&p=…#ir-tehran'
sudo simurgh restart
```

### Users

Change only the address in their config: from the foreign IP/domain to the
**Iranian IP**. Ports, credentials, TLS, SNI, everything else stays identical.

## Everyday commands

```bash
simurgh menu                     # English text menu (recommended first run)
simurgh status                   # tunnel, traffic, RTT, ports
simurgh mapping add 443 443      # Iranian port -> foreign port
simurgh mapping list | remove | toggle
simurgh speedtest                # real speed through the tunnel
simurgh logs -f                  # live log
simurgh doctor                   # self-diagnosis with hints
simurgh link --show              # setup link for the *other* server
simurgh join '<link>'            # build the config from a link
simurgh start | stop | restart   # systemd control
simurgh uninstall --purge        # remove everything
```

Full reference: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Web panel (bilingual)

`http://<iran-ip>:8787` — English **and** Persian (`EN / فا` switch, choice
remembered per browser, RTL/LTR automatic). Live tunnel state, throughput
graphs, add/remove/toggle port mappings **without restarting**, speed test, log
tail, service restart, and the setup link. One-click login:
`http://<iran-ip>:8787/?k=<panel-password>`. Everything is served locally — no
CDN, no external fonts.

> Everything a server operator sees outside the panel — text menu, CLI,
> installer output — is **English only**.

## Two engines, one protocol

Simurgh ships two implementations of the same wire protocol, and each side of a
tunnel may run whichever one it likes:

| Engine | What it is | When to use it |
|---|---|---|
| `python` *(default)* | the reference engine, pure standard library | everywhere; nothing to build |
| `go` | compiled data plane: one goroutine per user connection, all cores, ~8× less CPU per gigabyte | busy relays, many simultaneous users |

```bash
sudo bash tools/build-go.sh     # needs a Go toolchain (the script installs one)
simurgh engine go               # write engine = "go" into relay.toml / exit.toml
simurgh restart
```

Nothing else changes: the panel, the text menu and the CLI stay Python, they
read the same config files, and the live numbers in the panel come from the
`state.json` both engines keep. Watch the speedup while you switch:

```bash
python tools/latency_bench.py 60 80 plain            # python engine
go/bin/simurgh-go bench -mb 256 -delay 20 -streams 64  # go engine, 64 users
```

## Carriers (tunnel disguises)

| Carrier | Looks like | Use when |
|---|---|---|
| `tls` *(default)* | a real HTTPS site with a decoy page | almost always |
| `wss` | the same over WebSocket upgrade | only WebSocket is allowed |
| `raw` | random bytes + X25519 + ChaCha20-Poly1305 | TLS itself is suspicious |
| `plain` | TCP + HMAC header | two trusted/internal servers |

## Security

* 25-byte handshake header (version, timestamp, HMAC-SHA256[:16]) with a ±180 s
  window, keyed by `sha256("simurgh/v2/token|" + token)`; a 300 s replay cache
  blocks replayed handshakes.
* Without the token the listener answers like a normal web server (decoy page);
  probes learn nothing.
* The exit's self-signed certificate can be **pinned by SHA-256 fingerprint**
  (`fingerprint`), or a real Let's Encrypt certificate can be used.
* Config files are written atomically with mode `0600`.

## Verified in this repository

| Check | Result |
|---|---|
| `python -m pytest -q` | **118 passed** (protocol, mux, config, panel, links, e2e for all carriers, reverse mode, tunnel pool, shared window, window autotuning) |
| `go test ./...` (in `go/`) | **passing** — mux (round trip, credit return, refused open, shared window) and carrier/decoy (plain, TLS, wrong token, `site:` proxying, custom page) |
| `python tools/live_smoke.py` | **ALL GREEN 40/40** — 4 carriers × (small, 100 KB, 8 concurrent users) |
| User path | the foreign server's page served byte-for-byte through the Iranian port (200 MB piped) |
| Single stream on a 60 ms path | 2 MB/s with a fixed 256 KiB window → **40.4 MB/s** with the automatic window (Python engine, 80 MB, same 2 vCPU container) |
| Single stream on a 4 ms path | **233 MB/s (1.9 Gbit/s)** with the Python engine — the CPU limit of one core |
| Go engine, same 60 ms path | **42.3 MB/s (338 Mbit/s)** plain, **30.5 MB/s (244 Mbit/s)** over `tls` (30.4 over `wss`) |
| Go engine, short path | **383 MB/s (3.07 Gbit/s)** single stream |
| Go engine, many users | **430 MB/s (3.4 Gbit/s) for 64 simultaneous users on one tunnel connection**, 503 MB/s (4.0 Gbit/s) over two — 2 vCPUs |
| Go engine, CPU | **0.6-0.8 CPU-second per 256 MB** moved, relay + exit + test client in the same 2 vCPU process |
| Engine interop | Go relay ⇄ Python exit and Python relay ⇄ Go exit, both verified |
| Pool | 8 user connections over 4 tunnels: 13.0 MB/s vs 3.6 MB/s on one tunnel with the same per-flow cap, load spread 1/1/1/1 |
| Memory | 33-41 MB peak RSS for relay + exit + test target on a single fat stream; 95 MB moving 200 MB over 4 tunnels |
| Decoy | plain HTTP or TLS + HTTP → real nginx page (200), unknown path → 404, HTTP/2 → `GOAWAY(HTTP_1_1_REQUIRED)` like a normal site, `fallback = "site:host:port"` → the port really serves that site, wrong token → 400/binary-noise or a silent close |
| Self-heal | killing the exit: the relay reconnects when it returns |
| Reverse mode | traffic over tls/raw/plain, exit reconnects after a relay restart |
| UDP forwarding | 4/4 replies |

## Documentation

* [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — every key of `exit.toml` / `relay.toml`, carriers, mappings, pool, systemd units
* [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — symptoms, causes, fixes
* [tools/latency_bench.py](tools/latency_bench.py) — reproduce every throughput number above on your own machine (`python tools/latency_bench.py 60 40 tls --connections 4`)
* [README.fa.md](README.fa.md) — the Persian version of this page (توضیح فارسی)

## Development

```bash
git clone https://github.com/Dark-Sky07/Simurgh-Tunnel.git
cd Simurgh-Tunnel
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[secure]'
python -m pytest -q                 # 100 tests, no test-only dependencies
python tools/live_smoke.py          # live matrix on loopback (40 checks)
```

Layout: `simurgh/protocol.py` (frames, address codec), `mux.py` (multiplexer +
flow control), `carriers.py` (4 carriers) + `tlsio.py`/`obfs.py`/`decoy.py`,
`bridge.py` (socket ↔ stream), `exit.py` / `relay.py` (the two roles), `udp.py`,
`speedtest.py`, `panel.py` (web UI), `menu.py`/`cli.py`/`systemd.py`,
`config.py`, `links.py` (setup links), `certs.py`.

## License

MIT — see [LICENSE](LICENSE).

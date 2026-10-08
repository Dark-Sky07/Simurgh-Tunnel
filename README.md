# 🕊️ Simurgh Tunnel

[![version](https://img.shields.io/badge/version-2.0.0-2f6feb)](https://github.com/Dark-Sky07/Simurgh-Tunnel/releases)
[![python](https://img.shields.io/badge/python-3.9%2B-3776ab)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![platform](https://img.shields.io/badge/platform-linux%20%7C%20systemd-555)]()

**English** · **[فارسی](README.fa.md)**

A transparent, DPI-resistant tunnel between one or more Iranian servers and one
or more foreign servers. Customers keep the *exact* configuration they already
have — same protocol, same credentials, same port — and only change the server
address to your Iranian IP. Everything else is forwarded, byte for byte, to the
real foreign server.

Built for the Iranian network: carrier-grade filtering, blocked IPs and
domains, high latency, small VPSs, and an installation that has to be finished
in two minutes.

---

## Contents

- [Why Simurgh](#why-simurgh)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [First steps after installing](#first-steps-after-installing)
- [Web panel](#web-panel)
- [CLI reference](#cli-reference)
- [Configuration reference](#configuration-reference)
- [Carriers](#carriers)
- [Security](#security)
- [Performance](#performance)
- [Troubleshooting](#troubleshooting)
- [Upgrade & uninstall](#upgrade--uninstall)
- [Development](#development)
- [License](#license)

---

## Why Simurgh

| What you need | What Simurgh does |
|---|---|
| The foreign IP/domain must not be filtered | Customers only ever see the **Iranian** IP. The foreign address never appears in a user config, a QR code or a subscription. |
| Iran's DPI must not notice it | The tunnel is one real TLS connection: valid certificate, real SNI, and a **decoy website**. A prober without the token sees an nginx page. |
| Speed and quality must always be good | One multiplexed TCP connection per server pair, end-to-end credit-based flow control, no re-encryption of VPN traffic, no extra copies in the hot path. |
| Easy to install and use | One command per server, then paste one link. |
| A text menu **and** a web panel | English text menu/CLI on the server, bilingual (EN/FA) web panel with live charts. |
| Minimal CPU and RAM | Pure Python + asyncio, no database, no Node, no Go. Measured **~25–35 MB RSS**. |
| Different from (and better than) existing tools | One tunnel carries every port and every protocol, TCP *and* UDP, with automatic failover, live reconfiguration and no per-connection NAT state explosion. |

## How it works

```
customer (v2ray/xray/…)              same config, only the address changes
        │
        ▼
┌───────────────────────────────┐
│  Iranian server — relay       │  user ports + web panel + text menu
│  :443  :2053  :2083  …        │
└──────────────┬────────────────┘
               │  one disguised, multiplexed tunnel (TLS + auth header + decoy)
               ▼
┌───────────────────────────────┐
│  Foreign server — exit        │  forwards traffic to its own local panel
│  127.0.0.1:<panel port>       │
└───────────────────────────────┘
```

Every port on the relay maps to a port on the foreign server
(`simurgh mapping add 443 443`). Several foreign servers are supported: fill the
`[[pool]]` section of the relay config and the relay switches to the next server
automatically when the current one stops answering.

### Two directions

| Mode | Who connects | When to use it |
|---|---|---|
| **Direct** (default) | relay (Iran) → exit (foreign) | Normal case: the foreign server has an inbound port. |
| **Reverse** (`dial = "exit"`) | exit (foreign) → relay (Iran) | The foreign server is behind NAT, has a changing IP, or must not expose any port at all. |

In reverse mode the **Iranian relay listens and the foreign exit connects to it**.
The user-facing ports stay on the Iranian server, so nothing changes for
customers, and the exit reconnects by itself after a restart, a reboot or a
network outage.

## Requirements

* Two Linux servers (any systemd distribution; tested on Debian 12 / Ubuntu).
* Python **3.9+** (the installer creates its own virtualenv).
* Root on both servers (systemd units and privileged ports).
* The tunnel port open in the firewall on the listening side
  (443 by default; the Iranian side in reverse mode).
* No dependency on the user's panel software — x-ui, 3x-ui, OMEGA, plain xray
  or any TCP service works, as long as it listens on the foreign server.

## Installation

### 1. Foreign server (exit)

```bash
curl -fsSL https://raw.githubusercontent.com/Dark-Sky07/Simurgh-Tunnel/v2.0.0/install.sh \
  | sudo bash -s -- --ref v2.0.0 --role exit --name omega-de --listen 443,2053,2083
```

(or run `sudo bash install.sh --role exit --name omega-de` from a checkout.
`--ref` is only needed while installing from the released tag; once you track
`main`, drop it.)

The installer prepares Python, generates a self-signed certificate, writes
`/etc/simurgh/exit.toml`, installs the `simurgh-exit` systemd unit and prints
the **setup link**:

```
✔ installation complete.
  relay setup link: simurgh://203.0.113.9:8787/setup?u=admin&p=…#omega-de
```

Keep that link private: it contains the shared token.

### 2. Iranian server (relay)

Direct mode — the relay dials the exit:

```bash
sudo simurgh join 'simurgh://203.0.113.9:8787/setup?u=admin&p=…#omega-de'
sudo simurgh mapping add 443 443 --name panel
sudo simurgh start
```

Reverse mode — the relay waits for the exit instead:

```bash
sudo bash install.sh --role relay --dial exit --tunnel-port 8443 \
     --name ir-tehran --token <the-token-from-the-exit>
sudo simurgh link --show          # prints the link for the *exit* server
```

then, on the foreign server:

```bash
sudo simurgh join 'simurgh://IRAN-IP:8787/setup?u=admin&p=…#ir-tehran'
sudo simurgh restart
```

Either way the result is the same: users connect to the Iranian IP.

### 3. Customers

Change the server address in the existing config from the foreign IP/domain to
the **Iranian** IP. Everything else — UUID, port, SNI, path, transport — stays
as it is.

## First steps after installing

```bash
simurgh menu                       # English interactive menu (recommended first)
simurgh status                     # tunnel state, traffic, RTT, mappings
simurgh mapping add 2053 2053      # Iranian port 2053 → foreign port 2053
simurgh mapping list
simurgh speedtest                  # real throughput through the tunnel
simurgh logs -f                    # live log
simurgh doctor                     # self-check with hints
```

The panel prints its credentials on first start and stores them in
`/etc/simurgh/state.json` (`simurgh status` shows them too).

## Web panel

`http://<IRAN-IP>:8787` — bilingual **English / فارسی**, with a language switch
in the header. The choice is remembered in the browser and the layout flips
between LTR and RTL automatically; the first visit follows the browser
language.

* live tunnel state, RTT and reconnection counter
* download/upload throughput chart
* add, remove, enable and disable port mappings **without a restart**
* speed test, service restart/reconnect, log viewer
* the setup link for a new server, ready to copy
* one-click login: `http://<IRAN-IP>:8787/?k=<panel-password>`

Everything is served from the server itself: no CDN, no external font, no
telemetry.

> The server-side text menu, the CLI and the installer are **English only**;
> only the web panel carries both languages.

## CLI reference

| Command | What it does |
|---|---|
| `simurgh install` | config + systemd units, one shot (`--role exit\|relay\|both`, `--dial relay\|exit`, `--listen 443,2053`, `--exit HOST:PORT`, `--token`, `--panel-port`, `--no-start`, `--config-only`) |
| `simurgh init` | write the config files only |
| `simurgh exit` / `simurgh relay` | run a node in the foreground |
| `simurgh up \| start \| stop \| down \| restart \| enable \| disable \| status` | systemd control (`--role`, `--json`) |
| `simurgh mapping list \| add \| remove \| toggle` | port forwardings (`simurgh mapping add 443 443 --name panel --udp --target-host 127.0.0.1`) |
| `simurgh link --show` | setup link for the *other* server |
| `simurgh join '<link>'` | build the config from a link (both directions) |
| `simurgh speedtest` | download/upload measurement through the tunnel |
| `simurgh logs -f` | tail the log |
| `simurgh doctor` | health check with concrete hints |
| `simurgh web` | run only the web panel |
| `simurgh menu` | interactive text menu |
| `simurgh uninstall --purge` | remove services (and data) |

## Configuration reference

All keys live in `/etc/simurgh/exit.toml` and `/etc/simurgh/relay.toml`
(`~/.simurgh` when installed as a normal user). Files are written atomically
with mode `0600`. Changes need `simurgh restart`; port mappings can also be
changed live from the panel or `simurgh mapping`.

### `exit.toml` (foreign server)

| Key | Default | Meaning |
|---|---|---|
| `token` | — | shared secret; identical on both servers |
| `name` | host name | label shown on the relay/panel |
| `[[listen]]` | one block per port | `carrier`, `host`, `port`, `path`, `fallback = "decoy"`, `decoy_file`, `padding`, `enabled` |
| `[[listen]] dial` | *(empty)* | **reverse mode**: dial this address instead of listening; `fingerprint` / `insecure_skip_verify` pin the relay certificate |
| `cert_file` / `key_file` | generated | TLS certificate (`cert_auto = true` creates one) |
| `allow_ips` | any | IP allow-list for tunnel connections |
| `push_ports` / `push_enabled` | `443,2053,…` | ports offered to the relay automatically |
| `strict_ports` | `false` | refuse to dial ports that are not in `push_ports` |
| `speedtest_port` | `8808` | localhost-only speed endpoint |
| `proxy_protocol` | `off` | send a PROXY v1 header to the local panel (keeps real client IPs) |
| `log_level` | `info` | `debug`, `info`, `warning`, `error` |

### `relay.toml` (Iranian server)

| Key | Default | Meaning |
|---|---|---|
| `token` | — | same token as the exit |
| `name` | host name | label shown in the panel |
| `dial` | `relay` | `relay` = direct, `exit` = reverse mode |
| `[exit]` | — | `carrier`, `address`, `port`, `domain`, `path`, `fingerprint`, `insecure_skip_verify` |
| `[[pool]]` | empty | extra foreign servers for automatic failover |
| `[tunnel]` | `tls :8443` | reverse mode only: the listening carrier (`carrier`, `host`, `port`, `path`, `fallback`, `cert_auto`, `cert_file`) |
| `[[mapping]]` | empty | `listen` (Iranian port), `target_host`, `target_port`, `udp`, `name`, `enabled` |
| `accept_push` | `true` | accept the port list the exit sends |
| `stream_window` | `262144` | per-stream receive window (bytes) |
| `chunk` | `65536` | maximum payload per frame |
| `keepalive` | `25` | ping interval in seconds |
| `panel_port` | `8787` | web panel port |

## Carriers

| Carrier | Best for | What a prober sees |
|---|---|---|
| `tls` *(default)* | almost everything | a real HTTPS server with a certificate and a decoy site |
| `wss` | networks where only WebSocket passes | the same TLS plus a WebSocket upgrade on your path |
| `raw` | when TLS itself is suspicious | random noise, X25519 key exchange, ChaCha20-Poly1305 |
| `plain` | two trusted servers / internal links | plain TCP with an authentication header |

## Security

* Shared token, HMAC-SHA256 on a 25-byte header, ±180 s clock skew, replay cache
  (300 s / 20 000 entries) — a captured handshake cannot be replayed.
* Anything without a valid token gets the **decoy**: an nginx page for `tls` /
  `wss`, random bytes for `raw`, silence for malformed input.
* The exit's self-signed certificate is pinned by SHA-256 fingerprint on the
  relay; a real domain (Let's Encrypt) works too.
* The tunnel is fully transparent — customer TLS ends at the foreign panel, so
  no certificate is ever needed on the Iranian server.
* The panel is protected by HTTP Basic + a session cookie; the one-click link
  carries the password in the query string, so treat it like a secret.

## Performance

* One TCP connection per server pair carries every user and every port
  (stream multiplexing, 64 KiB chunks, 16 MiB per-connection window).
* Credit-based end-to-end flow control: sockets are read only while the peer has
  window space, so a slow customer never bloats memory on either side.
* Measured on this repository (loopback, one machine, both roles in one process):

  | Check | Result |
  |---|---|
  | `pytest -q` | **100 passed** (protocol, mux, config, panel, links, e2e for all carriers, reverse mode) |
  | 4-carrier matrix × small / 100 KB / 8 concurrent users | **ALL GREEN (40/40)** |
  | Reversed-mode matrix (tls / raw / plain) | all green, plus reconnection after a relay restart |
  | User path | foreign server content served byte-for-byte from the relay port (200 MB streamed) |
  | Prober | plain HTTP → decoy nginx page · TLS + garbage → silent close · raw → random noise |
  | UDP forwarding | 4/4 replies |
  | Idle memory | ~25–35 MB RSS per node |

  Real-world throughput is limited by the link between the two servers, not by
  the tunnel.

## Troubleshooting

| Symptom | What to do |
|---|---|
| The panel does not open | open the panel port: `ufw allow 8787` (and check `simurgh status`) |
| The tunnel never connects | `simurgh doctor`; open the tunnel port on the listening side; make sure both tokens are identical |
| Reverse mode does not connect | the **exit** dials the relay: check that `dial = "exit"` is in `relay.toml`, the tunnel port is open on the Iranian server, and look at the exit log |
| One port does not work | `simurgh mapping list`; the service must listen on `127.0.0.1:<target_port>` on the foreign server |
| Slow | `simurgh speedtest`; if the tunnel is fast, the bottleneck is the foreign server or the customer's network |
| Nothing works at all | `simurgh doctor`, then `simurgh logs -f`, then re-install with `sudo simurgh uninstall --purge` |

## Upgrade & uninstall

```bash
sudo bash install.sh --role relay --force   # re-run the installer, keeps the token
sudo simurgh uninstall                      # remove the systemd units
sudo simurgh uninstall --purge              # remove everything, including configs
```

## Development

```bash
git clone https://github.com/Dark-Sky07/Simurgh-Tunnel.git  # or the branch you track
cd Simurgh-Tunnel
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[secure]'
python -m pytest -q                      # 100 tests, no external test deps
python tools/live_smoke.py               # 4-carrier live matrix (40 checks)
```

Layout: `simurgh/protocol.py` (frames, address codec), `mux.py` (multiplexer,
flow control), `carriers.py` + `tlsio.py` + `obfs.py` + `decoy.py` (the four
carriers and the decoy), `bridge.py` (socket ↔ stream), `exit.py` / `relay.py`
(the two roles), `udp.py`, `speedtest.py`, `panel.py` (web UI + i18n),
`cli.py` / `menu.py` / `systemd.py`, `config.py`, `links.py`, `certs.py`.

## License

MIT — use it, change it, ship it. Built by Iranian users, for the hard days.

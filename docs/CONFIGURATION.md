# Configuration reference

**English** · [فارسی](CONFIGURATION.fa.md)

Simurgh keeps everything in two TOML files. Both are plain text, written
atomically and stored with mode `0600`.

| Server | File |
|---|---|
| Foreign (exit) | `/etc/simurgh/exit.toml` |
| Iranian (relay) | `/etc/simurgh/relay.toml` |
| Both, installed per-user | `~/.simurgh/…` (override with `--home` / `SIMURGH_HOME`) |
| Panel credentials | `/etc/simurgh/state.json` |
| Certificates | `/etc/simurgh/cert/{cert,key}.pem` |

Apply changes with `simurgh restart`. Port mappings can also be changed live —
from the panel or with `simurgh mapping …`, which the running relay picks up
without a restart.

---

## `exit.toml` — foreign server

```toml
token = "kQ0…"                  # shared secret, identical on both servers
name = "omega-de"               # label shown on the relay and in the panel
cert_auto = true                # generate a self-signed certificate if needed
# cert_file = "/etc/letsencrypt/live/panel.example.com/fullchain.pem"
# key_file  = "/etc/letsencrypt/live/panel.example.com/privkey.pem"
allow_ips = []                  # optional allow-list of tunnel peers
push_ports = [443, 2053, 2083]  # ports offered to the relay
push_enabled = true
strict_ports = false            # true = only dial ports from push_ports
speedtest_port = 8808           # localhost-only speed endpoint (0 = off)
proxy_protocol = "off"          # off | v1 | v2 — tell the panel the real client IP
log_level = "info"              # debug | info | warning | error

[[listen]]                      # one block per tunnel endpoint
carrier = "tls"                 # tls | wss | raw | plain
host = "0.0.0.0"
port = 443
path = "/ws"                    # wss (and tls) path
fallback = "decoy"              # decoy | close — what strangers see
padding = true                  # length obfuscation
enabled = true
```

### Reverse mode on the exit

Instead of listening, the exit dials the relay. Everything else stays the same:

```toml
token = "kQ0…"
name = "omega-de"

[[listen]]
carrier = "tls"
port = 8443                     # the port the *relay* listens on
dial = "203.0.113.9"            # relay address — this makes it a reverse tunnel
fingerprint = "AB:CD:…"         # pin the relay's certificate (recommended)
# insecure_skip_verify = true   # testing only
enabled = true
```

The exit reconnects by itself with a 0.5 s → 15 s backoff, so a restart of
either side needs no manual action.

### Key reference

| Key | Default | Meaning |
|---|---|---|
| `token` | — | required; HMAC key of the tunnel |
| `name` | host name | shown on the relay side |
| `cert_auto` | `true` | create `/etc/simurgh/cert/*.pem` when no certificate is given |
| `allow_ips` | `[]` | when non-empty, only these peers may open tunnels |
| `push_ports` | common panel ports | suggestions sent to the relay |
| `strict_ports` | `false` | refuse to dial a port that the relay was not offered |
| `speedtest_port` | `8808` | speed endpoint bound to `127.0.0.1` only |
| `proxy_protocol` | `off` | send a PROXY protocol header to the local service |
| `log_level` | `info` | log verbosity |

### `[[listen]]` keys

| Key | Default | Meaning |
|---|---|---|
| `carrier` | `tls` | disguise used by this endpoint |
| `host` / `port` | `0.0.0.0` / `8443` | listen address (direct mode) |
| `path` | `/ws` | HTTP path for `wss` |
| `fallback` | `decoy` | `decoy` = serve the fake website, `close` = drop silently |
| `decoy_file` | — | custom HTML file to serve to probers |
| `padding` | `true` | pad frames so lengths are not meaningful |
| `enabled` | `true` | disable without deleting the block |
| `dial` | — | **reverse mode**: dial this host instead of listening |
| `fingerprint` | — | pinned SHA-256 of the relay certificate (reverse) |
| `insecure_skip_verify` | `false` | accept any certificate (testing only) |

---

## `relay.toml` — Iranian server

```toml
token = "kQ0…"
name = "ir-tehran"
dial = "relay"                  # relay = direct, exit = reverse
panel_port = 8787
accept_push = true              # accept the port list the exit suggests
keepalive = 25                  # seconds between pings
stream_window = 262144          # starting per-stream receive window (bytes)
max_stream_window = 16777216    # ceiling for the automatic window growth
chunk = 65536                   # max payload per frame
connections = 1                 # tunnel connections to keep open (2-16 = pool)
log_level = "info"

[exit]                          # the foreign server we dial (direct mode)
carrier = "tls"
address = "203.0.113.9"
port = 443
domain = "panel.example.com"    # SNI, optional
path = "/ws"
fingerprint = "AB:CD:…"         # certificate pinning, optional
insecure_skip_verify = false

[[pool]]                        # extra foreign servers for failover
address = "198.51.100.7"
port = 443
carrier = "tls"

[[mapping]]                     # port forwarding: Iranian port -> foreign port
name = "panel"
listen = 443                    # port on THIS (Iranian) server
target_host = "127.0.0.1"       # where the exit connects
target_port = 443
udp = false
enabled = true
```

### Reverse mode on the relay

```toml
token = "kQ0…"
name = "ir-tehran"
dial = "exit"                   # we wait for the foreign server
panel_port = 8787

[tunnel]                        # the listener the exit dials
carrier = "tls"
host = "0.0.0.0"
port = 8443                     # open this port in the firewall
path = "/ws"
fallback = "decoy"
cert_auto = true                # a certificate for the listener
```

In reverse mode there is no `[exit]` block: the relay never dials out. The
`[[mapping]]` blocks stay exactly the same, because user ports always live on
the Iranian side.

### Key reference

| Key | Default | Meaning |
|---|---|---|
| `token` | — | required; must equal the exit's token |
| `dial` | `relay` | `relay` = direct, `exit` = reverse |
| `panel_port` | `8787` | web panel port |
| `accept_push` | `true` | honour the port list sent by the exit |
| `keepalive` | `25` | ping interval in seconds |
| `stream_window` | `262144` | starting per-stream receive window |
| `max_stream_window` | `16777216` | ceiling for the automatic window growth (16 MiB) |
| `chunk` | `65536` | max frame payload, in bytes |
| `connections` | `1` | tunnel connections kept open; 2-16 spread the load |
| `speedtest_port` | `0` | local speed endpoint (`0` = off) |
| `[exit]` | — | the foreign endpoint (direct mode) |
| `[tunnel]` | — | the listener (reverse mode) |
| `[[pool]]` | `[]` | fallback foreign servers, tried when the primary is down |
| `[[mapping]]` | `[]` | port forwardings |

### Speed and resilience keys (both files)

These three keys exist in `relay.toml` **and** `exit.toml`; the smaller of the
two sides wins for any given stream, so setting them on the relay is enough.

| Key | Default | What it does |
|---|---|---|
| `stream_window` | `256 KiB` | How much a stream may have in flight *before* the tunnel learns the path. Leave it alone unless you know the link is slow to start. |
| `max_stream_window` | `16 MiB` | The ceiling for the automatic growth. The tunnel measures the real throughput of each stream and doubles the window while the stream keeps the path busy, so a single user on a long (Iran ⇄ Europe) path is no longer capped at `window / RTT`. Lower it on a tiny VPS with little RAM. |
| `connections` | `1` | How many tunnel connections the relay keeps to the exit (1-16). Every connection is a separate TCP flow and a separate core's worth of work: on long, lossy paths 2-4 of them raise the total a lot and a single dropped connection only costs a fraction of the users. The relay hands each new user connection to the least busy tunnel. |
| `chunk` | `64 KiB` | Largest payload per frame. Bigger frames mean less per-frame overhead and a little more latency headroom; 64 KiB is a good middle. |

Measured on a simulated 120 ms path (2 vCPU container), single user, plain
carrier: **2 MB/s with a fixed 256 KiB window → 18 MB/s with the window
growing**, and the same class of test on a fast path reaches ~2 Gbit/s. RAM
stays bounded: an idle stream's window falls back to `stream_window`.

### `[[mapping]]` keys

| Key | Default | Meaning |
|---|---|---|
| `listen` | — | port opened on the Iranian server |
| `listen_host` | `0.0.0.0` | bind address for that port |
| `target_host` | `127.0.0.1` | address used **from the exit** |
| `target_port` | — | service port on the foreign server |
| `udp` | `false` | forward UDP instead of TCP |
| `name` | — | label in the panel and CLI |
| `enabled` | `true` | keep the entry but stop forwarding |

CLI equivalents:

```bash
simurgh mapping add 443 443 --name panel
simurgh mapping add 2096 2096 --udp
simurgh mapping add 8443 8443 --target-host 10.0.0.5
simurgh mapping toggle 443
simurgh mapping remove 443
```

---

## Setup links

A setup link is a single string that carries the token and the endpoint:

```
simurgh://<host>:<panel-port>/setup?u=<panel-user>&p=<base64 password>#<name>
```

* On the **exit**: `simurgh link --show` prints the link for a new relay.
* On a **reverse relay**: the same command prints the link for the exit.
* On the other server: `simurgh join '<link>'` writes the config; add
  `--host <ip>` to override the address, `--force` to overwrite an existing
  config.

Treat links as secrets: they contain the tunnel token.

## systemd units

| Unit | Role |
|---|---|
| `simurgh-exit.service` | the foreign server process |
| `simurgh-relay.service` | the Iranian server process |

```bash
systemctl status simurgh-relay
journalctl -u simurgh-relay -f          # or: simurgh logs -f
```

## Environment variables

| Variable | Meaning |
|---|---|
| `SIMURGH_HOME` | config directory (default `/etc/simurgh`) |
| `SIMURGH_ALLOW_NONROOT` | `1` — allow installing as a normal user (no systemd) |
| `SIMURGH_REF` / `--ref` | git ref the installer downloads (branch or tag) |
| `SIMURGH_REPO` | alternative repository URL |
| `SIMURGH_SRC`, `SIMURGH_VENV` | source and virtualenv locations |

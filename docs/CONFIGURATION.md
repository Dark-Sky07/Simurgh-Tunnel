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
split = 3                       # anti-DPI: send the first n bytes alone
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
| `carrier` | `tls` | disguise used by this endpoint: `tls`, `wss`, `raw` or `plain` — both engines speak all four and can face each other |
| `host` / `port` | `0.0.0.0` / `8443` | listen address (direct mode) |
| `path` | `/ws` | HTTP path for `wss` |
| `fallback` | `decoy` | `decoy` = serve the fake website, `close` = drop silently |
| `decoy_file` | — | custom HTML file to serve to probers |
| `padding` | `true` | pad frames so lengths are not meaningful. Only the `raw` carrier uses it, and only for the direction it sends in: a padded frame lands on the first of 128/256/512/…/16384 bytes that fits (or the next multiple of 64 above that) plus a 2-byte trailer, exactly like `simurgh/carriers.py`. Turning it off on one side is safe — the other side still removes its padding. |
| `split` | `0` (`simurgh init` writes `3`) | how many bytes of the **first write** leave in a TCP segment of their own; the rest follows ~10 ms later, so a middlebox that reads only the first segment of a connection sees a fragment. Only the TLS handshake is split, and only on the side that dials: the relay in direct mode, the exit in reverse mode. `0` disables it; `3` is the shape of a TLS record header, so the fragment still starts with the two bytes a TLS-aware checker looks for. The 10 ms pause is paid once per tunnel connection, at setup. |
| `enabled` | `true` | disable without deleting the block |
| `dial` | — | **reverse mode**: dial this host instead of listening |
| `fingerprint` | — | pinned SHA-256 of the relay certificate (reverse) |
| `insecure_skip_verify` | `false` | accept any certificate (testing only) |
| `cert_file` / `key_file` | — | `tls`/`wss` only: a certificate **for this listener alone**, overriding the one at the top of the file — one VPS, several domains, one certificate each. Both engines read it, and the exit watches it for expiry like any other |

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
split = 3                       # anti-DPI: send the first n bytes alone

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
| `speedtest_port` | `0` | local speed endpoint (`0` = off); the relay learns it from the exit's handshake, so `simurgh speedtest` follows a change |
| `[exit]` | — | the foreign endpoint (direct mode); accepts `split` too |
| `split` | `3` in files written by `simurgh init` | in `[exit]` (and in any `[[pool]]` entry): how many bytes of the first write leave in a TCP segment of their own — the anti-DPI trick described under `[[listen]]`. `simurgh install --split N` (or `init`) sets it |
| `[tunnel]` | — | the listener (reverse mode) |
| `[[pool]]` | `[]` | fallback foreign servers, tried when the primary is down |
| `[[mapping]]` | `[]` | port forwardings |

### Speed and resilience keys (both files)

These keys exist in `relay.toml` **and** `exit.toml`; the smaller of the two
sides wins for any given stream, so setting them on the relay is enough.

| Key | Default | What it does |
|---|---|---|
| `stream_window` | `256 KiB` (Go engine: `1 MiB`) | How much a stream may have in flight *before* the tunnel learns the path. The Python engine grows happily from 256 KiB; the compiled engine needs the bigger start because its growth is rate based and a small window never grows on a short path. `simurgh init` writes the value that suits the engine it configures. |
| `max_stream_window` | `8 MiB` | The ceiling for the automatic growth. The tunnel measures the real throughput of each stream and doubles the window while the stream keeps the path busy, so a single user on a long (Iran ⇄ Europe) path is no longer capped at `window / RTT`. The memory it can park follows demand (only a stream that really drains fast grows), and it is the worst case per busy user — lower it to `4 MiB` if you run hundreds of heavy users on a small VPS, raise it to `16 MiB`+ for one very fat flow. |
| — (shared) | `16 MiB` floor, `64 MiB` cap | The credit budget of the whole tunnel *connection*: it follows the sum of the live streams' current windows, so dozens of users on one connection never starve each other, while a peer that stops reading cannot park more than the cap per connection. Nothing to configure; it is what keeps 1000 users on one box from being limited to one user's window. |
| `connections` | `1` | How many tunnel connections the relay keeps to the exit (1-16). Every connection is a separate TCP flow and a separate core's worth of work: on long, lossy paths 2-4 of them raise the total a lot and a single dropped connection only costs a fraction of the users. The relay hands each new user connection to the least busy tunnel. |
| `chunk` | `64 KiB` | Largest payload per frame. Bigger frames mean less per-frame overhead and a little more latency headroom; 64 KiB is a good middle. |
| `tcp_nodelay` | `true` | Nagle off on every socket this node opens: the tunnel dial, the exit's dial to a service, and the user connections the relay accepts. A small write (a keystroke in SSH, a request header) leaves immediately instead of waiting for the peer's ACK. `false` trades one round trip per small write for a little less overhead on bulk transfers. Both engines honour it, including the compiled one. |
| `tcp_keepalive` | `0` (off) | Seconds of silence before the kernel starts probing a peer on those same sockets. `0` is off, and the tunnel's own ping (`keepalive`) is what notices a dead peer; a value of 15-30 s only reaps half-open user/service sockets sooner, which is worth it when a middlebox silently drops idle flows. |
| — in `[exit]` / `[[pool]]` | inherits the file | A foreign endpoint may set its own `tcp_nodelay` / `tcp_keepalive`; leaving them out means "use the file's value". That is per-hop, so a lossy detour can be tuned without touching the others. |
| `engine` | `go` | Which data plane runs this file: `go` (compiled, many cores, much less CPU per gigabyte) or `python` (portable, no build step). `simurgh init` writes `go` when this machine has the binary and `python` when it does not, so the default is whatever is actually installed; both speak the same protocol, so one side may run Go while the other still runs Python. Switch with `simurgh engine go` after building it (`sudo bash tools/build-go.sh`). The panel, the menu and the CLI stay Python. With `engine = "go"` the compiled engine runs as a **supervised child** of the Python process: the panel, `/metrics` and the alerts keep working next to it (the engine itself opens no HTTP port), `state.json` is followed so tunnel-up/down and quota events are announced, and an engine that dies is restarted with a growing backoff (0.5 s → 30 s) and reported. `simurgh relay --exec` hands the process over to the engine instead — lower overhead, but no panel and no alerts. |

Measured on a simulated long path (2 vCPU container), single user, plain
carrier, 80 MB: **2 MB/s with a fixed 256 KiB window → 40.4 MB/s with the window
growing** (20×), 233 MB/s on a short path. With `connections = 4` and a per-flow
cap of 4 MB/s, eight users get 13.0 MB/s against 3.6 MB/s on a single tunnel.
RAM stays bounded: an idle stream's window falls back to `stream_window`, the
shared budget caps each tunnel connection, and the whole benchmark process
(relay + exit + target) peaked at 95 MB.

The Go engine raises those ceilings and costs far less CPU per gigabyte:
`tools/build-go.sh` > `simurgh engine go` > restart. Measured with the same
harness on the same box:

| Workload | Python engine | Go engine |
|---|---|---|
| one stream, 60 ms path, 80 MB | 40.4 MB/s | **42.3 MB/s** (338 Mbit/s) |
| one stream, 60 ms path, `tls` | 22.7 MB/s | **30.5 MB/s** (244 Mbit/s) |
| one stream, short path, 256 MB | 233 MB/s | **383 MB/s** (3.07 Gbit/s) |
| 64 users on **one** tunnel connection, 20 ms | — | **430 MB/s** (3.4 Gbit/s) |
| 64 users on two tunnel connections | — | **503 MB/s** (4.0 Gbit/s) |
| eight users, 4 tunnels, 4 MB/s cap | 13.0 MB/s | 13.0 MB/s |
| CPU per 256 MB moved | 0.85 CPU-s | **0.6-0.8 CPU-s** |

Every row is one process running relay + exit + the simulated long path + the
test client, on a 2 vCPU container, so the numbers are conservative (a real pair
of servers has two boxes) and the loopback path is optimistic (no packet loss,
no real 60 ms of queueing). Reproduce them yourself:

```bash
python tools/latency_bench.py 60 80 plain            # Python engine
go/bin/simurgh-go bench -mb 256 -delay 20 -streams 64 -connections 1
```

For a thousand simultaneous users the shape of the answer is: keep each tunnel
connection around a few dozen *busy* streams and raise `connections`, because
every connection is another core's worth of work and another TCP flow:

```bash
simurgh init --exit-host 203.0.113.9 --connections 8   # 1000 users, 8 cores
```

### `[[mapping]]` keys

| Key | Default | Meaning |
|---|---|---|
| `listen` | — | port opened on the Iranian server |
| `listen_host` | `0.0.0.0` | bind address for that port |
| `target_host` | `127.0.0.1` | address used **from the exit** |
| `target_port` | — | service port on the foreign server |
| `udp` | `false` | forward UDP instead of TCP. One datagram is one frame, so boundaries survive and nothing is retransmitted; when the far side is behind the datagram is dropped like a full UDP socket would. Each client address gets its own stream on the relay, reaped after 60 s of silence, and the exit talks to the target through one UDP socket per stream. Both engines speak it. |
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

## The speed test

```bash
simurgh speedtest                 # throughput + latency, both sides real
simurgh speedtest --udp           # ... and datagram loss/jitter
simurgh speedtest --streams 4     # four tunnel streams, summed
simurgh speedtest --seconds 10 --json
simurgh speedtest --no-latency    # throughput only (the old behaviour)
```

It runs on the **Iranian** side and measures straight through the tunnel — the
carrier, the multiplexer and flow control included, which is the point:

| What | How |
|---|---|
| download / upload | the exit's localhost-only endpoint at `speedtest_port` gives data away and swallows what it receives |
| latency, idle and under load | a second stream opens in *echo* mode (its first bytes are a magic marker) and tiny samples are timed round trip: once before the load, then repeatedly while the link is saturated |
| bufferbloat grade | from the growth of the median round trip under load: `A` ≤30 ms, `B` ≤60, `C` ≤120, `D` ≤250, `F` above |
| UDP loss and jitter | `--udp`: datagrams are sent through the tunnel to the same endpoint, which echoes them back, and every missing sequence number is counted |

The endpoint binds `127.0.0.1` only, in **both** engines, and the exit
announces the port it uses in the connection handshake — so changing
`speedtest_port` (or `0` to switch the endpoint off) on the exit works without
touching the relay. With no tunnel up, `simurgh speedtest` says so and exits 3
instead of inventing numbers.

`--json` returns everything:

```json
{
  "seconds": 3.0, "streams": 1,
  "download_mbps": 3655.3, "upload_mbps": 1528.8,
  "latency": {"idle_ms": 0.6, "loaded_ms": 12.4, "increase_ms": 11.9,
              "jitter_ms": 6.5, "loaded_p95_ms": 25.9, "grade": "A"},
  "udp": {"sent": 200, "received": 200, "loss_percent": 0.0,
          "rtt_ms": 0.3, "jitter_ms": 0.1}
}
```

---

## Performance profiles (`simurgh tune`)

The keys above are a trade-off, not a setting, so the combinations that are
worth having have names:

```bash
simurgh tune                     # list the profiles and their values
simurgh tune gaming              # lowest latency, steadiest under load
simurgh tune speed               # highest aggregate throughput
simurgh tune balanced            # back to the shipped defaults
simurgh tune speed --role relay  # which file (default: whichever exists)
simurgh tune speed --dry-run     # print the difference, write nothing
simurgh tune speed --json        # {profile, role, config, changes[], wrote}
```

| | `stream_window` | `max_stream_window` | `chunk` | `connections` | `keepalive` (ping) | `tcp_keepalive` |
|---|---|---|---|---|---|---|
| `gaming` | 128 KiB | 2 MiB | 16 KiB | 1 | 10 s | 15 s |
| `balanced` (shipped) | 256 KiB | 8 MiB | 64 KiB | 1 | 25 s | 0 |
| `speed` | 512 KiB | 32 MiB | 64 KiB | 4 | 25 s | 0 |

A smaller window keeps less data in flight: shorter queues when the link is
busy, steadier latency, less memory — and a single flow is capped lower on a
long path. `gaming` goes that way, `speed` goes the other way (and raises
`connections` to 4, the one key that wants both ends reachable), and `balanced`
is exactly what `simurgh init` writes, so it doubles as the undo. Everything
the profiles touch is a key both engines already read; the carrier, the token,
the certificates and the mappings are never touched.

Only keys that actually differ are reported and written, only the config file
that exists is edited (on a one-box install, run it for each role), and the
running process keeps the old values until `simurgh restart` — the command
prints that reminder. `simurgh tune` with no profile, or `--show`, needs no
installation at all: it is the documentation of the three choices. The menu has
the same page as **[16] Performance profile**, and with no argument it prints
the current value of every key next to the profile it belongs to.

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

## Metrics and updates

### Prometheus metrics (`/metrics`)

Every node that runs a panel also exports Prometheus text on the same port:

```bash
curl -u admin:password http://127.0.0.1:8787/metrics
```

| Metric | Meaning |
|---|---|
| `simurgh_up` / `simurgh_uptime_seconds` | the node answers, and for how long |
| `simurgh_tunnel_connected`, `simurgh_tunnel_connections`, `simurgh_tunnel_rtt_seconds` | state of the tunnel(s) to the other side |
| `simurgh_streams`, `simurgh_reconnects_total`, `simurgh_blocked_total` | multiplexed streams, reconnects, users refused by a quota |
| `simurgh_bytes_in_total`, `simurgh_bytes_out_total` | traffic moved for users |
| `simurgh_mapping_*{key,name,listen,target}` | per-mapping bytes, connections, active, errors, `up` |
| `simurgh_traffic_bytes_total{key,period}` | the persistent ledger (`today` / `month` / `total`) |
| `simurgh_quota_*{key,period}` | quota limit, used bytes and whether it is exhausted |
| `simurgh_exit_*{endpoint}` | pool health: is the endpoint answering, and its RTT |
| `simurgh_panel_requests_total`, `simurgh_panel_lockouts_total`, `simurgh_panel_failed_logins` | panel traffic, addresses locked out after repeated bad logins, wrong passwords counted right now |
| `simurgh_alerts_sent_total`, `simurgh_alerts_failed_total`, `simurgh_alerts_enabled` | what the alerter delivered, what it could not, and whether a channel is configured at all |

Authentication is the same as the panel (Basic auth, the session cookie or the
one-click `?k=` link), so a scraper's credentials are the panel's. Everything is
read from the same status the panel shows, which means the metrics work with
**either engine** — with the Python engine the collector reads the node live, and
with the Go engine it reads the `state.json` that engine keeps updated.

`scrape_configs` example:

```yaml
- job_name: simurgh
  basic_auth: {username: admin, password: "…"}
  static_configs: [{targets: ["ir.example.com:8787"]}]
```

### `simurgh update`

```bash
simurgh update            # install the newest release
simurgh update --check     # only report (exit code 3 when one is available)
simurgh update --check --json
```

The command works out what kind of install it is looking at:

* **a git checkout** — it fetches the tags, checks out the newest release tag and
  rebuilds the Go engine (`--no-rebuild` skips that);
* **a packaged install** — it downloads the asset for this OS/architecture,
  checks its SHA-256 against the release's `SHA256SUMS.txt` and replaces the
  running binary atomically, keeping the old one as `simurgh-go.bak`;
* **a pip install** — it runs `pip install -U simurgh-tunnel`.

With no network it says so and changes nothing. After an update, `simurgh
restart` runs the new version.

## Alerts

The operator should hear about a dead tunnel from the tunnel, not from a
customer. Both roles take the same keys, in `relay.toml` and in `exit.toml`:

```toml
telegram_token = "8123456:AAH…"        # from @BotFather
telegram_chat  = "-1001234567890"      # the chat/group the bot posts to
# telegram_api = "http://127.0.0.1:8081"   # a local Bot API server (optional)
alert_webhook  = "https://hooks.example.com/simurgh"   # anything that takes JSON
alert_events   = ["connected", "disconnected", "quota", "cert", "error"]
```

| Key | Default | Meaning |
|---|---|---|
| `telegram_token` | — | bot token; needs `telegram_chat` as well |
| `telegram_chat` | — | chat id (a group id starts with `-`) |
| `telegram_api` | `https://api.telegram.org` | root of the Bot API; point it at your own Bot API server if the public one is blocked |
| `alert_webhook` | — | URL that receives a JSON `POST` per alert (Slack, Discord, ntfy, an internal endpoint) |
| `alert_events` | `connected, disconnected, quota, cert, error` | which events are sent; `["all"]` means everything |

The webhook receives one object per alert:

```json
{"event": "quota", "level": "warning", "text": "[ir-tehran] mapping tcp:0.0.0.0:443/… reached its 10.0 MB/month quota (block)",
 "host": "ir-tehran", "time": 1792163305.35, "mapping": "tcp:…", "used": 10485760, "limit": 10485760}
```

| Event | Fired when | Extra fields |
|---|---|---|
| `started` / `stopped` | the service comes up or is shut down | `pid` (Go engine) |
| `connected` / `disconnected` | the tunnel to the other side comes up or drops | `rtt_ms`, endpoint |
| `quota` | a mapping reaches its quota (`block`, `throttle` or `warn`) | `mapping`, `used`, `limit` |
| `throttle` | a mapping is being slowed to 128 KiB/s because of its quota | `mapping` |
| `cert` | a certificate this node serves expires within **14 days** (once a day per file) | `cert`, `days` |
| `login` | the panel locked an address out after repeated failed logins | `peer` |
| `error` | a node error, or the Go engine exited and is being restarted | `exit_code`, `restarts` |
| `test` | `simurgh alert test` | — |

Alerts are queued and sent from a worker thread, so a slow (or dead) endpoint
never blocks the data plane; identical events are collapsed with a 20-second
cooldown so a flapping tunnel cannot flood the chat. A node that has just been
shut down still delivers what is in the queue before it exits.

```bash
simurgh alert status                  # which channels and events are live
simurgh alert test                    # sends one message now (exit 1 if it fails)
simurgh alert events                  # show the list
simurgh alert events quota cert error # write a new list (then: simurgh restart)
simurgh alert status --role exit
```

## Certificates (Let's Encrypt)

Every `tls`/`wss` endpoint needs a certificate, and there are three ways to give
it one:

1. **`cert_auto = true`** (the default) — a self-signed certificate is generated
   on first start. Perfect for the tunnel itself: both sides speak TLS, the relay
   pins the fingerprint, and nobody sees the "untrusted" warning. Not usable for a
   panel or a website behind the tunnel.
2. **Your own files** — `cert_file` / `key_file` at the top of the file (or inside
   one `[[listen]]` block, for one box serving several domains). Point them at
   what your tool produced:

   ```toml
   cert_auto = false
   cert_file = "/etc/letsencrypt/live/panel.example.com/fullchain.pem"
   key_file  = "/etc/letsencrypt/live/panel.example.com/privkey.pem"
   ```

   After a renewal, restart the service (`simurgh restart`); if you forget, the
   `cert` alert from the section above tells you 14 days before it hurts.
3. **`simurgh cert issue`** — let Simurgh drive `acme.sh` (preferred: a single
   shell script, so it installs on a filtered server) or `certbot` for you, then
   wire the result into the config and install a renewal hook:

   ```bash
   sudo simurgh cert issue panel.example.com --email me@example.com \
        --webroot /var/www/html          # nginx/caddy already serves it
   sudo simurgh cert issue panel.example.com --standalone   # port 80 is free
   sudo simurgh cert issue panel.example.com --staging      # dry run, test CA
   sudo simurgh cert issue panel.example.com --port 8443     # this listener only
   ```

   The certificate is written into `exit.toml` (or the relay's
   `[tunnel]` block with `--role relay`), `cert_auto` is switched off so the
   engine never replaces it, and `acme.sh --install-cert --reloadcmd "systemctl
   restart simurgh-exit"` means the next renewal restarts the service by itself.
   `--no-write` obtains the certificate and leaves the config alone, and
   `--apply` runs that hook right away instead of waiting for the next renewal
   (handy when a service manager is not in play).

   ```bash
   simurgh cert                     # what is in use: paths, days left, fingerprint
   simurgh cert renew               # run the client's own renewal (= its cron job)
   simurgh cert status --role relay
   simurgh cert status --json       # the same, for a script or a check
   ```

   Rows marked `(self-signed)` come from `cert_auto`; they are fine for the
   tunnel itself, and `simurgh cert issue <domain>` replaces them with a real
   certificate.  `simurgh cert status` exits 1 when something is missing, not a
   certificate, or inside the last 14 days, so it works in a monitoring script.

   On a listener that already has its own `cert_file`, `simurgh cert issue`
   without `--port` leaves that block alone and says so.

### Panel hardening

* **Failed logins** are counted per source address. The fifth wrong password
  blocks that address for **300 seconds** (requests get `429`), which is enough
  to make guessing pointless, and the `login` alert tells the operator where it
  came from. A successful login clears the counter, and `simurgh_panel_lockouts_total`
  counts how often it happened.
* The panel **never prints the token or the bot token**; `simurgh alert status`
  masks it (`1234…ew11`).
* `?k=<password>` links still work for one-click access, but the password ends up
  in the browser history — prefer the `Set-Cookie` session the same link creates,
  or Basic auth.

## Environment variables

| Variable | Meaning |
|---|---|
| `SIMURGH_HOME` | config directory (default `/etc/simurgh`) |
| `SIMURGH_ALLOW_NONROOT` | `1` — allow installing as a normal user (no systemd) |
| `SIMURGH_REF` / `--ref` | git ref the installer downloads (branch or tag) |
| `SIMURGH_REPO` | alternative repository URL |
| `SIMURGH_SRC`, `SIMURGH_VENV` | source and virtualenv locations |
| `SIMURGH_GO_BIN` | full path of the compiled `simurgh-go` binary when it lives somewhere unusual (the service finds `simurgh-go` on `PATH` by itself) |
| `SIMURGH_ENGINE` / `--engine` | installer shortcut for `--engine go\|python\|auto` |
| `--split N` | how many bytes of the first handshake write travel in a TCP segment of their own; written into `[exit]` (direct) or the reverse exit's `[[listen]]`. Default `3`, `0` disables it |
| `GO_BIN`, `SIMURGH_GO_VERSION` | toolchain used by `tools/build-go.sh` (path override / version to download) |

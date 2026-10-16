# The web panel

**English** · [فارسی](PANEL.fa.md)

The panel is a page the tunnel serves about itself: a map of the two ends, the
numbers behind them, and the buttons an operator reaches for at 3 a.m. It is
part of the package — one Python file (`simurgh/panel.py`), one data file
(`simurgh/layout.py`), one string table (`simurgh/panel_i18n.py`) and the four
assets in `simurgh/assets/`. There is no build step, no framework, no CDN and no
JavaScript dependency: nothing the panel shows needs the internet to load.

## Opening it

```bash
simurgh web                       # the panel alone, next to a running node
simurgh web --link                # …and a one-time login link on stdout
simurgh web --port 9000 --host 127.0.0.1
```

The normal path is the service: `simurgh start` runs the node **and** the panel,
and the panel's port comes from `panel_port` in `relay.toml` (default `8787`).
On an exit the panel is served on the same port as the relay's panel if you
start it from the menu.

| Way in | How |
|---|---|
| password prompt | `http://<ip>:8787` — HTTP Basic, for scripts and `curl` |
| one-click link | `http://<ip>:8787/?k=<panel-password>` — for a browser without a password manager |
| login form | `http://<ip>:8787/login` — a real form, `EN / فا` |
| one-time link | `simurgh web --link`, or the Security page: single use, 10 minutes |

> Put the panel behind a firewall or an SSH tunnel if the machine is reachable
> from the internet. It is authenticated and hardened, but it is still a control
> plane: `ssh -L 8787:127.0.0.1:8787 root@server` and browse to
> `http://127.0.0.1:8787`.

## The map

The dashboard draws the tunnel as the bird it is named after:

```
        relay (left wing)              tunnel body              exit (right wing)
   ┌─────────────────┐   ╔═════════════════════════╗   ┌─────────────────┐
   │ country · name  │   ║  tls :443  · rtt 12 ms  ║   │ country · name  │
   │ uptime · conns  │   ║  pool: 2 exits, 1 live  ║   │ listen · tunnels│
   └─────────────────┘   ╚═════════════════════════╝   └─────────────────┘
            ╲                   tail feathers                     ╱
             ╲  443 ●── 50% quota ─▶ 127.0.0.1:8443  ↓ 2.1 Mb/s ╱
```

* **wings** — each end's name, uptime, connections and last error. A wing dims
  when its side is down, and carries the reason on hover.
* **body** — the carrier in use, the port, the RTT, and how many endpoints the
  pool knows versus how many are live. A standby endpoint is a hollow ring; the
  active one is filled. Hovering shows the failover state of each.
* **tail feathers** — one per mapping: protocol, listen port, target, quota ring,
  live rate and a sparkline of the last few minutes. Feathers light up when
  traffic crosses them.
* **chart** — aggregate in/out over the last half hour, from the panel's own
  history (`layout.History`, sampled every 2 s, shipped at 10 s resolution).

Everything on screen comes from one poll: `GET /api/layout` every two seconds
(and never while the tab is hidden).

## The views

| View | Notes |
|---|---|
| Dashboard | the map, the cards, the chart, the pool |
| Ports | add/remove/enable/disable/re-point mappings; changes are written to the config and applied to the running node without a restart |
| Wizard | preview → apply → rollback for a new tunnel; see below |
| Speed test | download/upload against the exit, latency grades, session history in the browser |
| Certificates | what `simurgh cert status` prints, with days left and fingerprints |
| Logs | the node's log tail, filter and auto-refresh |
| Security | sessions, lockouts, failed logins, one-time links, the hardening rules |
| Settings | the editable subset of the config, the engine in use, the tunnel token |
| Tools | restart / stop / start / reconnect, export the config, download the log, `/metrics` and `/healthz`, update check |

## The wizard

Building a tunnel by hand means getting the carrier, the port, the path, the
split and the first mapping right on both sides. The wizard turns that into a
plan you can read before anything is written:

1. **Preview** — `POST /api/wizard {"step": "preview", "plan": {...}}` returns
   the diff against the current config (`[exit] carrier tls -> wss`), and writes
   nothing. A refused plan explains itself: an unknown carrier, a port outside
   1–65535, a mapping that is not `listen:target`, a split that is not a number.
2. **Apply** — the current file is copied to `<config>.bak-wizard`, the plan is
   written, and the running node is reconfigured in place.
3. **Rollback** — the backup is copied back and reloaded. One click, and the old
   file is still there afterwards.

The plan is deliberately small: `carrier` (tls, wss, raw, plain, udp), `port`,
`host`, `path`, `split`, and `mapping` (`listen:target`). Everything else stays
in the config file, where you can still edit it by hand.

## Themes, language and phones

* `night` is the default; `dawn` is the light theme. The choice is per browser
  (`localStorage`), as is the language.
* English and Persian ship together (`simurgh/panel_i18n.py`), `dir` flips to
  `rtl` for Persian, and a test fails if the two tables drift apart.
* Under 720 px the sidebar becomes a bottom bar, the map stacks the wings above
  the body, and the tables scroll sideways — the panel works on a phone, which
  is where most of these servers are first checked.
* It is also installable as a web app: `/manifest.webmanifest` plus the icon.

## What keeps it safe

| Rule | Why |
|---|---|
| strict CSP (`default-src 'none'`), no inline script or style | a page that pulls nothing in cannot be made to pull something in |
| assets served from the package by name | `read_asset` refuses anything with a path in it; no traversal is possible |
| `Cache-Control: no-store` on every API answer and on the code | a browser never shows a stale number or a stale page |
| `X-Frame-Options: DENY` | the panel cannot be framed by a phishing page |
| five failed logins from one address → 300 s lockout (`429`) | brute force, on the panel and on `/metrics` |
| one-time links hashed, single-use, 10-minute TTL, at most 5 outstanding | a leaked link is worthless; `revoke` drops them all |
| tokens masked wherever they are printed | logs and the panel never print a secret |
| the panel is never in the data path | it can be stopped, restarted or broken without touching traffic |

## The API

Everything the panel does is one of these; they are all authenticated (Basic
auth, or the session cookie a login hands out):

| Method | Path | What |
|---|---|---|
| GET | `/` | the shell page (its strings travel in a JSON data block) |
| GET | `/app.css`, `/app.js`, `/login.js`, `/favicon.ico`, `/assets/<name>`, `/manifest.webmanifest` | the assets, straight from the package |
| GET | `/api/status` | node + panel status (the small answer) |
| GET | `/api/layout` | the map, cards, chart and rates (the big answer) |
| GET | `/api/config` | the editable config |
| POST | `/api/config` | write it back |
| POST | `/api/mapping` | add / remove / enable / disable a mapping, applied live |
| POST | `/api/speedtest` | run one |
| POST | `/api/action` | restart / stop / start / reconnect / reload |
| POST | `/api/tune` | apply a performance profile (`gaming`, `balanced`, `speed`) |
| POST | `/api/wizard` | preview / apply / rollback a tunnel plan |
| POST | `/api/login-token` | mint a one-time login link |
| POST | `/api/join` | the one-line install payload for a new relay |
| GET | `/api/update` | ask GitHub once whether a newer release exists |
| GET | `/api/logs`, `/api/security` | the log tail, the guard's counters |
| GET | `/login`, `POST /login`, `GET /logout` | the form, the form's answer, and goodbye |
| GET | `/metrics` | Prometheus text for the same numbers (`simurgh_listen_*`, `simurgh_panel_*`, …) |
| GET | `/healthz` | liveness, no credentials, for a watchdog |

`/api/layout` is the one to read if you are writing your own front end: it is
JSON, it is the same data the shipped map draws, and `simurgh/layout.py` is the
whole translation layer between it and a node's `status()`.

## Writing on it

The panel is a friend of the CLI, not a replacement for it: every write goes
through the same `config.save_*` the CLI uses, and the same validation. If you
would rather not have it at all, `simurgh relay` with `panel_port = 0` runs the
node with no page attached, and the CLI keeps working exactly as before.

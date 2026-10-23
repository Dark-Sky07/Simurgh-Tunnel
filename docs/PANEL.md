# The web panel

**English** · [فارسی](PANEL.fa.md)

The panel is a page the tunnel serves about itself: a map of the two ends, the
numbers behind them, and the buttons an operator reaches for at 3 a.m. It is
part of the package — one Python file (`simurgh/panel.py`), one data file
(`simurgh/layout.py`), one string table (`simurgh/panel_i18n.py`) and the
HTML/CSS/JavaScript, manifest and icon assets in `simurgh/assets/`. There is no
build step, no framework, no CDN and no JavaScript dependency: nothing the panel
shows needs the internet to load. From v3.2.5, the installer also verifies these
files inside the installed Python package, so a healthy `/healthz` cannot hide
an incomplete dashboard installation.

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
| legacy one-click link | `http://<ip>:8787/?k=<panel-password>` — exchanges the password for a cookie and redirects to `/` |
| login form | `http://<ip>:8787/login` — a real form, `EN / فا` |
| one-time link | `simurgh web --link`, or the Security page: single use, 10 minutes |

> Put the panel behind a firewall or an SSH tunnel if the machine is reachable
> from the internet. It is authenticated and hardened, but it is still a control
> plane: `ssh -L 8787:127.0.0.1:8787 root@server` and browse to
> `http://127.0.0.1:8787`.

The top-bar **Log out** button revokes the current form-login session on the
server and expires its cookie. Form sessions are opaque and expire after 12
hours; HTTP Basic credentials remain available for scripts and are managed by
the browser. For server pairing, firewall rules and a disposable end-to-end
response test, see **[Connecting and testing](CONNECTING.md)**.

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
| Pair | create a setup link on an exit or reverse relay; import it on a direct relay or reverse exit. No CLI pairing step is needed |
| Ports | add/remove/enable/disable mappings; changes are written to the config and applied to the running node without a restart |
| Wizard | preview → apply → rollback for a new tunnel; see below |
| Speed test | download/upload against the exit, latency grades, session history in the browser |
| Certificates | request a public multi-domain certificate with `acme.sh`/`certbot`, then view days left and fingerprints |
| Logs | the node's log tail, filter and auto-refresh |
| Security | sessions, lockouts, failed logins, one-time links, the hardening rules |
| Settings | name, log level, engine, tunnel direction and reverse listener, flow control, panel/speed-test ports, default quotas/rate limits and the masked tunnel token |
| Tools | restart / stop / start / reconnect, export the config, download the log, `/metrics` and `/healthz`, check and install updates |

## Pairing entirely from the panel

The universal installer can install an Iranian direct relay with
`unpaired = true`, before its foreign exit exists. The dashboard says **not
paired** and the relay tunnel stays idle; this is an intentional state, not a
failed installation.

### Direct mode

1. On the **exit**, open **Pair → Create a setup link** and press **Create setup
   link**. If the panel is reached through SSH/NAT, enter the public panel
   `IP/domain[:port]` first; that address is what the other server will dial.
2. Copy the `simurgh://…` link to the **relay's Pair page** and press **Pair**.
3. The relay fetches the token, exit endpoint/pool, certificate fingerprint and
   advertised mappings. Its own panel port, engine, limits and existing mappings
   remain intact. The Python relay applies the result live and keeps a backup at
   `relay.toml.bak-pair`.

`POST /api/setup-link {"host": "203.0.113.9:8787"}` creates the link on the
offering server. `POST /api/pair {"link": "simurgh://…"}` imports it on the
receiving server. Both require normal panel authentication. A direct relay
refuses a reverse-exit link and an empty/malformed link. On an already paired
relay, **Replace exit** requires explicit confirmation before replacing its
token, endpoint and fingerprint; local panel settings and ports are preserved.

### Reverse mode

On the relay, choose **Settings → Who dials the tunnel = exit**, set the reverse
listener carrier/port/path, save and restart. The relay generates its tunnel
secret if necessary and its Pair page can now offer a link. Import that link on
the foreign exit's **Connect to a reverse relay** card. This deliberately
replaces the exit's tunnel listeners, preserves its local engine/limits, writes
`exit.toml.bak-pair`, and asks for a restart from **Tools**.

Setup links contain panel credentials; fetched payloads contain the tunnel
secret. Use a private channel and a firewall/SSH tunnel for the panel itself.

## Issuing certificates from the panel

On **Certificates**, enter the primary public domain, optional subdomains (one per line
or comma/space separated), and an optional ACME account email. All names are sent to one
`acme.sh` or `certbot` HTTP-01 request and become SANs on the same certificate. Explicit
names are supported; wildcards require DNS-01 and are rejected by this flow.

* Every name must resolve to this server before requesting the certificate.
* **Standalone** temporarily needs TCP port 80 free and publicly reachable.
* **Webroot** needs an existing web server that serves `/.well-known/acme-challenge/`
  from the entered directory for every requested name.
* Install `acme.sh` or `certbot` first. The panel does not replace a missing client or
  pretend that an attempted issuance succeeded.

`POST /api/certificates/issue` requires normal panel authentication and allows one
issuance at a time. The button stays pending while the real ACME command runs; its result
or actionable error is rendered below the form. Only success wires and saves the
certificate, with a `<config>.bak-cert` backup. Failure leaves the active certificate,
mappings, token and pairing unchanged. The response explicitly requests a restart; the
certificate is not described as active until **Tools → Restart** reloads the listener.
This configures the tunnel certificate, not HTTPS for the panel's own web server.

## The wizard

Building a tunnel by hand means getting the carrier, the port, the path, the
split and the first mapping right on both sides. The wizard turns that into a
plan you can read before anything is written:

1. **Preview** — `POST /api/wizard {"step": "preview", "plan": {...}}` returns
   the diff against the current config (`[exit] carrier tls -> wss`), and writes
   nothing. A refused plan explains itself: an unknown carrier, a port outside
   1–65535, a mapping that is not `listen:target`, a split that is not a number.
2. **Apply** — the current file is copied to `<config>.bak-wizard` and the plan
   is written. Restart from **Tools** when listener or transport changes require it.
3. **Rollback** — the backup is copied back and reloaded. One click, and the old
   file is still there afterwards.

The plan is deliberately small: `carrier` (tls, wss, raw, plain, udp), `port`,
`host`, `path`, `split`, and `mapping` (`listen:target`). Routine operational settings
are available in **Settings**; the config file remains available for advanced
options that do not have a dedicated control.

## Themes, language and phones

* `night` is the default; `dawn` is the light theme. The choice is per browser
  (`localStorage`), as is the language.
* Storage is only a preference cache: if browser policy blocks it, the panel
  uses its defaults and keeps working. An unknown saved view falls back to the
  dashboard instead of hiding every section.
* English and Persian ship together (`simurgh/panel_i18n.py`), `dir` flips to
  `rtl` for Persian, and a test fails if the two tables drift apart.
* Under 720 px the sidebar becomes a bottom bar, the map stacks the wings above
  the body, and the tables scroll sideways — the panel works on a phone, which
  is where most of these servers are first checked.
* It is also installable as a web app: `/manifest.webmanifest` plus the icon.

## What keeps it safe

| Rule | Why |
|---|---|
| strict CSP (`default-src 'none'`), no inline script or external style; the panel's inline style attributes are allowed | no third-party code or styling, without breaking the shipped layout |
| assets served from the package by name | `read_asset` refuses anything with a path in it; no traversal is possible |
| `Cache-Control: no-store` on every API answer and on the code | a browser never shows a stale number or a stale page |
| `X-Frame-Options: DENY` | the panel cannot be framed by a phishing page |
| five explicit failed logins from one address → 300 s lockout (`429`); anonymous challenges do not count | brute-force protection without locking out ordinary visits, on the panel and `/metrics` |
| one-time links hashed, single-use, 10-minute TTL, at most 5 outstanding | a leaked link is worthless; `revoke` drops them all |
| tokens masked wherever they are printed | logs and the panel never print a secret |
| the panel is never in the data path | it can be stopped, restarted or broken without touching traffic |

## Installing an update

Open **Tools → Updates**. **Check for updates** only reads the latest GitHub release. **Install update** asks for confirmation, installs the new Python code and the matching checksummed Go engine, answers the browser, and only then restarts the service. The page polls `/healthz`, requires the reported version to match the release it installed, and reloads only when that new panel is actually available. If the old process remains healthy or the listener never returns, it reports a failed restart instead of silently accepting the old version.

Only one update can run at a time. Updating does not open or rewrite `relay.toml`, `exit.toml`, pairing state, port mappings or panel credentials. Release discovery tries the GitHub API and then a validated `/releases/latest` redirect if the API is rate-limited or unreachable; direct engine downloads still require the release's matching SHA-256 checksum. The same operation is option **17** in `simurgh menu`; automation can use `simurgh update --restart`, which also requires a healthy panel after the restart. On installations without service control, the files are updated and the UI asks for a manual restart instead.

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
| POST | `/api/mappings` | add / remove / enable / disable a mapping, applied live |
| POST | `/api/action` | restart / stop / start / reconnect, or run a speed test |
| POST | `/api/tune` | apply a performance profile (`gaming`, `balanced`, `speed`) |
| POST | `/api/setup-link` | create a pairing link on an exit or reverse relay |
| POST | `/api/pair` | import a direct relay or reverse exit setup link |
| POST | `/api/wizard` | preview / apply / rollback a tunnel plan |
| POST | `/api/login-token` | mint a one-time login link |
| POST | `/api/join` | authenticated setup payload fetched by the receiving server |
| GET | `/api/update` | ask GitHub once whether a newer release exists |
| POST | `/api/update` | install the newest release, then schedule a restart after the response |
| GET | `/api/logs` | the bounded log tail |
| GET | `/login`, `POST /login`, `GET /logout` | the form, the form's answer, and goodbye |
| GET | `/metrics` | Prometheus text for the same numbers (`simurgh_listen_*`, `simurgh_panel_*`, …) |
| GET | `/healthz` | liveness, no credentials, for a watchdog |

`/api/layout` is the one to read if you are writing your own front end: it is
JSON, it is the same data the shipped map draws, and `simurgh/layout.py` is the
whole translation layer between it and a node's `status()`.

## Writing on it

The panel is the normal configuration interface after the universal install.
Every write goes through the same `config.save_*` helpers and validation as the
CLI, with atomic files and backups for pairing/wizard operations. The CLI remains
available for diagnostics, recovery and advanced automation; it is not required
for ordinary pairing, port management or settings.

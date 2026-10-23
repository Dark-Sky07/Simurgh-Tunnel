# Troubleshooting

**English** · [فارسی](TROUBLESHOOTING.fa.md)

Start with the built-in self-check — it inspects the config, the token, the
firewall, the listeners and the log, and prints concrete hints:

```bash
simurgh doctor
simurgh logs -f          # live log (/etc/simurgh/logs/*.log)
```

---

## The panel does not open

* Open the port: `ufw allow 8787` (or `firewall-cmd --add-port=8787/tcp`).
* Check the service: `systemctl status simurgh-relay` / `simurgh status`.
* Before v3.2.2, `simurgh-relay: ok` only meant that systemd accepted the
  restart; it did not prove that the process stayed alive or bound port 8787.
  Current installers and `simurgh update --restart` require a real loopback
  `/healthz` response and refuse to print the success summary without one. On
  an older install, collect the actual startup error with:
  ```bash
  sudo systemctl status simurgh-relay --no-pager -l
  sudo journalctl -u simurgh-relay -n 60 --no-pager
  sudo tail -n 60 /etc/simurgh/logs/relay.log
  sudo ss -lntp | grep -E '(:8787|simurgh)' || true
  ```
* If the log says `error: unrecognized arguments: --home /etc/simurgh` and the
  unit is in an `auto-restart` loop, it was written by v3.2.0 with the global
  option after the role. v3.2.1 fixes the generator and accepts the legacy
  order during upgrades. To repair a relay unit immediately without touching
  its config, run:
  ```bash
  sudo sed -i 's#^ExecStart=/usr/local/bin/simurgh relay --home /etc/simurgh#ExecStart=/usr/local/bin/simurgh --home /etc/simurgh relay#' /etc/systemd/system/simurgh-relay.service
  sudo systemctl daemon-reload
  sudo systemctl restart simurgh-relay
  curl -sS http://127.0.0.1:8787/healthz
  ```
* Panel address and credentials are printed at startup and stored in
  `/etc/simurgh/state.json`; `simurgh status` shows them.
* Prefer a single-use login link from the panel's **Security** page. The legacy
  `http://<server>:8787/?k=<panel-password>` form still works, but immediately
  redirects to `/` so the password does not remain in the address bar.

### Blank dashboard or unexpected `429` on v3.2.2

v3.2.2 could count an anonymous HTTP Basic challenge as a wrong password. Five
ordinary visits or refreshes could therefore block the browser for 300 seconds,
and the next request received `429 too many failed logins` even with the correct
password. The same release could also leave every view hidden when an old
`localStorage` value named a removed view, abort dashboard startup when browser
storage was disabled, and block the shipped inline style attributes through CSP.

**Upgrade to v3.2.3**. It counts only explicit bad credentials as failed logins,
falls back to the dashboard for an unknown saved view, treats browser storage as
optional, and permits the shipped styles without permitting inline scripts. From
the authenticated panel use **Tools → Updates → Install update**, or run:

```bash
sudo simurgh update --restart
```

The update preserves `/etc/simurgh`, pairing, mappings and panel credentials;
do not delete the configuration to clear this browser-side failure. Actual wrong
Basic passwords and form submissions are still rate-limited.

## The tunnel never comes up (direct mode)

0. Is the relay paired at all? `simurgh status` (and the panel) say
   `not paired yet` on a server installed before its exit existed. Create a
   setup link on the exit's **Pair** page and import it on the relay's **Pair**
   page. Nothing has to be re-installed and no pairing CLI is required.
1. `simurgh doctor` on **both** servers.
2. The token must be byte-identical on both sides (copy the setup link again if
   unsure).
3. The tunnel port must be open **on the exit** (`443/tcp` in the example) —
   this is the port `[[listen]]` binds.
4. `simurgh status` on the relay: `last_error` and `bind_errors` tell you what
   the relay last saw.
5. If a certificate fingerprint is pinned, the relay refuses any other
   certificate: create a fresh link on the exit and use **Replace exit** on the
   relay's **Pair** page. To diagnose only, temporarily set
   `insecure_skip_verify = true` in `[exit]` to confirm that the pin was the
   problem, then restore verification.
6. `strict_ports = true` on the exit refuses ports the relay was not offered —
   add the port to `push_ports` or turn strictness off.

## The tunnel never comes up (reverse mode)

In reverse mode the **exit dials the relay**, so the checks are mirrored:

1. On the relay: `dial = "exit"` must be in `relay.toml`, and the `[tunnel]`
   port (e.g. 8443) must be open in the firewall.
2. On the exit: the log must show `dialling the relay at <ip>:<port>`; an error
   like `cannot reach the relay` means the address or the firewall is wrong.
3. A wrong token on either side is refused silently by the listener (it serves
   the decoy page instead) — verify the token.
4. If you replaced the relay's certificate, create a fresh setup link on the
   relay and import it again on the exit's **Pair** page so the fingerprint
   matches; restart the exit afterwards.
5. After `systemctl restart simurgh-relay`, the exit reconnects within a few
   seconds by itself.

## The tunnel is up but the connection is filtered

Iranian filters often classify a connection from its **first TCP packet**. The
tunnel answers that with `split`: the first bytes of the handshake (the TLS
record header, `0x16 0x03 …`) go out in a packet of their own and the rest
follows ~10 ms later, so a stateless filter sees a fragment of a record instead
of a readable ClientHello.

* Fresh installs write `split = 3` in the dialling side's config (`[exit]` on
  the relay, `[[listen]]` on a reverse exit). Check it with
  `grep -n split /etc/simurgh/relay.toml`.
* `simurgh install --split 0` (or `simurgh init --split 0`, or editing the file)
  turns it off — useful when the path has an aggressive middlebox that drops
  fragmented handshakes.
* Keep `split = 3` when the other side is an **older release** (v2.0.0, Python):
  it recognises three bytes as the start of TLS, while a one-byte fragment makes
  it serve the decoy page and the tunnel never comes up. This release and newer
  accept any fragment size.
* It costs one 10 ms pause per tunnel connection, and nothing at all afterwards:
  only the first write of the handshake is split.

## A port on the relay does not work

```bash
simurgh mapping list          # is it enabled? is it bound?
```

* The service must listen on `127.0.0.1:<target_port>` **on the foreign server**
  (that is where the exit connects). Use `--target-host 10.0.0.5` if it lives
  somewhere else.
* If the mapping is new, the panel applies it instantly; the CLI prints
  “saved” — run `simurgh restart` if you edited the file by hand.
* `simurgh mapping add` warns when the port is already in use; check with
  `ss -ltnp | grep <port>`.
* UDP services need `udp = true` (`simurgh mapping add 2096 2096 --udp`).

## Users connect but get no page

* Test locally on the relay: `curl -v http://127.0.0.1:<iran-port>/` — if this
  works, the tunnel and the exit are fine and the problem is the customer side
  (wrong port, wrong SNI, or the panel rejecting the foreign IP).
* Some panels bind only to a specific address; make sure the service listens on
  `127.0.0.1` (or the address you set as `target_host`).
* Panels that check the source IP see `127.0.0.1` unless you enable
  `proxy_protocol` on the exit and the panel supports PROXY protocol.

## Speed is lower than expected

```bash
simurgh speedtest        # measures through the tunnel itself
```

* If the tunnel is fast, the bottleneck is the foreign server, the panel, or the
  customer's link.
* Start from a profile instead of single keys: `simurgh tune speed` raises the
  windows and the number of tunnel connections, `simurgh tune balanced` puts the
  shipped values back (see [CONFIGURATION](CONFIGURATION.md#performance-profiles-simurgh-tune)).
  On very fast links (1 Gbit/s+) it is `stream_window` (e.g. `524288`) that
  matters, with `chunk = 65536` left alone.
* Check `simurgh status` for CPU and for reconnect counters: frequent
  reconnects hurt throughput more than any tuning.

## The relay says “port already in use”

Another process (or an older instance) holds it:

```bash
ss -ltnp | grep -E ':(443|8787)\b'
systemctl stop simurgh-relay    # then start it again
```

## The installer stops at “writing the config…”

An older `install.sh` aborted there with `CONNECTIONS: unbound variable`, and
when the script was piped into `bash` it also printed
`BASH_SOURCE[0]: unbound variable`. Update it and run it again — current
versions preserve the existing configuration and refresh the program and
systemd units, so nothing has to be removed first. Only an explicit `--force`
replaces config, and a timestamped backup is written first:

```bash
curl -fsSL https://raw.githubusercontent.com/Dark-Sky07/Simurgh-Tunnel/main/install.sh | sudo bash
```

A traceback ending in `ModuleNotFoundError: No module named 'tomllib'` is the
same story on Python 3.9/3.10 (Debian 11, Ubuntu 20.04/22.04): that module only
exists from Python 3.11 on. Current versions carry their own TOML reader
(`simurgh/_tomli`), so no PyPI access is needed there.

## “no compiled engine here; the portable python engine will be used”

That is not an error — both engines speak the same protocol and either role may
run either one. The compiled engine needs **Go 1.27+** (see `go/go.mod`), which
is newer than what `apt install golang-go` gives on Ubuntu 22.04 (Go 1.18), so
the installer keeps the Python engine. For the Go engine:

```bash
sudo bash /opt/simurgh-src/tools/build-go.sh    # downloads Go from go.dev if it can
sudo simurgh engine go
```

`tools/build-go.sh` prints the reason when it cannot build (missing toolchain,
a toolchain that is too old, or no route to `go.dev`).

## Nothing works and I want a clean start

```bash
sudo simurgh uninstall --purge     # removes services, configs and certificates
curl -fsSL https://raw.githubusercontent.com/Dark-Sky07/Simurgh-Tunnel/main/install.sh | sudo bash
```

---

## Collecting information for a bug report

```bash
simurgh --version
simurgh doctor
simurgh status
tail -n 100 /etc/simurgh/logs/relay.log     # or exit.log
```

Please remove the token and the addresses before posting logs publicly.

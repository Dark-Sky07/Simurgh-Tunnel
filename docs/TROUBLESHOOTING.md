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
* Panel address and credentials are printed at startup and stored in
  `/etc/simurgh/state.json`; `simurgh status` shows them.
* One-click login: `http://<server>:8787/?k=<panel-password>`.

## The tunnel never comes up (direct mode)

1. `simurgh doctor` on **both** servers.
2. The token must be byte-identical on both sides (copy the setup link again if
   unsure).
3. The tunnel port must be open **on the exit** (`443/tcp` in the example) —
   this is the port `[[listen]]` binds.
4. `simurgh status` on the relay: `last_error` and `bind_errors` tell you what
   the relay last saw.
5. If a certificate fingerprint is pinned, the relay refuses any other
   certificate: re-issue the link after changing certificates, or temporarily
   set `insecure_skip_verify = true` in `[exit]` to confirm that the pin was the
   problem.
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
4. If you replaced the relay's certificate, refresh the exit's config
   (`simurgh join '<new link>' --force`) so the fingerprint matches.
5. After `systemctl restart simurgh-relay`, the exit reconnects within a few
   seconds by itself.

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
* On very fast links (1 Gbit/s+) raise `stream_window` (e.g. `524288`) and keep
  `chunk = 65536`.
* Check `simurgh status` for CPU and for reconnect counters: frequent
  reconnects hurt throughput more than any tuning.

## The relay says “port already in use”

Another process (or an older instance) holds it:

```bash
ss -ltnp | grep -E ':(443|8787)\b'
systemctl stop simurgh-relay    # then start it again
```

## Nothing works and I want a clean start

```bash
sudo simurgh uninstall --purge     # removes services, configs and certificates
sudo bash install.sh --role relay --exit <foreign-ip>:443
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

# Rebuild node setup (development milestone)

This is a development milestone, not a production release. The same role-agnostic installer has a user mode (`sh install.sh`) and a system mode (`sudo sh install.sh --system`). In system mode, a dedicated unprivileged account runs a loopback-only web panel from a systemd service; existing `/etc/simurgh-next` files are archived before ownership changes, and `/etc/simurgh` is never touched. On either mode, roles are selected after installation, and roles marked for panel auto-start start when the panel does. The user-level service option (`--enable-service`) may need systemd lingering for operation before login. The installer does not open firewall ports or upgrade Simurgh v3. Do not use it on a production server yet.

## 1. Prepare an exit certificate

Choose the same `carrier` (`tls` or `wss`) in both node configurations. With WSS, the entry uses an HTTP/1.1 WebSocket upgrade at `/simurgh-tunnel` after TLS; a reverse proxy must pass that upgrade. Do not terminate TLS at an untrusted proxy, because the tunnel secret and data are visible after TLS termination.

The entry verifies both the certificate chain and `server_name` for either carrier; it never disables certificate checks. For an isolated lab, an operator may make a self-signed certificate and copy its public certificate to the entry as the trust anchor:

```sh
mkdir -p -m 700 "$HOME/.config/simurgh-next/tls"
umask 077
openssl req -x509 -newkey rsa:3072 -nodes -days 365 \
  -keyout "$HOME/.config/simurgh-next/tls/server.key" \
  -out "$HOME/.config/simurgh-next/tls/server.crt" \
  -subj "/CN=exit.example.net" \
  -addext "subjectAltName=DNS:exit.example.net" \
  -addext "basicConstraints=critical,CA:TRUE"
install -m 644 "$HOME/.config/simurgh-next/tls/server.crt" \
  "$HOME/.config/simurgh-next/tls/ca.crt"
```

For the system install, copy regular certificate files into `/etc/simurgh-next/tls/` and set their owner to the service account, for example `sudo install -o simurgh-next -g simurgh-next -m 600 server.key /etc/simurgh-next/tls/server.key`. The system installer rejects symlinks in the config tree so it cannot accidentally chown external certificate paths. For a real deployment, prefer a certificate issued by a CA you trust: keep the private key only on the exit and place the CA certificate on the entry. Set `server_name` to a name covered by the certificate.

## 2. Choose the role

On each server, use the same installer command, then choose the role in the panel or terminal menu:

```sh
# User-space development install (choose one option):
sh install.sh
# or add a user-level systemd service:
sh install.sh --enable-service

# Managed Linux/systemd install (runs the panel as simurgh-next):
sudo sh install.sh --system

# For user installs, run the panel or terminal menu:
~/.local/bin/simurgh-next-panel
~/.local/bin/simurgh-next
# System install panel: http://127.0.0.1:8780/ (use SSH forwarding remotely)
```

The panel and menu default to `~/.config/simurgh-next`; set `SIMURGH_NEXT_CONFIG_DIR` or pass `--config-dir` to override it. The web panel binds only to loopback. To open it from your workstation, run `ssh -L 8780:127.0.0.1:8780 user@server` and visit `http://127.0.0.1:8780/` locally. It has a one-time form token, rejects non-loopback Host/Origin values, and does not expose a public bind option. The user service starts the panel; roles marked with “Enable auto-start” start when the panel launches. Keeping the panel available before login may require an administrator to enable systemd lingering for that account.

On the exit, configure the route name and its fixed destination. On the entry, select TCP or UDP, use the same route name and secret, and point `server_name`/`ca_file` at the exit certificate. If the exit wizard generates a secret, copy it over a trusted channel; the entry wizard must be given that same secret.

The wizard writes files mode 0600, uses an atomic replacement, and makes a private backup before replacing a file. The panel lists managed per-role backups and restores only a valid backup for that role, after preserving the current file again. It never modifies `/etc/simurgh`.

An entry can optionally set `monthly_quota_mib` in its JSON config (or use the quota field in either setup UI). The number is MiB per UTC calendar month and counts client payload in both directions, excluding TLS and protocol overhead. The panel/menu display the current upload, download and total. The ledger lives beside the entry config as `entry-usage.json`, mode 0600. TCP flows close when the shared limit is reached; UDP datagrams that do not fit are dropped whole. Blank means unlimited; restart the entry node after changing the setting. A crash before the next one-second checkpoint can lose a small amount of usage accounting.

## 3. Run a node manually

```sh
~/.local/bin/simurgh-next-node --config "$HOME/.config/simurgh-next/exit.json"
~/.local/bin/simurgh-next-node --config "$HOME/.config/simurgh-next/entry.json"
```

For UDP, use the same entry command with the UDP config. The terminal command runs in the foreground and stops with Ctrl+C. To test the configured entry path without sending user payload, use `simurgh-next-node --check --config "$HOME/.config/simurgh-next/entry.json"`, the terminal menu's **Check the entry-to-exit path** action, or **Check route** in the local panel. For TCP, this verifies the TLS/WSS session and that the fixed destination accepts a TCP connection. For UDP, it verifies only tunnel association setup; no test datagram is sent, so UDP application reachability remains unverified. On a TCP entry, measure synthetic tunnel download throughput with `simurgh-next-node --speed-test-mib 4 --config "$HOME/.config/simurgh-next/entry.json"`, the terminal menu, or the panel's **Speed test · 4 MiB** button. CLI tests are bounded to 1–16 MiB. This does not represent end-to-end application performance. The loopback web panel can start and stop node processes launched by that panel instance; normal panel shutdown (Ctrl+C or SIGTERM) stops those children. The panel's **Check for updates** action only checks the namespaced GitHub release feed; apply updates from the terminal menu or with `simurgh-next-update --apply`. For system mode, use `sudo simurgh-next-update --apply --mode system`. The updater downloads the sdist, verifies the published SHA-256 manifest, rejects unsafe archive paths, then invokes the preserving installer; previous code releases are retained and service activation failure triggers rollback. The checksum is not a detached signature, so release publisher compromise is not mitigated. The service runs the panel only; it does not automatically restore or start tunnel roles. Do not assume production hardening.

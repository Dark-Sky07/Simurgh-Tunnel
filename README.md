<div align="center">
  <img src="assets/logo.png" width="160" alt="Simurgh Tunnel logo">

  # Simurgh Tunnel

  **An independent, clean-room tunnel for TCP and UDP forwarding.**

  TLS-protected · Role-agnostic setup · Loopback-only control panel
</div>

---

> **Development status:** This is the new independent implementation, version 0.1.0. It is not compatible with the previous Simurgh implementation or any other tunnel protocol, and it is not yet production-ready. The old code has been removed from this branch; this project does not modify or remove existing `/etc/simurgh` data on any host.

## What it does

Simurgh Tunnel connects an **entry** node to an **exit** node. The entry accepts local TCP or UDP traffic and forwards it over an authenticated TLS connection. The exit maps a named route to a fixed destination; the entry cannot ask the exit to connect to arbitrary hosts or ports.

- TCP forwarding with bidirectional flow and half-close handling.
- UDP forwarding with datagram boundaries, bounded sessions and idle expiry.
- TLS certificate and server-name verification at the entry.
- Shared-secret authentication and fixed named routes.
- Role setup in the panel or terminal menu—never during installation.
- Loopback-only web panel; panel-started nodes are supervised and can opt into auto-start.
- One installer entry point for both per-user and systemd-managed installation.

This is a deliberately small first release. Additional carriers, an authenticated release-update flow, real-host deployment testing and further production hardening remain on the roadmap. Do not deploy it as a replacement for a production tunnel yet.

## Install

Use the same installer on both servers. Choose a role later in the panel or terminal menu.

### Per-user development install

```sh
sh install.sh
# Optional user systemd service for the panel:
sh install.sh --enable-service
```

### System-wide service install

```sh
sudo sh install.sh --system
```

System mode requires Linux with systemd. It runs the panel as the dedicated `simurgh-next` account, keeps versioned code releases, and backs up existing `/etc/simurgh-next` configuration before changing its ownership. It does not read, alter or delete the legacy `/etc/simurgh` directory. The installer does not open firewall ports.

After installation, choose a role in the panel or terminal menu:

```sh
# User install
~/.local/bin/simurgh-next-panel
~/.local/bin/simurgh-next

# System install: access the loopback-only panel through SSH forwarding
ssh -L 8780:127.0.0.1:8780 user@server
```

Then open `http://127.0.0.1:8780/` on your workstation. For the system service, the panel uses `/etc/simurgh-next`; use `sudo systemctl status simurgh-next-panel.service` to inspect it. The panel is intentionally not exposed on a public interface.

## Development and tests

```sh
python3 -m rebuild.tunnel --config rebuild/examples/exit.json
python3 -m rebuild.tunnel --config rebuild/examples/entry.json
python3 -m rebuild.tunnel --config rebuild/examples/entry-udp.json

python3 -m pip install pytest cryptography ruff
python3 -m pytest -q
ruff check rebuild
```

See [`rebuild/SETUP.md`](rebuild/SETUP.md) for certificate preparation and role configuration, and [`rebuild/PROTOCOL.md`](rebuild/PROTOCOL.md) for the independently specified protocol and security boundaries.

## Project identity and compatibility

The name and logo identify this project; the implementation and protocol are an independent clean-room rewrite. Existing installations are not migrated automatically. Keep existing configuration and backups until the new implementation has been validated for your environment.

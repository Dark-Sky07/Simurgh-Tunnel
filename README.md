<div align="center">
  <img src="assets/logo.png" width="160" alt="Simurgh Tunnel logo">

  # Simurgh Tunnel

  **An independent, clean-room tunnel for TCP and UDP forwarding.**

  TLS-protected · Role-agnostic setup · Loopback-only control panel
</div>

---

> **Development status:** This is the new independent implementation, version 0.2.4. It is not compatible with the previous Simurgh implementation or any other tunnel protocol, and it is not yet production-ready. The old code has been removed from this branch; this project does not modify or remove existing `/etc/simurgh` data on any host.

## What it does

Simurgh Tunnel connects an **entry** node to an **exit** node. The entry accepts local TCP or UDP traffic and forwards it through either a verified TLS stream or a WebSocket-over-TLS (WSS) carrier. The exit maps a named route to a fixed destination; the entry cannot ask the exit to connect to arbitrary hosts or ports.

- TCP forwarding with bidirectional flow and half-close handling.
- UDP forwarding with datagram boundaries, bounded sessions and idle expiry.
- TLS certificate and server-name verification at the entry for both carriers.
- Per-role carrier selection: direct TLS stream or WSS with strict HTTP upgrade and bounded WebSocket frames.
- Shared-secret authentication and fixed named routes.
- Role setup in the panel or terminal menu—never during installation.
- Loopback-only web panel; panel-started nodes are supervised and can opt into auto-start.
- Config backups can be reviewed and restored through the panel; restoration validates the role and backs up the current file first.
- One installer entry point for both per-user and systemd-managed installation.
- Namespaced release checks in the panel, plus terminal update installation with SHA-256 asset verification.
- An entry-side route check (`simurgh-next-node --check --config ...`) verifies TLS/WSS authentication and whether the configured TCP destination accepts a connection. For UDP it confirms tunnel association setup only; it does not send a probe datagram to the destination.
- A bounded synthetic download speed test is available for TCP entries from the CLI (`--speed-test-mib 4`), terminal menu, and local panel. It measures tunnel capacity, not end-to-end application throughput; UDP is intentionally unsupported.

This is a deliberately small development release. Detached signatures or build attestations for updates, real-host deployment testing and further production hardening remain. WSS is an interoperability option, not a promise of concealment or availability. Do not deploy this as a production replacement yet.

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

## Updates and trust limits

The panel can check for releases tagged `simurgh-next-vX.Y.Z`; the terminal menu can download, verify and install one. The standalone command is `simurgh-next-update --apply`; for a system install, run `sudo simurgh-next-update --apply --mode system` from an administrator terminal. Updates keep previous code releases and use the same config-preserving installer. The updater validates the namespaced release, restricts downloads to GitHub HTTPS hosts, verifies the source archive against the published SHA-256 manifest, and rejects unsafe archive paths. The checksum is published alongside the archive, not signed by a separate key; it protects against corruption and mismatched assets, but does not protect against a compromised release publisher. Review releases and keep backups.

## Development and tests

```sh
python3 -m rebuild.tunnel --config rebuild/examples/exit.json
python3 -m rebuild.tunnel --config rebuild/examples/entry.json
python3 -m rebuild.tunnel --config rebuild/examples/entry-udp.json
python3 -m rebuild.tunnel --check --config rebuild/examples/entry.json
python3 -m rebuild.tunnel --speed-test-mib 4 --config rebuild/examples/entry.json

python3 -m pip install pytest cryptography ruff
python3 -m pytest -q
ruff check rebuild
```

See [`rebuild/SETUP.md`](rebuild/SETUP.md) for certificate preparation and role configuration, and [`rebuild/PROTOCOL.md`](rebuild/PROTOCOL.md) for the independently specified protocol and security boundaries.

## Project identity and compatibility

The name and logo identify this project; the implementation and protocol are an independent clean-room rewrite. Existing installations are not migrated automatically. Keep existing configuration and backups until the new implementation has been validated for your environment.

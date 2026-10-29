<div align="center">
  <img src="assets/logo.png" width="160" alt="Simurgh Tunnel logo">

  # Simurgh Tunnel

  **An independent, clean-room tunnel for TCP and UDP forwarding.**

  TLS-protected · Role-agnostic setup · Loopback-only control panel
</div>

---

> **Development status:** This is the new independent implementation, version 0.2.13. It is not compatible with the previous Simurgh implementation or any other tunnel protocol, and it is not yet production-ready. The old code has been removed from this branch; this project does not modify or remove existing `/etc/simurgh` data on any host.

## What it does

Forward mode connects an **entry** node to an **exit** node. Reverse mode instead has a private-side agent initiate an outbound TLS session to a public hub, which exposes a separate TCP listener and forwards only to that agent's fixed local target. Both are independent, non-interoperable tunnel modes.

- TCP forwarding with bidirectional flow and half-close handling; a half-closed flow with no further data progress is closed after two minutes.
- UDP forwarding with datagram boundaries, bounded sessions and idle expiry.
- Reverse TCP forwarding: a private-side agent dials a verified TLS hub and forwards public hub connections only to its configured fixed local target. Direct TLS only; one route/agent per hub; sessions are bounded.
- Optional client-session cap per source IP on TCP/UDP entry listeners and the reverse hub's public listener (1–512, default 128), in addition to global session bounds.
- TLS certificate and server-name verification at the entry for forward carriers and at the reverse agent for hub connections.
- Per-role carrier selection: direct TLS, WSS with strict HTTP upgrade and bounded WebSocket frames, or HTTP/2 (H2) with one bounded request stream per tunnel connection. The installer privately stages H2 and Sigstore runtime dependencies for each release.
- Shared-secret authentication and fixed named routes.
- Role setup in the panel or terminal menu—never during installation.
- Loopback-only web panel; panel-started nodes can opt into auto-start and bounded crash recovery (up to five restart attempts per five-minute window with exponential backoff). Manual stops are never retried.
- Optional HTTPS webhook notifications for node lifecycle and restart events. URLs are private, DNS results are pinned to public IPs, redirects are not followed, and payloads contain no tunnel secrets.
- Config backups can be reviewed and restored through the panel; restoration validates the role and backs up the current file first.
- One installer entry point for both per-user and systemd-managed installation.
- Namespaced release checks in the panel, plus terminal update installation with SHA-256 asset verification.
- An entry-side route check (`simurgh-next-node --check --config ...`) verifies TLS/WSS/H2 authentication and whether the configured TCP destination accepts a connection. For UDP it confirms tunnel association setup only; it does not send a probe datagram to the destination.
- A bounded synthetic download speed test is available for TCP entries from the CLI (`--speed-test-mib 4`), terminal menu, and local panel. It measures tunnel capacity, not end-to-end application throughput; UDP is intentionally unsupported.
- Entry traffic is counted by direction and shown in the panel/menu. An optional `monthly_quota_mib` setting applies a UTC-month payload limit; TCP flows stop at the boundary and UDP datagrams are never partially forwarded. State is private (`0600`) and periodically persisted.

This remains a development release, not feature parity or production readiness. Reverse forwarding currently supports TCP over direct TLS with one configured hub route and one agent; it is not a general private mesh and still needs real-host deployment validation. WSS and H2 are interoperability carriers, not promises of concealment or availability. Do not deploy this as a production replacement yet.

## Install

Use the same installer on both servers. Choose a role later in the panel or terminal menu. Both installer modes privately stage the H2 and Sigstore dependencies per release and need access to the configured Python package index.

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

The panel can check for releases tagged `simurgh-next-vX.Y.Z`; the terminal menu can download, verify and install one. The standalone command is `simurgh-next-update --apply`; for a system install, run `sudo simurgh-next-update --apply --mode system` from an administrator terminal. Updates keep previous code releases and use the same config-preserving installer. The updater validates namespaced releases, restricts downloads to GitHub HTTPS hosts, checks the source archive against its SHA-256 manifest, and verifies the archive's Sigstore build provenance against this repository, the `Release Python package` workflow, the `release` event, and the exact version tag before installation. The verifier uses the Sigstore trust root bundled with the privately installed verifier and verifies offline, so update checks need no Sigstore service access beyond the GitHub release assets. Missing or invalid provenance is a hard failure; unsafe archive paths are rejected. This helps detect substituted release assets, but does not protect against compromise of the repository, workflow, authorized maintainers, or the bundled verifier trust root. Review releases and keep backups.

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

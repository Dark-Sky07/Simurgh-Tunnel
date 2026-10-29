# Simurgh Tunnel — independent rebuild

This directory is a clean-room implementation, not a copy of or protocol-compatible with any other project. It deliberately uses a small, independently specified TLS transport and JSON configuration. The previous implementation has been removed from this branch at the owner’s request. Existing installations and `/etc/simurgh` are not migrated, deleted or rewritten by this work.

## First milestone

The initial core provides authenticated TLS forwarding for TCP and UDP:

- **entry** binds a local TCP or UDP listener and reaches the configured exit through TLS;
- **exit** validates the shared secret and maps a configured route name to a fixed destination; peers cannot choose arbitrary hosts or ports;
- TCP flows use a framed bidirectional stream and preserve half-close behavior;
- UDP datagrams retain message boundaries and use bounded per-client sessions with an idle timeout;
- the entry verifies the exit certificate and DNS name;
- no third-party runtime dependencies.

This milestone is intentionally not advertised as feature parity or production-ready. The role-agnostic installer supports both user-space and systemd system installs; role setup remains in the loopback panel and terminal menu, and the panel can start/stop only the child processes it owns. Configured roles can opt into starting when the panel launches. Broader network modes, in-panel release updates, real-server deployment validation and production hardening remain before production deployment or migration should be considered.

## Run from source

Use the same role-agnostic installer on both servers; it never asks for a role. For a user-space development install choose one of:

```sh
sh install.sh
# optionally enable the panel in the current user's systemd manager
sh install.sh --enable-service
```

For a managed Linux host with systemd, the same installer also supports a root-owned service install:

```sh
sudo sh install.sh --system
```

System mode runs the panel as the dedicated `simurgh-next` account, backs up existing `/etc/simurgh-next` content before permission changes, and never accesses or alters the legacy `/etc/simurgh`.

Then choose a role in either UI:

```sh
~/.local/bin/simurgh-next-panel
# or, for terminal setup, status and foreground run actions
~/.local/bin/simurgh-next
```

The panel defaults to `~/.config/simurgh-next`, binds only to loopback, and does not need a public port. For a remote server, use an SSH local forward (`ssh -L 8780:127.0.0.1:8780 user@server`).

For development, launch a node from one of the sample configs:

```sh
python3 -m rebuild.tunnel --config rebuild/examples/exit.json
python3 -m rebuild.tunnel --config rebuild/examples/entry.json
python3 -m rebuild.tunnel --config rebuild/examples/entry-udp.json
```

The wizard does not touch `/etc/simurgh`. See `SETUP.md` for the test certificate and run steps, and `PROTOCOL.md` for the wire contract and security limits. The protocol is not compatible with other tunnel formats.
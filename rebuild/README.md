# Simurgh Tunnel — independent rebuild

This directory contains the clean-room implementation, not a copy of or protocol-compatible with any other project. It deliberately uses an independently specified TLS protocol with direct-TLS, WebSocket-over-TLS, and HTTP/2-over-TLS carriers, plus JSON configuration. The previous implementation has been removed from this branch at the owner’s request. Existing installations and `/etc/simurgh` are not migrated, deleted or rewritten by this work.

## First milestone

The current core provides authenticated direct-TLS, WSS, and HTTP/2 forwarding for TCP and UDP:

- **entry** binds a local TCP or UDP listener and reaches the configured exit through TLS;
- **exit** validates the shared secret and maps a configured route name to a fixed destination; peers cannot choose arbitrary hosts or ports;
- TCP flows use a framed bidirectional stream and preserve half-close behavior; a half-closed flow with no further data progress is closed after two minutes, and forwarded sockets request best-effort OS keepalive;
- UDP datagrams retain message boundaries and use bounded per-client sessions with an idle timeout; entry datagrams have configurable per-source-IP, per-direction rate limits (5,000 packets/s by default, with a two-second burst allowance);
- entry payload bytes are counted by direction, with an optional UTC-month MiB quota enforced for TCP/UDP and visible in the local panel/menu;
- the entry route diagnostic sends one synthetic 16-byte UDP frame through the authenticated tunnel and verifies the echo without contacting the configured UDP destination;
- carriers are direct TLS, WebSocket binary streams over verified TLS, or one HTTP/2 request stream per TLS connection; the entry verifies the exit certificate and DNS name for every carrier;
- the HTTP/2 carrier uses the `h2` runtime package, and the updater uses `sigstore` to verify GitHub build provenance; both are installed privately alongside the application by either installer;
- reverse TCP/UDP forwarding adds a direct-TLS hub and an outbound agent behind NAT. The hub exposes one public listener and one named route; the agent forwards only to its fixed local target. Concurrent client sessions are bounded; a configurable per-source-IP session cap is available on entry and reverse public listeners (1–512, default 128), with UDP datagram rate caps on entry and reverse UDP listeners. One agent per hub is currently supported. Reverse WSS/H2 carriers are not implemented.

This milestone is intentionally not advertised as feature parity or production-ready. The role-agnostic installer supports both user-space and systemd system installs; role setup remains in the loopback panel and terminal menu, and the panel can start/stop only the child processes it owns. Configured roles can opt into starting when the panel launches, with bounded automatic recovery after failures; the panel can also send optional, secret-free lifecycle notifications to a public HTTPS webhook. The loopback panel checks namespaced releases, and the terminal menu/`simurgh-next-update` can download a source release, check its SHA-256 manifest, verify its Sigstore build provenance against the expected repository/workflow/tag, and run the same preserving installer. Verification uses the trust root bundled with the privately installed Sigstore package and runs offline, requiring no Sigstore-service access. This reduces risk from substituted release assets but does not prevent compromise of the repository, workflow, authorized maintainers, or bundled trust root. The panel can restore validated configuration backups without discarding the current config. Real-server deployment validation and production hardening remain before production migration should be considered.

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
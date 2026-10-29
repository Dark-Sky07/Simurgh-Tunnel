# Simurgh Rebuild transport v1

This is a new implementation contract for this repository. It is independently designed, not compatible with another tunnel format or protocol.

## Connection and authentication

Each entry TCP flow opens one TLS connection to the configured exit. A UDP client source address gets a bounded, idle-expiring TLS association. The entry verifies the exit certificate against its CA file and verifies the configured DNS name. TLS 1.3 is preferred; TLS 1.2 is the minimum.

Before relaying, the entry sends one UTF-8 JSON object followed by LF. TCP uses `open`; UDP uses `udp`:

```json
{"version":1,"op":"open","route":"web","secret":"…"}
```

```json
{"version":1,"op":"udp","route":"dns","secret":"…"}
```

The exit compares the shared secret in constant time and accepts only a route present in its local allowlist. A route maps to a fixed host and port configured at the exit; the peer cannot supply a destination address. The exit responds with one JSON line, either `{"ok":true}` or a generic rejection.

## Data frames

After a successful handshake, each frame has a five-byte header: one type byte and a four-byte unsigned big-endian payload length. TCP DATA (type 1) carries 1–65,536 bytes. FIN (type 2) has zero length. UDP DATA (type 3) carries one non-empty datagram of 1–65,507 bytes. Empty datagrams are dropped. Unknown types, oversized frames, truncation and malformed handshakes terminate the connection.

For TCP, DATA frames carry stream chunks and FIN propagates a half-close to the destination socket. For UDP, each UDP DATA frame represents exactly one non-empty datagram; the exit returns each received datagram as its own UDP DATA frame. The UDP entry bounds each per-client queue to 128 datagrams and expires idle associations after 45 seconds.

## Security and current limits

- TLS handshakes time out after ten seconds; the application handshake is capped at 8 KiB and has a five-second deadline.
- TCP sessions and UDP associations are capped at 1,024 per process. Per-association UDP queues hold at most 128 datagrams and expire after 45 idle seconds.
- Secrets travel only inside the verified TLS channel; configs contain secrets and should be mode 0600.
- Destinations are configured and allowlisted on the exit, never selected by the connecting peer.
- There is no unauthenticated fallback, protocol downgrade or arbitrary destination override.
- The current UDP path uses one TLS association per local source address; reconnect policy, endpoint migration and hostile-traffic rate limits are not implemented yet.
- The loopback panel writes initial config and exposes status/start/stop controls for node processes launched by that panel instance. Per-role auto-start preferences are stored privately and apply whenever the panel launches. The role-agnostic installer has user and root/systemd modes; system mode backs up `/etc/simurgh-next` before changing ownership and runs the panel as a dedicated account. Previous code releases are retained, and failed service restarts restore the previous release. In-panel release updates, managed config restore and real-host production validation remain future work.

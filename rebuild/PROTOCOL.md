# Simurgh Rebuild transport v1

This is a new implementation contract for this repository. It is independently designed, not compatible with another tunnel format or protocol.

## Connection and authentication

Each entry TCP flow opens one verified TLS connection to the configured exit. A UDP client source address gets a bounded, idle-expiring TLS association. Both ends must select the same carrier in their local configuration:

- `tls`: the application handshake and frames are carried directly over TLS.
- `wss`: after TLS, the entry performs an HTTP/1.1 WebSocket upgrade on `/simurgh-tunnel`; the exit validates the request and the RFC WebSocket accept value. The data protocol is carried in complete binary WebSocket messages. Client frames are masked, server frames are not, and extensions/compression and fragmented messages are rejected.
- `h2`: TLS negotiates ALPN `h2`; the entry sends one `POST /simurgh-tunnel` request with `application/octet-stream`, and the exit returns status 200. The tunnel's existing byte stream is carried in HTTP/2 DATA frames with HTTP/2 flow control; each TLS connection has exactly one request stream and no multiplexing. The `h2` package is installed privately by the role-agnostic installers.

WSS and H2 are interoperability carriers, not anonymity or censorship-evasion guarantees. The entry verifies the exit certificate against its CA file and verifies the configured DNS name for every carrier. TLS 1.3 is preferred; TLS 1.2 is the minimum. HTTP/2 and WebSocket header lists are limited to 8 KiB; WebSocket binary messages are limited to 65,541 bytes (one complete tunnel frame), and carrier stream buffers are capped at 256 KiB per connection.

Before relaying, the entry sends one UTF-8 JSON object followed by LF. TCP uses `open`; UDP uses `udp`:

```json
{"version":1,"op":"open","route":"web","secret":"…"}
```

```json
{"version":1,"op":"udp","route":"dns","secret":"…"}
```

The exit compares the shared secret in constant time and accepts only a route present in its local allowlist. A route maps to a fixed host and port configured at the exit; the peer cannot supply a destination address. The exit responds with one JSON line, either `{"ok":true}` or a generic rejection.

A TCP entry may also request the bounded diagnostic operation `speed`, with the same version, route, and secret fields plus an integer `bytes` from 1 through 16,777,216. The exit returns the accepted byte count and sends that many synthetic bytes in DATA frames followed by FIN; it does not connect to the route target during this operation. The entry verifies the exact count, then acknowledges completion with FIN before the exit closes the session, and reports download throughput. This measures the authenticated tunnel path, not application performance. UDP configs cannot use the speed test.

## Reverse TCP mode (development feature)

Reverse mode is a separate direct-TLS TCP service path. A reverse agent behind NAT initiates a TLS control connection to a reverse hub, validates the hub certificate and DNS name, and sends a version-1 `register` JSON line containing the shared secret and one configured route. The hub accepts only the route configured locally; it does not receive or choose the agent's target address. One agent may be registered per hub process. A version-1 `probe` message uses the same TLS, route, and secret checks but is non-registering: the hub acknowledges it without changing the active-agent state. The reverse-agent diagnostic then separately checks whether the configured local TCP target accepts a connection; neither probe sends application payload or proves application-level health.

For every incoming raw TCP client on the hub's separate public service listener, the hub creates a random channel token and sends an `open` control message to the agent. The agent connects only to its fixed local target from its own configuration, then opens a second verified TLS connection back to the hub and presents the channel token and shared secret in a `data` JSON line. The hub pairs that callback with the waiting public connection. Both directions then use the bounded DATA/FIN frame format below, preserving TCP half-close behavior. Failed target connections are reported without exposing target details to the public client. The agent sends control pings every 25 seconds; an idle control channel is retired after 75 seconds.

The hub's public service listener is raw TCP and is intentionally separate from the TLS control listener. The tunnel protects only agent-to-hub traffic; client-to-hub traffic must use application-layer TLS when confidentiality is required. Reverse mode currently supports one route and one agent per hub, direct TLS only, and TCP only. It is not a mesh, and it does not implement reverse UDP, WSS/H2, or multi-agent routing. Each listener independently enforces a 1,024-session limit. The public TCP listener applies the hub's configured per-source-IP client session cap (1–512, default 128); the TLS control listener keeps its fixed per-peer limit.

## Data frames

After a successful handshake, each frame has a five-byte header: one type byte and a four-byte unsigned big-endian payload length. TCP DATA (type 1) carries 1–65,536 bytes. FIN (type 2) has zero length. UDP DATA (type 3) carries one non-empty datagram of 1–65,507 bytes. Empty datagrams are dropped. Unknown types, oversized frames, truncation and malformed handshakes terminate the connection.

For TCP, DATA frames carry stream chunks and FIN propagates a half-close to the destination socket. For UDP, each UDP DATA frame represents exactly one non-empty datagram; the exit returns each received datagram as its own UDP DATA frame. The UDP entry bounds each per-client queue to 128 datagrams and expires idle associations after 45 seconds.

## Security and current limits

- TLS handshakes time out after ten seconds; the application handshake is capped at 8 KiB and has a five-second deadline.
- Established TCP sessions are capped at 1,024 per listener. TCP entry and reverse public listeners can additionally cap concurrent client sessions per source IP with `client_session_limit` (1–512, default 128); exit and reverse control listeners retain a fixed 128-per-peer cap. The exit and reverse-control gates run after TLS completes and are not defenses against pre-handshake floods. UDP associations are capped at 1,024 per entry process, with the same configurable per-source-IP session cap; each association queue holds at most 128 datagrams and expires after 45 idle seconds. UDP entry datagrams are separately token-bucket limited per source IP and direction using `udp_rate_limit_pps` (1–100,000, default 5,000) with a two-second burst allowance; excess datagrams are dropped before queueing. Once either TCP forwarding direction ends, the other direction has a 120-second no-progress grace period; payload movement refreshes that timer, and ordinary fully open but idle TCP sessions are not timed out by this rule. Forwarded TCP sockets also request OS keepalive (60-second idle, 15-second interval, four probes where the platform exposes these options). This is best-effort kernel behavior, not an application-level idle timeout, and actual detection timing varies by OS and network path.
- Secrets travel only inside the verified TLS channel; configs contain secrets and should be mode 0600.
- Destinations are configured and allowlisted on the exit, never selected by the connecting peer.
- There is no unauthenticated fallback, protocol downgrade or arbitrary destination override.
- The UDP path uses one TLS association per local source address. The configurable datagram rate cap applies after a packet reaches the entry's UDP listener; it is not an upstream volumetric-DDoS shield. UDP association reconnect policy and endpoint migration are not implemented yet.
- Optional entry-side `monthly_quota_mib` limits forwarded client payload by UTC calendar month; upload and download count toward the same quota, while TLS/frame overhead is excluded. TCP sessions are closed at the boundary; UDP packets are forwarded atomically or dropped. The entry writes 0600 usage state to `entry-usage.json`, flushing at least once per second and immediately at quota exhaustion and shutdown (a crash below quota can therefore lose roughly one second of metering).
- The loopback panel writes initial config and exposes status/start/stop controls for node processes launched by that panel instance. Per-role auto-start preferences are stored privately and apply whenever the panel launches; enabled nodes receive in-process crash monitoring with exponential backoff and a five-attempt circuit breaker per five-minute window, while explicit manual stops are not retried. The role-agnostic installer has user and root/systemd modes; system mode backs up `/etc/simurgh-next` before changing ownership and runs the panel as a dedicated account. Previous code releases are retained, and failed service restarts restore the previous release. The panel checks namespaced releases and directs operators to the terminal update command; the terminal updater checks the source archive against its SHA-256 manifest and verifies its Sigstore GitHub Actions provenance against the expected repository, release workflow, event, and version tag before invoking the config-preserving installer. Verification is offline using the Sigstore trust root bundled with the private verifier. This does not protect against compromise of that repository, workflow, authorized maintainers, or bundled trust root. The panel can restore a validated adjacent config backup after first preserving the current config. The panel can optionally deliver low-volume node lifecycle events to a configured HTTPS webhook; the webhook URL is stored privately, DNS is restricted to public IPs and pinned for delivery, redirects are not followed, and payloads exclude tunnel secrets. Real-host deployment validation and production hardening remain future work.

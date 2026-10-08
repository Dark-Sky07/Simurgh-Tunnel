"""Simurgh Tunnel v2 — the tunnel between an Iranian relay server and one or
more foreign (Kharej) exit servers.

Design goals (in order):

1. **Transparent pass-through.** The tunnel never terminates the user's TLS.
   A VPN client (v2ray/xray with VLESS/VMess/Trojan/Reality/...) is pointed at
   the Iranian server instead of the foreign one; every byte reaches the real
   panel untouched.  That is why "the same config, only the address changes"
   works.
2. **Looks like nothing.** Three carriers with very different wire signatures:
   ``tls`` (real certificate + decoy website + Trojan-style auth), ``raw``
   (noise + X25519 + AEAD, for links where TLS is impossible) and ``plain``
   (internal links only).
3. **One connection carries everything.** A stream multiplexer means the
   Iran <-> Kharej link holds a single long-lived session instead of hundreds
   of short ones — far less visible to DPI and far cheaper on the kernel.
4. **Fast.** The data path does the strict minimum: kernel TLS through OpenSSL,
   bulk 64 KiB reads, no double encryption of already-encrypted VPN traffic,
   credit-based flow control so memory stays flat under load.
5. **Easy.** One-command installer, Persian text menu, Persian web panel,
   one-string setup links.
"""

__version__ = "2.0.0"
__product__ = "Simurgh Tunnel"

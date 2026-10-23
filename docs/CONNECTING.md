# Connect the two servers and test the tunnel

**English** · [فارسی](CONNECTING.fa.md)

This guide pairs two existing servers from the web panel, then verifies the path
with a real application response, throughput measurements and traffic counters.
It does not require deleting or rewriting `/etc/simurgh`.

Replace `EXIT_IP` and `IRAN_IP` in every example with your servers' public IP
addresses.

## 1. Understand the roles and three different ports

In the default **Direct** mode:

```text
user ──► Iranian relay:8443 ── TLS tunnel ──► foreign exit ──► 127.0.0.1:8443
                             tunnel port:443
```

- **Relay / Iran:** accepts user traffic and dials the exit.
- **Exit / foreign:** accepts the tunnel and forwards to the target service.
- **Tunnel port:** for example `443/TCP` on the exit; only relay-to-exit traffic.
- **User port:** for example `8443/TCP` on the relay; your users connect here.
- **Panel port:** `8787/TCP` by default; this is not the tunnel port and should
  not remain open to the whole internet.

If `443` is occupied on the exit, choose a free port such as `4443` in the exit's
Settings and in its firewall. Start with `tls` and one tunnel connection; change
other carriers or performance settings only after establishing a working
baseline.

## 2. Install or update both servers

On an existing installation, use **Tools → Check for updates** and install the
latest release. For a fresh installation, run **the same single command on both
servers**. The installer detects the role; pairing and connection settings are
completed in the panel:

```bash
curl -fsSL https://raw.githubusercontent.com/Dark-Sky07/Simurgh-Tunnel/main/install.sh | sudo bash
```

Back up the existing configuration before significant changes:

```bash
sudo cp -a /etc/simurgh "/etc/simurgh.backup-$(date +%Y%m%d-%H%M%S)"
```

Do not use `--force` or delete `/etc/simurgh` as a troubleshooting step.

## 3. Open the panels securely

If the panels are not exposed publicly, create two SSH forwards in two local
terminals:

```bash
# Foreign panel becomes http://127.0.0.1:18787
ssh -L 18787:127.0.0.1:8787 root@EXIT_IP

# Iranian panel becomes http://127.0.0.1:28787
ssh -L 28787:127.0.0.1:8787 root@IRAN_IP
```

These localhost addresses are for your browser only. Do **not** put
`127.0.0.1` in the Public panel address used for pairing: the other server must
be able to reach that address.

## 4. Pair in Direct mode

### A. Prepare the foreign exit

1. Confirm that its dashboard shows the **exit** role.
2. In **Settings**, check the `tls` carrier and tunnel port (for example `443`).
   If you change them, save and use **Tools → Restart**.
3. Allow the relay to reach the tunnel port. UFW example:

   ```bash
   sudo ufw allow from IRAN_IP to any port 443 proto tcp
   ```

   Check the provider's cloud firewall/security group separately if present.
4. Confirm that the target service works on the exit itself. For port `8443`:

   ```bash
   sudo ss -lntp | grep ':8443 '
   curl -v --connect-timeout 5 http://127.0.0.1:8443/
   ```

   Use HTTPS instead if that is the target application's protocol. A tunnel
   cannot repair a stopped or misconfigured target service.

### B. Create the setup link on the exit

1. Open **Pair → Create a setup link**.
2. Enter `EXIT_IP:8787` (or the real public panel domain and port) as **Public
   panel address**.
3. Create and copy the `simurgh://…` link through a private channel. It contains
   panel credentials; never post it publicly or include it in a screenshot.

The relay must reach the exit panel while pairing. If the panel is firewalled,
temporarily allow only the relay's IP:

```bash
sudo ufw allow from IRAN_IP to any port 8787 proto tcp
```

### C. Import the link on the Iranian relay

1. Open **Pair**, paste the link into **Setup link**, and press **Pair**.
2. Success means the relay fetched and saved the token, endpoints, listeners and
   certificate fingerprint; no manual token copying is needed.
3. The tunnel starts dialling immediately. Dashboard should become **up** and
   show RTT.

### D. Add the user-facing forward on the relay

In **Ports** on the Iranian panel:

- **Listen port:** the public user port, for example `8443`.
- **Target port:** the service port on the exit, for example `8443`.
- **Protocol:** TCP or UDP, matching the actual service.
- **Name:** a readable label such as `3x-ui` or `HTTPS`.

The default target host is `127.0.0.1`, evaluated **on the exit**. Open the user
port on the relay as required:

```bash
sudo ufw allow 8443/tcp
# A UDP mapping needs a separate UDP rule, for example:
sudo ufw allow 5353/udp
```

## 5. Prove the complete path with a disposable response

This test is independent of 3x-ui or any VPN panel. It proves the user-facing
listener, tunnel and delivery to the exit's loopback.

### A. Run a temporary response on the exit

If port `18080` is free, run this on the exit and leave the terminal open:

```bash
python3 - <<'PY'
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"simurgh-tunnel-ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

HTTPServer(("127.0.0.1", 18080), Handler).serve_forever()
PY
```

### B. Forward the test port from the Iranian panel

Add a TCP mapping in **Ports** with Listen `18080` and Target `18080`, then open
it on the relay:

```bash
sudo ufw allow 18080/tcp
```

### C. Test from a device outside both servers

Run this from a laptop or third server, not only from the relay itself:

```bash
curl -v --connect-timeout 10 http://IRAN_IP:18080/
```

Expected response:

```text
simurgh-tunnel-ok
```

This proves the whole application path. `nc -vz IRAN_IP 18080` only checks a TCP
listener and is not a substitute.

Afterwards:

1. Stop the temporary server on the exit with `Ctrl+C`.
2. Delete the `18080` mapping in Ports.
3. Remove its temporary firewall rule: `sudo ufw delete allow 18080/tcp`.
4. If you temporarily opened the exit panel for pairing, restrict it again:

   ```bash
   sudo ufw delete allow from IRAN_IP to any port 8787 proto tcp
   ```

## 6. Test the real service, throughput and UDP

1. Test the real service using its actual protocol. For an HTTP service on
   `8443`:

   ```bash
   curl -v --connect-timeout 10 http://IRAN_IP:8443/
   ```

2. Compare **Dashboard / Ports** counters before and after; Moved and the
   throughput graph should reflect the traffic.
3. Run a short, controlled test **on the relay**:

   ```bash
   simurgh speedtest --seconds 6 --streams 1
   ```

   Compare `--streams 2` separately later. Avoid heavy tests at busy hours; the
   result depends on the real ISP route, time and both servers' limits.
4. Measure datagram loss and jitter with **Speed test → UDP loss probe**, or on
   the relay:

   ```bash
   simurgh speedtest --seconds 6 --streams 1 --udp
   ```

   `nc -u -vz` alone does not prove UDP forwarding. Test the real UDP service
   and its firewall rules; for DNS, perform a real DNS query.
5. Additional diagnostics:

   ```bash
   simurgh status --detail
   simurgh doctor
   simurgh logs -f
   ```

## 7. Reverse mode: the exit dials the Iranian relay

Reverse mode changes who starts the tunnel connection. **User-facing mappings
remain on the Iranian relay.** Keep the tunnel listener separate from user
ports: this example uses `9443` for the tunnel and `8443` for users.

1. In **Settings on the relay**, set **Who dials the tunnel** to `exit`, choose
   the reverse carrier and listener port `9443`, save, and restart from Tools.
2. Allow only the exit to reach that listener on the relay:

   ```bash
   sudo ufw allow from EXIT_IP to any port 9443 proto tcp
   ```

   Use a UDP rule instead if you deliberately selected the UDP carrier.
3. In the relay's **Pair** page, create a setup link. Public panel address must
   be the panel address the exit can reach; temporarily allow only the exit IP
   to the panel port if necessary.
4. In the exit's **Pair → Connect to a reverse relay**, paste and save the link,
   then restart the exit from Tools.
5. Create user mappings in the relay's **Ports** page and repeat the tests above.

Do not configure Direct and Reverse simultaneously for the same pair. Exactly
one side dials: the relay in Direct mode, the exit in Reverse mode.

## 8. If a test fails

| Symptom | Next check |
|---|---|
| Pairing cannot fetch the link | Public panel address must not be localhost; check peer-IP access to the panel port and link integrity. |
| Tunnel is down | Inspect `last_error` and Logs; check tunnel port, dialler IP, paired token and certificate fingerprint. |
| Tunnel is up but the service fails | Test the target from the exit's loopback; check Target port and TCP/UDP. |
| No listener on the relay | `sudo ss -lntup | grep ':8443 '`; check that the mapping is enabled and inspect bind errors. |
| Listener exists but an external test times out | Check UFW, cloud security groups and ISP/port restrictions; loopback does not prove external reachability. |
| TCP works but UDP does not | Check mapping protocol, UDP firewall rules, the UDP service and UDP passage on that ISP route separately. |
| Repeated reconnects | Inspect logs and measure RTT/loss on that route; change carrier or split one variable at a time with a rollback baseline. |

Filtering and route quality in Iran vary by ISP, province and time. No carrier or
setting guarantees universal connectivity; measure on your own relay and exit.

"""Authenticated setup links for panel-first direct and reverse pairing.

    simurgh://<panel-host>:<panel-port>/setup?u=<user>&p=<pass>#name

The offering server's **Pair** page creates the link. The receiving server
imports it on its own Pair page, fetches the token and tunnel endpoint from the
authenticated panel API, and writes its role's configuration. The pairing CLI
uses the same helpers for advanced automation.

The token travels over HTTP(S) to the panel. When the panel is reachable over
plain HTTP on a public IP, treat the link like a password: send it through a
private channel and restrict panel access with a firewall or SSH tunnel.
"""

from __future__ import annotations

import base64
import json
import socket
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote, unquote, urlparse

from .config import (ExitConfig, ExitEndpoint, ListenSpec, Mapping, RelayConfig)


class LinkError(Exception):
    pass


def _b64e(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _b64d(s: str) -> str:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad).decode()


def _url_host(host: str) -> str:
    """Format a host for a URL, including literal IPv6 addresses."""
    host = str(host).strip()
    return f"[{host.strip('[]')}]" if ":" in host else host


def build_join_link(host: str, panel_port: int, user: str, password: str,
                    name: str = "") -> str:
    q = f"u={quote(user)}&p={quote(_b64e(password))}"
    link = f"simurgh://{_url_host(host)}:{panel_port}/setup?{q}"
    if name:
        link += f"#{quote(name)}"
    return link


def parse_join_link(link: str) -> dict:
    if not link.startswith("simurgh://"):
        raise LinkError("link must start with simurgh://")
    try:
        u = urlparse(link)
        host = u.hostname
        port = u.port or 8787
    except ValueError as exc:
        raise LinkError("bad host or port in link") from exc
    if not host:
        raise LinkError("link has no host")
    if not 1 <= port <= 65535:
        raise LinkError("link port is outside 1-65535")
    if (u.path or "").strip("/") not in ("setup", ""):
        raise LinkError("not a setup link")
    q = parse_qs(u.query)
    user = q.get("u", [""])[0]
    pw_b64 = q.get("p", [""])[0]
    if not (user and pw_b64):
        raise LinkError("link is missing credentials")
    try:
        password = _b64d(pw_b64)
    except Exception as exc:
        raise LinkError("bad credentials in link") from exc
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "name": unquote(u.fragment) if u.fragment else "",
    }


def _unreachable(endpoint: str, exc: Exception) -> LinkError:
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, ConnectionRefusedError):
        detail = "connection refused"
    elif isinstance(reason, socket.gaierror):
        detail = f"DNS lookup failed ({reason})"
    elif isinstance(reason, (socket.timeout, TimeoutError)):
        detail = "connection timed out"
    else:
        detail = str(reason)
    return LinkError(
        f"cannot reach the panel at {endpoint}: {detail}. Check that the offering "
        "panel is running and that this server can reach its panel port through "
        "local and cloud firewalls; use the public panel IP/domain, not the "
        "browser's SSH-tunnel/localhost address."
    )


def fetch_setup(link: str, timeout: float = 15.0, scheme: str = "http") -> dict:
    """Ask the panel for the tunnel configuration (returns a dict)."""
    if scheme not in ("http", "https"):
        raise LinkError("setup links can only use HTTP or HTTPS")
    info = parse_join_link(link)
    endpoint = f"{scheme}://{_url_host(info['host'])}:{info['port']}"
    url = f"{endpoint}/api/join"
    payload = json.dumps({"user": info["user"], "password": info["password"]}).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    # Pairing is server-to-server.  An HTTP_PROXY/http_proxy inherited by the
    # service must not send these credentials through a local proxy or turn a
    # dead proxy into a misleading "panel connection refused" error.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read((1 << 20) + 1)
            if len(raw) > 1 << 20:
                raise LinkError("panel sent an oversized configuration")
            data = json.loads(raw.decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise LinkError("panel rejected the credentials") from exc
        raise LinkError(f"panel answered HTTP {exc.code}") from exc
    except LinkError:
        raise
    except urllib.error.URLError as exc:
        raise _unreachable(endpoint, exc) from exc
    except OSError as exc:
        raise _unreachable(endpoint, exc) from exc
    except Exception as exc:
        raise LinkError(f"cannot read the panel response from {endpoint}: {exc}") from exc
    if not isinstance(data, dict):
        raise LinkError("panel sent an invalid configuration")
    if not data.get("ok"):
        raise LinkError(str(data.get("error") or "panel refused the request"))
    cfg = data.get("relay") or data          # flat payload, or nested under "relay"
    if not isinstance(cfg, dict):
        raise LinkError("panel sent an invalid configuration")
    if not cfg.get("token"):
        raise LinkError("panel sent an incomplete configuration")
    if str(cfg.get("role") or "") == "exit":
        # reverse mode: the relay tells the exit where it waits for it
        if not (cfg.get("listen") or {}).get("address"):
            raise LinkError("panel sent an incomplete configuration")
    elif not (cfg.get("exit") or {}).get("address"):
        raise LinkError("panel sent an incomplete configuration")
    return {**cfg, "link_name": info["name"]}


def relay_payload_from_link(link: str, timeout: float = 15.0) -> dict:
    """Fetch and sanity-check a setup link; refuses a link meant for an exit."""
    payload = fetch_setup(link, timeout=timeout)
    if str(payload.get("role") or "") == "exit":
        raise LinkError("that link is for the exit server, not for the relay; "
                        "use the link created on the exit panel's Pair page")
    return payload


def merge_relay_payload(cfg: RelayConfig, payload: dict) -> dict:
    """Pair an installed-but-unpaired relay with a setup payload.

    Everything this install already chose -- the name, the panel port, the
    engine, the limits, the ports the operator added -- stays exactly as it is.
    Pairing brings the shared secret, the exit endpoint (and its failover pool),
    plus any port the exit advertises that this relay does not forward yet.
    """
    fresh = relay_config_from_payload(payload)
    cfg.token = fresh.token
    cfg.tokens = list(fresh.tokens)
    cfg.exit = fresh.exit
    cfg.pool = list(fresh.pool)
    cfg.unpaired = False
    known = {m.key() for m in cfg.mappings}
    added: list[int] = []
    for mapping in fresh.mappings:
        if mapping.key() in known:
            continue
        cfg.mappings.append(mapping)
        known.add(mapping.key())
        added.append(mapping.listen)
    return {
        "exit": cfg.exit.describe(),
        "mappings": [m.listen for m in cfg.mappings],
        "added": added,
    }


def relay_config_from_payload(payload: dict) -> RelayConfig:
    cfg = RelayConfig(token=str(payload["token"]))
    cfg.name = str(payload.get("name") or "")
    exit_data = payload["exit"]
    cfg.exit = ExitEndpoint(
        carrier=str(exit_data.get("carrier", "tls")),
        address=str(exit_data["address"]),
        port=int(exit_data.get("port", 8443)),
        domain=exit_data.get("domain"),
        path=str(exit_data.get("path", "/ws")),
        fingerprint=exit_data.get("fingerprint"),
        insecure_skip_verify=bool(exit_data.get("insecure_skip_verify", False)),
    )
    for item in payload.get("pool", []):
        cfg.pool.append(ExitEndpoint(
            carrier=str(item.get("carrier", "tls")),
            address=str(item["address"]),
            port=int(item.get("port", 8443)),
            domain=item.get("domain"),
            path=str(item.get("path", "/ws")),
            # fall back to the primary endpoint's pin so a self-signed exit
            # works out of the box on every advertised port
            fingerprint=item.get("fingerprint") or cfg.exit.fingerprint,
        ))
    items = payload.get("mappings")
    if not items:
        # the exit only advertised which ports it forwards: mirror them 1:1
        items = [{"port": int(port), "target": int(port), "name": f"port-{port}"}
                 for port in payload.get("push_ports", [])]
    for item in items:
        port = int(item.get("port") or item.get("listen"))
        cfg.mappings.append(Mapping(
            name=str(item.get("name") or f"port-{port}"),
            listen=port,
            target_port=int(item.get("target") or item.get("target_port") or port),
            target_host=str(item.get("target_host") or item.get("host") or "127.0.0.1"),
            udp=bool(item.get("udp", False)),
        ))
    return cfg


# --------------------------------------------------------------------------
# reverse mode (relay dial = "exit"): the relay produces the payload
# --------------------------------------------------------------------------


def reverse_payload(relay_cfg: RelayConfig, host: str,
                    fingerprint: str | None = None) -> dict:
    """What the exit needs to dial this relay and push traffic to it."""
    tunnel = relay_cfg.tunnel
    return {
        "ok": True,
        "role": "exit",
        "version": None,          # filled in by the caller (panel/cli)
        "name": relay_cfg.name or "relay",
        "token": relay_cfg.token,
        "listen": {              # where the relay waits for this exit
            "carrier": tunnel.carrier,
            "address": host,
            "port": tunnel.port,
            "path": tunnel.path,
            "fingerprint": fingerprint,
            "insecure_skip_verify": False,
        },
        "push_ports": [],
        "note": ("Keep this payload secret: the token is inside it. "
                 "Import it on the foreign exit panel's Pair page."),
    }


def exit_config_from_payload(payload: dict) -> ExitConfig:
    """Build ``exit.toml`` from a relay payload (reverse mode)."""
    relay = payload.get("listen") or payload.get("relay") or {}
    address = str(relay.get("address") or "")
    if not address:
        raise ValueError("the payload has no relay address")
    carrier = str(relay.get("carrier", "tls")).lower()
    cfg = ExitConfig(token=str(payload["token"]),
                     name=str(payload.get("name") or ""))
    cfg.cert_auto = False        # a reverse exit only dials out; no certificate
    cfg.listen = [ListenSpec(
        carrier=carrier,
        host="0.0.0.0",
        port=int(relay.get("port", 8443)),
        path=str(relay.get("path", "/ws")),
        dial=address,
        fingerprint=relay.get("fingerprint"),
        insecure_skip_verify=bool(relay.get("insecure_skip_verify", False)),
    )]
    return cfg

"""Setup links — one string that carries everything a new Iranian server needs.

    simurgh://<relay-ip>:<panel-port>/setup?u=<user>&p=<pass>#Iran-2

Paste that link into ``simurgh`` on a fresh Iranian server (``simurgh link
--join 'simurgh://...'``) and it fetches ``token`` + ``exit`` endpoint from the
existing panel, writes ``relay.toml`` and can start the service immediately.

The token travels over HTTP(S) to the panel.  When the panel is reachable over
plain HTTP on a public IP, treat the link like a password: send it through a
private channel.  (The file-copy path -- ``simurgh link --show`` -- never puts
the token on the wire.)
"""

from __future__ import annotations

import base64
import json
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


def build_join_link(host: str, panel_port: int, user: str, password: str,
                    name: str = "") -> str:
    q = f"u={quote(user)}&p={quote(_b64e(password))}"
    link = f"simurgh://{host}:{panel_port}/setup?{q}"
    if name:
        link += f"#{quote(name)}"
    return link


def parse_join_link(link: str) -> dict:
    if not link.startswith("simurgh://"):
        raise LinkError("link must start with simurgh://")
    u = urlparse(link)
    if not u.hostname:
        raise LinkError("link has no host")
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
        "host": u.hostname,
        "port": u.port or 8787,
        "user": user,
        "password": password,
        "name": unquote(u.fragment) if u.fragment else "",
    }


def fetch_setup(link: str, timeout: float = 15.0, scheme: str = "http") -> dict:
    """Ask the panel for the tunnel configuration (returns a dict)."""
    info = parse_join_link(link)
    url = f"{scheme}://{info['host']}:{info['port']}/api/join"
    payload = json.dumps({"user": info["user"], "password": info["password"]}).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise LinkError("panel rejected the credentials") from exc
        raise LinkError(f"panel answered HTTP {exc.code}") from exc
    except Exception as exc:
        raise LinkError(f"cannot reach the panel: {exc}") from exc
    if not data.get("ok"):
        raise LinkError(str(data.get("error") or "panel refused the request"))
    cfg = data.get("relay") or data          # flat payload, or nested under "relay"
    if not cfg.get("token"):
        raise LinkError("panel sent an incomplete configuration")
    if str(cfg.get("role") or "") == "exit":
        # reverse mode: the relay tells the exit where it waits for it
        if not (cfg.get("listen") or {}).get("address"):
            raise LinkError("panel sent an incomplete configuration")
    elif not (cfg.get("exit") or {}).get("address"):
        raise LinkError("panel sent an incomplete configuration")
    return {**cfg, "link_name": info["name"]}


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
                 "Import it on the foreign (exit) server with "
                 "`simurgh join <link>`."),
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

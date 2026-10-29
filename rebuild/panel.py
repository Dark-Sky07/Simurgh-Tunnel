"""Loopback-only first-run role/configuration panel for the rebuild."""

from __future__ import annotations

import argparse
import asyncio
import html
import ipaddress
import logging
import os
import secrets
import signal
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .paths import default_config_dir
from .supervisor import NodeSupervisor
from .tunnel import EntryConfig, check_entry_path, load_config, speed_test_entry
from .updates import UpdateError, latest_update
from .usage import format_bytes, read_usage_snapshot
from .wizard import list_config_backups, restore_config, save_config

LOG = logging.getLogger("simurgh.rebuild.panel")
MAX_BODY = 16 * 1024

_STYLE = """
:root{color-scheme:dark;--bg:#09121a;--card:#111f2b;--line:#27404d;--ink:#ecf4f4;--muted:#9bb0b4;--teal:#42ddc0;--gold:#f2c66d}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(80rem 40rem at 100% 0,#153244 0,transparent 55%),var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{width:min(960px,calc(100% - 32px));margin:48px auto 80px}header{margin-bottom:28px}.eyebrow{color:var(--teal);font-size:.78rem;font-weight:750;letter-spacing:.14em;text-transform:uppercase}
h1{font-size:clamp(2rem,6vw,3.6rem);line-height:1.07;letter-spacing:-.04em;margin:.35rem 0}.lede{max-width:62ch;color:var(--muted)}
form{display:grid;gap:16px}.card{background:linear-gradient(145deg,#142431,#0f1b25);border:1px solid var(--line);border-radius:18px;padding:clamp(18px,4vw,28px);min-width:0}.card h2{font-size:1.12rem;margin:0 0 16px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.field{min-width:0}.field.wide{grid-column:1/-1}label{display:block;color:var(--muted);font-size:.83rem;font-weight:650;margin-bottom:6px}input,select{width:100%;min-width:0;background:#09151e;color:var(--ink);border:1px solid #35505e;border-radius:10px;padding:12px 13px;font:inherit}input:focus,select:focus{outline:2px solid var(--teal);outline-offset:1px}.hint{font-size:.78rem;color:var(--muted);margin:6px 0 0;overflow-wrap:anywhere}.action{display:flex;align-items:center;gap:14px;flex-wrap:wrap}.button{border:0;background:var(--teal);color:#061a1a;border-radius:11px;padding:13px 20px;font:inherit;font-weight:700;cursor:pointer}.replace{display:flex;gap:9px;align-items:flex-start;color:var(--muted);font-size:.86rem}.replace input{width:auto;margin-top:5px}.notice{border-left:3px solid var(--gold);padding:10px 13px;background:#241f16;color:#f2dfb2;border-radius:4px 10px 10px 4px;font-size:.88rem}.footer{color:var(--muted);font-size:.8rem;margin-top:22px}.result{border:1px solid var(--line);border-radius:14px;padding:20px;background:var(--card)}.node{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;border-top:1px solid var(--line);padding:14px 0}.node h3,.node p{margin:0}.node p{color:var(--muted)}.node-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.node-actions form{display:block;margin:0}.node-actions .button{padding:9px 13px}.stop{background:#f2c66d}
@media(max-width:620px){main{margin-top:28px}.grid{grid-template-columns:1fr}.field.wide{grid-column:auto}.card{border-radius:14px}.action{align-items:stretch;flex-direction:column}.action .button{width:100%}.node-actions{width:100%}.node-actions form,.node-actions .button{width:100%}}
"""


def _page(body: str, *, status: str = "") -> bytes:
    title = "Simurgh Tunnel · Setup"
    message = f'<div class="result" role="status">{html.escape(status)}</div>' if status else ""
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><title>{title}</title>
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'">
<style>{_STYLE}</style></head><body><main><header><div class="eyebrow">Simurgh · independent rebuild</div>
<h1>Set up this node</h1><p class="lede">Choose this server’s role and define a fixed route. The entry never chooses a destination; the exit keeps that allowlist.</p></header>
{message}{body}<p class="footer">Local setup only · HTTP listens on loopback · existing /etc/simurgh is never changed</p></main></body></html>"""
    return page.encode("utf-8")


def _form(csrf: str, config_dir: Path) -> str:
    token = html.escape(csrf, quote=True)
    tls_dir = html.escape(str(config_dir / "tls"), quote=True)
    return f"""<form method="post" action="/setup">
<input type="hidden" name="csrf" value="{token}">
<section class="card"><h2>Role and local listener</h2><div class="grid">
<div class="field"><label for="role">This server is</label><select id="role" name="role"><option value="entry">Entry · accepts user traffic</option><option value="exit">Exit · connects to a fixed destination</option></select></div>
<div class="field"><label for="transport">Local entry transport</label><select id="transport" name="transport"><option value="tcp">TCP stream</option><option value="udp">UDP datagrams</option></select></div>
<div class="field"><label for="carrier">Tunnel carrier · must match both nodes</label><select id="carrier" name="carrier"><option value="tls">TLS stream</option><option value="wss">WebSocket over TLS (WSS)</option></select></div>
<div class="field"><label for="listen_host">Listen address</label><input id="listen_host" name="listen_host" value="127.0.0.1" required></div>
<div class="field"><label for="listen_port">Listen port</label><input id="listen_port" name="listen_port" type="number" min="1" max="65535" value="18080" required></div>
<div class="field wide"><label for="route">Route name (must match at both ends)</label><input id="route" name="route" value="web" maxlength="64" required></div>
<div class="field"><label for="monthly_quota_mib">Entry monthly quota in MiB · blank means unlimited</label><input id="monthly_quota_mib" name="monthly_quota_mib" type="number" min="1" max="1048576" placeholder="Unlimited"></div>
</div><p class="hint">Quota changes apply after restarting the entry node. The quota counts payload in both directions and resets on the UTC month.</p></section>
<section class="card"><h2>Secure link</h2><div class="grid">
<div class="field"><label for="secret">Shared secret · 32 bytes minimum</label><input id="secret" name="secret" type="password" minlength="32" autocomplete="new-password" required></div>
<div class="field"><label for="exit_host">Exit address · entry only</label><input id="exit_host" name="exit_host" value="exit.example.net"></div>
<div class="field"><label for="exit_port">Exit TLS port · entry only</label><input id="exit_port" name="exit_port" type="number" min="1" max="65535" value="8443"></div>
<div class="field"><label for="server_name">Expected TLS DNS name · entry only</label><input id="server_name" name="server_name" value="exit.example.net"></div>
<div class="field wide"><label for="ca_file">Trusted CA certificate path · entry only</label><input id="ca_file" name="ca_file" value="{tls_dir}/ca.crt"></div>
</div><p class="hint">Certificate verification cannot be disabled. Share the secret with the other server through a trusted channel.</p></section>
<section class="card"><h2>Destination allowlist · exit only</h2><div class="grid">
<div class="field"><label for="target_host">Fixed destination address</label><input id="target_host" name="target_host" value="127.0.0.1"></div>
<div class="field"><label for="target_port">Fixed destination port</label><input id="target_port" name="target_port" type="number" min="1" max="65535" value="8080"></div>
<div class="field"><label for="cert_file">Exit TLS certificate path</label><input id="cert_file" name="cert_file" value="{tls_dir}/server.crt"></div>
<div class="field"><label for="key_file">Exit TLS private key path</label><input id="key_file" name="key_file" value="{tls_dir}/server.key"></div>
</div><p class="hint">The connecting entry can request only the route name above. It cannot send a host or port.</p></section>
<section class="notice">The panel can start or stop its own node processes. It does not install services, change firewall rules, or replace existing files without making a private backup.</section>
<div class="action"><button class="button" type="submit">Save node configuration</button><label class="replace"><input type="checkbox" name="replace" value="yes">I understand that an existing rebuild config will be backed up before replacement.</label></div>
</form>"""


def _node_form(panel, endpoint: str, role: str, label: str, *, enabled: str | None = None,
               css_class: str = "") -> str:
    fields = (f'<input type="hidden" name="csrf" value="{html.escape(panel.csrf, quote=True)}">'
              f'<input type="hidden" name="role" value="{role}">')
    if enabled is not None:
        fields += f'<input type="hidden" name="enabled" value="{enabled}">'
    class_attr = f' class="button {css_class}"' if css_class else ' class="button"'
    return (f'<form method="post" action="{endpoint}">{fields}'
            f'<button{class_attr} type="submit">{label}</button></form>')


def _update_form(panel) -> str:
    token = html.escape(panel.csrf, quote=True)
    return ("<section class=\"card\"><h2>Software updates</h2>"
            "<p class=\"hint\">Check the official Simurgh Next release feed. The panel only checks; "
            "install updates from the terminal menu or update command.</p>"
            f'<form method="post" action="/update/check"><input type="hidden" name="csrf" value="{token}">'
            '<button class="button" type="submit">Check for updates</button></form></section>')


def _backup_section(panel) -> str:
    rows = []
    for role in ("entry", "exit"):
        for backup in list_config_backups(panel.config_dir, role)[:10]:
            name = html.escape(backup.name, quote=True)
            token = html.escape(panel.csrf, quote=True)
            rows.append(
                f'<article class="node"><div><h3>{role.title()} backup</h3>'
                f'<p><code>{name}</code></p></div><div class="node-actions">'
                f'<form method="post" action="/config/restore">'
                f'<input type="hidden" name="csrf" value="{token}">'
                f'<input type="hidden" name="role" value="{role}">'
                f'<input type="hidden" name="backup" value="{name}">'
                '<label class="replace"><input type="checkbox" name="confirm" value="yes" required>'
                'Back up the current config before restore</label>'
                '<button class="button stop" type="submit">Restore</button></form></div></article>')
    content = ("".join(rows) if rows else
               '<p class="hint">No saved config backups were found.</p>')
    return '<section class="card"><h2>Configuration backups</h2>' + content + '</section>'


def _usage_section(config_dir: Path) -> str:
    try:
        snapshot = read_usage_snapshot(config_dir / "entry-usage.json")
    except (OSError, ValueError):
        return ('<section class="card"><h2>Entry traffic</h2>'
                '<p class="hint">Usage state is unreadable; check its file and permissions.</p></section>')
    if snapshot is None:
        return ('<section class="card"><h2>Entry traffic</h2>'
                '<p class="hint">Traffic totals appear after the entry node runs.</p></section>')
    uploaded = snapshot["upload_bytes"]
    downloaded = snapshot["download_bytes"]
    used = uploaded + downloaded
    quota = snapshot["quota_bytes"]
    if quota:
        percent = min(100, int(used * 100 / quota))
        quota_label = f"<p>Monthly quota: {format_bytes(quota)} ({percent}% used)</p>"
    else:
        quota_label = "<p>Monthly quota: unlimited</p>"
    return (f'<section class="card"><h2>Entry traffic · {html.escape(snapshot["month"])}</h2>'
            f'<p>Upload: {format_bytes(uploaded)} · Download: {format_bytes(downloaded)} · '
            f'Total: {format_bytes(used)}</p>{quota_label}'
            '<p class="hint">Counts client payload bytes at the entry; month resets use UTC.</p>'
            '</section>')


def _dashboard(panel) -> str:
    rows = []
    states = panel.supervisor.status()
    state_error = states.get("error")
    for role in ("entry", "exit"):
        path = panel.config_dir / f"{role}.json"
        state = states[role]
        if not path.is_file():
            label = "Not configured"
            actions = ""
            autostart_label = ""
        else:
            if state["state"] == "running":
                label = f"Running · PID {state['pid']}"
                action = _node_form(panel, "/node/stop", role, f"Stop {role}",
                                    css_class="stop")
            else:
                label = "Configured · stopped"
                action = _node_form(panel, "/node/start", role, f"Start {role}")
            auto = state["autostart"]
            autostart_label = "Panel start: enabled" if auto else "Panel start: disabled"
            toggle = _node_form(
                panel, "/node/autostart", role,
                "Disable auto-start" if auto else "Enable auto-start",
                enabled="no" if auto else "yes",
            )
            check_action = (_node_form(panel, "/node/check", role, "Check route")
                            if role == "entry" else "")
            speed_action = ""
            if role == "entry":
                try:
                    entry_config = load_config(path)
                except (ValueError, OSError):
                    entry_config = None
                if isinstance(entry_config, EntryConfig) and entry_config.transport == "tcp":
                    speed_action = _node_form(panel, "/node/speed", role, "Speed test · 4 MiB")
            actions = (f'<div class="node-actions">{action}{toggle}'
                       f'{check_action}{speed_action}</div>')
        log_link = (f'<p class="hint">Log: <code>{html.escape(state["log"])}</code></p>'
                    if state.get("log") else "")
        rows.append(f'<article class="node"><div><h3>{role.title()}</h3>'
                    f'<p>{html.escape(label)}</p><p class="hint">'
                    f'{html.escape(autostart_label)}</p>{log_link}</div>{actions}</article>')
    warning = ""
    if state_error:
        warning = (f'<div class="notice" role="alert">Auto-start state error: '
                   f'{html.escape(state_error["message"])}</div>')
    return (warning + _update_form(panel) + _backup_section(panel)
            + _usage_section(panel.config_dir)
            + '<section class="card"><h2>Node control</h2>'
            '<p class="hint">Processes are started only by this local panel and run as its user. '
            'Closing the panel stops nodes it launched. “Panel start” starts a configured node '
            'whenever this panel launches.</p>' + "".join(rows) + "</section>")


def _one(form: dict[str, list[str]], key: str, default: str = "") -> str:
    return (form.get(key) or [default])[0].strip()


def _number(form: dict[str, list[str]], key: str) -> int:
    value = _one(form, key)
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be a valid port") from exc
    if not 1 <= number <= 65535:
        raise ValueError(f"{key} must be between 1 and 65535")
    return number


def _build_form_config(form: dict[str, list[str]]) -> tuple[str, dict]:
    role = _one(form, "role")
    if role not in ("entry", "exit"):
        raise ValueError("choose entry or exit")
    transport = _one(form, "transport", "tcp")
    if transport not in ("tcp", "udp"):
        raise ValueError("transport must be TCP or UDP")
    carrier = _one(form, "carrier", "tls")
    if carrier not in ("tls", "wss"):
        raise ValueError("carrier must be TLS or WSS")
    secret = _one(form, "secret")
    if len(secret.encode("utf-8")) < 32:
        raise ValueError("shared secret must contain at least 32 UTF-8 bytes")
    listen_host = _one(form, "listen_host")
    route = _one(form, "route")
    if not listen_host or not route or len(route) > 64:
        raise ValueError("listen address and a route name of 1–64 characters are required")

    if role == "exit":
        config = {
            "role": "exit", "carrier": carrier, "listen_host": listen_host,
            "listen_port": _number(form, "listen_port"),
            "cert_file": _one(form, "cert_file"), "key_file": _one(form, "key_file"),
            "secret": secret,
            "routes": {route: {"host": _one(form, "target_host"),
                               "port": _number(form, "target_port")}},
        }
        if not config["cert_file"] or not config["key_file"] or not config["routes"][route]["host"]:
            raise ValueError("exit certificate, key and fixed destination are required")
        return role, config

    config = {
        "role": "entry", "transport": transport, "carrier": carrier,
        "listen_host": listen_host, "listen_port": _number(form, "listen_port"),
        "exit_host": _one(form, "exit_host"), "exit_port": _number(form, "exit_port"),
        "server_name": _one(form, "server_name"), "ca_file": _one(form, "ca_file"),
        "secret": secret, "route": route,
    }
    if not all(config[key] for key in ("exit_host", "server_name", "ca_file")):
        raise ValueError("exit address, expected TLS name and CA path are required")
    quota_text = _one(form, "monthly_quota_mib")
    if quota_text:
        try:
            quota_mib = int(quota_text)
        except ValueError as exc:
            raise ValueError("monthly quota must be a whole number of MiB") from exc
        if not 1 <= quota_mib <= 1_048_576:
            raise ValueError("monthly quota must be from 1 to 1,048,576 MiB")
        config["monthly_quota_mib"] = quota_mib
    return role, config


class SetupPanel:
    """A loopback-only HTTP server. Use SSH port-forwarding for remote setup."""

    def __init__(self, config_dir: Path, host: str = "127.0.0.1", port: int = 8780):
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("setup panel bind host must be a loopback IP address") from exc
        if not address.is_loopback:
            raise ValueError("setup panel refuses non-loopback binds; use SSH forwarding")
        self.config_dir = Path(config_dir)
        self.csrf = secrets.token_urlsafe(32)
        self.supervisor = NodeSupervisor(self.config_dir)
        panel = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "SimurghSetup/0.2.5"
            sys_version = ""

            def setup(self):
                self.request.settimeout(10)
                super().setup()

            def log_message(self, fmt, *args):
                LOG.info("%s - %s", self.client_address[0], fmt % args)

            def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; style-src 'unsafe-inline'; "
                                 "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)

            def _host_is_local(self) -> bool:
                header = self.headers.get("Host", "").strip()
                try:
                    authority = urlparse(f"//{header}")
                    hostname = authority.hostname
                    port = authority.port or 80
                except ValueError:
                    return False
                return (hostname in ("127.0.0.1", "localhost", "::1")
                        and authority.username is None and authority.password is None
                        and not authority.path and not authority.query and not authority.fragment
                        and port == panel.address[1])

            def _origin_is_same_panel(self, origin: str) -> bool:
                try:
                    parsed = urlparse(origin)
                    hostname = parsed.hostname
                    port = parsed.port or 80
                except ValueError:
                    return False
                return (parsed.scheme == "http"
                        and hostname in ("127.0.0.1", "localhost", "::1")
                        and parsed.username is None and parsed.password is None
                        and not parsed.path and not parsed.query and not parsed.fragment
                        and port == panel.address[1])

            def do_GET(self):
                if not self._host_is_local():
                    self._send(421, _page("", status="Use the loopback address or an SSH tunnel."))
                elif self.path != "/":
                    self._send(404, _page("", status="Not found."))
                else:
                    self._send(200, _page(_dashboard(panel) + _form(panel.csrf, panel.config_dir)))

            def do_POST(self):
                if not self._host_is_local():
                    self._send(421, _page("", status="Use the loopback address or an SSH tunnel."))
                    return
                if self.path not in ("/setup", "/node/start", "/node/stop", "/node/check",
                                      "/node/speed", "/node/autostart", "/update/check", "/config/restore"):
                    self._send(404, _page("", status="Not found."))
                    return
                origin = self.headers.get("Origin")
                if origin and not self._origin_is_same_panel(origin):
                    self._send(403, _page("", status="Cross-site form submission refused."))
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length <= 0 or length > MAX_BODY:
                    self._send(413, _page("", status="Form is empty or too large."))
                    return
                form = parse_qs(self.rfile.read(length).decode("utf-8", "strict"),
                                keep_blank_values=True)
                if not secrets.compare_digest(_one(form, "csrf"), panel.csrf):
                    self._send(403, _page("", status="Form token expired; reload the page."))
                    return
                if self.path in ("/node/check", "/node/speed"):
                    label = "Route check" if self.path == "/node/check" else "Speed test"
                    try:
                        if _one(form, "role") != "entry":
                            raise ValueError("diagnostics are available only for the entry role")
                        config = load_config(panel.config_dir / "entry.json")
                        if not isinstance(config, EntryConfig):
                            raise TypeError("entry.json does not contain an entry config")
                        if self.path == "/node/check":
                            result = asyncio.run(check_entry_path(config))
                        else:
                            result = asyncio.run(speed_test_entry(config))
                        self._send(200, _page(
                            _dashboard(panel) + _form(panel.csrf, panel.config_dir),
                            status=f"{label}: {result}"))
                    except (TypeError, ValueError, OSError, TimeoutError) as exc:
                        self._send(503, _page(
                            _dashboard(panel) + _form(panel.csrf, panel.config_dir),
                            status=f"{label} failed: {exc}"))
                    return
                if self.path == "/config/restore":
                    try:
                        if _one(form, "confirm") != "yes":
                            raise ValueError("confirm that the current config should be backed up before restore")
                        role = _one(form, "role")
                        backup = _one(form, "backup")
                        preserved = restore_config(panel.config_dir, role, backup)
                        message = f"Restored {role} configuration from {backup}."
                        if preserved:
                            message += f" The replaced config was saved to {preserved.name}."
                        self._send(200, _page(_dashboard(panel) + _form(panel.csrf, panel.config_dir),
                                              status=message))
                    except (ValueError, OSError) as exc:
                        self._send(400, _page(_dashboard(panel) + _form(panel.csrf, panel.config_dir),
                                              status=f"Restore failed: {exc}"))
                    return
                if self.path == "/update/check":
                    try:
                        release = latest_update()
                        if release is None:
                            update_message = "No newer stable Simurgh Next release is available."
                        else:
                            version = html.escape(release["_simurgh_version"])
                            mode = os.environ.get("SIMURGH_NEXT_INSTALL_MODE", "user")
                            command = ("sudo simurgh-next-update --apply --mode system"
                                       if mode == "system" else "simurgh-next-update --apply")
                            update_message = (f"Simurgh Next {version} is available. "
                                              f"From a terminal, run: {command} — "
                                              "the installer verifies the published SHA-256 before applying it.")
                        self._send(200, _page(_dashboard(panel) + _form(panel.csrf, panel.config_dir),
                                              status=update_message))
                    except UpdateError as exc:
                        self._send(503, _page(_dashboard(panel) + _form(panel.csrf, panel.config_dir),
                                              status=f"Update check failed: {exc}"))
                    return
                if self.path == "/node/autostart":
                    role = _one(form, "role")
                    enabled_text = _one(form, "enabled")
                    try:
                        if enabled_text not in ("yes", "no"):
                            raise ValueError("auto-start state must be yes or no")
                        panel.supervisor.set_autostart(role, enabled_text == "yes")
                        message = (f"Auto-start enabled for {role}; it will start when the panel launches."
                                   if enabled_text == "yes" else f"Auto-start disabled for {role}.")
                        self._send(200, _page(_dashboard(panel) + _form(panel.csrf, panel.config_dir),
                                              status=message))
                    except (ValueError, OSError) as exc:
                        self._send(400, _page(_dashboard(panel) + _form(panel.csrf, panel.config_dir),
                                              status=str(exc)))
                    return
                if self.path in ("/node/start", "/node/stop"):
                    role = _one(form, "role")
                    try:
                        if self.path == "/node/start":
                            node = panel.supervisor.start(role)
                            message = f"Started {role} (pid {node.process.pid}); log: {node.log_path}"
                        else:
                            stopped = panel.supervisor.stop(role)
                            message = f"Stopped {role}." if stopped else f"{role} is already stopped."
                        self._send(200, _page(_dashboard(panel) + _form(panel.csrf, panel.config_dir),
                                              status=message))
                    except (ValueError, RuntimeError, OSError) as exc:
                        self._send(400, _page(_dashboard(panel) + _form(panel.csrf, panel.config_dir),
                                              status=str(exc)))
                    return
                try:
                    role, config = _build_form_config(form)
                    path = panel.config_dir / f"{role}.json"
                    if path.exists() and _one(form, "replace") != "yes":
                        self._send(409, _page(_form(panel.csrf, panel.config_dir), status=(
                            "A config already exists. Check the backup/replace box and submit again.")))
                        return
                    backup = save_config(path, config)
                    message = f"Saved {role} configuration to {path}. /etc/simurgh was not changed."
                    if backup:
                        message += f" Previous config backed up to {backup}."
                    self._send(201, _page(_form(panel.csrf, panel.config_dir), status=message))
                except (UnicodeDecodeError, ValueError, OSError) as exc:
                    self._send(400, _page(_form(panel.csrf, panel.config_dir), status=str(exc)))

            def do_HEAD(self):
                if self.path == "/healthz" and self._host_is_local():
                    self.send_response(204)
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                else:
                    self._send(404, _page("", status="Not found."))

        server_type = ThreadingHTTPServer
        if address.version == 6:
            class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
                address_family = socket.AF_INET6
            server_type = IPv6ThreadingHTTPServer
        self.server = server_type((host, port), Handler)
        self.server.daemon_threads = False
        self.thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self.thread is not None:
            raise RuntimeError("setup panel already started")
        self.thread = threading.Thread(target=self.server.serve_forever,
                                      name="simurgh-rebuild-setup", daemon=True)
        self.thread.start()
        self.supervisor.start_autostart()

    def close(self) -> None:
        if self.thread is not None:
            self.server.shutdown()
            self.thread.join(timeout=5)
            self.thread = None
        self.server.server_close()
        self.supervisor.stop_all()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the loopback-only role setup panel")
    parser.add_argument("--config-dir", type=Path, default=default_config_dir())
    parser.add_argument("--port", type=int, default=8780)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be in the range 1-65535")
    panel = SetupPanel(args.config_dir, port=args.port)
    shutdown_requested = threading.Event()

    def request_shutdown(_signum, _frame):
        shutdown_requested.set()

    previous_sigterm = signal.signal(signal.SIGTERM, request_shutdown)
    try:
        host, port = panel.address
        print(f"Open http://{host}:{port}/ on this server, or use SSH: ssh -L {port}:127.0.0.1:{port} <server>")
        panel.start()
        while panel.thread is not None and panel.thread.is_alive():
            if shutdown_requested.wait(0.25):
                break
    except KeyboardInterrupt:
        pass
    finally:
        panel.close()
        signal.signal(signal.SIGTERM, previous_sigterm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

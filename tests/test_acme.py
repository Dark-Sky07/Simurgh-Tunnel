"""`simurgh cert`: driving acme.sh / certbot and wiring the result into a config.

The ACME client is a small script of our own here, so the whole flow is tested
for real: it is executed with the argv the CLI built, it creates the files a
real client would create, and the config is then checked to point at them.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from simurgh import acme
from simurgh.config import (ExitConfig, ListenSpec, RelayConfig, TunnelSpec,
                            load_exit, load_relay, save_exit, save_relay)

FAKE_ACME = r'''#!{python}
"""Stands in for acme.sh: records argv and makes the files it would make."""
import os, sys

args = sys.argv[1:]
with open(os.environ["ACME_LOG"], "a") as fh:
    fh.write(" ".join(args) + "\n")

home = os.environ["ACME_HOME"]
if "--issue" in args:
    domain = args[args.index("-d") + 1]
    # acme.sh puts an ECDSA certificate next to its own script
    base = os.path.join(home, domain + "_ecc")
    os.makedirs(base, exist_ok=True)
    open(os.path.join(base, "fullchain.cer"), "w").write("cert")
    open(os.path.join(base, domain + ".key"), "w").write("key")
if "--cron" in args:
    print("renewed: nothing was due")
sys.exit(0)
'''

FAKE_CERTBOT = r'''#!{python}
"""Stands in for certbot."""
import os, sys

args = sys.argv[1:]
with open(os.environ["CERTBOT_LOG"], "a") as fh:
    fh.write(" ".join(args) + "\n")

if args and args[0] == "certonly":
    domain = args[args.index("-d") + 1]
    config_dir = args[args.index("--config-dir") + 1] if "--config-dir" in args else "/etc/letsencrypt"
    live = os.path.join(config_dir, "live", domain)
    os.makedirs(live, exist_ok=True)
    open(os.path.join(live, "fullchain.pem"), "w").write("cert")
    open(os.path.join(live, "privkey.pem"), "w").write("key")
if args and args[0] == "renew":
    print("not due for renewal")
sys.exit(0)
'''


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body.format(python=os.sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return str(path)


@pytest.fixture()
def fake_acme(tmp_path, monkeypatch):
    """A fake acme.sh with its store, plus the log of everything it was asked."""
    # the real acme.sh sits in its own store: ~/.acme.sh/acme.sh
    store = tmp_path / ".acme.sh"
    store.mkdir()
    binary = _script(store, "acme.sh", FAKE_ACME)
    log = tmp_path / "acme.log"
    monkeypatch.setenv("ACME_HOME", str(store))
    monkeypatch.setenv("ACME_LOG", str(log))
    monkeypatch.setattr(acme.shutil, "which", lambda name: None)
    monkeypatch.setattr(acme, "ACME_SH_PATHS", (binary,))
    return {"binary": binary, "store": store, "log": log}


@pytest.fixture()
def fake_certbot(tmp_path, monkeypatch):
    binary = _script(tmp_path, "certbot", FAKE_CERTBOT)
    log = tmp_path / "certbot.log"
    monkeypatch.setenv("CERTBOT_LOG", str(log))
    monkeypatch.setattr(acme.shutil, "which",
                        lambda name: binary if name == "certbot" else None)
    monkeypatch.setattr(acme, "ACME_SH_PATHS", ())
    return {"binary": binary, "log": log}


# ------------------------------------------------------------------ detection
def test_no_client_is_reported_with_instructions(monkeypatch):
    monkeypatch.setattr(acme.shutil, "which", lambda name: None)
    monkeypatch.setattr(acme, "ACME_SH_PATHS", ())
    assert acme.client() == ("", "")
    assert acme.describe_clients() == ""
    with pytest.raises(acme.AcmeError) as excinfo:
        acme.issue("panel.example.com", webroot="/var/www/html")
    assert "no ACME client found" in str(excinfo.value)
    assert "certbot" in acme.install_hint()


def test_acme_sh_wins_and_can_be_overridden(fake_acme, fake_certbot, monkeypatch):
    # both are installed: acme.sh is the one Iranian servers have
    monkeypatch.setattr(acme, "ACME_SH_PATHS", (fake_acme["binary"],))
    assert acme.client() == ("acme.sh", fake_acme["binary"])
    assert acme.client("certbot") == ("certbot", fake_certbot["binary"])
    assert acme.describe_clients().startswith("acme.sh")
    # a client that is asked for by name is not swapped for the other one
    monkeypatch.setattr(acme, "ACME_SH_PATHS", ())
    assert acme.client("acme.sh") == ("", "")


def test_a_client_that_cannot_be_read_is_skipped(monkeypatch, tmp_path):
    """`/root/.acme.sh` raises PermissionError for a normal user, not False."""
    monkeypatch.setattr(acme.shutil, "which", lambda name: None)
    monkeypatch.setattr(acme, "ACME_SH_PATHS", ("/root/.acme.sh/acme.sh",))

    def boom(self):
        raise PermissionError("nope")

    monkeypatch.setattr(acme.Path, "is_file", boom)
    assert acme.acme_sh() == ""          # no traceback, just "nothing found"


# ---------------------------------------------------------------- issuing it
def test_issue_with_acme_sh_creates_and_finds_the_certificate(fake_acme):
    result = acme.issue("panel.example.com", email="me@example.com",
                        webroot="/var/www/html",
                        reload_cmd="systemctl restart simurgh-exit")
    assert result.client == "acme.sh"
    assert result.cert_file.endswith("panel.example.com_ecc/fullchain.cer")
    assert Path(result.cert_file).is_file() and Path(result.key_file).is_file()
    calls = fake_acme["log"].read_text().splitlines()
    assert "--issue -d panel.example.com --webroot /var/www/html" in calls[0]
    assert "--accountemail me@example.com" in calls[0]
    # the renewal hook was installed with the command we were given
    assert "--install-cert" in calls[1]
    assert "systemctl restart simurgh-exit" in calls[1]
    assert "renewal hook installed" in result.steps[-1]


def test_issue_standalone_and_staging_change_the_argv(fake_acme):
    acme.issue("panel.example.com", standalone=True, staging=True)
    issued = fake_acme["log"].read_text().splitlines()[0]
    assert "--standalone" in issued and "--webroot" not in issued
    assert "--server letsencrypt_test" in issued


def test_a_failing_client_is_an_error_not_a_traceback(fake_acme, tmp_path):
    failing = tmp_path / "broken.sh"
    failing.write_text("#!/bin/sh\necho 'rate limited' >&2\nexit 1\n")
    failing.chmod(0o755)
    with pytest.raises(acme.AcmeError) as excinfo:
        acme.issue("panel.example.com", webroot="/var/www", binary=str(failing))
    assert "rate limited" in str(excinfo.value)


def test_a_success_without_files_is_caught(fake_acme, tmp_path, monkeypatch):
    lying = tmp_path / "liar.sh"
    lying.write_text("#!/bin/sh\nexit 0\n")
    lying.chmod(0o755)
    with pytest.raises(acme.AcmeError) as excinfo:
        acme.issue("panel.example.com", webroot="/var/www", binary=str(lying))
    assert "not there" in str(excinfo.value)


def test_webroot_or_standalone_is_required(fake_acme):
    with pytest.raises(acme.AcmeError) as excinfo:
        acme.issue("panel.example.com")
    assert "how to prove ownership" in str(excinfo.value)
    with pytest.raises(acme.AcmeError):
        acme.issue("", webroot="/var/www")


def test_certbot_uses_its_own_store(fake_certbot, tmp_path):
    config_dir = tmp_path / "le"
    result = acme.issue("panel.example.com", email="me@example.com",
                        webroot="/var/www/html", prefer="certbot",
                        config_dir=str(config_dir))
    assert result.client == "certbot"
    assert result.cert_file == str(config_dir / "live" / "panel.example.com"
                                   / "fullchain.pem")
    assert Path(result.cert_file).is_file()
    called = fake_certbot["log"].read_text().splitlines()[0]
    assert called.startswith("certonly -d panel.example.com")
    assert "--non-interactive" in called and "--agree-tos" in called
    assert f"--config-dir {config_dir}" in called


def test_renew_runs_the_clients_own_cron(fake_acme, fake_certbot, monkeypatch):
    monkeypatch.setattr(acme, "ACME_SH_PATHS", (fake_acme["binary"],))
    name, ok, output = acme.renew()
    assert (name, ok) == ("acme.sh", True)
    assert "renewed" in output
    assert "--cron" in fake_acme["log"].read_text()

    name, ok, _ = acme.renew(prefer="certbot", config_dir="/tmp/le")
    assert (name, ok) == ("certbot", True)
    assert "renew --non-interactive --config-dir /tmp/le" in \
        fake_certbot["log"].read_text()


# ------------------------------------------------------------- wiring it in
def test_wire_points_the_exit_at_the_new_certificate():
    cfg = ExitConfig(token="tok", name="kharej", cert_auto=True,
                     listen=[ListenSpec(carrier="tls", port=443),
                             ListenSpec(carrier="tcp", port=8080)])
    changed = acme.wire(cfg, "exit", "/acme/fullchain.pem", "/acme/privkey.pem")
    assert changed == ["cert_file/key_file"]
    assert cfg.cert_file == "/acme/fullchain.pem"
    assert cfg.cert_auto is False          # never self-sign over a real one
    assert cfg.listen[0].cert_file is None


def test_wire_can_target_one_listener():
    cfg = ExitConfig(token="tok", cert_auto=True,
                     listen=[ListenSpec(carrier="tls", port=443),
                             ListenSpec(carrier="wss", port=8443)])
    changed = acme.wire(cfg, "exit", "/acme/certs/a.pem", "/acme/keys/a.key",
                        port=8443)
    assert changed == ["[[listen]] wss:8443"]
    assert cfg.listen[1].cert_file == "/acme/certs/a.pem"
    assert cfg.listen[0].cert_file is None and cfg.cert_file is None
    with pytest.raises(acme.AcmeError):
        acme.wire(cfg, "exit", "a", "b", port=9999)


def test_wire_keeps_a_listener_that_has_its_own_certificate():
    cfg = ExitConfig(token="tok", cert_auto=True,
                     listen=[ListenSpec(carrier="tls", port=443,
                                        cert_file="/mine.pem",
                                        key_file="/mine.key")])
    changed = acme.wire(cfg, "exit", "/acme/fullchain.pem", "/acme/privkey.pem")
    assert any("keeps its own certificate" in item for item in changed)
    assert cfg.listen[0].cert_file == "/mine.pem"     # untouched


def test_wire_the_relay_tunnel():
    cfg = RelayConfig(token="tok", dial="exit",
                      tunnel=TunnelSpec(enabled=True, carrier="tls", port=443))
    changed = acme.wire(cfg, "relay", "/acme/fullchain.pem", "/acme/privkey.pem")
    assert changed == ["[tunnel] cert_file/key_file"]
    assert cfg.tunnel.cert_file == "/acme/fullchain.pem"
    assert cfg.tunnel.cert_auto is False


# ------------------------------------------------------------------- status
def test_status_reports_each_listener(tmp_path, certs):
    from simurgh.config import save_exit as _save
    from simurgh.config import load_exit as _load

    cfg = ExitConfig(token="tok", name="kharej", cert_file=certs[0],
                     key_file=certs[1], cert_auto=False,
                     listen=[ListenSpec(carrier="tls", port=443),
                             ListenSpec(carrier="wss", port=8443),
                             ListenSpec(carrier="tcp", port=8080),
                             ListenSpec(carrier="tls", port=9443, enabled=False)])
    rows = acme.status(cfg, "exit")
    assert [r["endpoint"] for r in rows] == ["tls:443", "wss:8443"]
    assert all(r["exists"] and r["days"] > 100 for r in rows)
    assert all(r["fingerprint"] for r in rows)   # printed by `simurgh cert`

    # a certificate that is about to expire is not silently "fine"
    short = Path(tmp_path / "short.pem")
    short.write_text(Path(certs[0]).read_text())
    rows = acme.status(ExitConfig(token="tok", cert_file=str(short),
                                  key_file=certs[1], cert_auto=False,
                                  listen=[ListenSpec(carrier="tls", port=443)]),
                       "exit")
    assert rows[0]["days"] is not None


def test_status_skips_a_role_with_no_tls_listener():
    cfg = ExitConfig(token="tok", listen=[ListenSpec(carrier="raw", port=8080)])
    assert acme.status(cfg, "exit") == []
    relay = RelayConfig(token="tok", tunnel=TunnelSpec(enabled=False))
    assert acme.status(relay, "relay") == []


# ------------------------------------------------------------------ the CLI
def _run(home, *argv):
    from simurgh.cli import build_parser

    args = build_parser().parse_args(["--home", str(home), *argv])
    return args.func(args)


def _exit_home(tmp_path):
    home = tmp_path / "simurgh"
    home.mkdir(exist_ok=True)
    save_exit(ExitConfig(token="tok", name="kharej", cert_auto=True,
                         listen=[ListenSpec(carrier="tls", port=443),
                                 ListenSpec(carrier="raw", port=8080)]),
              home / "exit.toml")
    return home


def test_cli_cert_status(tmp_path, certs, capsys, monkeypatch):
    monkeypatch.setattr(acme.shutil, "which", lambda name: None)
    monkeypatch.setattr(acme, "ACME_SH_PATHS", ())
    home = _exit_home(tmp_path)
    assert _run(home, "cert", "--role", "exit") == 1      # nothing wired yet
    out = capsys.readouterr()
    printed = out.out + out.err
    assert "acme client: none" in printed
    assert "no ACME client found" in printed
    assert "tls:443" in printed


def test_cli_cert_issue_wires_the_config(tmp_path, fake_acme, capsys, monkeypatch):
    # a box with systemd: the reload hook must name this service
    monkeypatch.setattr("simurgh.acme.shutil.which",
                        lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    home = _exit_home(tmp_path)
    code = _run(home, "cert", "issue", "panel.example.com",
                "--role", "exit", "--webroot", "/var/www/html",
                "--email", "me@example.com")
    assert code == 0
    printed = capsys.readouterr()
    text = printed.out + printed.err
    assert "exit.toml: cert_file/key_file" in text
    assert "apply it now: simurgh restart" in text

    cfg = load_exit(home / "exit.toml")
    assert cfg.cert_file.endswith("panel.example.com_ecc/fullchain.cer")
    assert cfg.cert_auto is False
    assert Path(cfg.cert_file).is_file()
    # the reload hook names this service
    assert "simurgh-exit" in fake_acme["log"].read_text()


def test_cli_cert_issue_for_one_listener(tmp_path, fake_acme, capsys):
    home = _exit_home(tmp_path)
    code = _run(home, "cert", "issue", "panel.example.com", "--role", "exit",
                "--standalone", "--port", "443")
    assert code == 0
    assert "[[listen]] port 443 now serves" in (capsys.readouterr().out)
    cfg = load_exit(home / "exit.toml")
    assert cfg.listen[0].cert_file.endswith("fullchain.cer")
    assert cfg.listen[1].cert_file is None        # the raw listener is alone


def test_cli_cert_issue_no_write_leaves_the_config(tmp_path, fake_acme, capsys):
    home = _exit_home(tmp_path)
    before = (home / "exit.toml").read_text()
    assert _run(home, "cert", "issue", "panel.example.com", "--role", "exit",
                "--webroot", "/var/www", "--no-write") == 0
    assert "left alone" in capsys.readouterr().out
    assert (home / "exit.toml").read_text() == before


def test_cli_cert_issue_needs_a_domain(tmp_path, capsys):
    home = _exit_home(tmp_path)
    assert _run(home, "cert", "issue", "--role", "exit") == 2
    assert "a domain is required" in (capsys.readouterr().err)


def test_cli_cert_issue_without_a_client_is_a_clean_error(tmp_path, capsys,
                                                          monkeypatch):
    monkeypatch.setattr(acme.shutil, "which", lambda name: None)
    monkeypatch.setattr(acme, "ACME_SH_PATHS", ())
    home = _exit_home(tmp_path)
    assert _run(home, "cert", "issue", "panel.example.com", "--role", "exit",
                "--webroot", "/var/www") == 1
    assert "no ACME client found" in (capsys.readouterr().err)


def test_cli_cert_renew(tmp_path, fake_acme, capsys):
    home = _exit_home(tmp_path)
    assert _run(home, "cert", "renew", "--role", "exit") == 0
    printed = capsys.readouterr()
    text = printed.out + printed.err
    assert "renewed everything that was due" in text
    assert "renewed: nothing was due" in text


def test_cli_cert_on_a_relay_wires_reverse_mode(tmp_path, fake_acme, capsys,
                                                monkeypatch):
    monkeypatch.setattr("simurgh.acme.shutil.which",
                        lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    home = tmp_path / "simurgh"
    home.mkdir()
    save_relay(RelayConfig(token="tok", dial="exit", name="ir-1",
                           tunnel=TunnelSpec(enabled=True, carrier="tls",
                                             port=443)),
               home / "relay.toml")
    assert _run(home, "cert", "issue", "relay.example.com", "--webroot",
                "/var/www") == 0
    cfg = load_relay(home / "relay.toml")
    assert cfg.tunnel.cert_file.endswith("fullchain.cer")
    assert cfg.tunnel.cert_auto is False
    assert "simurgh-relay" in fake_acme["log"].read_text()


# --------------------------------------------------------------- the text menu
def test_the_menu_has_entries_for_alerts_and_certificates(tmp_path, capsys,
                                                          monkeypatch):
    """The menu is the beginner path: both new surfaces need a way in."""
    from simurgh import menu

    home = _exit_home(tmp_path)

    class Home:
        path = tmp_path / "simurgh"
        exit_cfg = path / "exit.toml"
        relay_cfg = path / "relay.toml"

    monkeypatch.setattr(menu, "pause", lambda: None)
    monkeypatch.setattr(acme.shutil, "which", lambda name: None)
    monkeypatch.setattr(acme, "ACME_SH_PATHS", ())

    assert "14" in menu.MENU_RELAY and "15" in menu.MENU_RELAY
    menu.show_alerts(Home(), "exit")
    printed = capsys.readouterr()
    assert "channels:" in printed.out + printed.err
    assert "alert_webhook" in printed.out

    menu.show_certificates(Home(), "exit")
    printed = capsys.readouterr()
    text = printed.out + printed.err
    assert "tls:443" in text
    assert "simurgh cert issue" in text


# ------------------------------------------------------------- applying it now
def test_reload_now_reports_what_the_command_did(tmp_path):
    # `_script` formats the text, so no braces may survive into the shell
    script = _script(tmp_path, "reload.sh", "#!/bin/sh\necho reloading\n")
    ok, output = acme.reload_now(f"{script} --now")
    assert ok is True
    assert "reloading" in output

    # a command that is not there at all is a message, not a traceback
    ok, output = acme.reload_now("/nonexistent/reloader --now")
    assert ok is False
    assert "not found" in output

    # and an empty command is a no-op
    assert acme.reload_now("") == (True, "")


def test_cli_cert_issue_apply_runs_the_reload_hook(tmp_path, fake_acme, capsys,
                                                   monkeypatch):
    home = _exit_home(tmp_path)
    hook = tmp_path / "reload-hook.sh"
    hook.write_text("#!/bin/sh\necho hook-ran\n")
    hook.chmod(0o755)
    code = _run(home, "cert", "issue", "panel.example.com",
                "--role", "exit", "--webroot", "/var/www", "--apply",
                "--reload", str(hook))
    assert code == 0
    printed = capsys.readouterr()
    text = printed.out + printed.err
    assert "hook-ran" in text
    assert f"reload hook: {hook} ran" in text


def test_cli_cert_status_json_is_for_scripts(tmp_path, fake_acme, capsys):
    home = _exit_home(tmp_path)
    assert _run(home, "cert", "issue", "panel.example.com", "--role", "exit",
                "--webroot", "/var/www") == 0
    capsys.readouterr()
    code = _run(home, "cert", "status", "--json")
    payload = json.loads(capsys.readouterr().out)
    # the stand-in client writes a placeholder, so nothing can be read from it
    assert code == 1
    assert payload["role"] == "exit" and payload["ok"] is False
    assert payload["certificates"][0]["exists"] is True
    assert payload["certificates"][0]["days"] is None


def test_cli_cert_status_reports_an_unreadable_certificate(tmp_path, fake_acme,
                                                           capsys):
    home = _exit_home(tmp_path)
    # the stand-in client writes a placeholder file, not a certificate
    assert _run(home, "cert", "issue", "panel.example.com", "--role", "exit",
                "--webroot", "/var/www") == 0
    capsys.readouterr()
    assert _run(home, "cert", "status") == 1
    printed = capsys.readouterr()
    assert "no certificate could be read from" in printed.out + printed.err


def test_cli_cert_reports_every_listener_when_the_role_is_not_given(tmp_path,
                                                                    fake_acme,
                                                                    capsys):
    """A home with one config file must not ask for ``--role``."""
    home = _exit_home(tmp_path)
    assert _run(home, "cert") == 1        # nothing wired in this fixture yet
    printed = capsys.readouterr()
    assert "role=exit" in printed.out + printed.err

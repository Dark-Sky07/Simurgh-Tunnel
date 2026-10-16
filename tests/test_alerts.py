"""Alerts: telegram, webhook, the event filter, and the expiry watcher.

Nothing here touches the network: ``urllib.request.urlopen`` is replaced with a
recorder, so a test can look at the exact URL and JSON body a real alert would
have sent.  The certificate tests build a real self-signed certificate, so
``certs.expiry_days`` is exercised against actual X.509 data.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import urllib.error

import pytest

from simurgh import alerts as alerts_mod
from simurgh.alerts import (DEFAULT_EVENTS, EVENTS, TELEGRAM_API, Alerts,
                            alerts_from_config)
from simurgh.certs import expiry_days
from simurgh.config import (ExitConfig, ListenSpec, RelayConfig, TunnelSpec)

pytestmark = pytest.mark.asyncio

TOKEN = "123456:AAHfake-token-for-tests"
CHAT = "-1001234567890"


class Recorder:
    """Stand in for ``urlopen`` and remember every request."""

    def __init__(self, status: int = 200, error: Exception | None = None):
        self.status = status
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, req, timeout=None):
        body = None
        if getattr(req, "data", None):
            body = json.loads(req.data.decode("utf-8"))
        self.calls.append({"url": req.full_url, "body": body, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return _Response(self.status)


class _Response:
    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def recorder(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(alerts_mod.urllib.request, "urlopen", rec)
    return rec


# --------------------------------------------------------------------- basics
def test_alerts_are_off_without_a_channel():
    alerts = Alerts()
    assert alerts.enabled is False
    assert alerts.describe() == "off"
    assert alerts.test() is False
    alerts.notify("quota", "this goes nowhere")
    assert (alerts.sent, alerts.failed) == (0, 0)
    snapshot = alerts.snapshot()
    assert snapshot["enabled"] is False and snapshot["events"] == list(DEFAULT_EVENTS)


def test_half_a_channel_is_not_a_channel():
    # a token without a chat id cannot deliver anything, and vice versa
    assert Alerts(telegram_token=TOKEN).enabled is False
    assert Alerts(telegram_chat=CHAT).enabled is False
    assert Alerts(telegram_token=TOKEN, telegram_chat=CHAT).enabled is True
    assert Alerts(webhook="https://example.invalid/hook").enabled is True


def test_alerts_from_config_reads_every_key():
    cfg = RelayConfig(token="tok", name="ir-tehran", telegram_token=TOKEN,
                      telegram_chat=CHAT, alert_webhook="https://example.invalid/x",
                      alert_events=["quota", "cert"],
                      telegram_api="http://127.0.0.1:8081/")
    alerts = alerts_from_config(cfg)
    assert alerts.enabled is True
    assert alerts.describe() == "telegram + webhook"
    assert alerts.events == ("quota", "cert")
    assert alerts.name == "ir-tehran"
    # the override loses its trailing slash; the default is the public API
    assert alerts.telegram_api == "http://127.0.0.1:8081"
    assert alerts_from_config(RelayConfig(token="t")).telegram_api == TELEGRAM_API
    # an explicit name wins over the config's
    assert alerts_from_config(cfg, name="edge").name == "edge"


# ------------------------------------------------------------------- delivery
def test_telegram_alert_goes_to_the_configured_api_root(recorder):
    alerts = Alerts(telegram_token=TOKEN, telegram_chat=CHAT, name="ir-tehran",
                    telegram_api="http://127.0.0.1:8081")
    assert alerts.test() is True
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["url"] == f"http://127.0.0.1:8081/bot{TOKEN}/sendMessage"
    assert call["body"]["chat_id"] == CHAT
    assert "test alert" in call["body"]["text"]
    assert "[ir-tehran]" in call["body"]["text"]
    assert (alerts.sent, alerts.failed) == (1, 0)


def test_webhook_alert_posts_the_event_payload(recorder):
    alerts = Alerts(webhook="https://example.invalid/hook", name="edge-1")
    alerts.notify("quota", "monthly quota reached", level="warning", used=1234)
    assert len(recorder.calls) == 1
    body = recorder.calls[0]["body"]
    assert recorder.calls[0]["url"] == "https://example.invalid/hook"
    assert body["event"] == "quota" and body["level"] == "warning"
    assert body["host"] == "edge-1" and body["used"] == 1234
    assert body["text"] == "[edge-1] monthly quota reached"
    assert isinstance(body["time"], float)


def test_both_channels_are_used_when_both_are_configured(recorder):
    alerts = Alerts(telegram_token=TOKEN, telegram_chat=CHAT,
                    webhook="https://example.invalid/hook")
    assert alerts.test() is True
    urls = [c["url"] for c in recorder.calls]
    assert urls == [f"{TELEGRAM_API}/bot{TOKEN}/sendMessage",
                    "https://example.invalid/hook"]
    assert alerts.sent == 1                     # one alert, two delivery attempts


def test_a_broken_endpoint_is_counted_not_raised(recorder):
    recorder.error = urllib.error.URLError("no route to host")
    alerts = Alerts(webhook="https://example.invalid/hook")
    assert alerts.test() is False
    assert (alerts.sent, alerts.failed) == (0, 1)
    assert "URLError" in alerts.last_error


def test_a_non_2xx_answer_counts_as_a_failure(monkeypatch):
    monkeypatch.setattr(alerts_mod.urllib.request, "urlopen",
                        Recorder(status=500))
    alerts = Alerts(webhook="https://example.invalid/hook")
    assert alerts.test() is False and alerts.failed == 1


# --------------------------------------------------------------- event filter
def test_events_are_filtered(recorder):
    alerts = Alerts(webhook="https://example.invalid/hook", events=["quota"])
    alerts.notify("quota", "yes")
    alerts.notify("connected", "no")
    assert [c["body"]["event"] for c in recorder.calls] == ["quota"]


def test_all_is_a_wildcard(recorder):
    alerts = Alerts(webhook="https://example.invalid/hook", events=["all"])
    for event in ("connected", "cert", "login"):
        alerts.notify(event, event)
    assert [c["body"]["event"] for c in recorder.calls] == \
        ["connected", "cert", "login"]
    assert set(EVENTS) >= {"connected", "disconnected", "quota", "throttle",
                           "cert", "login", "started", "stopped", "error", "test"}


def test_cooldown_collapses_a_storm_into_one_message(recorder):
    alerts = Alerts(webhook="https://example.invalid/hook", cooldown=60.0)
    for _ in range(50):
        alerts.notify("disconnected", "tunnel dropped")
    alerts.notify("quota", "different event, own cooldown")
    assert [c["body"]["event"] for c in recorder.calls] == ["disconnected", "quota"]


def test_no_cooldown_sends_everything(recorder):
    alerts = Alerts(webhook="https://example.invalid/hook", cooldown=0.0)
    for _ in range(3):
        alerts.notify("error", "boom")
    assert len(recorder.calls) == 3


# ----------------------------------------------------------- the queue worker
async def test_the_worker_delivers_queued_alerts(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(Alerts, "_send_one",
                        lambda self, payload: (sent.append(payload), True)[1])
    alerts = Alerts(webhook="https://example.invalid/hook", name="ir-1",
                    events=["all"])
    await alerts.start()
    alerts.notify("connected", "tunnel is up")
    for _ in range(200):
        if sent:
            break
        await asyncio.sleep(0.01)
    await alerts.stop()
    assert [p["text"] for p in sent] == ["[ir-1] tunnel is up"]


async def test_stop_drains_what_is_still_queued(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(Alerts, "_send_one",
                        lambda self, payload: (sent.append(payload), True)[1])
    alerts = Alerts(webhook="https://example.invalid/hook", events=["all"])
    await alerts.start()
    alerts.notify("started", "hello")
    alerts.notify("stopped", "bye")
    await alerts.stop()
    assert [p["event"] for p in sent] == ["started", "stopped"]


async def test_a_full_queue_drops_alerts_instead_of_blocking(monkeypatch):
    alerts = Alerts(webhook="https://example.invalid/hook", cooldown=0.0,
                    events=["all"])
    alerts.queue = asyncio.Queue(maxsize=1)
    alerts.queue.put_nowait({"event": "x"})
    alerts.notify("quota", "one too many")      # must not raise
    assert alerts.queue.qsize() == 1


# ------------------------------------------------------------- certificate age
def _certificate(path, days: int) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "simurgh.test")])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=days))
            .sign(key, hashes.SHA256()))
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(path)


def test_expiry_days_counts_a_real_certificate(tmp_path):
    soon = expiry_days(_certificate(tmp_path / "soon.pem", 10))
    assert soon is not None and 9.5 < soon < 10.5
    far = expiry_days(_certificate(tmp_path / "far.pem", 200))
    assert far is not None and 199 < far < 201


def test_expiry_days_survives_missing_and_broken_files(tmp_path):
    assert expiry_days(str(tmp_path / "nope.pem")) is None
    assert expiry_days("") is None
    junk = tmp_path / "junk.pem"
    junk.write_text("this is not a certificate")
    assert expiry_days(str(junk)) is None


def test_a_watched_certificate_flows_through_the_relay_loop(tmp_path, home):
    from simurgh.relay import CERT_ALERT_DAYS, RelayNode

    cert_file = _certificate(tmp_path / "leaf.pem", 5)
    cfg = RelayConfig(token="tok", name="ir-1", dial="exit",
                      tunnel=TunnelSpec(enabled=True, port=443, carrier="tls",
                                        cert_file=cert_file, cert_auto=False),
                      telegram_token=TOKEN, telegram_chat=CHAT,
                      alert_events=["cert"])
    assert cfg.reverse is True
    node = RelayNode(cfg, home=home, listen=False)
    assert node.alerts.enabled is True
    assert CERT_ALERT_DAYS >= 7          # two weeks is the documented window

    node._watch_certificate("tunnel", cert_file)
    assert node._check_certs() == ["tunnel"]
    # one alert a day per certificate, however often the loop wakes up
    assert node._check_certs() == []

    # a healthy certificate clears the "already warned" mark again
    node._watch_certificate("tunnel", _certificate(tmp_path / "fresh.pem", 90))
    assert node._check_certs() == [] and node._cert_warned == {}


def test_the_exit_watches_the_certificates_it_serves(tmp_path, home):
    from simurgh.exit import CERT_ALERT_DAYS, ExitNode

    cert_file = _certificate(tmp_path / "exit.pem", 3)
    cfg = ExitConfig(token="tok", name="kharej", cert_file=cert_file,
                     cert_auto=False, listen=[
                         ListenSpec(carrier="tls", port=443),
                         ListenSpec(carrier="wss", port=8443),
                         ListenSpec(carrier="tcp", port=8080),
                         ListenSpec(carrier="tcp", port=1, enabled=False),
                     ], telegram_token=TOKEN, telegram_chat=CHAT,
                     alert_events=["cert"])
    node = ExitNode(cfg)
    node._collect_certificates()
    # every served tls/wss listener, and nothing that is not one
    assert sorted(node._watched_certs) == ["tls:443", "wss:8443"]
    assert CERT_ALERT_DAYS >= 7
    assert sorted(node._check_certs()) == ["tls:443", "wss:8443"]
    assert node._check_certs() == []

    # a listener may carry its own certificate (the Go engine always read this)
    mixed = ExitConfig(token="tok", cert_file=cert_file, cert_auto=False,
                       listen=[ListenSpec(carrier="tls", port=443),
                               ListenSpec(carrier="wss", port=8443,
                                          cert_file=_certificate(
                                              tmp_path / "wss.pem", 2),
                                          key_file=str(tmp_path / "wss.key"))])
    mixed_node = ExitNode(mixed)
    mixed_node._collect_certificates()
    assert mixed_node._watched_certs["tls:443"] == cert_file
    assert mixed_node._watched_certs["wss:8443"].endswith("wss.pem")

    # a certificate this node generated itself needs no watching
    auto = ExitConfig(token="tok", cert_file=cert_file, cert_auto=True,
                      listen=[ListenSpec(carrier="tls", port=443)])
    auto_node = ExitNode(auto)
    auto_node._collect_certificates()
    assert auto_node._watched_certs == {}


# ------------------------------------------------------------------- the CLI
def _output(capsys) -> str:
    """Everything the CLI printed: `_err` goes to stderr, `_ok` to stdout."""
    captured = capsys.readouterr()
    return captured.out + captured.err


def _run(argv: list[str]) -> int:
    from simurgh.cli import build_parser

    args = build_parser().parse_args(argv)
    return args.func(args)


def _relay_config(tmp_path, **kw):
    from simurgh.config import ExitEndpoint, save_relay

    home = tmp_path / "simurgh"
    home.mkdir(exist_ok=True)
    cfg = RelayConfig(token="tok", name="ir-tehran",
                      exit=ExitEndpoint(carrier="tls", address="203.0.113.9",
                                        port=443),
                      **kw)
    save_relay(cfg, home / "relay.toml")
    return home, cfg


def test_cli_alert_status_masks_the_token(tmp_path, capsys):
    home, _ = _relay_config(tmp_path, telegram_token=TOKEN, telegram_chat=CHAT,
                            alert_events=["quota", "cert"])
    assert _run(["--home", str(home), "alert", "status"]) == 0
    out = _output(capsys)
    assert "channels: telegram" in out
    assert CHAT in out and "api https://api.telegram.org" in out
    assert TOKEN not in out and "1234…" in out          # the secret never prints
    assert "events:   quota, cert" in out


def test_cli_alert_status_says_what_is_missing(tmp_path, capsys):
    home, _ = _relay_config(tmp_path)
    assert _run(["--home", str(home), "alert", "status"]) == 0
    out = _output(capsys)
    assert "channels: off" in out
    assert "add telegram_token + telegram_chat" in out


def test_cli_alert_test_without_a_channel_exits_1(tmp_path, capsys):
    home, _ = _relay_config(tmp_path)
    assert _run(["--home", str(home), "alert", "test"]) == 1
    assert "no alert channel is configured" in _output(capsys)


def test_cli_alert_test_delivers_through_the_webhook(tmp_path, recorder, capsys):
    home, _ = _relay_config(tmp_path, alert_webhook="https://example.invalid/hook")
    assert _run(["--home", str(home), "alert", "test"]) == 0
    assert "test alert sent through webhook" in _output(capsys)
    assert recorder.calls[0]["url"] == "https://example.invalid/hook"
    assert recorder.calls[0]["body"]["event"] == "test"


def test_cli_alert_test_reports_a_dead_channel(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(alerts_mod.urllib.request, "urlopen",
                        Recorder(error=urllib.error.URLError("down")))
    home, _ = _relay_config(tmp_path, alert_webhook="https://example.invalid/hook")
    assert _run(["--home", str(home), "alert", "test"]) == 1
    assert "did not accept the message" in _output(capsys)


def test_cli_alert_events_reads_and_writes_the_config(tmp_path, capsys):
    from simurgh.config import load_relay

    home, _ = _relay_config(tmp_path)
    assert _run(["--home", str(home), "alert", "events"]) == 0
    assert "connected, disconnected, quota, cert, error" in _output(capsys)

    assert _run(["--home", str(home), "alert", "events", "quota", "cert"]) == 0
    assert "restart the service to apply" in _output(capsys)
    assert load_relay(home / "relay.toml").alert_events == ["quota", "cert"]

    assert _run(["--home", str(home), "alert", "events", "bogus"]) == 2
    assert "unknown event(s): bogus" in _output(capsys)


def test_cli_alert_events_can_be_set_for_the_exit(tmp_path, capsys):
    from simurgh.config import ExitConfig, ListenSpec, load_exit, save_exit

    home = tmp_path / "simurgh"
    home.mkdir()
    save_exit(ExitConfig(token="tok", name="kharej",
                         listen=[ListenSpec(carrier="raw", port=8080)]),
              home / "exit.toml")
    assert _run(["--home", str(home), "alert", "events", "all",
                 "--role", "exit"]) == 0
    assert load_exit(home / "exit.toml").alert_events == ["all"]
    assert _run(["--home", str(home), "alert", "status", "--role", "exit"]) == 0
    assert "events:   all" in _output(capsys)


def test_cli_alert_without_a_config_is_an_error(tmp_path, capsys):
    home = tmp_path / "simurgh"
    home.mkdir()
    assert _run(["--home", str(home), "alert", "status"]) == 2
    assert "cannot read" in _output(capsys)


# ------------------------------------------------- and the whole thing, live
class Collector:
    """A real HTTP endpoint that records the JSON bodies it is POSTed."""

    def __init__(self):
        import http.server
        import threading

        outer = self
        self.bodies: list[dict] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):                             # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    outer.bodies.append(json.loads(raw))
                except ValueError:                         # pragma: no cover
                    pass
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):                  # keep the test quiet
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/alerts"

    def events(self) -> list[str]:
        return [body.get("event") for body in self.bodies]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


async def test_a_live_tunnel_reports_itself_to_the_webhook(certs):
    """The whole path: an event on a real node, the queue, urllib, the endpoint."""
    from conftest import free_port
    from simurgh.config import ExitEndpoint, Mapping
    from simurgh.exit import ExitNode
    from simurgh.relay import RelayNode

    collector = Collector()
    try:
        exit_port, relay_port, web_port = free_port(), free_port(), free_port()

        async def web(reader, writer):
            try:
                await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            except Exception:
                writer.close()
                return
            body = b"<h1>foreign</h1>"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n"
                         b"Connection: close\r\n\r\n" % len(body) + body)
            await writer.drain()
            writer.close()

        web_server = await asyncio.start_server(web, "127.0.0.1", web_port)

        exit_node = ExitNode(ExitConfig(
            token="tok", name="kharej", cert_file=certs[0], key_file=certs[1],
            cert_auto=False, alert_webhook=collector.url, alert_events=["all"],
            listen=[ListenSpec(carrier="raw", host="127.0.0.1", port=exit_port)],
        ))
        relay = RelayNode(RelayConfig(
            token="tok", name="ir-tehran", alert_webhook=collector.url,
            alert_events=["all"],
            exit=ExitEndpoint(carrier="raw", address="127.0.0.1", port=exit_port),
            mappings=[Mapping(name="web", listen=relay_port, target_port=web_port)],
        ))
        await exit_node.start()
        await relay.start()
        for _ in range(80):
            if relay.connected.is_set():
                break
            await asyncio.sleep(0.05)
        assert relay.connected.is_set(), "the tunnel did not come up"

        # one real user connection through the tunnel
        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        writer.write(b"GET / HTTP/1.1\r\nHost: web\r\n\r\n")
        await writer.drain()
        assert b"<h1>foreign</h1>" in await asyncio.wait_for(reader.read(4096), 15)
        writer.close()

        await relay.stop()
        await exit_node.stop()
        web_server.close()

        events = collector.events()
        assert "started" in events and "connected" in events and "stopped" in events
        assert events.count("started") == 2          # relay and exit announced
        texts = [body["text"] for body in collector.bodies]
        assert any("ir-tehran" in text for text in texts)
        assert any("kharej" in text for text in texts)
        # every alert carries its host and a timestamp
        assert all(body["host"] and isinstance(body["time"], float)
                   for body in collector.bodies)
    finally:
        collector.close()


# ------------------------------------------------------- `simurgh alert ...`
def _printed(capsys) -> str:
    """Everything the CLI printed (`_warn` goes to stdout, `_err` to stderr)."""
    captured = capsys.readouterr()
    return captured.out + captured.err


def _alert_cli(home, *argv: str) -> int:
    from simurgh.cli import build_parser

    args = build_parser().parse_args(["--home", str(home), "alert", *argv])
    return args.func(args)


def _relay_home(tmp_path, **kw):
    from simurgh.config import ExitEndpoint, save_relay

    home = tmp_path / "simurgh"
    home.mkdir(exist_ok=True)
    save_relay(RelayConfig(token="tok", name="ir-tehran",
                           exit=ExitEndpoint(carrier="tls",
                                             address="203.0.113.9", port=443),
                           **kw), home / "relay.toml")
    return home


def test_alert_status_lists_the_channels_without_leaking_the_token(tmp_path,
                                                                   capsys):
    home = _relay_home(tmp_path, telegram_token=TOKEN, telegram_chat=CHAT,
                       alert_webhook="https://example.invalid/hook",
                       alert_events=["quota", "cert"])
    assert _alert_cli(home, "status") == 0
    out = _printed(capsys)
    assert "role=relay" and "ir-tehran" in out
    assert "channels: telegram + webhook" in out
    assert f"chat {CHAT}" in out and "api https://api.telegram.org" in out
    assert "1234…" in out and TOKEN not in out
    assert "events:   quota, cert" in out


def test_alert_status_says_what_is_missing(tmp_path, capsys):
    home = _relay_home(tmp_path)
    assert _alert_cli(home, "status") == 0
    out = _printed(capsys)
    assert "channels: off" in out
    assert "add telegram_token + telegram_chat, or alert_webhook" in out


def test_alert_test_without_a_channel_exits_1(tmp_path, capsys):
    home = _relay_home(tmp_path)
    assert _alert_cli(home, "test") == 1
    assert "no alert channel is configured" in _printed(capsys)


def test_alert_test_delivers_through_the_webhook(tmp_path, capsys, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(alerts_mod.urllib.request, "urlopen", rec)
    home = _relay_home(tmp_path, alert_webhook="https://example.invalid/hook")
    assert _alert_cli(home, "test") == 0
    assert "test alert sent through webhook" in _printed(capsys)
    assert rec.calls[0]["body"]["event"] == "test"
    assert "[ir-tehran]" in rec.calls[0]["body"]["text"]


def test_alert_test_reports_a_dead_endpoint(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(alerts_mod.urllib.request, "urlopen",
                        Recorder(error=urllib.error.URLError("no route")))
    home = _relay_home(tmp_path, alert_webhook="https://example.invalid/hook")
    assert _alert_cli(home, "test") == 1
    assert "did not accept the message" in _printed(capsys)


def test_alert_events_reads_and_writes_the_list(tmp_path, capsys):
    from simurgh.config import load_relay

    home = _relay_home(tmp_path)
    assert _alert_cli(home, "events") == 0
    assert "known events:" in _printed(capsys)

    assert _alert_cli(home, "events", "quota", "cert") == 0
    assert "restart the service to apply" in _printed(capsys)
    assert load_relay(home / "relay.toml").alert_events == ["quota", "cert"]

    assert _alert_cli(home, "events", "all") == 0
    assert load_relay(home / "relay.toml").alert_events == ["all"]

    assert _alert_cli(home, "events", "nonsense") == 2
    printed = _printed(capsys)
    assert "unknown event(s): nonsense" in printed
    assert "known events:" in printed


def test_alert_events_for_the_exit(tmp_path, capsys):
    from simurgh.config import (ExitConfig, ListenSpec, load_exit, save_exit)

    home = tmp_path / "simurgh"
    home.mkdir()
    save_exit(ExitConfig(token="tok", name="kharej",
                         listen=[ListenSpec(carrier="raw", port=8080)]),
              home / "exit.toml")
    assert _alert_cli(home, "events", "all", "--role", "exit") == 0
    assert "for the exit: all" in _printed(capsys)
    assert load_exit(home / "exit.toml").alert_events == ["all"]


def test_alert_without_a_config_is_an_error(tmp_path, capsys):
    home = tmp_path / "simurgh"
    home.mkdir()
    assert _alert_cli(home, "status") == 2
    assert "cannot read" in _printed(capsys)

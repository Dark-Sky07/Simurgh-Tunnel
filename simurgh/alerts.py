"""Alerts: tell the operator before a customer does.

Two channels, both optional: a Telegram bot and a generic webhook (Slack,
Discord, ntfy, an internal endpoint -- anything that takes a JSON POST).

Events are emitted by the relay and the exit (tunnel up/down, quota reached,
traffic spike, certificate about to expire, repeated failed logins) and queued
to a worker task, so a slow or unreachable endpoint never blocks the data
plane.  Sending happens in a thread: ``urllib`` is blocking, and the alert
volume is tiny.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request

from .util import get_logger

log = get_logger("simurgh.alerts")

#: event names the code emits (config lists them in ``alert_events``)
EVENTS = ("connected", "disconnected", "quota", "throttle", "cert", "login",
          "started", "stopped", "error", "test")
DEFAULT_EVENTS = ("connected", "disconnected", "quota", "cert", "error")
MAX_QUEUE = 200
#: Telegram Bot API root (a local Bot API server can be configured instead)
TELEGRAM_API = "https://api.telegram.org"


class Alerts:
    """Fire-and-forget notifications."""

    def __init__(self, *, telegram_token: str = "", telegram_chat: str = "",
                 webhook: str = "", events=None, name: str = "",
                 cooldown: float = 20.0, timeout: float = 10.0,
                 telegram_api: str = TELEGRAM_API):
        self.telegram_token = (telegram_token or "").strip()
        self.telegram_chat = str(telegram_chat or "").strip()
        self.webhook = (webhook or "").strip()
        self.events = tuple(events or DEFAULT_EVENTS)
        self.name = name
        self.cooldown = max(0.0, cooldown)
        self.timeout = timeout
        #: https://api.telegram.org by default; overridable so a test (or an
        #: operator behind a local Bot API server) can point somewhere else
        self.telegram_api = (telegram_api or TELEGRAM_API).rstrip("/")
        self.queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._last: dict[str, float] = {}
        self.sent = 0
        self.failed = 0
        self.last_error = ""

    # ------------------------------------------------------------------ config
    @property
    def enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat) or bool(self.webhook)

    def wants(self, event: str) -> bool:
        return event in self.events or "all" in self.events

    @property
    def sent_ok(self) -> int:
        return self.sent

    def snapshot(self) -> dict:
        """What `simurgh alert` and the panel show."""
        return {
            "enabled": self.enabled,
            "channels": self.describe(),
            "events": list(self.events),
            "sent": self.sent,
            "failed": self.failed,
            "last_error": self.last_error,
            "name": self.name,
        }

    def describe(self) -> str:
        bits = []
        if self.telegram_token and self.telegram_chat:
            bits.append("telegram")
        if self.webhook:
            bits.append("webhook")
        return " + ".join(bits) if bits else "off"

    # ------------------------------------------------------------------ runtime
    async def start(self) -> None:
        if not self.enabled:
            return
        self.queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self._task = asyncio.ensure_future(self._worker())

    async def stop(self) -> None:
        # Drain what is still queued -- a "stopped" alert must not be lost just
        # because the worker is going away.  A ``None`` sentinel ends the
        # worker *after* everything before it has been delivered.
        task, queue = self._task, self.queue
        if task is None:
            self.queue = None
            return
        if queue is not None:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:                  # nothing will fit anyway
                task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
        except Exception:                              # pragma: no cover
            task.cancel()
        self._task = None
        self.queue = None
        await asyncio.sleep(0)

    def notify(self, event: str, message: str, level: str = "info", **fields) -> None:
        """Queue an alert.  Never raises, never blocks."""
        if not self.enabled or not self.wants(event):
            return
        now = time.monotonic()
        # `time.monotonic()` is the machine's uptime, not an epoch: a missing
        # entry must mean "never sent", not "sent at 0".  With `0` as the
        # default, every first alert was suppressed on a box whose uptime is
        # shorter than the cooldown -- a fresh server, a container, a CI runner.
        last = self._last.get(event)
        if self.cooldown and last is not None and now - last < self.cooldown:
            return
        self._last[event] = now
        text = message if not self.name else f"[{self.name}] {message}"
        payload = {"event": event, "level": level, "text": text,
                   "host": self.name, "time": time.time(), **fields}
        queue = self.queue
        if queue is None:
            # not inside the event loop (CLI, tests): send synchronously
            self._send_one(payload)
            return
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            log.debug("alert queue is full; dropping %s", event)

    async def _worker(self) -> None:
        assert self.queue is not None
        while True:
            payload = await self.queue.get()
            if payload is None:                        # sentinel from stop()
                return
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._send_one, payload)
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("alert send failed: %s", exc)

    # ------------------------------------------------------------------ sending
    def _send_one(self, payload: dict) -> bool:
        ok = False
        text = payload.get("text", "")
        if self.telegram_token and self.telegram_chat:
            ok |= self._post_json(
                f"{self.telegram_api}/bot{self.telegram_token}/sendMessage",
                {"chat_id": self.telegram_chat, "text": text,
                 "disable_web_page_preview": True},
            )
        if self.webhook:
            ok |= self._post_json(self.webhook, payload)
        if ok:
            self.sent += 1
        else:
            self.failed += 1
        return ok

    def _post_json(self, url: str, body: dict) -> bool:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "simurgh-alerts/1"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return 200 <= resp.status < 300
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.debug("alert to %s failed: %s", url, self.last_error)
            return False

    def test(self) -> bool:
        """Send a test message now (used by ``simurgh alert test``)."""
        if not self.enabled:
            return False
        return self._send_one({
            "event": "test",
            "level": "info",
            "text": (f"[{self.name or 'simurgh'}] test alert — "
                     "alerts are configured correctly"),
            "host": self.name,
            "time": time.time(),
        })


def alerts_from_config(cfg, name: str = "") -> Alerts:
    """Build an :class:`Alerts` from a loaded relay/exit config."""
    return Alerts(
        telegram_token=getattr(cfg, "telegram_token", "") or "",
        telegram_chat=getattr(cfg, "telegram_chat", "") or "",
        webhook=getattr(cfg, "alert_webhook", "") or "",
        events=getattr(cfg, "alert_events", None) or DEFAULT_EVENTS,
        name=name or getattr(cfg, "name", "") or "",
        telegram_api=getattr(cfg, "telegram_api", "") or TELEGRAM_API,
    )

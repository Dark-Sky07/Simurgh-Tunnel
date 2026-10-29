"""Optional, bounded HTTPS webhook notifications for panel-managed node events."""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import os
import queue
import socket
import ssl
import stat
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .tunnel import VERSION
from .wizard import save_config

LOG = logging.getLogger("simurgh.rebuild.notifications")
_QUEUE_LIMIT = 32
_BODY_LIMIT = 2048
_TIMEOUT = 3.0


def validate_webhook_url(value: str) -> str:
    """Accept only an explicit HTTPS endpoint without embedded credentials."""
    value = value.strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("webhook URL is malformed") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or len(value) > 2048):
        raise ValueError("webhook must be an HTTPS URL without credentials or a fragment")
    if "%" in parsed.hostname:
        raise ValueError("webhook hostname must not include a scoped address")
    try:
        parsed.hostname.encode("idna")
    except UnicodeError as exc:
        raise ValueError("webhook hostname is not a valid DNS name") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("webhook port must be from 1 to 65535")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("webhook IP address must be public")
    return value


def _public_address(host: str, port: int) -> str:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise OSError(f"webhook host lookup failed: {exc}") from exc
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses:
        raise OSError("webhook host did not resolve")
    parsed = [ipaddress.ip_address(address.split("%", 1)[0]) for address in addresses]
    if any(not address.is_global for address in parsed):
        raise OSError("webhook must resolve only to public IP addresses")
    return addresses[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to an already validated IP while retaining hostname TLS checks."""

    def __init__(self, host: str, port: int, address: str, *, timeout: float,
                 context: ssl.SSLContext):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _deliver(url: str, event: str, role: str) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname
    if host is None:
        raise ValueError("webhook URL has no host")
    host = host.encode("idna").decode("ascii")
    port = parsed.port or 443
    address = _public_address(host, port)
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    connection = _PinnedHTTPSConnection(host, port, address, timeout=_TIMEOUT, context=context)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    payload = json.dumps({
        "event": event,
        "role": role,
        "version": VERSION,
        "timestamp": int(time.time()),
    }, separators=(",", ":")).encode("utf-8")
    if len(payload) > _BODY_LIMIT:
        raise ValueError("notification payload exceeds its size limit")
    host_header = f"[{host}]" if ":" in host else host
    if port != 443:
        host_header += f":{port}"
    try:
        connection.request("POST", path, body=payload, headers={
            "Host": host_header,
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "User-Agent": f"SimurghTunnel/{VERSION}",
            "Connection": "close",
        })
        response = connection.getresponse()
        response.read(1024)
        if not 200 <= response.status < 300:
            raise OSError(f"webhook returned HTTP {response.status}")
    finally:
        connection.close()


class WebhookNotifier:
    """Send low-volume lifecycle events on one bounded background worker."""

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        self._lock = threading.Lock()
        self._url = self._load()
        self._queue: queue.Queue[tuple[str, str, str] | None] = queue.Queue(_QUEUE_LIMIT)
        self._thread: threading.Thread | None = None
        self._closed = False

    def _load(self) -> str:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(self.config_path, flags)
        except FileNotFoundError:
            return ""
        except OSError as exc:
            LOG.error("cannot safely open webhook notification config: %s", exc)
            return ""
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                LOG.error("refusing non-regular webhook notification config: %s",
                          self.config_path)
                return ""
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = -1
                text = stream.read(4097)
            if len(text) > 4096:
                raise ValueError("webhook notification config exceeds its size limit")
            raw = json.loads(text)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            LOG.error("cannot read webhook notification config: %s", exc)
            return ""
        finally:
            if fd >= 0:
                os.close(fd)
        if not isinstance(raw, dict) or not isinstance(raw.get("webhook_url", ""), str):
            LOG.error("webhook notification config is malformed")
            return ""
        try:
            return validate_webhook_url(raw.get("webhook_url", ""))
        except ValueError as exc:
            LOG.error("webhook notification config is invalid: %s", exc)
            return ""

    @property
    def configured(self) -> bool:
        with self._lock:
            return bool(self._url)

    def set_url(self, value: str) -> None:
        url = validate_webhook_url(value)
        with self._lock:
            if self._closed:
                raise RuntimeError("notification service is closed")
            save_config(self.config_path, {"webhook_url": url})
            self._url = url

    def notify(self, event: str, role: str) -> None:
        with self._lock:
            if not self._url or self._closed:
                return
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="simurgh-webhook-notifier", daemon=True)
                self._thread.start()
            item = (self._url, event, role)
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                LOG.warning("dropping webhook event because the notification queue is full")

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            url, event, role = item
            try:
                _deliver(url, event, role)
            except (OSError, ValueError, ssl.SSLError, http.client.HTTPException) as exc:
                LOG.warning("could not deliver %s notification for %s: %s", event, role, exc)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            thread = self._thread
            if thread is not None and thread.is_alive():
                try:
                    self._queue.put_nowait(None)
                except queue.Full:
                    # The worker will drain one slot; a sentinel is retried asynchronously.
                    threading.Thread(target=self._stop_when_drained, daemon=True).start()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_TIMEOUT + 1)

    def _stop_when_drained(self) -> None:
        self._queue.put(None)

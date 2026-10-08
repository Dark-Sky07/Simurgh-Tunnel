"""The decoy: what an active prober sees when it knocks on the relay's door.

Three behaviours, in order of preference (configured per listening endpoint):

``fallback = "decoy"``          answer with a boring, believable web page.
``fallback = "site:host:port"`` transparently proxy to a real website, so the
                                port really *is* a website.
``fallback = "close"``          say nothing at all (a firewall-ish black hole).

The important part is *consistency*: a prober must not be able to tell the
tunnel apart from a quiet web server, whether it sends TLS, plain HTTP, or
garbage.
"""

from __future__ import annotations

import asyncio

from .util import get_logger

log = get_logger("simurgh.decoy")

#: A deliberately boring default page. Real servers all over the internet serve
#: a variation of this; blending in is the goal, not looking pretty.
DECOY_PAGE = b"""<!DOCTYPE html>
<html>
<head>
<title>Welcome to nginx!</title>
<style>
html { color-scheme: light dark; }
body { width: 35em; margin: 0 auto; font-family: Tahoma, Verdana, Arial, sans-serif; }
</style>
</head>
<body>
<h1>Welcome to nginx!</h1>
<p>If you see this page, the nginx web server is successfully installed and
working. Further configuration is required.</p>

<p>For online documentation and support please refer to
<a href="http://nginx.org/">nginx.org</a>.<br/>
Commercial support is available at
<a href="http://nginx.com/">nginx.com</a>.</p>

<p><em>Thank you for using nginx.</em></p>
</body>
</html>
"""


def _load_page(carrier) -> bytes:
    path = getattr(carrier, "decoy_file", None)
    if path:
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            pass
    return DECOY_PAGE


def http_response(status: str, body: bytes, ctype: str = "text/html") -> bytes:
    return (
        f"HTTP/1.1 {status}\r\n"
        "Server: nginx\r\n"
        f"Date: {_http_date()}\r\n"
        f"Content-Type: {ctype}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("latin-1") + body


def _http_date() -> str:
    from email.utils import formatdate

    return formatdate(usegmt=True)


async def _read_head(stream, initial: bytes, timeout: float = 8.0, limit: int = 16384) -> bytes:
    data = bytearray(initial or b"")
    try:
        while b"\r\n\r\n" not in data and len(data) < limit:
            chunk = await asyncio.wait_for(stream.read_some(4096), timeout)
            if not chunk:
                break
            data += chunk
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    return bytes(data)


def _request_path(head: bytes) -> str | None:
    try:
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
    except Exception:
        return None
    parts = line.split(" ")
    if len(parts) < 2:
        return None
    if not parts[0].upper().startswith(("GET", "HEAD", "POST", "PUT", "OPTIONS")):
        return None
    return parts[1]


async def serve_decoy(stream, carrier, initial: bytes = b"", status: str | None = None) -> None:
    """Answer like a quiet web server over an already-established stream."""
    fallback = (getattr(carrier, "fallback", "decoy") or "decoy").strip()
    try:
        if fallback.startswith("site:"):
            await relay_to_fallback(stream, initial, fallback[5:])
            return
        if fallback == "close":
            return
        head = await _read_head(stream, initial)
        path = _request_path(head)
        if path is None:
            # Not HTTP: a real server would simply give up (or wait). Most
            # probers time out here -- exactly like against a normal site.
            if not head:
                return
            body = DECOY_PAGE
            resp = http_response(status or "400 Bad Request", body)
        else:
            page = _load_page(carrier)
            ok = path in ("/", "/index.html", "/index.htm")
            resp = http_response("200 OK" if ok else (status or "404 Not Found"),
                                 page if ok else _not_found_page())
        stream.write(resp)
        await stream.drain()
    except (ConnectionError, OSError, asyncio.TimeoutError):
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


async def serve_decoy_plain(conn, initial: bytes, carrier) -> None:
    """Same idea, but for a connection that never spoke TLS."""
    await serve_decoy(conn, carrier, initial=initial)


_NOT_FOUND = b"""<html>
<head><title>404 Not Found</title></head>
<body>
<center><h1>404 Not Found</h1></center>
<hr><center>nginx</center>
</body>
</html>
"""


def _not_found_page() -> bytes:
    return _NOT_FOUND


async def relay_to_fallback(stream, initial: bytes, target: str) -> None:
    """Turn the endpoint into a real reverse proxy for a real website."""
    host, _, port = target.rpartition(":")
    if not host:
        host, port = target, "80"
    try:
        rr, ww = await asyncio.open_connection(host, int(port))
    except Exception:
        try:
            stream.close()
        except Exception:
            pass
        return

    if initial:
        ww.write(initial)

    async def a_to_b():
        try:
            while True:
                data = await stream.read_some(65536)
                if not data:
                    break
                ww.write(data)
                await ww.drain()
        except Exception:
            pass
        finally:
            try:
                ww.write_eof()
            except Exception:
                pass

    async def b_to_a():
        try:
            while True:
                data = await rr.read(65536)
                if not data:
                    break
                stream.write(data)
                await stream.drain()
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    await asyncio.gather(a_to_b(), b_to_a(), return_exceptions=True)

"""Shared fixtures for Detroit-school tests: a real HTTP server and a real glovebox process.

- Fixture server: serves tests/fixtures and is listed in GLOVEBOX_ALLOW_HOSTS (the only allowed host, since it lives on 127.0.0.1).
- Secret server: plays an "internal service" and is **not allowlisted**; any successful attack pushes its hit counter above 0.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "TOP-SECRET-TOKEN-7731"
TYPES = {".html": "text/html; charset=utf-8", ".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8",
         ".md": "text/markdown; charset=utf-8", ".json": "application/json", ".png": "image/png",
         ".bin": "application/octet-stream"}
XML_TYPES = {"feed.xml": "application/rss+xml", "atom.xml": "application/atom+xml", "data.xml": "text/xml"}


class _Secret(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):  # noqa: N802
        type(self).hits += 1
        body = SECRET.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, *a):
        pass


def _fixture_handler(secret_port: int):
    class H(BaseHTTPRequestHandler):
        def _send(self, status: int, ctype: str, body: bytes, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            secret = f"http://127.0.0.1:{secret_port}/secret"
            if path == "/flip.bin":
                # First request (browser via proxy) gets the file, later ones (_download refetch) redirect to the
                # internal network: an attacker can tell which request is which
                type(self).flips = getattr(type(self), "flips", 0) + 1
                if type(self).flips == 1:
                    return self._send(200, "application/octet-stream", b"\x00\x01binary")
                return self._send(302, "text/plain", b"", {"Location": secret})
            if path == "/redirect-secret":
                return self._send(302, "text/plain", b"", {"Location": secret})
            if path == "/redirect-decimal":
                return self._send(302, "text/plain", b"", {"Location": f"http://2130706433:{secret_port}/secret"})
            if path == "/attack.html":
                # Malicious page: subresources, JS fetch, XHR, iframe and WebSocket all aimed at the internal secret service
                html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Attack page</title>
<link rel="stylesheet" href="{secret}/css"><script src="{secret}/js"></script></head><body>
<article><h1>A normal-looking article</h1><p>{'This is ordinary content so the main-content extractor treats it as an article. ' * 4}</p>
<img src="{secret}/img"><iframe src="{secret}/frame"></iframe><div id="out">Not fetched yet</div></article>
<script>
fetch('{secret}/fetch').then(r => r.text()).then(t => document.getElementById('out').textContent = 'LEAK:' + t).catch(() => {{}});
const x = new XMLHttpRequest(); x.open('GET', '{secret}/xhr'); x.send();
try {{ new WebSocket('ws://127.0.0.1:{secret_port}/ws'); }} catch (e) {{}}
</script></body></html>"""
                return self._send(200, "text/html; charset=utf-8", html.encode())
            if path.startswith("/slow/"):
                time.sleep(2)
                body = (FIXTURES / "slow.html").read_bytes().replace(b"Slow page", f"Slow page {path[6:]}".encode(), 1)
                return self._send(200, "text/html; charset=utf-8", body)
            f = FIXTURES / path.lstrip("/")
            if not f.is_file():
                return self._send(404, "text/plain", b"not found")
            ctype = XML_TYPES.get(f.name) or TYPES.get(f.suffix, "application/octet-stream")
            return self._send(200, ctype, f.read_bytes())

        def log_message(self, *a):
            pass

    return H


@contextmanager
def servers():
    """Start the fixture and secret servers; yield (fixture base URL, secret server port, secret hit-count getter)."""
    _Secret.hits = 0
    secret = ThreadingHTTPServer(("127.0.0.1", 0), _Secret)
    sport = secret.server_address[1]
    site = ThreadingHTTPServer(("127.0.0.1", 0), _fixture_handler(sport))
    for s in (secret, site):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{site.server_address[1]}", sport, lambda: _Secret.hits
    finally:
        for s in (secret, site):
            s.shutdown()
            s.server_close()


@asynccontextmanager
async def connect(base: str, **env: str):
    """Connect to a real glovebox process as Claude would; only the fixture server is allowlisted.

    Not a pytest async fixture: its setup and teardown run in different tasks, which anyio's cancel scope forbids.
    """
    allow = base.split("://", 1)[1]
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "glovebox.server"],
        env={**os.environ, "GLOVEBOX_ALLOW_HOSTS": allow, **env},
    )
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        yield session


async def call(session: ClientSession, tool: str, **args) -> str:
    res = await session.call_tool(tool, args)
    text = "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")
    if res.is_error:
        raise AssertionError(f"{tool} returned an error: {text}")
    return text


async def call_error(session: ClientSession, tool: str, **args) -> str:
    """A call expected to fail: confirm it really is an error and return the message."""
    res = await session.call_tool(tool, args)
    text = "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")
    assert res.is_error, f"{tool} should have failed but returned: {text[:200]}"
    return text

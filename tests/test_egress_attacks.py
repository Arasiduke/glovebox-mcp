"""Egress-guard tests (Detroit school): a real "internal secret service" + a real browser + real attack techniques.

There is one criterion: **the secret service must be hit 0 times, and the secret string must not appear anywhere Claude can see**.
Checking only that "the content didn't come back" is not enough -- sending the request at all is blind SSRF
(e.g. triggering an internal delete API).
"""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil
import pytest

from glovebox.browser import BrowserSession
from glovebox.egress import EgressProxy
from support import SECRET, call, call_error, connect, servers


async def test_internal_addresses_in_any_notation_are_blocked_before_any_request():
    with servers() as (base, sport, hits):
        async with connect(base) as claude:
            for url in (f"http://127.0.0.1:{sport}/secret", f"http://localhost:{sport}/secret",
                        f"http://2130706433:{sport}/secret", f"http://0x7f000001:{sport}/secret",
                        f"http://[::1]:{sport}/secret", "http://169.254.169.254/latest/meta-data/",
                        f"http://127.1:{sport}/secret"):
                msg = await call_error(claude, "open", url=url)
                assert "internal" in msg or "this machine" in msg, (url, msg)
        assert hits() == 0


async def test_302_redirect_to_internal_network_never_sends_the_request():
    with servers() as (base, sport, hits):
        async with connect(base) as claude:
            for path in ("/redirect-secret", "/redirect-decimal"):
                msg = await call_error(claude, "open", url=f"{base}{path}")
                assert "egress guard" in msg, msg
                assert SECRET not in msg
        assert hits() == 0


async def test_malicious_page_subresources_fetch_xhr_iframe_websocket_cannot_reach_internal_network():
    with servers() as (base, sport, hits):
        async with connect(base) as claude:
            outline = await call(claude, "open", url=f"{base}/attack.html")
            body = outline + "\n".join([await call(claude, "read", page_id="p1", section=1)])
            assert SECRET not in body
            assert "LEAK:" not in body
        assert hits() == 0, f"internal secret service was hit {hits()} times"


async def test_allowlist_admits_only_the_exact_host_port():
    """The fixture server is allowlisted; another port on the same 127.0.0.1 (the secret service) is still blocked."""
    with servers() as (base, sport, hits):
        async with connect(base) as claude:
            assert "Coffee Roasting" in await call(claude, "open", url=f"{base}/hidden_injection.html")
            await call_error(claude, "open", url=f"http://127.0.0.1:{sport}/")
        assert hits() == 0


async def test_over_memory_limit_browser_is_killed_and_next_call_explains_then_restarts():
    with servers() as (base, _, _):
        async with connect(base, GLOVEBOX_MEM_LIMIT_MB="1") as claude:
            first = await claude.call_tool("open", {"url": f"{base}/hidden_injection.html"})
            texts = [c.text for c in first.content if getattr(c, "type", "") == "text"]
            if not first.is_error:  # the watchdog may fire after the page was processed; the next call is sure to hit it
                res = await claude.call_tool("outline", {"page_id": "p1"})
                assert res.is_error
                texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
            assert any("memory" in t and "limit" in t for t in texts), texts


async def test_watchdog_really_kills_all_browser_processes_then_restarts(monkeypatch):
    """Checking the error message is not enough: the old browser processes must actually be gone, or no memory was freed."""
    with servers() as (base, _, _):
        monkeypatch.setenv("GLOVEBOX_ALLOW_HOSTS", base.split("//")[1])
        s = BrowserSession()
        s.mem_limit = 1 << 50
        try:
            await s.open(f"{base}/hidden_injection.html")
            old = psutil.Process().children(recursive=True)
            assert old
            s.mem_limit = 1
            for _ in range(50):
                if s._reset_reason:
                    break
                await asyncio.sleep(0.1)
            assert s._reset_reason
            _, alive = psutil.wait_procs(old, timeout=5)
            assert not alive, f"processes still alive: {[p.pid for p in alive]}"
            s.mem_limit = 1 << 50
            with pytest.raises(RuntimeError, match="memory"):
                await s.open(f"{base}/hidden_injection.html")
            await s.open(f"{base}/hidden_injection.html")
            assert s.launches == 2
        finally:
            await s.close()


async def test_download_refetch_cannot_bypass_the_proxy_via_no_proxy():
    """_download uses urllib, which by default honors NO_PROXY / the Windows registry and connects directly,
    skipping the internal-network check that lives in the proxy."""
    with servers() as (base, _, hits):
        async with connect(base, NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost") as claude:
            await claude.call_tool("open", {"url": f"{base}/flip.bin"})
        assert hits() == 0, f"internal secret service was hit {hits()} times"


class _KeepAlive(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive by default: without Connection: close it keeps serving the next request on the same connection
    hits = 0

    def do_GET(self):  # noqa: N802
        type(self).hits += 1
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


async def test_proxy_forces_connection_close_so_a_second_request_on_the_connection_is_not_forwarded(monkeypatch):
    """The proxy checks only the first request on a connection and forwards the rest as-is;
    if upstream keeps the connection alive, later requests bypass the check."""
    up = ThreadingHTTPServer(("127.0.0.1", 0), _KeepAlive)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{up.server_port}"
    monkeypatch.setenv("GLOVEBOX_ALLOW_HOSTS", host)
    proxy = EgressProxy()
    await proxy.start()
    try:
        r, w = await asyncio.open_connection("127.0.0.1", proxy.port)
        first = f"GET http://{host}/a HTTP/1.1\r\nHost: {host}\r\nConnection: keep-alive\r\n\r\n"
        smuggled = "GET http://10.0.0.1/admin HTTP/1.1\r\nHost: 10.0.0.1\r\n\r\n"
        w.write((first + smuggled).encode())
        await w.drain()
        await asyncio.wait_for(r.read(), timeout=5)  # reading to EOF = upstream honored Connection: close
        w.close()
        assert _KeepAlive.hits == 1, f"{_KeepAlive.hits} requests on one connection were forwarded"
    finally:
        await proxy.close()
        up.shutdown()

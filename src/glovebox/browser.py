"""Browser layer: Chromium driven by Playwright.

- Each launch uses a fresh incognito context: no profile, no login state, downloads disabled, service workers blocked.
- All connections are forced through the egress proxy; WebRTC may only use the proxy, so no direct UDP to the LAN.
- Headless by default (this app is for Claude, no GUI needed); set `GLOVEBOX_HEADED=1` to watch a window.
- Concurrency: simultaneous requests don't block each other -- the browser is initialized once (lock), each tab has
  its own lock, and CPU work such as extraction and parsing runs in threads so the event loop stays free.
- Resource guard: a watchdog sums memory over the browser process tree, kills the whole tree when over the limit,
  and the next call rebuilds it.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import psutil
from playwright.async_api import Browser, BrowserContext, Page, Playwright, Response, async_playwright

from glovebox import formats, search
from glovebox.cleaning import STRIP_HIDDEN_JS
from glovebox.document import Doc, build_doc
from glovebox.egress import Blocked, EgressProxy, check_url

_GOTO_TIMEOUT_MS = 30_000
_IDLE_CAP_MS = 5_000
_VIEWPORT = {"width": 1280, "height": 900}
_MAX_TABS = 10
_MAX_HTML_CHARS = 15_000_000
_MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024
_MB = 1024 * 1024
# busy: a visible aria-busy region remains, or a text node consists solely of a placeholder word
# (whole-node match, so body text that merely mentions "loading" doesn't count).
# Chinese placeholders stay so Chinese sites are detected too.
_SETTLE_PROBE_JS = r"""() => {
  const text = document.body ? document.body.innerText : "";
  const placeholder = /^(載入中|加載中|讀取中|读取中|請稍候|请稍候|loading|please wait)[.…。\s]*$/i;
  const shown = el => el && el.checkVisibility ? el.checkVisibility() : !!(el && el.offsetParent);
  let busy = [...document.querySelectorAll('[aria-busy="true"]')].some(shown);
  if (!busy && document.body) {
    const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = walk.nextNode(); n && !busy; n = walk.nextNode())
      busy = placeholder.test(n.nodeValue.trim()) && shown(n.parentElement);
  }
  return {text, busy};
}"""


@dataclass
class _Tab:
    page: Page
    doc: Doc | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class _AlwaysProxy(urllib.request.ProxyHandler):
    """The stock handler honors NO_PROXY / the Windows registry and connects directly, skipping the proxy's internal-network check; here every hop is forced through the proxy."""

    def proxy_open(self, req, proxy, type):  # noqa: A002
        req.set_proxy(proxy.split("://", 1)[1], "http")


class BrowserSession:
    def __init__(self, headless: bool | None = None) -> None:
        self.headless = os.environ.get("GLOVEBOX_HEADED") != "1" if headless is None else headless
        self.mem_limit = int(os.environ.get("GLOVEBOX_MEM_LIMIT_MB", "3072")) * _MB
        self.region = search.region()
        self._proxy = EgressProxy()
        self._init_lock = asyncio.Lock()
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._ctx: BrowserContext | None = None
        self._watchdog: asyncio.Task | None = None
        self._tabs: OrderedDict[str, _Tab] = OrderedDict()
        self._seq = 0
        self._reset_reason: str | None = None
        self.launches = 0  # lets tests confirm concurrent calls launch only once

    # ── Lifecycle ────────────────────────────────────────────

    async def _context(self) -> BrowserContext:
        async with self._init_lock:
            if self._ctx is None:
                if not self._proxy.port:
                    await self._proxy.start()
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(
                    headless=self.headless,
                    # bypass "<-loopback>": Chromium lets localhost bypass the proxy by default; this removes that implicit exception
                    proxy={"server": f"http://127.0.0.1:{self._proxy.port}", "bypass": "<-loopback>"},
                    args=["--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                          "--webrtc-ip-handling-policy=disable_non_proxied_udp"],
                )
                # Drop "HeadlessChrome" from the User-Agent: many sites (Bing included) block it or serve degraded results
                probe = await self._browser.new_page()
                ua = (await probe.evaluate("() => navigator.userAgent")).replace("HeadlessChrome", "Chrome")
                await probe.close()
                self._ctx = await self._browser.new_context(
                    viewport=_VIEWPORT, locale=search.REGIONS[self.region][0], user_agent=ua,
                    accept_downloads=False, service_workers="block")
                self.launches += 1
                self._watchdog = asyncio.create_task(self._watch())
        return self._ctx

    async def _watch(self) -> None:
        me = psutil.Process()
        while self._ctx is not None:
            await asyncio.sleep(0.5)
            total, kids = 0, []
            with contextlib.suppress(psutil.Error):
                kids = me.children(recursive=True)
            for p in kids:
                with contextlib.suppress(psutil.Error):
                    total += p.memory_info().rss
            if total > self.mem_limit:
                for p in kids:
                    with contextlib.suppress(psutil.Error):
                        p.kill()
                self._drop(f"Browser memory {total // _MB:,} MB exceeded the {self.mem_limit // _MB:,} MB limit and was force-closed; "
                           "all tabs are gone, the next call will start a clean browser automatically")
                return

    def _drop(self, reason: str) -> None:
        """Drop all handles after the browser is killed (the processes are dead, so don't await close)."""
        self._ctx = self._browser = self._pw = None
        self._tabs.clear()
        self._reset_reason = reason

    def _raise_if_reset(self) -> None:
        if self._reset_reason:
            reason, self._reset_reason = self._reset_reason, None
            raise RuntimeError(reason)

    async def close(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
        for closer in (self._ctx, self._browser):
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer.close()
        if self._pw is not None:
            with contextlib.suppress(Exception):
                await self._pw.stop()
        self._ctx = self._browser = self._pw = None
        self._tabs.clear()
        await self._proxy.close()

    # ── Tabs ─────────────────────────────────────────────────

    def _tab(self, page_id: str) -> _Tab:
        self._raise_if_reset()
        if page_id not in self._tabs:
            known = ", ".join(self._tabs) or "(no pages open)"
            raise ValueError(f"No page {page_id}; open pages: {known}")
        return self._tabs[page_id]

    def doc(self, page_id: str) -> Doc:
        doc = self._tab(page_id).doc
        if doc is None:
            raise ValueError(f"Page {page_id} is still loading")
        return doc

    async def _new_tab(self) -> tuple[str, _Tab]:
        ctx = await self._context()
        while len(self._tabs) >= _MAX_TABS:  # tab limit: close the oldest
            _, old = self._tabs.popitem(last=False)
            with contextlib.suppress(Exception):
                await old.page.close()
        self._seq += 1
        page_id = f"p{self._seq}"
        tab = _Tab(await ctx.new_page())
        self._tabs[page_id] = tab
        return page_id, tab

    # ── Public operations ────────────────────────────────────

    async def open(self, url: str, page_id: str | None = None) -> Doc:
        """Open a URL; with page_id, navigate within that tab (keeping history), otherwise open a new tab."""
        try:
            check_url(url)
        except Blocked as e:
            raise ValueError(str(e)) from None
        self._raise_if_reset()
        if page_id is None:
            page_id, tab = await self._new_tab()
            fresh = True
        else:
            tab, fresh = self._tab(page_id), False
        async with tab.lock:
            try:
                resp = await tab.page.goto(url, wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
            except Exception as e:
                if "Download is starting" in str(e):
                    tab.doc = await self._download(page_id, url)
                    return tab.doc
                if fresh:
                    self._tabs.pop(page_id, None)
                    with contextlib.suppress(Exception):
                        await tab.page.close()
                raise self._explain(url, e) from None
            tab.doc = await self._guard(url, self._process(tab.page, page_id, resp))
            return tab.doc

    async def _guard(self, url: str, coro):
        """Turn browser errors during processing (e.g. tab killed by the watchdog) into explained errors so MCP doesn't swallow the cause."""
        try:
            return await coro
        except (ValueError, RuntimeError):
            raise
        except Exception as e:
            raise self._explain(url, e) from None

    async def back(self, page_id: str) -> Doc:
        tab = self._tab(page_id)
        async with tab.lock:
            before = tab.page.url
            try:
                resp = await tab.page.go_back(wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
            except Exception as e:
                raise self._explain(before, e) from None
            if resp is None and tab.page.url == before:
                raise ValueError(f"Page {page_id} has no previous page")
            tab.doc = await self._guard(tab.page.url, self._process(tab.page, page_id, resp))
            return tab.doc

    async def screenshot(self, page_id: str, part: int = 1) -> tuple[bytes, int]:
        """Capture part number `part` (one part = one viewport height); returns (JPEG, total parts)."""
        tab = self._tab(page_id)
        async with tab.lock:
            return await self._guard(tab.page.url, self._shoot(tab.page, page_id, part))

    async def _shoot(self, page: Page, page_id: str, part: int) -> tuple[bytes, int]:
        height = await page.evaluate("() => document.documentElement.scrollHeight")
        parts = max(1, min(20, -(-height // _VIEWPORT["height"])))
        if not 1 <= part <= parts:
            raise ValueError(f"Page {page_id} has {parts} parts; there is no part {part}")
        await page.evaluate(f"() => window.scrollTo(0, {(part - 1) * _VIEWPORT['height']})")
        await page.wait_for_timeout(200)
        img = await page.screenshot(type="jpeg", quality=60)
        await page.evaluate("() => window.scrollTo(0, 0)")
        return img, parts

    # ── Internals ────────────────────────────────────────────

    def _explain(self, url: str, e: Exception) -> Exception:
        if self._reset_reason:
            reason, self._reset_reason = self._reset_reason, None
            return RuntimeError(reason)
        host = (urlsplit(url).hostname or "").lower()
        why = self._proxy.reason_for(host)
        if why:
            return ValueError(f"Blocked by egress guard: {why}")
        return RuntimeError(f"Failed to open page ({type(e).__name__}): {str(e).splitlines()[0][:200]}")

    async def _process(self, page: Page, page_id: str, resp: Response | None) -> Doc:
        final_host = (urlsplit(page.url).hostname or "").lower()
        if resp is not None and resp.status == 403 and self._proxy.reason_for(final_host):
            raise ValueError(f"Blocked by egress guard: {self._proxy.reason_for(final_host)}")
        notice = f"HTTP {resp.status}" if resp is not None and resp.status >= 400 else ""
        ctype = (resp.headers.get("content-type", "") if resp is not None else "").lower()
        if ctype and "html" not in ctype:
            # Not resp.body(): Chromium decodes text without a charset as Latin-1 and re-encodes it,
            # so those aren't the original bytes. Refetch the raw content through the same egress proxy instead.
            return await self._download(page_id, page.url, notice)
        await self._settle(page)
        r = await page.evaluate(STRIP_HIDDEN_JS)
        html = r["html"]
        if len(html) > _MAX_HTML_CHARS:
            html = html[:_MAX_HTML_CHARS]
            notice = "; ".join(x for x in (notice, "Page too large; only the first 15 MB processed") if x)
        if search.is_search_page(page.url):
            return await asyncio.to_thread(search.results_doc, page_id, html, page.url)
        return await asyncio.to_thread(build_doc, page_id, html, r["title"], page.url, r["visibleText"], notice)

    async def _settle(self, page: Page) -> None:
        """Wait for dynamic content: network idle (capped) -> scroll to bottom to trigger lazy loading -> wait until visible text stops changing."""
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=_IDLE_CAP_MS)
        prev = -1
        for _ in range(8):
            h = await page.evaluate(
                "() => { const h = document.documentElement.scrollHeight; window.scrollTo(0, h); return h; }")
            await page.wait_for_timeout(350)
            if h == prev:
                break
            prev = h
        await page.evaluate("() => window.scrollTo(0, 0)")
        # "Text stopped changing" alone would accept a static "Loading..." as final: keep waiting while a placeholder is visible, up to ~9 s
        last, stable = None, 0
        for _ in range(30):
            state = await page.evaluate(_SETTLE_PROBE_JS)
            stable = stable + 1 if state["text"] == last and not state["busy"] else 0
            if stable >= 2:
                break
            last = state["text"]
            await page.wait_for_timeout(300)

    async def _download(self, page_id: str, url: str, notice: str = "") -> Doc:
        """When the browser treats it as a download (PDF, binary), fetch the bytes from Python through the same egress proxy."""
        proxy = f"http://127.0.0.1:{self._proxy.port}"
        opener = urllib.request.build_opener(_AlwaysProxy({"http": proxy, "https": proxy}))

        def fetch() -> tuple[str, str, bytes]:
            with opener.open(url, timeout=_GOTO_TIMEOUT_MS / 1000) as r:
                data = r.read(_MAX_DOWNLOAD_BYTES + 1)
                return r.geturl(), r.headers.get("content-type", ""), data

        try:
            final, ctype, data = await asyncio.to_thread(fetch)
        except Exception as e:
            raise self._explain(url, e) from None
        if len(data) > _MAX_DOWNLOAD_BYTES:
            notice = "; ".join(x for x in (notice, "File too large; only the first 30 MB processed") if x)
        return await asyncio.to_thread(formats.from_bytes, page_id, final, ctype, data[:_MAX_DOWNLOAD_BYTES], notice)

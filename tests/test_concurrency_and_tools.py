"""Concurrency and browsing-tool tests (Detroit school).

Concurrency (requirement: "three searches can run at once without one blocking the others"):
no hard-coded seconds; time a single slow page as the baseline -- three at once take ~3x if serialized, ~1x if parallel.
"""
from __future__ import annotations

import asyncio
import re
import time

from support import call, call_error, connect, servers


async def test_three_slow_pages_opened_together_do_not_block_each_other():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            await call(claude, "open", url=f"{base}/hidden_injection.html")  # warm up first to exclude browser startup time
            t0 = time.perf_counter()
            await call(claude, "open", url=f"{base}/slow/0")
            single = time.perf_counter() - t0

            t0 = time.perf_counter()
            outs = await asyncio.gather(*(call(claude, "open", url=f"{base}/slow/{i}") for i in (1, 2, 3)))
            together = time.perf_counter() - t0

            assert together < single * 2, f"one page {single:.1f}s, three at once {together:.1f}s -- looks serialized"
            titles = {re.match(r"\[page (p\d+)\] (.+)", o).group(2) for o in outs}
            assert titles == {"Slow page 1", "Slow page 2", "Slow page 3"}, titles
            ids = {re.match(r"\[page (p\d+)\]", o).group(1) for o in outs}
            assert len(ids) == 3, "each open must get its own tab"


async def test_reading_another_page_does_not_wait_for_a_pending_open():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            await call(claude, "open", url=f"{base}/hidden_injection.html")
            slow = asyncio.create_task(call(claude, "open", url=f"{base}/slow/9"))
            await asyncio.sleep(0.3)
            t0 = time.perf_counter()
            await call(claude, "read", page_id="p1", section=1)
            assert time.perf_counter() - t0 < 1.0, "reading an already open page was blocked by the slow page"
            await slow


async def test_links_lists_only_valid_links_and_click_takes_only_numbers():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            await call(claude, "open", url=f"{base}/links.html")
            listing = await call(claude, "links", page_id="p1")
            assert "javascript:" not in listing and "#top" not in listing
            nums = dict((text, int(n)) for n, text in re.findall(r"\[(\d+)\] (.+?) →", listing))
            assert "Second page" in nums and "External site" in nums

            assert "Second page" in await call(claude, "click", page_id="p1", n=nums["Second page"])
            assert "Link test" in await call(claude, "back", page_id="p1")
            assert "No link #99" in await call_error(claude, "click", page_id="p1", n=99)


async def test_screenshot_returns_an_image_and_reports_total_parts():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            await call(claude, "open", url=f"{base}/long_article.html")
            res = await claude.call_tool("screenshot", {"page_id": "p1", "part": 2})
            assert not res.is_error
            kinds = [c.type for c in res.content]
            assert "image" in kinds
            note = next(c.text for c in res.content if c.type == "text")
            m = re.search(r"screen part 2/(\d+)", note)
            assert m and int(m.group(1)) >= 2
            assert "there is no part 999" in await call_error(claude, "screenshot", page_id="p1", part=999)

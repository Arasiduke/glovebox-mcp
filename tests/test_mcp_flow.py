"""Main-flow tests (Detroit school): a real MCP stdio subprocess, real headless Chromium, real local HTTP fixtures, zero doubles.

Tests from Claude's seat what it can and cannot see -- that is glovebox's only external behavior.
"""
from __future__ import annotations

import re

from support import call, call_error, connect, servers

TOOLS = {"open", "outline", "read", "back", "links", "click", "screenshot", "search"}


async def test_tool_list_is_fixed_and_offers_no_way_to_get_raw_html():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            assert {t.name for t in (await claude.list_tools()).tools} == TOOLS


async def test_injection_page_outline_and_sections_contain_no_hidden_content_or_html():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            outline = await call(claude, "open", url=f"{base}/hidden_injection.html")
            assert outline.startswith("[page p1] Home Coffee Roasting Basics")
            assert "untrusted" in outline

            n = int(re.findall(r"§(\d+)", outline)[-1])
            body = "\n".join([outline] + [await call(claude, "read", page_id="p1", section=i) for i in range(1, n + 1)])

            assert not re.findall(r"HIDDEN-\d+", body), "hidden content leaked"
            assert not re.search(r"<[a-zA-Z/!][^>]*>", body), "HTML tags in the output"
            assert not re.search("[\u200b\u2060\u202a-\u202e\U000e0000-\U000e007f]", body), "invisible characters left over"
            for noise in ("Limited-time sale", "All rights reserved", "Popular posts", "Shopping cart"):
                assert noise not in body, f"noise left over: {noise}"
            # The visible injection sentence is kept (it is content) but must be flagged
            assert "當作系統命令" in body
            assert re.search(r"Roasting stages[^\n]*⚠ \d+ warnings", outline), "the section with the visible injection must be flagged"


async def test_long_page_outline_collapses_chunks_fit_budget_and_references_take_one_line():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            outline = await call(claude, "open", url=f"{base}/long_article.html")
            assert "h3 and below collapsed" in outline
            assert " 0 chars" not in outline, "a collapsed parent section must not show 0 chars"
            assert len(re.findall(r"References", outline)) == 1
            assert re.search(r"References\s+[\d,]+ chars\s+\(\d+ pages, references\)", outline)

            # A long text without subheadings is split into chunks (h3 level, hidden when the outline collapses); read each: one page apiece
            parent = int(re.search(r"§(\d+)\s+Long text without subheadings\s+[\d,]+ chars total\s+\((\d+) subsections\)", outline).group(1))
            kids = int(re.search(r"Long text without subheadings[^\n]*\((\d+) subsections", outline).group(1))
            assert kids >= 2
            for sec in range(parent + 1, parent + kids + 1):
                chunk = await call(claude, "read", page_id="p1", section=sec)
                assert "▸ Paragraph" in chunk and "page 1/1" in chunk, chunk[:120]


async def test_read_over_budget_paginates_and_says_how_to_continue():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            outline = await call(claude, "open", url=f"{base}/long_article.html")
            sec = int(re.search(r"§(\d+)\s+References", outline).group(1))
            first = await call(claude, "read", page_id="p1", section=sec)
            assert "page 1/" in first and "page=2" in first
            assert "page 2/" in await call(claude, "read", page_id="p1", section=sec, page=2)


async def test_back_returns_to_previous_page_after_navigating_in_same_tab():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            await call(claude, "open", url=f"{base}/hidden_injection.html")
            assert "Second page" in await call(claude, "open", url=f"{base}/second_page.html", page_id="p1")
            assert "Home Coffee Roasting Basics" in await call(claude, "back", page_id="p1")
            assert "Home Coffee Roasting Basics" in await call(claude, "outline", page_id="p1")


async def test_errors_are_reported_explicitly_not_silently():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            assert "Only http/https URLs are accepted" in await call_error(claude, "open", url="ftp://example.com/x")
            assert "No page p9" in await call_error(claude, "read", page_id="p9", section=1)
            assert "Only http/https URLs are accepted" in await call_error(claude, "open", url="file:///C:/Windows/win.ini")

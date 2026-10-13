"""Content-type tests (Detroit school): one real fixture per type, through the real browser and egress proxy.

Static HTML is covered in test_mcp_flow; this file covers dynamic pages and non-HTML content.
"""
from __future__ import annotations

import re

from support import call, call_error, connect, servers


async def _everything(claude, url: str) -> str:
    """Open the page and read every section; return all the text Claude can see."""
    outline = await call(claude, "open", url=url)
    pid = re.match(r"\[page (p\d+)\]", outline).group(1)
    nums = sorted({int(n) for n in re.findall(r"§(\d+)", outline)})
    total = max(nums) if nums else 0
    parts = [outline]
    for i in range(1, total + 1):
        parts.append(await call(claude, "read", page_id=pid, section=i))
    return "\n".join(parts)


async def test_spa_late_rendered_content_is_captured():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            text = await _everything(claude, f"{base}/spa.html")
            assert "SPA-RENDERED" in text
            assert "載入中" not in text


async def test_placeholder_static_for_seconds_does_not_end_waiting_early():
    """The old mechanism only checked that text length was stable, so a "Loading..." still for 0.6 s counted as final."""
    with servers() as (base, _, _):
        async with connect(base) as claude:
            text = await _everything(claude, f"{base}/spa_late.html")
            assert "LATE-RENDERED" in text
            assert "載入中" not in text


async def test_lazy_load_all_three_batches_appearing_on_scroll_are_captured():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            text = await _everything(claude, f"{base}/lazy.html")
            for n in (1, 2, 3):
                assert f"LAZY-{n}" in text, f"lazy-loaded batch {n} missing"


async def test_shadow_dom_content_is_captured_and_hidden_parts_still_stripped():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            text = await _everything(claude, f"{base}/shadow.html")
            assert "SHADOW-VISIBLE" in text
            assert "SHADOW-HIDDEN" not in text


async def test_product_listing_keeps_all_twenty_items_without_nav_or_footer():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            text = await _everything(claude, f"{base}/listing.html")
            for i in range(1, 21):
                assert f"ITEM-{i:02d}" in text
            assert "All rights reserved" not in text and "Shopping cart" not in text


async def test_portal_page_falls_back_to_whole_page_mode_when_readability_misses():
    """Verified on portal.html: readability alone captures none of the 24 items."""
    with servers() as (base, _, _):
        async with connect(base) as claude:
            text = await _everything(claude, f"{base}/portal.html")
            assert "whole-page mode" in text
            for i in range(1, 25):
                assert f"PORTAL-{i:02d}" in text, f"item {i} missing"
            assert "All rights reserved" not in text and "Log in" not in text


async def test_pdf_does_not_fail_on_headless_download_and_each_page_is_a_section():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            text = await _everything(claude, f"{base}/doc.pdf")
            assert "Content type: PDF" in text
            assert "Page 1" in text and "Page 2" in text
            assert "PDF-PAGE-ONE" in text and "PDF-PAGE-TWO" in text


async def test_plain_text_keeps_indentation():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            text = await _everything(claude, f"{base}/notes.txt")
            assert "plain text" in text and "PLAIN-TEXT" in text
            assert "  indentation must be kept" in text


async def test_markdown_is_split_into_sections_by_heading():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            outline = await call(claude, "open", url=f"{base}/readme.md")
            assert re.search(r"§\d+\s+Install", outline) and re.search(r"§\d+\s+Test", outline)
            assert "MARKDOWN-OK" in await call(claude, "read", page_id="p1", section=1)


async def test_json_gets_one_section_per_key():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            text = await _everything(claude, f"{base}/data.json")
            assert re.search(r"§\d+\s+name", text) and re.search(r"§\d+\s+deps", text)
            assert "JSON-OK 中文" in text  # non-ASCII must survive JSON pretty-printing


async def test_rss_and_atom_one_section_per_item_with_clickable_links():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            rss = await _everything(claude, f"{base}/feed.xml")
            assert "Test feed" in rss and "RSS-ITEM-1" in rss and "First item summary" in rss
            assert "<p>" not in rss
            links = await call(claude, "links", page_id="p1")
            assert "https://example.com/1" in links
            atom = await _everything(claude, f"{base}/atom.xml")
            assert "ATOM-ENTRY" in atom and "Atom summary" in atom


async def test_generic_xml_does_not_expand_external_entities():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            text = await _everything(claude, f"{base}/data.xml")
            assert "XML-TEXT" in text
            assert "[fonts]" not in text.lower() and "for 16-bit" not in text.lower()


async def test_image_suggests_screenshot_and_binary_is_rejected_explicitly():
    with servers() as (base, _, _):
        async with connect(base) as claude:
            assert "screenshot" in await call(claude, "read", page_id=re.match(
                r"\[page (p\d+)\]", await call(claude, "open", url=f"{base}/pixel.png")).group(1), section=1)
            assert "Unsupported content type" in await call_error(claude, "open", url=f"{base}/blob.bin")


async def test_sphinx_docs_version_notes_are_not_dropped_as_noise():
    """readability drops divs under 25 chars, so Sphinx "Added in version" notes vanished wholesale."""
    with servers() as (base, _, _):
        async with connect(base) as claude:
            text = await _everything(claude, f"{base}/sphinx_doc.html")
            assert "whole-page mode" not in text  # the bug only shows on the readability path
            for i in range(1, 7):
                # The "added" note is deliberately under 25 chars (real docs are that short); the "changed" note
                # carries a link, so its link density is high
                assert f"Added in version 3.{i}." in text, f"version-added note {i} missing"
                for tag in ("VER-CHANGED", "VER-DEPR"):
                    assert f"{tag}-{i}" in text, f"{tag}-{i} missing"

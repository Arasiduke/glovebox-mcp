"""Search: Bing first, DuckDuckGo's HTML version as fallback (neither needs an API key).

Tested 2026-10-12: DuckDuckGo always serves an anti-bot challenge to headless browsers; Bing works,
but needs locale parameters or Chinese queries get unrelated results.

The results page is itself an external web page, so it goes through the egress proxy and cleaning like any other;
this module only turns the results into a clickable list.
"""
from __future__ import annotations

import base64
import os
from urllib.parse import parse_qs, quote_plus, urljoin, urlsplit

import lxml.html

from glovebox.cleaning import clean_text
from glovebox.document import Doc, Link, Section, assemble

BING = "www.bing.com"
DDG = "html.duckduckgo.com"

# GLOVEBOX_REGION -> (browser locale, Bing locale parameters). Bing needs these or results drift to another region.
REGIONS = {
    "zh-TW": ("zh-TW", "setlang=zh-Hant&cc=TW&mkt=zh-TW"),
    "en-US": ("en-US", "setlang=en&cc=US&mkt=en-US"),
}
DEFAULT_REGION = "zh-TW"


def region(name: str | None = None) -> str:
    """The region to use: argument, else GLOVEBOX_REGION, else the default. Unknown values fail at startup, not silently.

    >>> region("en-US")
    'en-US'
    >>> region("fr")
    Traceback (most recent call last):
    ...
    ValueError: GLOVEBOX_REGION='fr' is not supported; use one of: zh-TW, en-US
    """
    name = name or os.environ.get("GLOVEBOX_REGION") or DEFAULT_REGION
    if name not in REGIONS:
        raise ValueError(f"GLOVEBOX_REGION={name!r} is not supported; use one of: {', '.join(REGIONS)}")
    return name


def search_urls(query: str, region_name: str = DEFAULT_REGION) -> list[str]:
    """Search URLs to try, in order.

    >>> search_urls("coffee roasting")[0]
    'https://www.bing.com/search?q=coffee+roasting&setlang=zh-Hant&cc=TW&mkt=zh-TW'
    >>> search_urls("coffee roasting", "en-US")[0]
    'https://www.bing.com/search?q=coffee+roasting&setlang=en&cc=US&mkt=en-US'
    >>> search_urls("x")[1]
    'https://html.duckduckgo.com/html/?q=x'
    """
    q = quote_plus(query)
    return [f"https://{BING}/search?q={q}&{REGIONS[region_name][1]}", f"https://{DDG}/html/?q={q}"]


def is_search_page(url: str) -> bool:
    p = urlsplit(url)
    return (p.hostname == BING and p.path == "/search") or p.hostname == DDG


def _unwrap(href: str) -> str:
    """Unwrap the search engine's click-tracking redirect to get the real target URL.

    >>> _unwrap("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1&rut=x")
    'https://example.com/a?b=1'
    >>> _unwrap("https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9wYXRo&ntb=1")
    'https://example.com/path'
    >>> _unwrap("https://example.com/")
    'https://example.com/'
    """
    full = urljoin("https://duckduckgo.com/", href)
    p = urlsplit(full)
    qs = parse_qs(p.query)
    if p.path.startswith("/l/") and qs.get("uddg"):
        return qs["uddg"][0]
    if p.hostname == BING and p.path.startswith("/ck/") and qs.get("u", [""])[0].startswith("a1"):
        raw = qs["u"][0][2:]
        try:
            return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return full
    return full


def parse_results(html: str) -> list[tuple[str, str, str]]:
    """Return (title, url, snippet); understands both Bing and DuckDuckGo result markup and skips ads.

    >>> bing = '''<ol id="b_results"><li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9hLnR3Lw">Site A</a></h2>
    ... <div class="b_caption"><p class="b_lineclamp2">Snippet A</p></div></li><li class="b_ad"><a href="https://ad.tw/">Ad</a></li></ol>'''
    >>> parse_results(bing)
    [('Site A', 'https://a.tw/', 'Snippet A')]
    >>> ddg = '''<div class="result"><h2 class="result__title"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fb.tw%2F">Site B</a></h2>
    ... <a class="result__snippet">Snippet B</a></div><div class="result result--ad"><a class="result__a" href="https://ad.tw/">Ad</a></div>'''
    >>> parse_results(ddg)
    [('Site B', 'https://b.tw/', 'Snippet B')]
    """
    root = lxml.html.fromstring(html)
    out = []
    for li in root.xpath("//li[contains(concat(' ', normalize-space(@class), ' '), ' b_algo ')]"):
        a = li.xpath(".//h2//a[@href]")
        if a:
            p = li.xpath(".//div[contains(@class, 'b_caption')]//p") or li.xpath(".//p")
            out.append((a[0].text_content(), a[0].get("href"), p[0].text_content() if p else ""))
    for r in root.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' result ')]"):
        if "result--ad" in (r.get("class") or ""):
            continue
        a = r.xpath(".//a[contains(@class, 'result__a')]")
        if a:
            snip = r.xpath(".//*[contains(@class, 'result__snippet')]")
            out.append((a[0].text_content(), a[0].get("href"), snip[0].text_content() if snip else ""))
    results = []
    for title, href, snippet in out:
        url = _unwrap(href or "")
        if urlsplit(url).scheme in ("http", "https"):
            results.append((clean_text(title), url, clean_text(snippet)))
    return results


def results_doc(page_id: str, html: str, url: str) -> Doc:
    query = parse_qs(urlsplit(url).query).get("q", [""])[0]
    engine = "Bing" if urlsplit(url).hostname == BING else "DuckDuckGo"
    results = parse_results(html)
    sections = []
    for title, link, snippet in results:
        s = Section(1, title or link)
        if snippet:
            s.add(snippet)
        s.add(f"→ {link}", [Link(title[:80] or link, link)])
        sections.append(s)
    notice = f"{engine} search for \"{query}\": {len(results)} results; use links to see numbers, click to follow" if results else \
        f"{engine} search for \"{query}\" returned no results, or the search engine blocked the automated request"
    return assemble(page_id, f"Search: {query}", url, sections, notice)

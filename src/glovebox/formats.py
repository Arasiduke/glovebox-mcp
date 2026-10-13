"""Non-HTML content to Doc: PDF, plain text, Markdown, JSON, RSS/Atom, generic XML, images.

Everything goes through the same Section -> assemble -> render_outline path, so Claude sees the same format as for web pages.
"""
from __future__ import annotations

import io
import json
import re
from urllib.parse import urlsplit

from lxml import etree

from glovebox.cleaning import clean_text, strip_invisible
from glovebox.document import Doc, Link, Section, assemble, build_doc

MAX_PDF_PAGES = 500
# When plain text has no blank lines to split on, group this many lines per paragraph
_LINES_PER_PARA = 20


def kind_of(content_type: str, url: str, head: bytes) -> str:
    """Determine the content kind from content-type, file extension and leading bytes.

    >>> kind_of("application/pdf", "https://a.tw/x", b"")
    'pdf'
    >>> kind_of("application/octet-stream", "https://a.tw/x", b"%PDF-1.4")
    'pdf'
    >>> kind_of("text/plain", "https://a.tw/readme.md", b"")
    'markdown'
    >>> [kind_of(t, "https://a.tw/", b"") for t in ("application/feed+json", "application/rss+xml", "text/xml", "image/png", "application/zip")]
    ['json', 'feed', 'xml', 'image', 'unsupported']

    When the server only sends a generic type, sniff the content instead (real case: Blogger serves RSS as application/octet-stream):

    >>> [kind_of("application/octet-stream", "https://a.tw/", h) for h in (
    ...     b'<?xml version="1.0"?><rss>', b"\\xef\\xbb\\xbf<feed xmlns=", b'{"a": 1}', b"\\x89PNG\\r\\n", b"plain words", b"\\x00\\x01\\x02")]
    ['feed', 'feed', 'json', 'image', 'text', 'unsupported']
    """
    ct = content_type.split(";")[0].strip().lower()
    path = urlsplit(url).path.lower()
    if ct in _GENERIC:
        return _sniff(head)
    if ct == "application/pdf" or head.startswith(b"%PDF"):
        return "pdf"
    if ct.startswith("image/"):
        return "image"
    if ct in ("text/markdown", "text/x-markdown") or path.endswith((".md", ".markdown")):
        return "markdown"
    if ct == "application/json" or ct.endswith("+json"):
        return "json"
    if ct in ("application/rss+xml", "application/atom+xml") or ct.endswith(("rss+xml", "atom+xml")):
        return "feed"
    if ct in ("text/xml", "application/xml") or ct.endswith("+xml"):
        return "xml"
    if ct.startswith("text/"):
        return "text"
    return "unsupported"


_GENERIC = {"", "application/octet-stream", "binary/octet-stream", "application/x-unknown", "application/unknown"}
_IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF", b"<svg")


def _sniff(head: bytes) -> str:
    """Determine the kind from the leading bytes (only when the server gave no useful content-type)."""
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(_IMAGE_MAGIC):
        return "image"
    if b"\x00" in head:
        return "unsupported"
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        text = head.decode("utf-8", errors="ignore") if head.count(b"\x00") == 0 and len(head) > 64 else ""
        if not text:
            return "unsupported"
    low = text.lstrip("\ufeff \t\r\n").lower()
    if low.startswith(("<?xml", "<rss", "<feed", "<rdf:rdf")):
        return "feed" if any(t in low for t in ("<rss", "<feed", "<rdf:rdf")) else "xml"
    if low.startswith(("<!doctype html", "<html")):
        return "html"
    if low.startswith(("{", "[")):
        return "json"
    return "text"


def _decode(data: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type, re.I)
    for enc in ([m.group(1)] if m else []) + ["utf-8"]:
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _keep_layout(s: str) -> str:
    """Plain text/JSON must keep indentation and line breaks: strip only invisible characters (no whitespace collapsing).

    >>> _keep_layout("line two\\n  indent kept")
    'line two\\n  indent kept'
    """
    return strip_invisible(s)


def _paragraphs(text: str) -> list[str]:
    """Split on blank lines; if there are none at all (logs, code), use 20 lines per paragraph.

    >>> _paragraphs("a\\n\\nb\\nc")
    ['a', 'b\\nc']
    >>> len(_paragraphs("\\n".join(str(i) for i in range(45))))
    3
    """
    blocks = [b.strip("\n") for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) == 1 and blocks[0].count("\n") > _LINES_PER_PARA:
        lines = blocks[0].split("\n")
        blocks = ["\n".join(lines[i:i + _LINES_PER_PARA]) for i in range(0, len(lines), _LINES_PER_PARA)]
    return blocks


def text_sections(text: str, markdown: bool) -> list[Section]:
    """Markdown is split into sections on # headings; plain text only into paragraphs.

    >>> [(s.level, s.heading, s.paras) for s in text_sections("Preface\\n# Install\\nRun uv sync\\n## Test\\nRun pytest", True)]
    [(0, '(intro)', ['Preface']), (1, 'Install', ['Run uv sync']), (2, 'Test', ['Run pytest'])]
    """
    sections = [Section(0, "(intro)")]
    buf: list[str] = []

    def flush() -> None:
        for p in _paragraphs("\n".join(buf)):
            sections[-1].add(p)
        buf.clear()

    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line) if markdown else None
        if m:
            flush()
            sections.append(Section(len(m.group(1)), clean_text(m.group(2))))
        else:
            buf.append(line)
    flush()
    if not sections[0].paras:
        sections.pop(0)
    return sections


def json_sections(text: str) -> list[Section]:
    """Pretty-print JSON and split by lines; a top-level object gets one section per key.

    >>> [s.heading for s in json_sections('{"name": "glovebox", "deps": ["mcp"]}')]
    ['name', 'deps']
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON: {e}") from None
    if isinstance(data, dict) and 0 < len(data) <= 200:
        out = []
        for k, v in data.items():
            s = Section(1, clean_text(str(k))[:60] or "(empty key)")
            for p in _paragraphs(json.dumps(v, ensure_ascii=False, indent=2)):
                s.add(_keep_layout(p))
            out.append(s)
        return out
    s = Section(0, "(intro)")
    for p in _paragraphs(json.dumps(data, ensure_ascii=False, indent=2)):
        s.add(_keep_layout(p))
    return [s]


def _xml_root(data: bytes):
    # Guard against XXE and entity-expansion bombs: no entity resolution, no network, no DTD loading
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, recover=True)
    root = etree.fromstring(data, parser=parser)
    if root is None:
        raise RuntimeError("Cannot parse XML")
    return root


def _local(tag) -> str:
    return tag.split("}")[-1].lower() if isinstance(tag, str) else ""


def _html_text(s: str) -> str:
    if "<" in s:
        try:
            return clean_text(etree.fromstring(f"<div>{s}</div>", etree.HTMLParser()).xpath("string()"))
        except (etree.XMLSyntaxError, ValueError):
            pass
    return clean_text(s)


def feed_sections(data: bytes) -> tuple[str, list[Section]]:
    """RSS 2.0 / RSS 1.0 / Atom: one section per entry (title, summary, date, link).

    >>> t, secs = feed_sections(b"<rss><channel><title>F</title><item><title>A</title><link>https://a.tw/1</link><description>&lt;p&gt;hi&lt;/p&gt;</description></item></channel></rss>")
    >>> t, [(s.heading, s.paras, s.plinks[-1][0].url) for s in secs]
    ('F', [('A', ['hi', '→ https://a.tw/1'], 'https://a.tw/1')])
    """
    root = _xml_root(data)
    title = ""
    entries = []
    for el in root.iter():
        name = _local(el.tag)
        if name == "title" and not title and _local(el.getparent().tag) in ("channel", "feed"):
            title = clean_text(el.xpath("string()"))
        if name in ("item", "entry"):
            entries.append(el)
    out = []
    for e in entries:
        fields: dict[str, str] = {}
        link = ""
        for c in e:
            n = _local(c.tag)
            if n == "link":
                link = link or (c.get("href") or c.xpath("string()")).strip()
            elif n in ("title", "description", "summary", "content", "encoded", "pubdate", "published", "updated", "date"):
                fields.setdefault(n, c.xpath("string()"))
        s = Section(1, clean_text(fields.get("title", "")) or "(untitled)")
        body = fields.get("description") or fields.get("summary") or fields.get("content") or fields.get("encoded") or ""
        if body.strip():
            s.add(_html_text(body))
        when = fields.get("pubdate") or fields.get("published") or fields.get("updated") or fields.get("date")
        if when:
            s.add(f"Date: {clean_text(when)}")
        if link.startswith(("http://", "https://")):
            s.add(f"→ {link}", [Link(s.heading[:80], link)])
        out.append(s)
    return title, out


def xml_sections(data: bytes) -> list[Section]:
    """Generic XML: take all text content and split into paragraphs."""
    root = _xml_root(data)
    text = "\n\n".join(t.strip() for t in root.itertext() if t.strip())
    return text_sections(_keep_layout(text), markdown=False)


def pdf_sections(data: bytes) -> list[Section]:
    """PDF: one section per page. For scanned PDFs with no extractable text, the page says to use screenshot."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = reader.pages
        total = len(pages)
    except (PdfReadError, ValueError, KeyError) as e:
        raise RuntimeError(f"Cannot parse PDF: {e}") from None
    out = []
    for i, page in enumerate(pages[:MAX_PDF_PAGES], 1):
        try:
            text = page.extract_text() or ""
        except Exception:  # one broken page must not fail the whole file
            text = ""
        s = Section(1, f"Page {i}")
        paras = _paragraphs(_keep_layout(text))
        for p in paras or ["(No text on this page; it may be a scanned image)"]:
            s.add(p)
        out.append(s)
    if total > MAX_PDF_PAGES:
        out.append(Section(1, f"({total} pages; only the first {MAX_PDF_PAGES} processed)", ["(omitted)"], [[]]))
    return out


def from_bytes(page_id: str, url: str, content_type: str, data: bytes, notice: str = "") -> Doc:
    """Entry point for non-HTML content. Unsupported types raise a clear error instead of silently returning nothing."""
    kind = kind_of(content_type, url, data[:512])
    name = urlsplit(url).path.rsplit("/", 1)[-1] or url
    label = {"pdf": "PDF", "markdown": "Markdown", "json": "JSON", "feed": "RSS/Atom feed",
             "xml": "XML", "text": "plain text", "image": "image", "html": "HTML"}.get(kind)
    if kind == "unsupported":
        raise RuntimeError(f"Unsupported content type: {content_type or 'unknown'} ({len(data):,} bytes); glovebox does not handle binary files")
    notice = "; ".join(x for x in (notice, f"Content type: {label}") if x)
    if kind == "image":
        s = Section(1, "(image)", [f"This is an image ({content_type}, {len(data) // 1024:,} KB) with no text to extract. Use screenshot to see it."], [[]])
        return assemble(page_id, name, url, [s], notice)
    if kind == "pdf":
        return assemble(page_id, name, url, pdf_sections(data), notice)
    if kind == "feed":
        title, secs = feed_sections(data)
        return assemble(page_id, title or name, url, secs, notice)
    if kind == "xml":
        root_name = _local(_xml_root(data).tag)
        if root_name in ("rss", "feed", "rdf"):
            title, secs = feed_sections(data)
            return assemble(page_id, title or name, url, secs, "; ".join((notice, "detected as an RSS/Atom feed")))
        return assemble(page_id, name, url, xml_sections(data), notice)
    text = _decode(data, content_type)
    if kind == "html":  # only when the server sent no type and the browser treated it as a download
        return build_doc(page_id, text, name, url, notice="; ".join((notice, "not rendered by the browser; dynamic content may be missing")))
    if kind == "json":
        return assemble(page_id, name, url, json_sections(text), notice)
    return assemble(page_id, name, url, text_sections(_keep_layout(text), kind == "markdown"), notice)

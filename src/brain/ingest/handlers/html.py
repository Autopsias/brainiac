"""HTML handler — stdlib ``html.parser`` readable-text conversion (no new
required dependency); ``lxml`` is used as an optional faster/more-robust path
when already installed, mirroring the sha256-verified reference-vault reference
(ADR-0003 Appendix B) which proves the stdlib fallback alone is production-
adequate."""
from __future__ import annotations

import html as _html_stdlib
import html.parser
import re
from pathlib import Path

from .base import ExtractResult, Handler, density_gate

MAX_HTML_BYTES = 50 * 1024 * 1024

_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "canvas", "iframe", "object", "embed"})
_BLOCK_TAGS = frozenset({
    "div", "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "dt", "dd", "tr", "td", "th", "section", "article", "header",
    "footer", "nav", "main", "aside", "pre", "blockquote", "table",
    "thead", "tbody", "tfoot", "ul", "ol", "dl", "figure", "figcaption",
})


class _TextExtractor(html.parser.HTMLParser):
    """Minimal HTML -> plain-text via stdlib. Void elements (br, hr, meta,
    link, ...) fire ``handle_starttag`` but never ``handle_endtag`` — they are
    deliberately absent from ``_SKIP_TAGS`` so ``_skip`` can never be left
    incremented forever by one."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip += 1
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._chunks.append(data)

    def get_text(self) -> str:
        raw = "".join(self._chunks)
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.splitlines()]
        text = "\n".join(lines)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def _extract_title(raw_html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    title = _html_stdlib.unescape(m.group(1))
    title = re.sub(r"\s+", " ", title).strip()
    return title or None


def _extract_text(raw_html: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    try:
        from lxml.html import fromstring as _fromstring

        doc = _fromstring(raw_html)
        for bad in doc.xpath("//script|//style|//noscript"):
            bad.drop_tree()
        text = doc.text_content()
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            return text, warnings
        # fall through to stdlib if lxml produced nothing
    except ImportError:
        pass  # lxml not installed — stdlib fallback below
    except Exception as exc:
        warnings.append(f"lxml_parse_warning: {type(exc).__name__}: {exc}")

    extractor = _TextExtractor()
    try:
        extractor.feed(raw_html)
        return extractor.get_text(), warnings
    except Exception as exc:
        warnings.append(f"html_parse_warning: {type(exc).__name__}: {exc}")
        text = re.sub(r"<[^>]+>", " ", raw_html)
        return re.sub(r"\s+", " ", text).strip(), warnings


class HtmlHandler(Handler):
    extensions = (".html", ".htm")
    dependency_name = "stdlib"

    @classmethod
    def available(cls) -> bool:
        return True

    @classmethod
    def extract(cls, path: Path) -> ExtractResult:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > MAX_HTML_BYTES:
            return ExtractResult.quarantine(
                "file_too_large", warnings=[f"{size} bytes exceeds cap {MAX_HTML_BYTES}"]
            )
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return ExtractResult.quarantine("html_read_error", warnings=[f"{type(exc).__name__}: {exc}"])

        text_raw = None
        for enc in ("utf-8", "latin-1"):
            try:
                text_raw = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text_raw is None:
            return ExtractResult.quarantine("html_decode_error")

        title = _extract_title(text_raw)
        body, warnings = _extract_text(text_raw)
        if not body:
            return ExtractResult.quarantine("empty_or_low_text_density", warnings=warnings)

        markdown = f"# {title}\n\n{body}\n" if title else f"{body}\n"
        reason = density_gate(markdown)
        if reason:
            return ExtractResult.quarantine(reason, warnings=warnings)
        return ExtractResult(markdown=markdown, warnings=warnings, metadata={"title": title})

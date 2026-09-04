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

from . import concealment
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


#: Control characters a <title> should never carry into a note's frontmatter
#: (LOW-02) — ``deliverables_absorb._UNSAFE_PROJECT``'s pattern MINUS tab,
#: newline and carriage return. Those three are ordinary whitespace inside a
#: pretty-printed ``<title>``, and deleting them outright joined the words
#: either side: ``"Q3 Results\nDraft"`` came out as ``"Q3 ResultsDraft"``.
#: Left in, the ``\s+`` collapse two lines below turns each into one space,
#: which is what a reader expects. Every other control character is a
#: frontmatter hazard with no reading, so it still goes.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _extract_title(raw_html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    title = _html_stdlib.unescape(m.group(1))
    title = _CONTROL_CHARS.sub("", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title or None


def _extract_text(raw_html: str, seen: object = None) -> tuple[str, list[str], list[dict]]:
    """The readable text, the warnings, and the concealed runs.

    ``seen`` is the coverage ledger's sink for ``html:text``. Whichever parser
    path runs reports the text nodes IT read into it, so the ledger compares
    two readings of the same bytes rather than trusting a table that says they
    agree (V15, 2026-09-03).
    """
    warnings: list[str] = []
    try:
        from lxml.html import fromstring as _fromstring

        doc = _fromstring(raw_html)
        for bad in doc.xpath("//script|//style|//noscript"):
            bad.drop_tree()
        # BEFORE text_content(): this is the last statement that can still see
        # colour, size and position (M-3).
        # `raw_html` too: the <style> subtrees were dropped above, and a
        # class rule that hides text is only readable from the source.
        concealed = concealment.collect(
            lambda: concealment.html_runs_lxml(doc, raw_html, seen), warnings)
        text = doc.text_content()
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            return text, warnings, concealed
        # fall through to stdlib if lxml produced nothing
    except ImportError:  # coverage-audit: lxml absent; the stdlib walk below reads the same bytes
        pass  # lxml not installed — stdlib fallback below
    except Exception as exc:  # coverage-audit: warns, and the stdlib walk below reads the same bytes
        warnings.append(f"lxml_parse_warning: {type(exc).__name__}: {exc}")

    # ponytail: the fallback parses twice (text, then styles). It runs only
    # when lxml is missing or raised, and the alternative is threading style
    # state through _TextExtractor's text assembly.
    concealed = concealment.collect(
        lambda: concealment.html_runs_stdlib(raw_html, seen), warnings)
    extractor = _TextExtractor()
    try:
        extractor.feed(raw_html)
        return extractor.get_text(), warnings, concealed
    except Exception as exc:  # coverage-audit: the regex fallback's text is still compared against what the walk reported, so a walk that stopped early cannot cover it
        warnings.append(f"html_parse_warning: {type(exc).__name__}: {exc}")
        text = re.sub(r"<[^>]+>", " ", raw_html)
        return re.sub(r"\s+", " ", text).strip(), warnings, concealed


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
        except OSError:  # coverage-audit: an unreadable size falls through to the gates below
            size = 0
        if size > MAX_HTML_BYTES:
            return ExtractResult.quarantine(
                "file_too_large", warnings=[f"{size} bytes exceeds cap {MAX_HTML_BYTES}"]
            )
        try:
            raw = path.read_bytes()
        except OSError as exc:  # coverage-audit: quarantines; no text is admitted, so none is claimed
            return ExtractResult.quarantine("html_read_error", warnings=[f"{type(exc).__name__}: {exc}"])

        text_raw = None
        for enc in ("utf-8", "latin-1"):
            try:
                text_raw = raw.decode(enc)
                break
            except UnicodeDecodeError:  # coverage-audit: tries the next encoding; exhausting them quarantines
                continue
        if text_raw is None:
            return ExtractResult.quarantine("html_decode_error")

        # The coverage ledger: every chunk of this note's text, by source, so
        # `attest` can check that what it claims was searched IS what the note
        # carries (concealment_gate's coverage rule). `<title>` is a text node
        # of the same tree the walk covered, so it rides on `html:text`.
        admitted = concealment.Admitted()
        title = _extract_title(text_raw)
        body, warnings, concealed = _extract_text(
            text_raw, admitted.watch("html:text"))
        if not body:
            return ExtractResult.quarantine("empty_or_low_text_density", warnings=warnings)

        markdown = f"# {title}\n\n{body}\n" if title else f"{body}\n"
        reason = density_gate(markdown)
        if reason:
            return ExtractResult.quarantine(reason, warnings=warnings)
        if title:
            admitted.chrome("#")
            # DECLARED repeat, not a second text: `text_content()` already
            # carries the <title> inside `body`, and the walker read that one
            # text node once. The note carries the letters twice, so the
            # residue check must see them twice; the walk saw them once, so
            # the occurrence-aware coverage check must not demand a second
            # sighting. Saying so here is what lets that check stay strict
            # everywhere else (round 7, 2026-09-04).
            admitted.repeat("html:text", title)
        admitted.add("html:text", body)
        return ExtractResult(markdown=markdown, warnings=warnings,
                             metadata={"title": title, "concealed": concealed,
                                       **concealment.attest(
                                           warnings, body=markdown,
                                           admitted=admitted)})

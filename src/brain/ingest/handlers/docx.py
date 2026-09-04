"""DOCX handler — python-docx. Paragraphs + tables (Markdown tables, headers
retained per HARDENED:grill — see .tables.rows_to_markdown)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import concealment
from .base import ExtractResult, Handler, density_gate, ooxml_expansion_gate
from .tables import rows_to_markdown

try:
    import docx  # python-docx
    _HAS_DOCX = True
except ImportError:  # pragma: no cover  # coverage-audit: lane unavailable, never a degraded walk
    _HAS_DOCX = False

MAX_DOCX_BYTES = 100 * 1024 * 1024


def _open_document(path: Path) -> Any | ExtractResult:
    try:
        size = path.stat().st_size
    except OSError:  # coverage-audit: an unreadable size falls through to the gates below
        size = 0
    if size > MAX_DOCX_BYTES:
        return ExtractResult.quarantine("file_too_large")
    # ...and the size on disk is not the size extraction has to hold: a .docx
    # is a zip, and a 110 KB one measured 278:1 (M-8, 2026-09-02).
    bomb = ooxml_expansion_gate(path)
    if bomb is not None:
        return bomb
    try:
        return docx.Document(str(path))
    except Exception as exc:  # coverage-audit: quarantines; no text is admitted, so none is claimed
        return ExtractResult.quarantine(
            "docx_extraction_error",
            warnings=[f"{type(exc).__name__}: {exc}"],
        )


def _render_document(document: Any) -> ExtractResult:
    warnings: list[str] = []
    admitted = concealment.Admitted()
    # Collected from the LIVE paragraphs, where w:vanish, colour and size
    # still exist; `paragraph.text` below drops all three (M-3). The walk
    # reports what it read into the ledger, which then checks that report
    # against the text this handler admits — `paragraph.runs` is NOT the same
    # set as `paragraph.text`, and nothing here has to know why (V15).
    concealed = concealment.collect(
        lambda: concealment.docx_runs(document, admitted.watch("docx:body")),
        warnings)
    try:
        parts = _document_parts(document, admitted)
    except Exception as exc:  # coverage-audit: quarantines, never swallows
        return ExtractResult.quarantine(
            "docx_extraction_error",
            warnings=[f"{type(exc).__name__}: {exc}"],
        )
    body_md = "\n".join(parts)
    reason = density_gate(body_md)
    if reason:
        return ExtractResult.quarantine(reason, warnings=warnings)
    return ExtractResult(markdown=body_md, warnings=warnings,
                         metadata={"tables": len(document.tables),
                                   "concealed": concealed,
                                   **concealment.attest(
                                       warnings, body=body_md,
                                       admitted=admitted)})


def _document_parts(document: Any, admitted: Any) -> list[str]:
    """Render paragraphs and tables in their original body order.

    Each part goes on the coverage ledger AS IT IS RENDERED, in the order the
    body carries it: a paragraph as one `docx:body` chunk, a table cell by
    cell with its pipes recorded as handler chrome (``tables.rows_to_markdown``).
    """
    parts: list[str] = []
    body = document.element.body
    table_by_element = {table._tbl: table for table in document.tables}
    paragraph_by_element = {paragraph._p: paragraph for paragraph in document.paragraphs}

    def admit(kind: str, text: str) -> str:
        return (admitted.chrome(text) if kind == "chrome"
                else admitted.add("docx:body", text))

    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            rendered = admitted.add(
                "docx:body", _render_paragraph(paragraph_by_element.get(child)))
        elif tag == "tbl":
            rendered = _render_table(table_by_element.get(child), admit)
        else:
            rendered = ""
        if rendered:
            parts.append(rendered)
    return parts


def _paragraph_text(paragraph: Any) -> str:
    """Every ``w:r`` the paragraph OWNS, at any depth — the SAME element set
    ``concealment.docx_runs`` walks, so the two halves cannot disagree.

    ``paragraph.text`` renders its DIRECT run children and its hyperlinks, so
    Word's OTHER wrappers vanish from the note: a content control (``w:sdt``),
    a smart tag, a tracked insertion. Measured on the reference corpus, that
    silently dropped text from 8 documents — and because the walker DID read
    it, the paragraph the handler admitted was no longer contiguous inside the
    walker's report, so the coverage cursor could not match it and a correct
    walk read ``uninspected`` (round 7, 2026-09-04). Reading the element that
    HOLDS text closes both at once, and names no wrapper: the next one nobody
    has seen falls in with them.
    """
    from docx.oxml.ns import qn
    from docx.text.run import Run
    return "".join(Run(element, paragraph).text
                   for element in paragraph._p.iter(qn("w:r")))


def _render_paragraph(paragraph: Any) -> str:
    if paragraph is None:
        return ""
    text = _paragraph_text(paragraph).strip()
    if not text:
        return ""
    if paragraph.style and paragraph.style.name and paragraph.style.name.startswith("Heading"):
        return f"## {text}\n"
    return f"{text}\n"


def _render_table(table: Any, admit: Any = None) -> str:
    if table is None:
        return ""
    # `cell.text` joins `paragraph.text`, so a cell carries the same blind
    # spot `_paragraph_text` exists to close — measured on the reference
    # corpus, a cell whose middle paragraph sits in a `w:sdt` rendered as
    # `"a;  ; b"` while the walker read all three (round 7, 2026-09-04).
    rows = [["\n".join(_paragraph_text(para) for para in cell.paragraphs)
             for cell in row.cells] for row in table.rows]
    return rows_to_markdown(rows, admit=admit)


class DocxHandler(Handler):
    extensions = (".docx",)
    dependency_name = "python-docx"

    @classmethod
    def available(cls) -> bool:
        return _HAS_DOCX

    @classmethod
    def extract(cls, path: Path) -> ExtractResult:
        if not _HAS_DOCX:
            return ExtractResult.quarantine("missing_dependency:python-docx")
        document = _open_document(path)
        if isinstance(document, ExtractResult):
            return document
        return _render_document(document)

"""DOCX handler — python-docx. Paragraphs + tables (Markdown tables, headers
retained per HARDENED:grill — see .tables.rows_to_markdown)."""
from __future__ import annotations

from pathlib import Path

from .base import ExtractResult, Handler, density_gate
from .tables import rows_to_markdown

try:
    import docx  # python-docx
    _HAS_DOCX = True
except ImportError:  # pragma: no cover
    _HAS_DOCX = False

MAX_DOCX_BYTES = 100 * 1024 * 1024


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
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > MAX_DOCX_BYTES:
            return ExtractResult.quarantine("file_too_large")
        try:
            document = docx.Document(str(path))
        except Exception as exc:
            return ExtractResult.quarantine(
                "docx_extraction_error", warnings=[f"{type(exc).__name__}: {exc}"]
            )

        parts: list[str] = []
        try:
            # Walk body children in document order so tables land inline with
            # the surrounding prose rather than all at the end.
            body = document.element.body
            table_by_elem = {t._tbl: t for t in document.tables}
            # C10: re-scanning document.paragraphs per body child was O(n^2).
            # Build the element->paragraph lookup once, like table_by_elem.
            para_by_elem = {p._p: p for p in document.paragraphs}
            for child in body.iterchildren():
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "p":
                    p = para_by_elem.get(child)
                    if p is not None:
                        text = p.text.strip()
                        if text:
                            if p.style and p.style.name and p.style.name.startswith("Heading"):
                                parts.append(f"## {text}\n")
                            else:
                                parts.append(f"{text}\n")
                elif tag == "tbl":
                    tbl = table_by_elem.get(child)
                    if tbl is not None:
                        rows = [[c.text for c in row.cells] for row in tbl.rows]
                        parts.append(rows_to_markdown(rows))
        except Exception as exc:
            return ExtractResult.quarantine(
                "docx_extraction_error", warnings=[f"{type(exc).__name__}: {exc}"]
            )

        body_md = "\n".join(p for p in parts if p.strip())
        reason = density_gate(body_md)
        if reason:
            return ExtractResult.quarantine(reason)
        return ExtractResult(markdown=body_md, metadata={"tables": len(document.tables)})

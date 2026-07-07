"""PPTX handler — python-pptx. One `## Slide N` section per slide; text frames
+ tables (Markdown tables, headers retained)."""
from __future__ import annotations

from pathlib import Path

from .base import ExtractResult, Handler, density_gate
from .tables import rows_to_markdown

try:
    from pptx import Presentation
    _HAS_PPTX = True
except ImportError:  # pragma: no cover
    _HAS_PPTX = False

MAX_PPTX_BYTES = 150 * 1024 * 1024


class PptxHandler(Handler):
    extensions = (".pptx",)
    dependency_name = "python-pptx"

    @classmethod
    def available(cls) -> bool:
        return _HAS_PPTX

    @classmethod
    def extract(cls, path: Path) -> ExtractResult:
        if not _HAS_PPTX:
            return ExtractResult.quarantine("missing_dependency:python-pptx")
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > MAX_PPTX_BYTES:
            return ExtractResult.quarantine("file_too_large")
        try:
            prs = Presentation(str(path))
        except Exception as exc:
            return ExtractResult.quarantine(
                "pptx_extraction_error", warnings=[f"{type(exc).__name__}: {exc}"]
            )

        sections: list[str] = []
        slide_count = 0
        try:
            for i, slide in enumerate(prs.slides, start=1):
                slide_count = i
                lines = [f"## Slide {i}\n"]
                for shape in slide.shapes:
                    if shape.has_table:
                        tbl = shape.table
                        rows = [[c.text for c in row.cells] for row in tbl.rows]
                        lines.append(rows_to_markdown(rows))
                    elif shape.has_text_frame:
                        text = shape.text_frame.text.strip()
                        if text:
                            lines.append(text + "\n")
                sections.append("\n".join(lines))
        except Exception as exc:
            return ExtractResult.quarantine(
                "pptx_extraction_error", warnings=[f"{type(exc).__name__}: {exc}"]
            )

        body = "\n".join(sections)
        reason = density_gate(body)
        if reason:
            return ExtractResult.quarantine(reason)
        return ExtractResult(markdown=body, metadata={"slide_count": slide_count})

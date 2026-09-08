"""PPTX handler — python-pptx. One `## Slide N` section per slide; text frames
+ tables (Markdown tables, headers retained)."""
from __future__ import annotations

from pathlib import Path

from . import concealment
from .base import (ExtractResult, Handler, density_gate, ocr_available, ocr_image,
                   ooxml_expansion_gate)
from .tables import rows_to_markdown

try:
    from pptx import Presentation
    _HAS_PPTX = True
except ImportError:  # pragma: no cover  # coverage-audit: lane unavailable, never a degraded walk
    _HAS_PPTX = False

MAX_PPTX_BYTES = 150 * 1024 * 1024


def _ocr_pictures(slide: object) -> str:
    """OCR the pictures on one slide. A deck exported as one rendered image
    per slide carries ALL its text inside those pictures — the same problem a
    scanned PDF has, and the reference vault held a two-slide deck whose
    entire cost/timeline comparison was invisible for that reason. Returns ""
    when the slide has no picture or OCR reads nothing."""
    import io

    texts = []
    for shape in slide.shapes:  # type: ignore[attr-defined]
        blob = getattr(getattr(shape, "image", None), "blob", None)
        if blob is None:
            continue
        try:
            from PIL import Image

            with Image.open(io.BytesIO(blob)) as img:
                text, _ = ocr_image(img)
        except Exception:  # coverage-audit: this picture's OCR text is simply not admitted
            continue
        if text:
            texts.append(text)
    return "\n\n".join(texts).strip()


def _open_presentation(path: Path) -> "Presentation | ExtractResult":
    """Size pre-flight then open — the docx handler's `_open_document` shape.

    Two bounds, and they measure different things. `MAX_PPTX_BYTES` is the file
    on disk. `ooxml_expansion_gate` is what extraction has to HOLD: a .pptx is a
    zip, and a small one can declare hundreds of megabytes of XML (M-8,
    2026-09-02).
    """
    try:
        size = path.stat().st_size
    except OSError:  # coverage-audit: an unreadable size falls through to the gates below
        size = 0
    if size > MAX_PPTX_BYTES:
        return ExtractResult.quarantine("file_too_large")
    bomb = ooxml_expansion_gate(path)
    if bomb is not None:
        return bomb
    try:
        return Presentation(str(path))
    except Exception as exc:  # coverage-audit: quarantines; no text is admitted, so none is claimed
        return ExtractResult.quarantine(
            "pptx_extraction_error", warnings=[f"{type(exc).__name__}: {exc}"]
        )


def _slide_lines(slide: object, i: int, admitted: "concealment.Admitted",
                 admit, can_ocr: bool) -> tuple[list[str], bool]:
    """The Markdown lines for one slide, and whether OCR of its pictures
    supplied them. Admission order is the reading order: the header, then
    every shape as the deck holds it, then picture OCR only when no shape
    carried native text."""
    lines = [admitted.chrome(f"## Slide {i}\n")]
    native = False
    for shape in slide.shapes:  # type: ignore[attr-defined]
        if shape.has_table:
            rows = [[c.text for c in row.cells] for row in shape.table.rows]
            lines.append(rows_to_markdown(rows, admit=admit))
            native = True
        elif shape.has_text_frame:
            text = shape.text_frame.text.strip()
            if text:
                lines.append(admitted.add("pptx:native", text + "\n"))
                native = True
    if native or not can_ocr:
        return lines, False
    # No native text on this slide — read its pictures.
    picture_text = _ocr_pictures(slide)
    if not picture_text:
        return lines, False
    lines.append(admitted.add("pptx:picture_ocr", picture_text + "\n"))
    return lines, True


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
        prs = _open_presentation(path)
        if isinstance(prs, ExtractResult):
            return prs

        warnings: list[str] = []
        admitted = concealment.Admitted()
        # From the LIVE shapes, where font colour, size and shape position
        # still exist; `shape.text_frame.text` below drops all three (M-3).
        # The walk reports what it read into the ledger, which then checks
        # that report against the text admitted below — `paragraph.runs` does
        # not hold field (a:fld) text `text_frame.text` renders, and nothing
        # here has to know that (V15).
        concealed = concealment.collect(
            lambda: concealment.pptx_runs(prs, admitted.watch("pptx:native")),
            warnings)

        sections: list[str] = []
        slide_count = 0
        ocr_slides: list[int] = []
        can_ocr = ocr_available()
        # `pptx:native` is what `pptx_runs` above walked; `pptx:picture_ocr`
        # is text lifted out of a RASTER, which no walker reads — so a deck
        # carrying any of it can never read `full` (V13, 2026-09-03). Neither
        # name is special-cased anywhere: the first is in `COVERED_SOURCES`
        # and the second is not.
        def admit(kind: str, text: str) -> str:
            return (admitted.chrome(text) if kind == "chrome"
                    else admitted.add("pptx:native", text))

        try:
            for i, slide in enumerate(prs.slides, start=1):
                slide_count = i
                lines, used_ocr = _slide_lines(slide, i, admitted, admit, can_ocr)
                if used_ocr:
                    ocr_slides.append(i)
                sections.append("\n".join(lines))
        except Exception as exc:  # coverage-audit: quarantines; no text is admitted, so none is claimed
            return ExtractResult.quarantine(
                "pptx_extraction_error", warnings=[f"{type(exc).__name__}: {exc}"]
            )

        body = "\n".join(sections)
        reason = density_gate(body)
        if reason:
            return ExtractResult.quarantine(reason, warnings=warnings + [
                "no native text on any slide, and "
                + ("OCR of the slide pictures read nothing" if can_ocr
                   else "no local OCR engine is installed (pytesseract + tesseract)")])
        if ocr_slides:
            warnings.append(
                f"ocr_slides: {len(ocr_slides)}/{slide_count} read by local OCR")
        return ExtractResult(markdown=body, warnings=warnings,
                             metadata={"slide_count": slide_count,
                                       "ocr_slides": ocr_slides,
                                       "concealed": concealed,
                                       **concealment.attest(
                                           warnings, body=body,
                                           admitted=admitted)})

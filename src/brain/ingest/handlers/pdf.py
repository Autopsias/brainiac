"""PDF handler — pypdf only (ADR-0003 Ruling 1(g): pure-ish Python, no system
binaries; no poppler/pdfplumber acceleration — out of scope for this session,
add if a size/latency ceiling is ever measured to need it).

ponytail: no image extraction, no poppler fast-lane for huge PDFs (the
reference vault's biggest complexity driver) — this session's deliverable is
faithful TEXT extraction with a quality gate, not image/asset pipelines. Add
poppler-accelerated large-PDF lane if a real corpus ever needs it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import ExtractResult, Handler, density_gate, ocr_available, ocr_image

try:
    from pypdf import PdfReader
    _HAS_PYPDF = True
except ImportError:  # pragma: no cover - exercised via degraded-deps test  # coverage-audit: lane unavailable, never a degraded walk
    _HAS_PYPDF = False

_MIN_PAGE_CHARS = 5
# HARDENED:codex — max-size cap so a pathological file can't hang extraction
# or blow memory before it ever reaches the signed write path.
MAX_PDF_BYTES = 200 * 1024 * 1024  # 200 MB
# OCR costs ~1s/IMAGE, and the drain holds the single-writer lock while it
# runs — a scan is usually one full-page image, but a page can embed several,
# and each one is its own tesseract call. The budget therefore counts IMAGES,
# not pages (LOW-03): a page decrementing it once regardless of how many
# images it held under-charged an image-heavy page and mis-stated what the
# cap actually bought. A page with a text layer spends nothing — OCR never
# runs on it at all. Images past the cap are reported as un-OCR'd
# (`ocr_images_skipped`), never silently dropped.
DEFAULT_OCR_MAX_PAGES = 400


def _ocr_max_pages() -> int:
    import os

    try:
        return int(os.environ.get("BRAIN_PDF_OCR_MAX_PAGES", DEFAULT_OCR_MAX_PAGES))
    except ValueError:  # coverage-audit: an OCR page budget, not a walk
        return DEFAULT_OCR_MAX_PAGES


def _ocr_page(page: object, budget: int) -> tuple[str, int, int]:
    """OCR up to ``budget`` of one page's embedded images, by reading the
    page's own rasters — no rasterizer and no system binary beyond the
    optional local tesseract the image handler already uses.

    Returns ``(text, images_ocred, images_skipped)`` — the last is how many
    of this page's images were never attempted because the budget was
    already spent, which is what ``ocr_images_skipped`` in the ingest report
    sums across the document (LOW-03: the budget counts images, the cost
    unit, not pages)."""
    try:
        images = list(page.images)  # type: ignore[attr-defined]
    except Exception:  # coverage-audit: no OCR text is admitted from this page, so none is claimed
        return "", 0, 0
    texts = []
    ocred = 0
    skipped = 0
    for embedded in images:
        if budget <= 0:
            skipped += 1
            continue
        budget -= 1
        ocred += 1
        try:
            text, _ = ocr_image(embedded.image)
        except Exception:  # coverage-audit: no OCR text is admitted from this image, so none is claimed
            continue
        if text:
            texts.append(text)
    return "\n\n".join(texts).strip(), ocred, skipped


def _open_empty_password(reader: object, note: list[str]) -> bool:
    """True when the file's USER password is empty — i.e. it is not access
    restricted at all, and every PDF viewer opens it without prompting.

    Most "encrypted" corporate PDFs are this: encryption carries permission
    flags (no printing, no copying) while anyone may open the document. The
    handler used to quarantine on `is_encrypted` alone, which refused 680
    pages of readable due-diligence reports in the reference vault.

    Deliberately NARROW: only an empty USER password (`PasswordType`
    ``USER_PASSWORD``) counts. An owner-password match would mean overriding
    restrictions the author set on someone who cannot open the file anyway,
    so it is treated as still-encrypted, exactly as before."""
    try:
        from pypdf import PasswordType
    except ImportError:  # pragma: no cover - pypdf below the pinned floor  # coverage-audit: treated as still encrypted; the document is quarantined
        return False
    try:
        outcome = reader.decrypt("")  # type: ignore[attr-defined]
    except Exception as exc:  # coverage-audit: treated as still encrypted; the document is quarantined
        note.append(f"decrypt attempt failed: {type(exc).__name__}: {exc}")
        return False
    if outcome == PasswordType.USER_PASSWORD:
        note.append("opened with an empty user password (permissions-only encryption)")
        return True
    note.append("a real password is required — supply an unlocked copy")
    return False


def _page_text(page: Any, page_no: int, concealed: list[dict],
               warnings: list[str], seen: Any = None) -> str:
    """Extract one page's text, watching the text-render-mode operators.

    Mode 3 and 7 draw text the reader never sees while
    ``page.extract_text()`` returns it like any other line (M-3). The
    operand visitor is the only place that state is visible, and it must
    never cost the extraction: a visitor failure falls back to the plain
    call rather than quarantining a readable PDF.

    ``page`` is passed to the visitor so it can decode glyph-coded operands
    through the page's own fonts (gap 5, closed round 7). Passing the page,
    not a pre-built font map, keeps the build inside the visitor's own
    catches: a page whose font resources cannot be read still extracts.

    That fallback is REPORTED, in the same
    ``concealment_scan_warning:`` shape ``concealment.collect`` uses in the
    other four lanes (2026-09-02, review finding V7). It is what makes the
    note's ``injection_assessment.concealment_scan`` read ``incomplete``
    rather than passing an unwalked page off as walked-and-clean.
    """
    from . import concealment

    concealment.take_degraded()
    try:
        text = page.extract_text(
            visitor_operand_before=concealment.pdf_operand_visitor(
                page_no, concealed, seen, page)) or ""
    except Exception as exc:  # coverage-audit: warns, and the lost coverage
        # is what stops this page's text reading `full` (V15).
        warnings.append(
            f"concealment_scan_warning: page {page_no}: {type(exc).__name__}: {exc}")
        return page.extract_text() or ""
    # The visitor's own per-operator catch is silent to the walk by design;
    # it is not silent to the RECORD (V12, 2026-09-02).
    skipped = concealment.take_degraded()
    if skipped:
        warnings.append(
            f"concealment_scan_warning: page {page_no}: {skipped} operator(s) "
            "skipped after an internal error; some hiding may be unseen")
    return text


def _read_pages(
    reader: Any, ocr_budget: int, warnings: list[str], admitted: Any, *, can_ocr: bool,
) -> tuple[list[str], list[int], list[int], int, list[dict], int]:
    """One Markdown section per page, plus the page numbers that came back
    empty and the ones OCR rescued, plus the total image count the budget
    turned away (``images_skipped``). A page with no text layer is a SCAN,
    not an empty page — it is OCR'd rather than dropping the whole document
    into quarantine (owner ruling 2026-08-17).

    Every chunk goes on the coverage ledger as it is appended.
    ``pdf:text_layer`` is what the operand visitor watched; ``pdf:page_ocr``
    is text lifted out of a RASTER, which no walker reads, so one OCR'd page
    is enough to stop the whole document reading ``full`` (V13, 2026-09-03).

    The visitor also REPORTS what it decoded, into ``admitted.watch``. A page
    whose font makes the operands glyph codes (gap 5) produces a report that
    does not cover the page ``extract_text()`` returned, and the note reads
    ``unknown`` — because on such a page this visitor genuinely cannot search
    the text the reader gets (V15, 2026-09-03).
    """
    sections: list[str] = []
    scanned: list[int] = []
    ocred: list[int] = []
    concealed: list[dict] = []
    images_skipped = 0
    for i, page in enumerate(reader.pages, start=1):
        text = _page_text(page, i, concealed, warnings,
                          admitted.watch("pdf:text_layer")).strip()
        if len(text) >= _MIN_PAGE_CHARS:
            admitted.chrome(f"## Page {i}")
            admitted.add("pdf:text_layer", text)
            sections.append(f"## Page {i}\n\n{text}\n")
            continue
        ocr_text = ""
        if can_ocr:
            ocr_text, consumed, skipped = _ocr_page(page, ocr_budget)
            ocr_budget -= consumed
            images_skipped += skipped
        if len(ocr_text) >= _MIN_PAGE_CHARS:
            ocred.append(i)
            admitted.chrome(f"## Page {i} (OCR)")
            admitted.add("pdf:page_ocr", ocr_text)
            sections.append(f"## Page {i} (OCR)\n\n{ocr_text}\n")
        else:
            scanned.append(i)
            admitted.chrome(f"## Page {i} (scanned — no text extracted)")
            sections.append(f"## Page {i} (scanned — no text extracted)\n")
    return sections, scanned, ocred, ocr_budget, concealed, images_skipped


def _open_reader(path: Path) -> Any | ExtractResult:
    try:
        size = path.stat().st_size
    except OSError:  # coverage-audit: an unreadable size falls through to the gates below
        size = 0
    if size > MAX_PDF_BYTES:
        return ExtractResult.quarantine(
            "file_too_large",
            warnings=[f"pdf {size} bytes exceeds cap {MAX_PDF_BYTES}"],
        )
    try:
        return PdfReader(str(path))
    except Exception as exc:  # coverage-audit: quarantines; no text is admitted, so none is claimed
        return ExtractResult.quarantine(
            "pdf_extraction_error",
            warnings=[f"{type(exc).__name__}: {exc}"],
        )


def _render_pdf(reader: Any) -> ExtractResult:
    from . import concealment as _concealment

    encrypted_note: list[str] = []
    if reader.is_encrypted and not _open_empty_password(reader, encrypted_note):
        # Pre-sign guard (HARDENED:grill) — never sign a garbage/opaque
        # extraction of a file that really does need a password.
        return ExtractResult.quarantine("pdf_encrypted", warnings=encrypted_note)

    can_ocr = ocr_available()
    # Carried onto the SUCCESS path too: the note that this source was
    # permissions-encrypted belongs on the ingested record, not only in a
    # quarantine sidecar that no longer gets written. Built HERE rather than
    # after the read so `_page_text` can report a concealment-walk failure
    # into it.
    warnings = list(encrypted_note)
    admitted = _concealment.Admitted()
    try:
        total = len(reader.pages)
        sections, scanned_pages, ocr_pages, _budget_left, concealed, images_skipped = (
            _read_pages(reader, _ocr_max_pages() if can_ocr else 0, warnings,
                       admitted, can_ocr=can_ocr))
    except Exception as exc:  # coverage-audit: quarantines; no text is admitted, so none is claimed
        return ExtractResult.quarantine(
            "pdf_extraction_error",
            warnings=[f"{type(exc).__name__}: {exc}"],
        )

    if total == 0 or len(scanned_pages) == total:
        # Nothing readable came back — from the text layer OR from OCR.
        # Name WHY, so "install the local OCR engine" is distinguishable
        # from "this file genuinely holds no text".
        why = ("no text layer, and no local OCR engine is installed "
               "(pytesseract + the tesseract binary)") if not can_ocr else (
               "no text layer, and OCR extracted nothing")
        return ExtractResult.quarantine(
            "pdf_no_text_layer",
            warnings=[f"{len(scanned_pages)}/{total} pages had no extractable text",
                      why],
        )
    body = "\n".join(sections)
    reason = density_gate(body)
    if reason:
        return ExtractResult.quarantine(reason)

    if ocr_pages:
        warnings.append(f"ocr_pages: {len(ocr_pages)}/{total} read by local OCR")
    if scanned_pages:
        warnings.append(f"scanned_pages: {scanned_pages}")
    if images_skipped and can_ocr:
        warnings.append(
            f"ocr_image_cap reached ({_ocr_max_pages()}); "
            f"ocr_images_skipped: {images_skipped} — raise "
            "$BRAIN_PDF_OCR_MAX_PAGES and re-ingest")
    return ExtractResult(
        markdown=body,
        warnings=warnings,
        metadata={"page_count": total, "scanned_pages": scanned_pages,
                  "ocr_pages": ocr_pages, "ocr_images_skipped": images_skipped,
                  "concealed": concealed,
                  **_concealment.attest(warnings, body=body,
                                        admitted=admitted)},
    )


class PdfHandler(Handler):
    extensions = (".pdf",)
    dependency_name = "pypdf"

    @classmethod
    def available(cls) -> bool:
        return _HAS_PYPDF

    @classmethod
    def extract(cls, path: Path) -> ExtractResult:
        if not _HAS_PYPDF:
            return ExtractResult.quarantine("missing_dependency:pypdf")
        reader = _open_reader(path)
        if isinstance(reader, ExtractResult):
            return reader
        return _render_pdf(reader)

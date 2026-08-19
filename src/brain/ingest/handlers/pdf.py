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
except ImportError:  # pragma: no cover - exercised via degraded-deps test
    _HAS_PYPDF = False

_MIN_PAGE_CHARS = 5
# HARDENED:codex — max-size cap so a pathological file can't hang extraction
# or blow memory before it ever reaches the signed write path.
MAX_PDF_BYTES = 200 * 1024 * 1024  # 200 MB
# OCR costs ~1s/page, and the drain holds the single-writer lock while it runs.
# A cap keeps one pathological scan from stalling the hourly nightly; pages past
# it are reported as un-OCR'd, never silently dropped.
DEFAULT_OCR_MAX_PAGES = 400


def _ocr_max_pages() -> int:
    import os

    try:
        return int(os.environ.get("BRAIN_PDF_OCR_MAX_PAGES", DEFAULT_OCR_MAX_PAGES))
    except ValueError:
        return DEFAULT_OCR_MAX_PAGES


def _ocr_page(page: object) -> str:
    """OCR one page that carries no text layer, by reading the page's own
    embedded raster (a scan is one full-page image) — no rasterizer and no
    system binary beyond the optional local tesseract the image handler
    already uses. Returns "" when the page has no usable image."""
    try:
        images = list(page.images)  # type: ignore[attr-defined]
    except Exception:
        return ""
    texts = []
    for embedded in images:
        try:
            text, _ = ocr_image(embedded.image)
        except Exception:
            continue
        if text:
            texts.append(text)
    return "\n\n".join(texts).strip()


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
    except ImportError:  # pragma: no cover - pypdf below the pinned floor
        return False
    try:
        outcome = reader.decrypt("")  # type: ignore[attr-defined]
    except Exception as exc:
        note.append(f"decrypt attempt failed: {type(exc).__name__}: {exc}")
        return False
    if outcome == PasswordType.USER_PASSWORD:
        note.append("opened with an empty user password (permissions-only encryption)")
        return True
    note.append("a real password is required — supply an unlocked copy")
    return False


def _read_pages(reader: Any, ocr_budget: int) -> tuple[list[str], list[int], list[int], int]:
    """One Markdown section per page, plus the page numbers that came back
    empty and the ones OCR rescued. A page with no text layer is a SCAN, not
    an empty page — it is OCR'd rather than dropping the whole document into
    quarantine (owner ruling 2026-08-17)."""
    sections: list[str] = []
    scanned: list[int] = []
    ocred: list[int] = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if len(text) >= _MIN_PAGE_CHARS:
            sections.append(f"## Page {i}\n\n{text}\n")
            continue
        ocr_text = ""
        if ocr_budget > 0:
            ocr_budget -= 1
            ocr_text = _ocr_page(page)
        if len(ocr_text) >= _MIN_PAGE_CHARS:
            ocred.append(i)
            sections.append(f"## Page {i} (OCR)\n\n{ocr_text}\n")
        else:
            scanned.append(i)
            sections.append(f"## Page {i} (scanned — no text extracted)\n")
    return sections, scanned, ocred, ocr_budget


def _open_reader(path: Path) -> Any | ExtractResult:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size > MAX_PDF_BYTES:
        return ExtractResult.quarantine(
            "file_too_large",
            warnings=[f"pdf {size} bytes exceeds cap {MAX_PDF_BYTES}"],
        )
    try:
        return PdfReader(str(path))
    except Exception as exc:
        return ExtractResult.quarantine(
            "pdf_extraction_error",
            warnings=[f"{type(exc).__name__}: {exc}"],
        )


def _render_pdf(reader: Any) -> ExtractResult:
    encrypted_note: list[str] = []
    if reader.is_encrypted and not _open_empty_password(reader, encrypted_note):
        # Pre-sign guard (HARDENED:grill) — never sign a garbage/opaque
        # extraction of a file that really does need a password.
        return ExtractResult.quarantine("pdf_encrypted", warnings=encrypted_note)

    can_ocr = ocr_available()
    try:
        total = len(reader.pages)
        sections, scanned_pages, ocr_pages, budget_left = _read_pages(
            reader, _ocr_max_pages() if can_ocr else 0)
    except Exception as exc:
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

    # Carried onto the SUCCESS path too: the note that this source was
    # permissions-encrypted belongs on the ingested record, not only in a
    # quarantine sidecar that no longer gets written.
    warnings = list(encrypted_note)
    if ocr_pages:
        warnings.append(f"ocr_pages: {len(ocr_pages)}/{total} read by local OCR")
    if scanned_pages:
        warnings.append(f"scanned_pages: {scanned_pages}")
    if budget_left == 0 and scanned_pages and can_ocr:
        warnings.append(
            f"ocr_page_cap reached ({_ocr_max_pages()}); later scanned pages "
            "were not OCR'd — raise $BRAIN_PDF_OCR_MAX_PAGES and re-ingest")
    return ExtractResult(
        markdown=body,
        warnings=warnings,
        metadata={"page_count": total, "scanned_pages": scanned_pages,
                  "ocr_pages": ocr_pages},
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

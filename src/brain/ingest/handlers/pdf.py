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

from .base import ExtractResult, Handler, density_gate

try:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    _HAS_PYPDF = True
except ImportError:  # pragma: no cover - exercised via degraded-deps test
    _HAS_PYPDF = False

_MIN_PAGE_CHARS = 5
# HARDENED:codex — max-size cap so a pathological file can't hang extraction
# or blow memory before it ever reaches the signed write path.
MAX_PDF_BYTES = 200 * 1024 * 1024  # 200 MB


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
            reader = PdfReader(str(path))
        except Exception as exc:
            return ExtractResult.quarantine(
                "pdf_extraction_error", warnings=[f"{type(exc).__name__}: {exc}"]
            )
        if reader.is_encrypted:
            # Pre-sign guard (HARDENED:grill) — never sign a garbage/opaque
            # extraction of a password-protected file.
            return ExtractResult.quarantine("pdf_encrypted")

        scanned_pages: list[int] = []
        sections: list[str] = []
        try:
            total = len(reader.pages)
            for i, page in enumerate(reader.pages, start=1):
                text = (page.extract_text() or "").strip()
                if len(text) < _MIN_PAGE_CHARS:
                    scanned_pages.append(i)
                    sections.append(f"## Page {i} (scanned — no text extracted)\n")
                else:
                    sections.append(f"## Page {i}\n\n{text}\n")
        except PdfReadError as exc:
            return ExtractResult.quarantine(
                "pdf_extraction_error", warnings=[f"{type(exc).__name__}: {exc}"]
            )
        except Exception as exc:
            return ExtractResult.quarantine(
                "pdf_extraction_error", warnings=[f"{type(exc).__name__}: {exc}"]
            )

        if total == 0 or len(scanned_pages) == total:
            return ExtractResult.quarantine(
                "pdf_no_text_layer",
                warnings=[f"{len(scanned_pages)}/{total} pages had no extractable text"],
            )

        body = "\n".join(sections)
        reason = density_gate(body)
        if reason:
            return ExtractResult.quarantine(reason)

        warnings = []
        if scanned_pages:
            warnings.append(f"scanned_pages: {scanned_pages}")
        return ExtractResult(
            markdown=body,
            warnings=warnings,
            metadata={"page_count": total, "scanned_pages": scanned_pages},
        )

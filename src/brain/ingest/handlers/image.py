"""Image handler — Pillow for metadata, optional LOCAL-only OCR (pytesseract
+ the system tesseract binary). ADR-0003 Ruling 1(g): image OCR degrades
gracefully when the local engine is absent; there is NO cloud OCR code path
at all in this kernel (never optional, never a fallback) — a screenshot with
no local OCR available simply becomes a metadata-only note, never a quarantine.

ponytail: no HEIC/HEIF (would need the extra pillow-heif optional dep) — add
if a real HEIC drop shows up; Pillow alone already covers the common formats.
"""
from __future__ import annotations

from pathlib import Path

from .base import ExtractResult, Handler, density_gate

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:  # pragma: no cover - exercised via degraded-deps test
    _HAS_PIL = False

try:
    import pytesseract
    _HAS_PYTESSERACT = True
except ImportError:  # pragma: no cover
    _HAS_PYTESSERACT = False

MAX_IMAGE_BYTES = 100 * 1024 * 1024


def _ocr(img: "Image.Image") -> tuple[str, list[str]]:
    """LOCAL-only OCR. Never raises: missing binding, missing tesseract
    binary (pytesseract.TesseractNotFoundError), or any other engine failure
    all degrade to a metadata-only body — there is no cloud fallback to
    reach for, so failure here is never fatal to the ingest."""
    if not _HAS_PYTESSERACT:
        return "", ["ocr_unavailable: pytesseract not installed (metadata-only)"]
    try:
        text = pytesseract.image_to_string(img)
        return text.strip(), []
    except Exception as exc:
        return "", [f"ocr_unavailable: {type(exc).__name__}: {exc}"]


class ImageHandler(Handler):
    extensions = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff")
    dependency_name = "Pillow"

    @classmethod
    def available(cls) -> bool:
        return _HAS_PIL

    @classmethod
    def extract(cls, path: Path) -> ExtractResult:
        if not _HAS_PIL:
            return ExtractResult.quarantine("missing_dependency:Pillow")
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > MAX_IMAGE_BYTES:
            return ExtractResult.quarantine(
                "file_too_large", warnings=[f"{size} bytes exceeds cap {MAX_IMAGE_BYTES}"]
            )
        try:
            with Image.open(path) as img:
                img.load()  # force decode now, inside the try
                fmt = img.format or path.suffix.lstrip(".").upper() or "image"
                width, height = img.size
                mode = img.mode
                ocr_text, warnings = _ocr(img)
        except Exception as exc:
            return ExtractResult.quarantine(
                "image_open_error", warnings=[f"{type(exc).__name__}: {exc}"]
            )

        body = (
            "## OCR (verbatim)\n\n"
            f"{ocr_text if ocr_text else '[no text detected]'}\n\n"
            "## Image metadata\n\n"
            f"- **Format:** {fmt}\n"
            f"- **Dimensions:** {width} x {height} px\n"
            f"- **Mode:** {mode}\n"
        )
        reason = density_gate(body)
        if reason:
            return ExtractResult.quarantine(reason, warnings=warnings)
        return ExtractResult(
            markdown=body, warnings=warnings,
            metadata={"format": fmt, "width": width, "height": height, "ocr": bool(ocr_text)},
        )

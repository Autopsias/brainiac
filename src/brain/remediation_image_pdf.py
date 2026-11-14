"""extract_retry's conversion step: re-render an unreadable quarantined file
as an IMAGE-ONLY PDF, so the PDF handler's own OCR reads it on the retry.

Two shapes the handlers cannot read, measured 2026-11-14 on the live vault:

* ``pdf_no_text_layer`` — a "Microsoft Print to PDF" file draws its text as
  vector shapes: no font, no embedded image, so the PDF handler's OCR (which
  reads EMBEDDED images only) gets nothing. Rendering the pages fixes it.
* ``empty_or_low_text_density`` .docx — slides pasted into Word as pictures.
  The docx handler has no OCR at all.

An image-only PDF goes through ``pdf._ocr_page`` and is admitted as
``pdf:page_ocr``, the source the concealment gate already declares. An
``ocrmypdf`` text layer is NOT used: the PDF handler flags every word of an
invisible text layer as concealed (measured the same day).

Returns ``None`` whenever there is nothing to convert or anything fails, and
the caller then copies the original bytes exactly as before.
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, BinaryIO

logger = logging.getLogger(__name__)

#: quarantine reason -> the one suffix this step converts for it.
CONVERTIBLE = {"pdf_no_text_layer": ".pdf", "empty_or_low_text_density": ".docx"}
MAX_PAGES = 30
#: Long side in pixels. ~300 dpi on A4 is 3508; 2000 keeps OCR good and RAM low.
MAX_SIDE = 2000
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff")
#: launchd's PATH has no Homebrew (see base._TESSERACT_FALLBACKS).
_PDFTOPPM_FALLBACKS = ("/opt/homebrew/bin/pdftoppm", "/usr/local/bin/pdftoppm")


def _pdftoppm() -> str | None:
    found = shutil.which("pdftoppm")
    if found:
        return found
    return next((c for c in _PDFTOPPM_FALLBACKS if os.access(c, os.X_OK)), None)


def _natural(name: str) -> list[Any]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def _fit(img: Any) -> Any:
    from .ingest.handlers.base import too_many_pixels

    if too_many_pixels(img):
        return None
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    return img


def _docx_images(fh: BinaryIO, path: Path) -> list[Any]:
    from PIL import Image

    from .ingest.handlers.base import ooxml_expansion_gate

    if ooxml_expansion_gate(path) is not None:  # a zip bomb stays quarantined
        return []
    with zipfile.ZipFile(fh) as z:
        names = sorted((n for n in z.namelist() if n.startswith("word/media/")
                        and n.lower().endswith(_IMAGE_SUFFIXES)), key=_natural)
        images = []
        for name in names[:MAX_PAGES]:
            with Image.open(io.BytesIO(z.read(name))) as img:
                fitted = _fit(img)
            if fitted is not None:
                images.append(fitted)
        return images


def _pdf_pages(path: Path, workdir: Path) -> list[Any]:
    from PIL import Image

    binary = _pdftoppm()
    if binary is None:
        return []
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [binary, "-gray", "-scale-to", str(MAX_SIDE), "-l", str(MAX_PAGES),
         "-png", str(path), str(workdir / "page")],
        check=True, capture_output=True, timeout=180)
    images = []
    for page in sorted(workdir.glob("page-*.png"), key=lambda p: _natural(p.name)):
        with Image.open(page) as img:
            fitted = _fit(img)
        if fitted is not None:
            images.append(fitted)
    return images


def image_pdf(fh: BinaryIO, source: Path, reason: str) -> bytes | None:
    """Image-only PDF bytes for ``source`` (read from the already-open,
    ``O_NOFOLLOW`` descriptor ``fh``), or ``None`` to copy it unchanged."""
    if CONVERTIBLE.get(reason) != source.suffix.lower():
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="brain-retry-") as tmp:
            work = Path(tmp)
            # Convert a private copy, never the path: the mount is VM-writable.
            copy = work / f"in{source.suffix.lower()}"
            with open(copy, "wb") as out:
                shutil.copyfileobj(fh, out)
            if source.suffix.lower() == ".docx":
                with open(copy, "rb") as zf:
                    images = _docx_images(zf, copy)
            else:
                images = _pdf_pages(copy, work)
            if not images:
                return None
            buf = io.BytesIO()
            images[0].save(buf, "PDF", save_all=True,
                           append_images=images[1:], resolution=200)
            return buf.getvalue()
    except Exception:  # coverage-audit: no conversion; the original is copied as before
        logger.warning("extract_retry: image-PDF conversion failed for %s",
                       source.name, exc_info=True)
        return None
    finally:
        fh.seek(0)

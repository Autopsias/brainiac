"""Bounded local fallback for blank OCR on small, low-contrast screenshots."""
from __future__ import annotations

import re
import time

MAX_RETRY_PIXELS = 10_000_000


def read_image(engine, img, lang: str, timeout: int) -> tuple[str, list[str]]:
    """Keep ordinary OCR; retry blank small images within the same time budget.

    Only an in-memory recognition copy is changed. Original bytes are untouched.
    Fallback text is explicitly flagged for review, not certified as accurate.
    """
    started = time.monotonic()
    text = engine.image_to_string(img, lang=lang, timeout=timeout).strip()
    if text or timeout <= 0 or min(img.size) < 100:
        return text, []
    width, height = img.size
    if width * height * 9 > MAX_RETRY_PIXELS:
        return '', ['ocr_retry_skipped: recognition-copy pixel budget exceeded']
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        return '', ['ocr_retry_skipped: OCR time budget exhausted']
    from PIL import Image
    scan = img.convert('L').resize((width * 3, height * 3), Image.Resampling.LANCZOS)
    scan = scan.point(lambda value: 0 if value < 200 else 255)
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        return '', ['ocr_retry_skipped: OCR time budget exhausted']
    text = engine.image_to_string(scan, lang=lang, config='--psm 11', timeout=remaining).strip()
    if len(re.findall(r'[A-Za-zÀ-ÿ]{3,}', text)) < 8:
        return '', ['ocr_retry_empty: insufficient text in recognition copy']
    return text, ['ocr_low_contrast_retry: local recognition copy; verify against original image']

"""Extension -> Handler registry (ING-01)."""
from __future__ import annotations

from .base import ExtractResult, Handler, density_gate, strip_control_chars
from .docx import DocxHandler
from .email import EmailHandler
from .html import HtmlHandler
from .image import ImageHandler
from .pdf import PdfHandler
from .pptx import PptxHandler
from .text import TextHandler
from .xlsx import XlsxHandler
from .zip import ZipHandler

ALL_HANDLERS: tuple[type[Handler], ...] = (
    PdfHandler, DocxHandler, PptxHandler, XlsxHandler, TextHandler,
    ImageHandler, EmailHandler, HtmlHandler, ZipHandler,
)

REGISTRY: dict[str, type[Handler]] = {
    ext: handler for handler in ALL_HANDLERS for ext in handler.extensions
}


def handler_for(path) -> type[Handler] | None:
    from pathlib import Path

    return REGISTRY.get(Path(path).suffix.lower())


def capability_report() -> dict[str, dict]:
    """Which extension handlers are available right now (ING-01 dep probe)."""
    return {
        ext: {"handler": handler.__name__, "dependency": handler.dependency_name,
              "available": handler.available()}
        for ext, handler in REGISTRY.items()
    }


__all__ = [
    "ExtractResult", "Handler", "density_gate", "strip_control_chars",
    "ALL_HANDLERS", "REGISTRY", "handler_for", "capability_report",
]

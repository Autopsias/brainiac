"""Plain-text / Markdown pass-through handler."""
from __future__ import annotations

from pathlib import Path

from .base import ExtractResult, Handler, density_gate

_ENCODINGS = ("utf-8", "utf-8-sig", "latin-1")


class TextHandler(Handler):
    extensions = (".txt", ".md", ".markdown", ".csv")
    dependency_name = "stdlib"

    @classmethod
    def available(cls) -> bool:
        return True

    @classmethod
    def extract(cls, path: Path) -> ExtractResult:
        # C2(b): read_bytes() used to be unguarded — an OSError (permission
        # denied, disk error, vanished file) propagated as a raw exception out
        # of a handler, which the contract (see handlers/base.py Handler.extract
        # docstring) forbids: a crash here must never abort the whole drain.
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return ExtractResult.quarantine(
                "text_read_error", warnings=[f"{type(exc).__name__}: {exc}"]
            )
        text: str | None = None
        for enc in _ENCODINGS:
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            return ExtractResult.quarantine("text_decode_error")
        reason = density_gate(text)
        if reason:
            return ExtractResult.quarantine(reason)
        return ExtractResult(markdown=text, metadata={"encoding": "utf-8"})

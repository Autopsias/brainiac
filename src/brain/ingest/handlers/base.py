"""Handler contract (ING-01/ING-02, ADR-0003 Ruling 1).

Every format handler implements one function contract: given a Path, return an
``ExtractResult``. Handlers never touch the vault, the index, or the audit
chain — that is the orchestrator's job (``brain.ingest.run_ingest``). This
keeps a handler pure and trivially testable: bytes in, Markdown (or a
quarantine reason) out.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Below this many non-whitespace characters, a "successfully extracted"
# document is treated as empty/near-empty content — the OHRBench finding that
# upstream extraction failure (not the write path) is the dominant corpus-
# corruption vector. Quarantine, never sign a near-empty source.
MIN_CONTENT_CHARS = 40


@dataclass
class ExtractResult:
    """Outcome of one handler's extraction attempt.

    ``quarantine_reason`` is ``None`` on success. ``markdown``/``warnings``/
    ``metadata`` are always populated (possibly empty) so callers never branch
    on attribute presence.
    """

    markdown: str = ""
    warnings: list[str] = field(default_factory=list)
    quarantine_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.quarantine_reason is None

    @staticmethod
    def quarantine(reason: str, *, warnings: list[str] | None = None) -> "ExtractResult":
        return ExtractResult(quarantine_reason=reason, warnings=list(warnings or []))


def density_gate(markdown: str, *, min_chars: int = MIN_CONTENT_CHARS) -> str | None:
    """Generic extraction-quality gate (HARDENED:grill): empty-text / low text
    density detector shared by every handler. Strips table-unparsed fences and
    page markers before counting so a document that is ENTIRELY scanned pages
    or an unparsed table dump does not slip past on marker text alone.
    Returns a quarantine reason, or ``None`` if the content passes."""
    import re

    stripped = re.sub(r"^#{1,3}\s.*$", "", markdown, flags=re.MULTILINE)
    stripped = stripped.replace("```", "")
    content_chars = len(stripped.strip())
    if content_chars < min_chars:
        return "empty_or_low_text_density"
    return None


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def strip_control_chars(name: str) -> str:
    """Strip control chars (incl. embedded newlines) from an untrusted name
    (zip member, email attachment/header, HTML title, ...) before it flows
    into generated Markdown body text or a report entry (S06 HARDENED — the
    S05 lesson was frontmatter; a control char in body text can still forge a
    fake heading/table row in the rendered note or a report line)."""
    if not name:
        return name
    return _CONTROL_CHARS.sub("", name)


class Handler(ABC):
    """One handler per file extension family."""

    #: lower-cased extensions this handler claims, e.g. (".pdf",)
    extensions: tuple[str, ...] = ()
    #: human label for capability-probe reporting, e.g. "pypdf"
    dependency_name: str = ""

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """True iff this handler's extraction dependency import-succeeds."""

    @classmethod
    @abstractmethod
    def extract(cls, path: Path) -> ExtractResult:
        """Extract ``path`` to Markdown. MUST NOT raise on malformed input —
        catch and return a quarantine ``ExtractResult`` instead (a crash here
        would abort the whole drain, not just this one file)."""

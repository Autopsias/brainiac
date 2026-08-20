"""Is this sha256 a DUPLICATE of an ingested document, or a RETRY of one the
engine failed to read?

The two look identical from the manifest, which is why they were confused: the
duplicate check keys on the ORIGINAL's sha256 and runs before extraction, so
nothing in it can see whether the note that sha already produced holds the
document or holds a failure to read it.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .handlers.base import NO_TEXT_MARKER

if TYPE_CHECKING:  # the stage record; a runtime import here would be a cycle
    from .pipeline_stages import ClaimRecord


def prior_extraction_failed(vault: Path, existing_id: str) -> bool:
    """True when the note this sha256 already produced is a FAILED EXTRACTION.

    An image whose OCR returned nothing is still ingested — its dimensions and
    format are a real record — but the note carries `NO_TEXT_MARKER` where its
    text should be. That note is not the document; it is the record of a
    failure to read it, and the document itself never entered the corpus.

    Until 2026-08-19 nothing could tell the two apart, and the consequence was
    permanent: the failed extraction put the binary in the ingest manifest, and
    re-offering the same bytes — after installing tesseract, after any handler
    improvement — was refused as "identical content already ingested". Measured
    on the reference vault the same day: 20 sources, every one a carve-out memo
    annex or scenario deck, each holding 616-4199 characters that the CURRENT
    engine reads perfectly, none of it reachable and none of it re-ingestible.

    The PDF handler never had this problem because it QUARANTINES a no-text-
    layer file, and a quarantined file never enters the manifest.
    """
    note = vault / "raw" / f"{existing_id}.md"
    try:
        return NO_TEXT_MARKER in note.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def record_duplicate(record: "ClaimRecord", existing_id: str) -> "ClaimRecord":
    """Move the claimed file aside as a duplicate of an already-ingested note."""
    from . import pipeline as facade

    assert record.claimed is not None
    facade._move(record.claimed, record.drain.duplicate_dir / record.claimed.name)
    (record.drain.duplicate_dir / f"{record.claimed.name}.duplicate-of.txt").write_text(
        f"identical content already ingested as raw/{existing_id}.md\n",
        encoding="utf-8",
    )
    record.append("duplicates", {
        "file": record.claimed.name,
        "existing_id": existing_id,
        "classification": facade._existing_note_classification(
            record.drain.vault,
            existing_id,
        ),
    })
    record.terminal = True
    return record

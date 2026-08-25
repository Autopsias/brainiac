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


def rendition_slug(
    record: "ClaimRecord", note_path: Path, existing_meta: dict
) -> str:
    """``"<slug>-<ext>"`` when the incoming file is another FORMAT of the note
    already at ``<slug>``, else ``""``.

    The drop zone slugifies a filename STEM, so `memo_v15.pdf` and
    `memo_v15.md` claim one id and the second arrived as a
    ``note_id_collision`` — refused, reachable by nobody. Measured 2026-08-25
    on the reference vault: one such file in quarantine, and every future
    docx+pdf attachment pair would have joined it. A format twin is ingested
    under its own id instead; the version-link lane's format-twin rule
    (`versionlink_stages`) then proposes retiring it under the primary, which
    keeps the RETIREMENT an owner decision rather than an ingest-time guess.
    A same-format collision is still a collision.
    """
    assert record.claimed is not None
    incoming = record.claimed.suffix.casefold()
    existing = Path(str(existing_meta.get("origin") or "")).suffix.casefold()
    if not incoming or not existing or incoming == existing:
        return ""
    candidate = f"{record.slug}-{incoming.lstrip('.')}"
    if (note_path.parent / f"{candidate}.md").exists():
        return ""   # the rendition id is taken too — a real collision, refuse
    return candidate


def take_rendition(
    record: "ClaimRecord", note_path: Path, existing_meta: dict
) -> Path | None:
    """Retarget ``record`` at its rendition id, or ``None`` to refuse.

    Only the ``id`` moves. The archived original is unchanged and still the
    file this note came from, and every other key was already decided for
    these exact bytes.

    It REBUILT the metadata from ``pipeline._meta`` until 2026-08-25, and that
    silently undid the ENF-04 tier verdict: ``_meta`` declares ``Internal``
    for every drop-zone ingest, and the guard's raise, its
    ``classification_guard*`` stamps and the deliverable lane's declared tier
    are all written AFTER it returns. A rendition of an MNPI document was
    therefore signed as ``Internal``, admitted into the guard's own corpus at
    ``Internal``, and anchored at ``Internal`` — three lanes, one cause. The
    rebuild could never have been right either way: ``slug`` is ``_meta``'s
    only changed argument and it reaches exactly one key.
    """
    slug = rendition_slug(record, note_path, existing_meta)
    if not slug:
        return None
    record.append("renditions", {
        "file": record.orig_name, "id": slug, "primary": record.slug,
    })
    record.slug = slug
    record.meta["id"] = slug
    return record.drain.vault / "raw" / f"{slug}.md"


def record_existing_note(
    record: "ClaimRecord", existing_meta: dict
) -> "ClaimRecord":
    """File the claim as a duplicate of the note ALREADY at ``record.slug``
    (same id, same bytes) and record its tier, so a capped reader learns the
    copy exists at a tier it cannot see rather than nothing at all."""
    from . import pipeline as facade

    assert record.claimed is not None
    record.drain.manifest[record.original_sha] = record.slug
    facade._save_manifest(record.drain.vault, record.drain.manifest)
    record.claimed.unlink(missing_ok=True)
    tier = existing_meta.get("classification")
    record.append("duplicates", {
        "file": record.orig_name,
        "existing_id": record.slug,
        "classification": str(tier) if tier else None,
    })
    record.terminal = True
    return record


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

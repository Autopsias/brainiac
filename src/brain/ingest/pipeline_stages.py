"""Advance one ingest candidate through the secure admission state machine."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import handlers as H
from . import tierguard as TG
from .handlers.base import NO_TEXT_MARKER
from .pipeline_duplicates import prior_extraction_failed, record_duplicate


@dataclass
class DrainRecord:
    """Run-scoped state shared by every claim in one inbox drain."""

    core: Any
    vault: Path
    inbox: Path
    processing_dir: Path
    quarantine_dir: Path
    duplicate_dir: Path
    manifest: dict[str, str]
    failures: dict[str, int]
    guard: TG.CrossTierGuard
    today: str
    report: dict[str, Any]
    released_shas: set[str] = field(default_factory=set)
    anchor_pubkey: Any = None


@dataclass
class ClaimRecord:
    """Mutable state for one candidate as it crosses each admission stage."""

    drain: DrainRecord
    path: Path
    orig_name: str
    claimed: Path | None = None
    original_bytes: bytes | None = None
    original_sha: str = ""
    anchor: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    handler: Any = None
    result: Any = None
    slug: str = ""
    archive_path: Path | None = None
    linked_markdown: str = ""
    autolink_added: Any = None
    meta: dict[str, Any] = field(default_factory=dict)
    verdict: TG.Verdict | None = None
    classification: str = ""
    terminal: bool = False
    stop_drain: bool = False
    depth: int = 0
    budget: dict[str, int] = field(default_factory=lambda: {"bytes": 0, "items": 0})
    parent: str | None = None

    def append(self, bucket: str, entry: dict[str, Any]) -> None:
        if self.parent is not None:
            entry["parent"] = self.parent
        self.drain.report[bucket].append(entry)


def sweep_stage(record: DrainRecord) -> DrainRecord:
    """Apply the crash-recovery sweep before the candidate scan."""
    from . import pipeline as facade

    facade._sweep_stale_processing(
        record.processing_dir,
        record.inbox,
        vault=record.vault,
        quarantine_dir=record.quarantine_dir,
        failures=record.failures,
    )
    return record


def claim_stage(record: ClaimRecord) -> ClaimRecord:
    """Acquire the atomic processing claim."""
    from . import pipeline as facade

    record.claimed = facade._claim(record.path, record.drain.processing_dir)
    if record.claimed is None:
        record.append("skipped", {"file": record.path.name, "reason": "claimed_elsewhere"})
        record.terminal = True
    return record


def nofollow_read_stage(record: ClaimRecord) -> ClaimRecord:
    """Read the claimed inode once without following a swapped name."""
    if record.terminal:
        return record
    from .. import cos as COS
    from . import pipeline as facade

    assert record.claimed is not None
    try:
        # INT-04.6: the size cap is enforced on this no-follow descriptor.
        original_bytes = COS.read_nofollow(
            record.claimed,
            max_bytes=facade.MAX_INGEST_BYTES,
        )
    except COS.ApprovedTooLarge as exc:
        facade._quarantine(
            record.claimed,
            record.drain.quarantine_dir,
            "file_too_large",
            [f"{exc} (ingest cap {facade.MAX_INGEST_BYTES})"],
        )
        record.append("quarantined", {"file": record.claimed.name, "reason": "file_too_large"})
        record.terminal = True
        return record
    except COS.ApprovedRefused as exc:
        facade._quarantine(
            record.claimed,
            record.drain.quarantine_dir,
            "irregular_entry",
            [str(exc)],
        )
        record.append("quarantined", {"file": record.claimed.name, "reason": "irregular_entry"})
        record.terminal = True
        return record
    record.original_bytes = original_bytes
    record.original_sha = facade._sha256_bytes(original_bytes)
    return record


def acceptance_anchor_stage(record: ClaimRecord) -> ClaimRecord:
    """Verify accepted bytes against the off-mount host-signed anchor."""
    if record.terminal:
        return record
    from .. import cos as COS
    from . import pipeline as facade

    assert record.claimed is not None
    assert record.original_bytes is not None
    try:
        if record.drain.anchor_pubkey is None and COS.attachment_anchor_exists(
            record.drain.vault,
            record.path,
            record.original_sha,
        ):
            record.drain.anchor_pubkey = COS.approved_verify_key(record.drain.vault)
        # INT-04: verify the exact buffer that later stages may sign.
        anchor = COS.verify_attachment_bytes(
            record.drain.vault,
            record.path,
            record.original_bytes,
            pubkey=record.drain.anchor_pubkey,
        )
    except COS.ApprovedKeyUnavailable as exc:
        if record.claimed.exists():
            facade._move(
                record.claimed,
                facade._unique_dest(record.drain.inbox, record.claimed.name),
            )
        record.append("skipped", {
            "file": record.path.name,
            "reason": f"systemic_error:{type(exc).__name__}: {exc}",
        })
        record.terminal = True
        record.stop_drain = True
        return record
    except COS.ApprovedRefused as exc:
        facade._quarantine(
            record.claimed,
            record.drain.quarantine_dir,
            "approved_anchor_mismatch",
            [str(exc)],
        )
        COS.log_defect(
            record.drain.vault,
            "attachment-anchor-mismatch",
            f"{record.path.name}: {exc}",
        )
        record.append("quarantined", {
            "file": record.path.name,
            "reason": "approved_anchor_mismatch",
        })
        record.terminal = True
        return record
    if anchor is None and record.original_sha in record.drain.released_shas:
        reason = "approved_anchor_missing"
        facade._quarantine(record.claimed, record.drain.quarantine_dir, reason, [
            "a release record names these exact bytes as an owner-accepted "
            "attachment, but no acceptance anchor covers them any more — "
            "ingesting them now would drop them to `Internal`. Re-accept "
            "the attachment (the payload is intact in the quarantine dir).",
        ])
        COS.log_defect(
            record.drain.vault,
            "attachment-anchor-missing",
            f"{record.path.name}: a release record names these bytes and no "
            "anchor covers them (anchor store lost or GC'd) — NOT ingested",
        )
        record.append("quarantined", {"file": record.path.name, "reason": reason})
        record.terminal = True
        return record
    record.anchor = anchor
    record.provenance = (anchor or {}).get("claim") or None
    return record


def operational_set_aside_stage(record: ClaimRecord) -> ClaimRecord:
    """Set aside a UTF-8 operational artifact before handler dispatch."""
    if record.terminal:
        return record
    from . import pipeline as facade

    assert record.claimed is not None
    assert record.original_bytes is not None
    try:
        source_text = record.original_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return record
    declared = facade._operational_type(source_text)
    if declared:
        facade._set_aside_operational(record.claimed, record.drain.inbox, declared)
        record.append("skipped", {
            "file": record.claimed.name,
            "reason": f"operational_artifact:{declared}",
        })
        record.terminal = True
    return record


def extraction_handler_stage(record: ClaimRecord) -> ClaimRecord:
    """Extract verified bytes with the selected format handler."""
    if record.terminal:
        return record
    from . import pipeline as facade

    assert record.claimed is not None
    assert record.original_bytes is not None
    retry_of: str | None = None
    if record.original_sha in record.drain.manifest:
        existing_id = record.drain.manifest[record.original_sha]
        if not prior_extraction_failed(record.drain.vault, existing_id):
            return record_duplicate(record, existing_id)
        # The prior ingest failed to read this file. Let it through and see
        # whether THIS engine does better; if it does not, the tail of this
        # function files it as the duplicate it would otherwise have been, so
        # an hourly drain can never accumulate one stub per run.
        retry_of = existing_id
    handler = H.handler_for(record.claimed)
    if handler is None:
        return _quarantine_handler_failure(record, "no_handler_for_extension", [])
    if not handler.available():
        return _quarantine_handler_failure(
            record,
            f"missing_dependency:{handler.dependency_name}",
            [],
        )
    record.handler = handler
    result = facade._extract_verified(
        handler,
        record.original_bytes,
        record.claimed.suffix,
        record.drain.vault,
    )
    if not result.ok:
        return _quarantine_handler_failure(
            record,
            result.quarantine_reason,
            result.warnings,
        )
    if retry_of is not None and NO_TEXT_MARKER in (result.markdown or ""):
        # Re-read produced nothing either. Nothing has improved, so this is the
        # duplicate the manifest said it was — filed as one, not written as a
        # second identical stub.
        return record_duplicate(record, retry_of)
    record.result = result
    # Defense for handler-produced Markdown that was not directly decodable.
    declared = facade._operational_type(result.markdown or "")
    if declared:
        facade._set_aside_operational(record.claimed, record.drain.inbox, declared)
        record.append("skipped", {
            "file": record.claimed.name,
            "reason": f"operational_artifact:{declared}",
        })
        record.terminal = True
    return record


def _quarantine_handler_failure(
    record: ClaimRecord,
    reason: str,
    warnings: list[str],
) -> ClaimRecord:
    from . import pipeline as facade

    assert record.claimed is not None
    facade._quarantine(record.claimed, record.drain.quarantine_dir, reason, warnings)
    record.append("quarantined", {"file": record.claimed.name, "reason": reason})
    record.terminal = True
    return record


def tierguard_stage(record: ClaimRecord) -> ClaimRecord:
    """Apply autolinks, provenance, and the ENF-04 admission verdict."""
    if record.terminal:
        return record
    from .. import autolink
    from . import pipeline as facade
    from ..notes import safe_slug

    assert record.claimed is not None
    assert record.result is not None
    stem = facade._slugify_stem(record.claimed.stem)
    record.slug = safe_slug(f"{record.drain.today}-{stem}")
    archive_subdir = (
        record.drain.vault / "raw" / "originals" / f"{record.drain.today}-{stem}"
    )
    record.archive_path = archive_subdir / facade._sanitize_archive_name(record.claimed.name)
    record.linked_markdown, record.autolink_added = autolink.apply_autolinks(
        record.result.markdown,
        title=record.orig_name,
        vault=record.drain.vault,
    )
    parsed = (
        record.result.metadata.get("provenance")
        if isinstance(record.result.metadata, dict)
        else None
    )
    provenance = record.provenance
    if isinstance(parsed, dict) and parsed:
        provenance = {**parsed, "verified": True}
    record.provenance = provenance
    record.meta = facade._meta(
        record.slug,
        record.drain.today,
        record.archive_path,
        record.drain.vault,
        hashlib.sha256(record.linked_markdown.encode("utf-8")).hexdigest(),
        provenance,
    )
    record.verdict = record.drain.guard.verdict(
        record.linked_markdown,
        str(record.meta["classification"]),
    )
    record.meta["classification"] = record.verdict.tier
    record.meta.update(record.verdict.frontmatter())
    record.classification = str(record.meta["classification"])
    return record


def archive_original_stage(record: ClaimRecord) -> ClaimRecord:
    """Create the immutable archived original without replacement."""
    if record.terminal:
        return record
    from . import pipeline as facade

    assert record.claimed is not None
    assert record.original_bytes is not None
    assert record.archive_path is not None
    status = facade._create_exclusive_or_collision(
        record.archive_path,
        record.original_bytes,
        known_sha=record.original_sha,
    )
    if status == "collision":
        reason = "archive_collision"
        facade._quarantine(record.claimed, record.drain.quarantine_dir, reason, [
            "archived-original target already holds different content: "
            f"{record.archive_path}",
        ])
        record.append("quarantined", {"file": record.claimed.name, "reason": reason})
        record.terminal = True
    return record


def signed_note_write_stage(record: ClaimRecord) -> ClaimRecord:
    """Commit the signed raw note and publish its in-run tierguard admission."""
    if record.terminal:
        return record
    from .. import frontmatter as fm
    from . import pipeline as facade

    assert record.claimed is not None
    assert record.archive_path is not None
    assert record.result is not None
    assert record.verdict is not None
    note_rel = f"raw/{record.slug}.md"
    note_path = record.drain.vault / note_rel
    if note_path.exists():
        existing_meta, _ = fm.parse_text(note_path.read_text(encoding="utf-8"))
        if str(existing_meta.get("sha256", "")) != str(record.meta["sha256"]):
            reason = "note_id_collision"
            facade._quarantine(record.claimed, record.drain.quarantine_dir, reason, [
                f"raw/{record.slug}.md already exists with different content",
            ])
            record.append("quarantined", {"file": record.claimed.name, "reason": reason})
            record.terminal = True
            return record
        record.drain.manifest[record.original_sha] = record.slug
        facade._save_manifest(record.drain.vault, record.drain.manifest)
        record.claimed.unlink(missing_ok=True)
        existing_classification = existing_meta.get("classification")
        record.append("duplicates", {
            "file": record.orig_name,
            "existing_id": record.slug,
            "classification": (
                str(existing_classification) if existing_classification else None
            ),
        })
        record.terminal = True
        return record
    content = facade._build_frontmatter(record.meta, record.linked_markdown)
    record.drain.core.write_note(
        note_rel,
        content,
        reason=(
            f"ingest {record.orig_name} -> raw/{record.slug}.md "
            f"(original archived at {record.archive_path.relative_to(record.drain.vault)})"
        ),
        subtree="raw",
    )
    record.drain.manifest[record.original_sha] = record.slug
    facade._save_manifest(record.drain.vault, record.drain.manifest)
    record.claimed.unlink(missing_ok=True)
    record.drain.guard.admit(
        record.slug,
        record.linked_markdown,
        record.classification,
    )
    entry = {
        "file": record.orig_name,
        "id": record.slug,
        "note": note_rel,
        "archived": str(record.archive_path.relative_to(record.drain.vault)),
        "classification": record.classification,
        "warnings": record.result.warnings,
        "autolink_added": record.autolink_added,
        "guard": record.verdict.status,
    }
    if record.verdict.status != TG.CLEAR:
        entry["guard_reason"] = record.verdict.reason
    record.append("processed", entry)
    _expand_nested(record)
    record.terminal = True
    return record


_VERIFIED_STAGES: tuple[Callable[[ClaimRecord], ClaimRecord], ...] = (
    operational_set_aside_stage,
    extraction_handler_stage,
    tierguard_stage,
    archive_original_stage,
    signed_note_write_stage,
)


def process_verified_claim(record: ClaimRecord) -> ClaimRecord:
    """Advance a claim whose bytes already crossed the no-follow boundary."""
    for stage in _VERIFIED_STAGES:
        record = stage(record)
        if record.terminal:
            break
    return record


def _expand_nested(record: ClaimRecord) -> None:
    from . import pipeline_nested

    nested = (
        record.result.metadata.get("nested")
        if isinstance(record.result.metadata, dict)
        else None
    )
    if nested:
        pipeline_nested.process_nested(
            nested,
            parent_slug=record.slug,
            depth=record.depth,
            budget=record.budget,
            drain=record.drain,
            provenance=record.provenance,
        )

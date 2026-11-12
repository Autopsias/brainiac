"""Acceptance-anchor stage: verify claimed bytes against the host-signed anchor.

Split out of ``pipeline_stages`` to keep that module under the size limit;
re-exported there so ``stages.acceptance_anchor_stage`` still resolves.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .. import _optional

if TYPE_CHECKING:
    from .pipeline_stages import ClaimRecord


def acceptance_anchor_stage(record: ClaimRecord) -> ClaimRecord:
    """Verify accepted bytes against the off-mount host-signed anchor."""
    if record.terminal:
        return record
    if not _optional.cos_available():
        return record  # ADR 0013: no COS, no attachment anchors; a drop is a drop.
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

"""Acceptance-anchor stage: verify claimed bytes against the host-signed anchor.

Split out of ``pipeline_stages`` to keep that module under the size limit;
re-exported there so ``stages.acceptance_anchor_stage`` still resolves.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .. import _optional

if TYPE_CHECKING:
    from .pipeline_stages import ClaimRecord


def _is_anchor_refusal_error(exc: Exception) -> bool:
    """True when ``exc`` indicates an anchor/signature mismatch rather than a transient error."""
    from .. import host_hooks as _host_hooks

    if isinstance(exc, _host_hooks.HookAnchorRefused):
        return True
    return any(cls.__name__ == "ApprovedRefused" for cls in type(exc).__mro__)


def _log_defect_via_hooks(
    hooks: tuple[object, ...], vault: object, kind: str, detail: str,
) -> None:
    """Best-effort defect logging via hooks; never aborts quarantine or ingest."""
    from .. import host_hooks as _host_hooks

    for provider in _host_hooks.providers_with(hooks, "log_ingest_defect"):
        try:
            _host_hooks.call_hook_method(
                provider, "log_ingest_defect", vault, kind, detail,
            )
        except Exception as exc:  # noqa: BLE001 — reporting failure must not interrupt ingest
            logging.getLogger(__name__).warning(
                "ingest: log_ingest_defect failed on %s: %s: %s",
                getattr(provider, "name", "hook"), type(exc).__name__, exc,
            )


def _quarantine_missing_anchor(
    record: ClaimRecord,
    hooks: tuple[object, ...],
    *,
    defect_detail: str,
    messages: list[str],
) -> ClaimRecord:
    """Quarantine a released attachment that has no valid acceptance anchor."""
    from . import pipeline as facade

    assert record.claimed is not None
    reason = "approved_anchor_missing"
    facade._quarantine(record.claimed, record.drain.quarantine_dir, reason, messages)
    _log_defect_via_hooks(
        hooks, record.drain.vault, "attachment-anchor-missing", defect_detail,
    )
    record.append("quarantined", {"file": record.path.name, "reason": reason})
    record.terminal = True
    return record


def _acceptance_anchor_via_hooks(
    record: ClaimRecord,
    hooks: tuple[object, ...],
    *,
    defect_hooks: tuple[object, ...] = (),
) -> ClaimRecord:
    """Verify accepted bytes via approved HostHookProvider instances."""
    from .. import host_hooks as _host_hooks
    from . import pipeline as facade

    assert record.claimed is not None
    assert record.original_bytes is not None
    anchor = None
    all_defect_hooks = defect_hooks or hooks
    for provider in hooks:
        try:
            anchor = _host_hooks.call_hook_method(
                provider,
                "ingest_verify_bytes",
                record.drain.vault,
                record.path,
                record.original_sha,
                record.original_bytes,
                record.drain.anchor_pubkey,
                default=None,
            )
            if anchor is not None:
                break
        except Exception as exc:  # noqa: BLE001 — split refusal vs transient error
            if not _is_anchor_refusal_error(exc):
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
            facade._quarantine(
                record.claimed,
                record.drain.quarantine_dir,
                "approved_anchor_mismatch",
                [str(exc)],
            )
            _log_defect_via_hooks(
                all_defect_hooks,
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
        return _quarantine_missing_anchor(
            record,
            all_defect_hooks,
            defect_detail=f"{record.path.name}: released attachment missing anchor — NOT ingested",
            messages=[
                "a release record names these exact bytes as an owner-accepted "
                "attachment, but no acceptance anchor covers them any more.",
            ],
        )
    record.anchor = anchor
    record.provenance = (anchor or {}).get("claim") or None
    return record


def acceptance_anchor_stage(record: ClaimRecord) -> ClaimRecord:
    """Verify accepted bytes against the off-mount host-signed anchor."""
    if record.terminal:
        return record
    from .. import host_hooks as _host_hooks

    all_hooks = _host_hooks.load_host_hooks(vault=record.drain.vault)
    hooks = _host_hooks.providers_with(all_hooks, "ingest_verify_bytes")
    if hooks:
        return _acceptance_anchor_via_hooks(record, hooks, defect_hooks=all_hooks)
    if not _optional.cos_available():
        if record.original_sha in record.drain.released_shas:
            return _quarantine_missing_anchor(
                record,
                all_hooks,
                defect_detail=(
                    f"{record.path.name}: released attachment has no registered "
                    "anchor verifier — NOT ingested"
                ),
                messages=[
                    "a release record names these exact bytes as an owner-accepted "
                    "attachment, and no anchor verifier is registered to verify them.",
                ],
            )
        return record  # ADR 0013: no COS, no attachment anchors; a drop is a drop.
    return _acceptance_anchor_via_cos(record)


def _acceptance_anchor_via_cos(record: ClaimRecord) -> ClaimRecord:
    """Legacy COS attachment-anchor verification path (removed in cos-06)."""
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


def record_attachment_receipt(record: ClaimRecord) -> None:
    """F46: receipt, off the mount, which note the host signed for owner-accepted bytes.

    Only a claim that crossed :func:`acceptance_anchor_stage` with a host-signed
    anchor is a COS attachment; an owner's own drop leaves no receipt. The owner
    undo reads this, never ``.brain/ingest-manifest.json`` (VM-writable), to name
    the note it retires. A failed write is logged there and never stops the ingest."""
    if record.anchor is None:
        return
    from .. import host_hooks as _host_hooks

    hooks = _host_hooks.providers_with(
        _host_hooks.load_host_hooks(vault=record.drain.vault), "record_ingest_receipt",
    )
    if hooks:
        for provider in hooks:
            try:
                _host_hooks.call_hook_method(
                    provider, "record_ingest_receipt",
                    record.drain.vault, record.original_sha, record.slug,
                )
            except Exception as exc:  # noqa: BLE001 — receipt logging never stops ingest
                logging.getLogger(__name__).warning(
                    "ingest: record_ingest_receipt failed on %s: %s: %s",
                    getattr(provider, "name", "hook"), type(exc).__name__, exc,
                )
        return
    if not _optional.cos_available():
        return  # ADR 0013: no COS, no attachment receipts.
    from .. import cos as COS

    COS.record_attachment_ingest(
        record.drain.vault, sha=record.original_sha, note_id=record.slug)

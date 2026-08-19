"""COS approval cleanup."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._approval import approved_anchor_path, approved_payload_path, approved_queue_root
from ._layout import _ts, _utcnow
from ._learning_ledger import log_defect

def approved_pending(vault) -> list[Path]:
    """Queued payloads awaiting the signature (host-only; never raises)."""
    try:
        d = approved_queue_root(vault)
    except ApprovedQueueUnsafe:
        return []
    return sorted(d.glob("*.md")) if d.is_dir() else []

def approved_refused(vault) -> list[Path]:
    try:
        d = approved_queue_root(vault)
    except ApprovedQueueUnsafe:
        return []
    # payloads only — one entry per refused ITEM, not one per quarantined file
    return sorted(d.glob("*.md.refused")) if d.is_dir() else []

def clear_approved(vault, nid: str) -> bool:
    """Drop a queued item — after a successful signature, or on an undo.

    Returns True only when NEITHER file is on disk afterwards. It used to
    swallow every OSError and return nothing, so an undo reported
    ``draft-deleted`` while the item was still queued and the next drain signed
    the thing the owner had just revoked. A caller acting on the return value
    is the point; a silent best-effort delete is not."""
    try:
        payload = approved_payload_path(vault, nid)
        anchor = approved_anchor_path(vault, nid)
    except (ApprovedQueueUnsafe, ValueError):
        return True                       # no reachable queue: nothing is staged
    for p in (payload, anchor):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    return not (payload.exists() or anchor.exists())

def refuse_approved(vault, path: Path, reason: str,
                    now: _dt.datetime | None = None) -> Path | None:
    """Quarantine a payload that failed verification, and say so LOUDLY.

    Moved OUT of the queue (never signed, never retried silently) and recorded
    as a defect — leaving it would re-refuse it on every drain, which reads as
    noise instead of the security event it is."""
    stamp = (now or _utcnow()).strftime("%Y%m%dT%H%M%S")
    dest = None
    try:
        nid = safe_slug(path.stem)
    except ValueError:
        nid = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:12]
    try:
        d = approved_queue_root(vault)
        dest = d / f"{stamp}-{nid}.md.refused"
        if path.is_symlink() or path.exists():
            os.replace(path, dest)
        anchor = approved_anchor_path(vault, nid)
        if anchor.exists():
            os.replace(anchor, d / f"{stamp}-{nid}.anchor.json.refused")
    except (ApprovedQueueUnsafe, OSError, ValueError):
        pass
    log_defect(vault, "approved-queue-refusal", f"{nid}: {reason}",
               ts=_ts(now))
    return dest

__all__ = ['approved_pending', 'approved_refused', 'clear_approved', 'refuse_approved']

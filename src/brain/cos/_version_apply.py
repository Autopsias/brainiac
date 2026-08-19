"""COS version-apply operation."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._layout import _ts
from ._learning_ledger import _record_verdict
from ._version_links import _record_version_link, _version_link_pending, _version_link_stale, version_link_digest

def _apply_version_link(core, meta: dict[str, Any], *, accepted: bool,
                        expected_sha: str | None, answer_mode: str,
                        batch_size: int, batch_id: str, now: _dt.datetime,
                        report: dict[str, Any]) -> None:
    """Apply ONE owner verdict on a version-link proposal. Idempotent."""
    vault = core.vault
    nid = meta["id"]
    pair = str(meta.get("pair_key") or nid)
    stamp = _ts(now)
    pending = _version_link_pending(vault) / f"{nid}.json"

    if not accepted:
        _record_verdict(vault, meta, outcome="rejected", answer_mode=answer_mode,
                        batch_size=batch_size, ts=stamp)
        _record_version_link(vault, pair, "rejected", ts=stamp, id=nid)
        pending.unlink(missing_ok=True)
        report["rejected"].append(nid)
        return

    # Proposal-level CAS: the proposal the owner approved must be the proposal
    # the batch digest covered.
    if expected_sha is not None and version_link_digest(meta) != expected_sha:
        report["invalid"].append(
            {"batch_id": batch_id, "id": nid,
             "reason": "version-link proposal drifted since the batch digest "
                       "— not applied"})
        return

    # The owner DID decide; that judgement is evidence whatever happens next.
    _record_verdict(vault, meta, outcome="accepted", answer_mode=answer_mode,
                    batch_size=batch_size, ts=stamp)

    # STALENESS: the nightly folds keep running while a proposal waits, so
    # re-verify BOTH sides against the vault as it is NOW. `core.supersede`'s
    # `expect` re-checks the same facts inside its own lock — that is the
    # authoritative gate; this check exists so a pair that moved on declines
    # cleanly and legibly instead of surfacing as an exception.
    # The two content hashes are the whole precondition — frontmatter lives
    # inside the file, so any chain mutation moves them. `old_superseded_by`
    # rides along only because "it was chained while you were deciding" is the
    # case worth naming out loud. `is_latest_version` is deliberately NOT
    # asserted: a live head legitimately carries `true` after an earlier
    # supersession, so pinning it would decline honest pairs.
    expect = {"old_sha256": meta["old_sha256"], "new_sha256": meta["new_sha256"],
              "old_superseded_by": ""}
    stale = _version_link_stale(core, meta)
    if stale:
        _record_version_link(vault, pair, "stale", ts=stamp, id=nid, reason=stale)
        pending.unlink(missing_ok=True)
        report.setdefault("supersedes_declined", []).append(
            {"id": nid, "reason": stale})
        return
    try:
        core.supersede(meta["old_id"], meta["new_id"], expect=expect,
                       reason=f"owner-accepted version-link proposal {nid} "
                              f"(COS deduced from email context)")
    except Exception as exc:  # noqa: BLE001 — one bad pair never aborts a batch
        detail = f"{type(exc).__name__}: {exc}"
        _record_version_link(vault, pair, "failed", ts=stamp, id=nid, reason=detail)
        pending.unlink(missing_ok=True)
        report.setdefault("supersedes_failed", []).append(
            {"id": nid, "reason": detail})
        return
    _record_version_link(vault, pair, "applied", ts=stamp, id=nid)
    pending.unlink(missing_ok=True)
    report["accepted"].append(nid)
    report.setdefault("supersedes_applied", []).append(
        {"id": nid, "old_id": meta["old_id"], "new_id": meta["new_id"]})

__all__ = ['_apply_version_link']

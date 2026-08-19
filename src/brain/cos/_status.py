"""COS status operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._approval import approved_queue_root
from ._approval_cleanup import approved_pending, approved_refused
from ._attachment_anchors import attachment_anchors_awaiting_drain
from ._attachment_store import attachment_metas
from ._batches import open_batches
from ._claims_state import _pending_metas, quarantined_claims
from ._corrections import list_corrections
from ._hold_store import hold_list
from ._layout import _parse_ts, _utcnow, drop_dir, host_dir, ops_dir, priority_map_path, proposal_drop_dir, shared_dir
from ._learning_config import _env_int
from ._learning_ledger import defects
from ._routing import route_stats
from ._taxonomy import ingest_taxonomy
from ._version_links import version_link_metas

def batch_liveness(vault, now: _dt.datetime | None = None) -> dict[str, Any]:
    now = now or _utcnow()
    open_ = open_batches(vault)
    ages = [(now - c).total_seconds() / 3600.0
            for c in (_parse_ts(str(b.get("created", ""))) for b in open_)
            if c is not None]
    oldest = max(ages) if ages else None
    queued = {c["id"] for b in open_ for c in b.get("candidates", [])}
    waiting = [m["id"] for m in
               _pending_metas(vault) + attachment_metas(vault, state="pending")
               if m["id"] not in queued]
    threshold = float(_env_int(BATCH_STALE_HOURS_ENV, DEFAULT_BATCH_STALE_HOURS))
    stats = route_stats(vault)
    out = {
        "open_batches": len(open_),
        "oldest_open_batch_hours": round(oldest, 1) if oldest is not None else None,
        "pending_behind_backpressure": len(waiting),
        "threshold_hours": threshold,
        "alert": bool(oldest is not None and oldest > threshold),
        # B8: the both-keys policy silently suspends the pattern auto-capture
        # lane that is live today, until the producer stamps category +
        # extraction_rules_version. This counter is what makes it not silent —
        # it rides `batch_liveness` so `brain status` AND the morning brief
        # both see it with no extra plumbing.
        "unstamped_batched": int(stats.get("unstamped_batched", 0)),
        "unstamped_last": stats.get("last_unstamped"),
        "pattern_autocapture": PATTERN_AUTOCAPTURE_STATUS,
        # STA-01: candidates the host could not attribute to a VALID run. Same
        # loudness as `unstamped_batched` — a silent quarantine would be the
        # same instrument-lies failure in a new place.
        "quarantined_claims": len(quarantined_claims(vault)),
        "unjoined_claims_total": int(stats.get("unjoined_claims", 0)),
        "quarantined_claims_total": int(stats.get("quarantined_claims", 0)),
        "quarantine_last": stats.get("last_quarantine"),
    }
    # INS-01: the host run validator's own finding, on the SAME carrier as
    # `unstamped_batched` — `batch_liveness` is what both `brain status` and the
    # morning brief read, so a run scored INVALID/INCONCLUSIVE cannot be a
    # silent log entry.
    try:
        from .. import cos_runverify
        out.update(cos_runverify.alert(vault))
    except Exception as exc:  # noqa: BLE001 — liveness must never crash status
        out["run_validity_error"] = f"{type(exc).__name__}: {exc}"
    if out["quarantined_claims"]:
        by_code: dict[str, int] = {}
        for q in quarantined_claims(vault):
            code = str(q.get("code") or "unknown")
            by_code[code] = by_code.get(code, 0) + 1
        out["quarantine_reasons"] = by_code
        out["quarantine_text"] = (
            f"{out['quarantined_claims']} COS candidate(s) held in claim "
            "quarantine — the host cannot attribute them to a VALID run "
            f"({', '.join(f'{k}×{v}' for k, v in sorted(by_code.items()))}); "
            "they are released automatically once the run validator scores "
            "their run VALID, and never bound while it does not")
    if out["alert"]:
        out["alert_text"] = (
            f"COS ingestion batch unanswered for {out['oldest_open_batch_hours']}h "
            f"(threshold {int(threshold)}h) — {out['pending_behind_backpressure']} "
            f"candidate(s) held behind it; answer it via /brain-inbox")
    return out

def status_block(vault, role: str) -> dict[str, Any]:
    """Cheap counts for ``brain status --json``. The VM view only reads the
    zones it may touch (drop/ + shared/); host/ counts are host-only."""
    out: dict[str, Any] = {
        "ops_dir": str(ops_dir(vault)),
        "zones": {"host_private": str(host_dir(vault)),
                  "vm_readable": str(shared_dir(vault)),
                  "vm_writable": str(drop_dir(vault))},
    }
    try:
        pdir = proposal_drop_dir(vault)
        out["proposal_drops"] = len(list(pdir.glob("*.md"))) if pdir.is_dir() else 0
        out["priority_map_present"] = priority_map_path(vault).exists()
        if role == "host":
            out["pending_proposals"] = len(_pending_metas(vault))
            out["open_batches"] = len(open_batches(vault))
            out["attachments_awaiting_verdict"] = len(
                attachment_metas(vault, state="pending"))
            out["version_links_awaiting_verdict"] = len(version_link_metas(vault))
            out["batch_liveness"] = batch_liveness(vault)
            out["ingest_taxonomy"] = ingest_taxonomy(vault)["mode"]
            out["taxonomy_defects"] = len(defects(vault))
            # B8: `ingest_taxonomy: active` alone was misleading — the taxonomy
            # parsing fine says nothing about whether ANY candidate can reach
            # the auto lane. State the lane's actual status and its cost.
            out["pattern_autocapture"] = PATTERN_AUTOCAPTURE_STATUS
            out["route_stats"] = route_stats(vault)
            holds = hold_list(vault)
            out["holds"] = len(holds)
            # ING-04 daily digest: id + not_before only (never content) so a
            # pending auto-capture is never silent — revert with
            # `brain cos-hold cancel <id>` before it releases.
            out["holds_pending"] = [
                {"id": h.get("id"), "not_before": h.get("not_before")}
                for h in holds]
            out["corrections"] = len(list_corrections(vault))
            # INT-01: the accept -> signature waiting room, and anything the
            # signing gate REFUSED there (a refusal is a security event, so it
            # is visible here and not only in defects.jsonl).
            refused = approved_refused(vault)
            out["approved_awaiting_signature"] = len(approved_pending(vault))
            # INT-04: the attachment lane's equivalent — an armed acceptance
            # anchor is the ONLY thing that keeps its inbox file at the
            # email-derived MNPI floor, and it is not rebuildable from vault/.
            out["attachment_anchors_awaiting_drain"] = \
                attachment_anchors_awaiting_drain(vault)
            # CAP-02: the capture corpus. Unfiltered MNPI mail bodies, and the
            # one thing under the index dir nothing else reports — so an
            # operator repointing $BRAIN_INDEX_DIR or uninstalling has to be
            # able to see how much is on disk, and whether the nightly
            # retention fold has actually run HERE.
            # corpus_summary never raises; it reports its own error inline.
            from .. import cos_corpus as _corpus
            out["capture_corpus"] = _corpus.corpus_summary(vault)
            out["approved_refused"] = len(refused)
            if refused:
                out["approved_refused_files"] = [p.name for p in refused]
            try:
                # NOT one of the ops-dir `zones`: this one is deliberately
                # outside the vault (and so off the VM mount) entirely.
                out["approved_queue"] = str(approved_queue_root(vault))
            except ApprovedQueueUnsafe as exc:
                out["approved_queue_error"] = str(exc)
            try:
                from .. import spine as spine_mod
                rep = spine_mod.radar(vault)
                out["spine"] = {"late": len(rep["late"]), "at_risk": len(rep["at_risk"]),
                                "open": len(spine_mod.list_all(vault, status="open"))}
            except Exception:  # noqa: BLE001 — spine status is best-effort
                out["spine"] = {"error": "unavailable"}
    except Exception as exc:  # noqa: BLE001 — status must never crash on cos state
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out

__all__ = ['batch_liveness', 'status_block']

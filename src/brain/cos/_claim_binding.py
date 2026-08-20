"""COS claim-binding operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._claims_state import _quarantined, _sweep_pending_without_valid_run, claim_quarantine_dir, quarantined_claims
from ._criteria import evidence_lineage_key, evidence_unit_key
from ._ingress import _validate_correction_payload
from ._io import _append_jsonl, _read_jsonl, _write_atomic
from ._layout import _env_days, _ts, _utcnow, proposal_drop_dir, proposals_dir, verdict_drop_dir
from ._learning_ledger import _claims_path, demote_category, log_defect, record_outcome
from ._ledger_join import join_ledger_category, ledger_index
from ._routing import _bump_route_stats
from ._run_migration import run_manifest
from ._runs import run_validity
from ._taxonomy import ingest_taxonomy, resolve_category
from ._claim_evaluation import _bind_claim
from ._claim_sweep import _claim_correction_drops, _claim_text_drops

def claim_drops(vault, now: _dt.datetime | None = None) -> dict[str, Any]:
    """Validate + claim every proposal drop into ``host/proposals/pending/``.

    HOST side of the trust boundary. Each drop is: schema-validated
    (``capture.validate``), classification-checked, secret-scrubbed, and
    replay-checked against the content-hash claims ledger. A drop that fails
    any check is moved to ``rejected/`` (never signed, never silently lost);
    a replayed drop (hash already claimed) is deleted and logged.

    Runs under the writer lock (R2, 2026-07-30 review, HIGH). A claim can fire
    `demote_category` on a security defect, and `hold_release_due` — which DOES
    hold the lock — re-checks eligibility from those same statistics before it
    moves a held item. Unlocked, a demotion could land between that check and
    the move, and the held item of a just-demoted category would still be
    released. The two must be mutually exclusive."""
    from .. import capture as cap_mod

    with vault_writer_lock(vault, verb="cos-claim"):
        return _claim_drops_locked(vault, cap_mod, now or _utcnow())

def _claim_drops_locked(vault, cap_mod, now: _dt.datetime) -> dict[str, Any]:
    quarantined: list[dict[str, Any]] = []
    ledger = _read_jsonl(_claims_path(vault))
    seen_hashes = {e.get("sha256") for e in ledger}
    pending = proposals_dir(vault) / "pending"
    rej_dir = proposals_dir(vault) / "rejected"
    pending.mkdir(parents=True, exist_ok=True)
    rej_dir.mkdir(parents=True, exist_ok=True)

    ttl_days = _env_days(PROPOSAL_TTL_DAYS_ENV, DEFAULT_PROPOSAL_TTL_DAYS)
    # Read the owner's ingest taxonomy ONCE per claim pass (and log the
    # fail-closed defect at most once), never per candidate. Same for the
    # ledger index: one scan of every run ledger, not one per candidate.
    taxonomy = ingest_taxonomy(vault, log=True)
    ledger_idx = ledger_index(vault)

    # RUN VALIDITY GATES CLAIMING, in both directions and on every pass:
    #   (a) candidates bound BEFORE this gate existed, or whose run has since
    #       been scored INVALID/INCONCLUSIVE, leave `pending/` for quarantine;
    #   (b) anything a verdict has since cleared is released back into
    #       `pending/` — so a queue built while s03's validator did not yet
    #       exist drains by itself the hour after it lands.
    # Release BEFORE sweep, deliberately: a candidate swept out of `pending/`
    # in this pass waits for the next one rather than being re-examined
    # milliseconds later by the same fold. One decision per pass is easier to
    # read in the defect log than a sweep and a re-bind at the same timestamp.
    released = _release_quarantined_claims(
        vault, cap_mod, now, taxonomy=taxonomy, ledger_idx=ledger_idx,
        ttl_days=ttl_days)
    swept = _sweep_pending_without_valid_run(vault, now)
    quarantined.extend(swept)

    outcome = _claim_text_drops(
        vault, cap_mod, now, taxonomy=taxonomy, ledger_idx=ledger_idx, ttl_days=ttl_days,
        seen_hashes=seen_hashes, rejected_dir=rej_dir)
    corrections_claimed, correction_rejected = _claim_correction_drops(vault, now, rej_dir)
    quarantined.extend(outcome["quarantined"])
    rejected = outcome["rejected"] + correction_rejected
    unjoined = outcome["unjoined"]

    if quarantined or unjoined:
        _bump_route_stats(vault, now=now, unjoined_claims=unjoined,
                          quarantined_claims=len(quarantined))
    return {"claimed": outcome["claimed"], "rejected": rejected, "replayed": outcome["replayed"],
            "corrections_claimed": corrections_claimed,
            # Loud, never silent: an unjoinable or unvalidated candidate is
            # reported per pass here, counted cumulatively in `route_stats`,
            # and surfaced in `brain status` + the morning brief exactly like
            # `unstamped_batched`.
            "quarantined": quarantined,
            "unjoined_claims": unjoined,
            "released_from_quarantine": released,
            "quarantine_open": len(quarantined_claims(vault))}

def _release_quarantined_claims(vault, cap_mod, now: _dt.datetime, *,
                                taxonomy: dict[str, Any],
                                ledger_idx: dict[str, list[dict[str, str]]],
                                ttl_days: int) -> list[str]:
    """Re-run the gate over every quarantined candidate; bind the ones that now
    pass. This is the other half of gating on run validity: a candidate parked
    because no verdict existed yet is NOT stranded — s03's arrival releases it
    on the next hourly pass, with no operator ritual and no re-drop."""
    qdir = claim_quarantine_dir(vault)
    rej_dir = proposals_dir(vault) / "rejected"
    released: list[str] = []
    for m in quarantined_claims(vault):
        nid = str(m.get("id"))
        try:
            text = (qdir / f"{nid}.md").read_text(encoding="utf-8")
        except OSError:
            continue
        sha = sha256_text(text)
        out = _bind_claim(vault, cap_mod, text=text, sha=sha,
                          source=f"{nid}.md", now=now, taxonomy=taxonomy,
                          ledger_idx=ledger_idx, ttl_days=ttl_days)
        if out["state"] == "quarantined":
            continue                        # still unproven — reason refreshed
        for suffix in (".md", ".json"):
            (qdir / f"{nid}{suffix}").unlink(missing_ok=True)
        if out["state"] == "claimed":
            released.append(nid)
            _append_jsonl(_claims_path(vault),
                          {"sha256": sha, "id": nid, "ts": _ts(now),
                           "disposition": "claimed (released from quarantine)"}, vault=vault)
        else:
            rej_dir.mkdir(parents=True, exist_ok=True)
            public("_write_atomic")(rej_dir / f"{now.strftime('%Y%m%dT%H%M%S')}-{nid}.md",
                          text.encode("utf-8"))
            log_defect(vault, "claim-quarantine-rejected",
                       f"{nid}: {out['reason']}", ts=_ts(now))
    return released

__all__ = ['claim_drops', '_claim_drops_locked', '_bind_claim', '_release_quarantined_claims']

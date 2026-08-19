"""COS learning-ledger operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._io import _append_jsonl, _read_jsonl
from ._layout import _ts, proposals_dir

def _claims_path(vault) -> Path:
    return proposals_dir(vault) / "claims.jsonl"

def _record_verdict(vault, bound: dict[str, Any], *, outcome: str,
                    answer_mode: str, batch_size: int, ts: str) -> None:
    record_outcome(
        vault, pattern=bound.get("pattern"), ident=bound["id"], outcome=outcome,
        bundle_version=bound.get("bundle_version"), ts=ts,
        category=bound.get("category"), lane=bound.get("lane"),
        tier=bound.get("tier"), rules_version=bound.get("rules_version"),
        kind=bound.get("kind"), answer_mode=answer_mode, batch_size=batch_size,
        evidence_unit=bound.get("evidence_unit"),
        evidence_lineage=bound.get("evidence_lineage"))

def _outcomes_path(vault=None) -> Path:
    return proposals_dir(vault) / "outcomes.jsonl"

def record_outcome(vault, *, pattern: str, ident: str, outcome: str,
                   bundle_version: str, ts: str | None = None,
                   category: str | None = None, lane: str | None = None,
                   tier: str | None = None, rules_version: str | None = None,
                   kind: str | None = None, answer_mode: str | None = None,
                   batch_size: int | None = None,
                   evidence_unit: str | None = None,
                   evidence_lineage: str | None = None) -> dict[str, Any]:
    """Append ONE owner-decision or claim-time-defect record. Never mutated,
    never deleted (the acceptance evidence this gate reads is itself
    audit-shaped, even though it lives outside the signed note chain).

    IDEMPOTENT PER (proposal id, outcome) — the expected double-count failure
    mode is one candidate appearing in TWO batches after a TTL requeue; a
    second ``accepted`` for the same id is a re-record of one owner decision,
    not a second one, so it is dropped rather than appended.

    LRN-01 adds the CATEGORY dimension: ``category``/``lane``/``tier``/
    ``rules_version`` are the graduation evidence key (HOST-bound at claim
    time, never read back off the VM-authored candidate), and ``kind``/
    ``answer_mode``/``batch_size`` are what the bulk-accept guard and the
    reports need. ``evidence_unit`` (HARDENED:codex-4) is the stable identity
    of the underlying material: the FIRST record for a unit counts, every
    later one is stored with ``counted: false`` so repeated forwards, thread
    re-extractions and whitespace variants cannot inflate the Wilson sample.
    ``evidence_lineage`` (B5) dedups the OTHER way round — one HOST-VERIFIED
    conversation is one unit even when its messages differ. Either match is
    enough to stop a second verdict counting; neither can be produced by a
    VM claim, so nothing a producer writes can raise the sample.
    """
    existing = _read_jsonl(_outcomes_path(vault))
    for e in existing:
        if e.get("id") == ident and e.get("outcome") == outcome:
            return e
    counted = True
    if evidence_unit or evidence_lineage:
        for e in existing:
            if (e.get("counted") is False
                    or e.get("outcome") not in ("accepted", "rejected")
                    or outcome not in ("accepted", "rejected")):
                continue
            if ((evidence_unit and e.get("evidence_unit") == evidence_unit)
                    or (evidence_lineage
                        and e.get("evidence_lineage") == evidence_lineage)):
                counted = False
                break
    rec: dict[str, Any] = {
        "pattern": pattern or CATEGORY_UNCLASSIFIED, "id": ident, "outcome": outcome,
        "bundle_version": bundle_version or "unknown", "ts": ts or _ts(),
        "category": category or CATEGORY_UNCLASSIFIED,
        "lane": lane or LANE_TEXT,
        "tier": tier or "unknown",
        "rules_version": rules_version or "unknown",
        "counted": counted,
    }
    for k, v in (("kind", kind), ("answer_mode", answer_mode),
                 ("batch_size", batch_size), ("evidence_unit", evidence_unit),
                 ("evidence_lineage", evidence_lineage)):
        if v is not None:
            rec[k] = v
    _append_jsonl(_outcomes_path(vault), rec, vault=vault)
    return rec

def _defects_path(vault=None) -> Path:
    return proposals_dir(vault) / "defects.jsonl"

def log_defect(vault, kind: str, detail: str, ts: str | None = None) -> dict[str, Any]:
    """Append ONE ingestion-taxonomy defect. Doctrine alone is not a gate: an
    unparseable taxonomy or a refused `never` candidate has to leave a
    machine-readable trace, not just a comment in a spec."""
    rec = {"kind": kind, "detail": scrub(str(detail)), "ts": ts or _ts()}
    _append_jsonl(_defects_path(vault), rec, vault=vault)
    return rec

def defects(vault) -> list[dict[str, Any]]:
    return _read_jsonl(_defects_path(vault))

def _demotions_path(vault=None) -> Path:
    return proposals_dir(vault) / "demotions.jsonl"

def demote_category(vault, category: str | None, *, reason: str,
                    ts: str | None = None) -> dict[str, Any]:
    """Un-graduate ``category`` NOW and reset its evidence.

    Append-only: the reset is a DEMOTION MARKER, not a deletion of history —
    ``category_stats`` ignores every outcome at or before the newest marker.
    Fired by a claim-time security defect and by EVERY undo path."""
    rec = {"category": str(category or CATEGORY_UNCLASSIFIED),
           "reason": reason, "ts": ts or _ts()}
    _append_jsonl(_demotions_path(vault), rec, vault=vault)
    return rec

def _last_demotion(vault, category: str) -> str | None:
    stamps = [str(e.get("ts", "")) for e in _read_jsonl(_demotions_path(vault))
              if e.get("category") == category and e.get("ts")]
    return max(stamps) if stamps else None

__all__ = ['_claims_path', '_record_verdict', '_outcomes_path', 'record_outcome', '_defects_path', 'log_defect', 'defects', '_demotions_path', 'demote_category', '_last_demotion']

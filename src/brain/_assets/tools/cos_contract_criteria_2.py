"""Compute deterministic COS contract outcomes."""

from __future__ import annotations

from pathlib import Path

from cos_contract import _sha
from cos_contract_criteria import (
    _bucket_counts,
    _candidate_inputs,
    _is_degenerate,
    _liveness_checks,
    _provenance_checks,
    _sent_zero_send,
    lane_pin,
    validate,
)


def _result_payload(
    pre: dict,
    post: dict,
    run_id: str,
    profile: str,
    pin: str | None,
    counts: dict,
    provenance: dict,
    liveness: dict[str, dict],
    unattributed_total: int,
    zero_send_proof: dict | None,
    reasons: list[str],
) -> dict:
    """Assemble the stable public outcome-contract result."""
    before = provenance["conversation_before"]
    after = provenance["conversation_after"]
    folder_before = provenance["folder_items_before"]
    folder_after = provenance["folder_items_after"]
    legacy = provenance["legacy_counts"]
    elected = pre["browser_election"]["elected"] if not legacy else None
    return {
        "run_profile": profile, "run_id": str(run_id),
        "enumerated_at": pre["enumerated_at"],
        "enumeration_complete": provenance["enumeration_complete"],
        "enumerated": pre["enumerated"], "pre_run_holds": pre["pre_run_holds"],
        "post_run": post["post_run"], "counts": counts,
        "arrived_during_run": post["arrived_during_run"],
        "inbox_conversation_count_before": before,
        "inbox_conversation_count_after": after,
        "inbox_conversation_delta": after - before,
        "owa_folder_item_count_before": folder_before,
        "owa_folder_item_count_after": folder_after,
        "owa_folder_item_delta": folder_after - folder_before,
        "inbox_count_before": before, "inbox_count_after": after,
        "inbox_delta": after - before,
        "split": {"archive": counts["archived"], "hold": counts["held_non_drafted"],
                  "drafted": counts["held_drafted"]},
        "unaccounted_convids": provenance["unaccounted_convids"],
        "stopped_by_guard_convids": provenance["stopped_convids"],
        "guard_stop": provenance["guard_stop"],
        "lane": {"elected": elected, "pin": pin,
                 "pin_honoured": (None if pin is None or legacy else elected == pin)},
        "unknown_convids": provenance["stray"],
        "unattributed_ledger_rows": unattributed_total,
        "capability_liveness": liveness, "zero_send_proof": zero_send_proof,
        "verdict": "FAILED" if reasons else "PASS",
        "verdict_reasons": reasons,
        "verdict_source": f"tools/cos_contract.py@{_sha()}",
    }


def evaluate(
    pre: dict,
    post: dict,
    ledgers: Path,
    run_id: str,
    profile: str,
) -> dict:
    """Compute the deterministic outcome-contract verdict."""
    pin = lane_pin(ledgers)
    validate(pre, post, profile, run_id, pin)
    enumerated = list(pre["enumerated"])
    counts = _bucket_counts(enumerated, post["post_run"])
    provenance = _provenance_checks(pre, post, enumerated, counts, ledgers, run_id,
                                    profile, pin)
    reasons = list(provenance["reasons"])
    zero_send_proof = None
    if not provenance["legacy_counts"] and profile == "full":
        zero_send_proof, zero_send_reasons = _sent_zero_send(pre, post)
        reasons = zero_send_reasons + reasons
    eligible_seen, raw_seen, archive_candidates, candidate_reasons = _candidate_inputs(post)
    reasons.extend(candidate_reasons)
    if _is_degenerate(profile, counts, pre["pre_run_holds"], enumerated,
                      post["post_run"], post["arrived_during_run"], archive_candidates):
        reasons.append("OC-degenerate")
    liveness, unattributed_total, liveness_reasons = _liveness_checks(
        post, ledgers, run_id, profile, eligible_seen, raw_seen
    )
    reasons.extend(liveness_reasons)
    return _result_payload(pre, post, run_id, profile, pin, counts, provenance,
                           liveness, unattributed_total, zero_send_proof, reasons)


__all__ = ["evaluate"]

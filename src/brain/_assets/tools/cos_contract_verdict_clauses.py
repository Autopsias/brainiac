"""Verdict-clause sub-steps of the outcome contract's ``evaluate``.

One function per clause family the verdict is computed from — the
distrust-its-own-inputs provenance clauses, clause (a)'s accounting with the
guard-stop corroboration, the candidate scan feeding the anti-degenerate
guard, the degenerate guard itself, and capability liveness. ``evaluate``
stays in :mod:`cos_contract` with an unchanged signature (callers, doctrine
text and tests name it there) and dispatches to these; every parent value or
callable the clauses need (``ACCOUNTED``, ``run_scoped_rows``, the guard-stop
helpers) arrives as a parameter, so this module never imports the parent and
a monkeypatched parent attribute keeps working.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable


def provenance_reasons(
        pre: dict, post: dict, counts: dict, buckets: tuple[str, ...],
        enumerated: list[str], enumerated_set: set[str], arrived: list[str],
        conversation_before: int, conversation_after: int,
        folder_items_after: int, legacy_counts: bool,
        complete_enumeration: Callable[[dict, int], bool],
        sent_zero_send: Callable[[dict, dict], tuple[dict, list[str]]],
        profile: str) -> tuple[bool, list[str], dict | None]:
    """The provenance clauses plus the full-profile zero-send proof.

    Returns ``(enumeration_complete, reasons, zero_send_proof, stray)`` with
    the reasons in the exact order the single function appended them.
    """
    reasons: list[str] = []
    zero_send_proof = None
    if not legacy_counts and profile == "full":
        zero_send_proof, zero_send_reasons = sent_zero_send(pre, post)
        reasons.extend(zero_send_reasons)

    # --- provenance: the checker distrusts its own inputs -------------------
    enumeration_complete = (
        complete_enumeration(pre, conversation_before)
        and complete_enumeration(post, conversation_after)
    )
    if not legacy_counts and not enumeration_complete:
        reasons.append("OC-provenance-incomplete-enumeration")
    if not legacy_counts and conversation_before != len(enumerated):
        reasons.append("OC-provenance-pre-enumeration-count")
    bucket_sum = sum(counts[b] for b in buckets)
    if bucket_sum != len(enumerated):
        reasons.append("OC-provenance-bucket-sum")
    # A guard-stopped row was never archived, so it is still in the Inbox and
    # counts toward residency exactly as an unaccounted one did.
    resident = (counts["held_non_drafted"] + counts["held_drafted"]
                + counts["chipped"] + counts["unaccounted"]
                + counts["stopped_by_guard"] + len(arrived))
    if resident != conversation_after:
        reasons.append("OC-provenance-residency")
    if legacy_counts and folder_items_after != conversation_after:
        reasons.append("OC-provenance-folder-count")
    stray = sorted(set(post["post_run"]) - enumerated_set - set(arrived))
    if stray:
        reasons.append("OC-provenance-unknown-convid")
    return enumeration_complete, reasons, zero_send_proof, stray


def accounting_reasons(
        profile: str, accounted: frozenset[str], post: dict, pre: dict,
        enumerated: list[str], enumerated_set: set[str], counts: dict,
        legacy_counts: bool, pin: str | None, ledgers: Path, run_id: str,
        guard_stop_shape: Callable[[dict, set[str]], dict | None],
        guard_stop_corroborated: Callable[[Path, str, str], bool],
) -> tuple[list[str], list[str], dict | None, list[str]]:
    """Clause (a) with the guard-stop corroboration, the lane-pin clause and
    the label-only scope clause. Returns ``(unaccounted_convids,
    stopped_convids, guard_stop, reasons)``."""
    reasons: list[str] = []
    # --- clause (a): accounted, per the run's declared profile --------------
    #
    # A STOP HALTS ACTION, NEVER ACCOUNTING (v5.48 for the ingestion ledger,
    # v5.52 here). A run whose safety guard correctly ended every mutation still
    # owes a terminal bucket for every row it enumerated: `stopped_by_guard`
    # says "no disposition was written because writing one was forbidden", and
    # it is ACCOUNTED. It is not a free pass — the stop must be RECORDED, and
    # the record must be corroborated by the run's own ledgers. A row
    # unaccounted for any OTHER reason still FAILS exactly as before.
    unaccounted_convids = sorted(
        c for c in enumerated if post["post_run"].get(c) not in accounted)
    if unaccounted_convids:
        reasons.append("OC-a-unaccounted")

    stopped_convids = sorted(
        c for c in enumerated if post["post_run"].get(c) == "stopped_by_guard")
    guard_stop = guard_stop_shape(post, enumerated_set)
    if stopped_convids and guard_stop is None:
        reasons.append("OC-guard-stop-unrecorded")
    elif stopped_convids and not guard_stop_corroborated(
            ledgers, run_id, guard_stop["guard"]):
        reasons.append("OC-guard-stop-uncorroborated")

    # The lane the owner pinned is the lane the run owes. A fallback is a named
    # failure with the elected lane on the record, never a silent lane change.
    if pin is not None and not legacy_counts:
        if pre["browser_election"]["elected"] != pin:
            reasons.append("OC-lane-pin-not-honoured")

    # `label-only` forbids archiving: an archived row is a scope violation.
    if profile == "label-only" and counts["archived"]:
        reasons.append("OC-scope-violation-archived-under-label-only")
    return unaccounted_convids, stopped_convids, guard_stop, reasons


def candidate_scan(post: dict, capabilities: tuple[str, ...],
                   reasons: list[str]) -> tuple[dict[str, int], dict[str, int],
                                                set[str]]:
    """Tally the run's candidate records (eligible/raw per capability, archive
    candidates), appending the no-exclusion-reason clause in place."""
    eligible_seen = {cap: 0 for cap in capabilities}
    raw_seen = {cap: 0 for cap in capabilities}
    archive_candidates: set[str] = set()
    for rec in post["candidates"]:
        cap = rec["capability"]
        raw_seen[cap] += 1
        if cap == "archives":
            archive_candidates.add(rec["convid"])
        if rec["eligible"]:
            eligible_seen[cap] += 1
        elif not rec.get("exclusion_reason"):
            if "OC-candidate-no-exclusion-reason" not in reasons:
                reasons.append("OC-candidate-no-exclusion-reason")
    return eligible_seen, raw_seen, archive_candidates


def degenerate_reasons(profile: str, counts: dict, pre_holds: dict,
                       enumerated: list[str], post_run: dict,
                       arrived: list[str],
                       archive_candidates: set[str]) -> list[str]:
    # A newly classified hold is legitimate when its archive decision was
    # reported and rejected by a safety guard. Missing that per-conversation
    # evidence is the degenerate "label everything Held" shape.
    reasons: list[str] = []
    held_total = counts["held_non_drafted"] + counts["held_drafted"]
    newly_held = {
        convid for convid in enumerated
        if convid not in pre_holds
        and post_run.get(convid) in {"held_non_drafted", "held_drafted"}
    }
    if (profile == "full" and held_total > len(pre_holds)
            and counts["archived"] == 0 and not arrived
            and not newly_held.issubset(archive_candidates)):
        reasons.append("OC-degenerate")
    return reasons


def capability_liveness(
        post: dict, profile: str, ledgers: Path, run_id: str,
        eligible_seen: dict[str, int], raw_seen: dict[str, int],
        reasons: list[str], capabilities: tuple[str, ...],
        in_scope: dict[str, dict[str, bool]],
        ledger_glob: dict[str, str],
        counters: dict[str, Callable[[list[dict]], int]],
        run_scoped_rows: Callable[[Path, str, str],
                                  tuple[list[dict], int]],
) -> tuple[dict[str, dict], int]:
    """The per-capability liveness recount, appending its clauses in place.

    Returns ``(liveness, unattributed_total)``."""
    liveness: dict[str, dict] = {}
    unattributed_total = 0
    for cap in capabilities:
        declared = post["capabilities"].get(cap)
        scope = in_scope[profile][cap]          # computed, never read
        if not isinstance(declared, dict):
            reasons.append(f"OC-liveness-missing:{cap}")
        elif declared.get("in_scope") != scope:
            reasons.append(f"OC-liveness-in-scope:{cap}")
        rows, unattributed = run_scoped_rows(ledgers, ledger_glob[cap], run_id)
        unattributed_total += unattributed
        output = counters[cap](rows)
        liveness[cap] = {
            "in_scope": scope,
            "output": output,
            "eligible_inputs": eligible_seen[cap],
            "raw_inputs": raw_seen[cap],
        }
        if scope and output == 0 and eligible_seen[cap] > 0:
            reasons.append(f"OC-liveness:{cap}")
    return liveness, unattributed_total

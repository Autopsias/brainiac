#!/usr/bin/env python3
"""Render the run-level OUTCOME CONTRACT verdict — the run supplies data, this
script supplies the verdict.

WHY THIS EXISTS (measured). Six consecutive COS runs scored 27/27 on the
E-checks while archiving nothing for seven days. The E-checks verify the
PARTS; nothing verified the OUTCOME. Agents assert completion while the
environment disagrees in 45-78% of failing trajectories, and an LLM judge
cannot detect it (no configuration above AUROC 0.65) — while a plain
count-comparison outperforms reflective self-checks 4-8x. So the verdict is
computed by deterministic code from three inputs, never composed by the run.

The three inputs are all authored by the run being judged, so this checker
DISTRUSTS them: a bucket sum that does not equal the enumerated set, a PRE
conversation count that does not equal that set, a residency mismatch against
the post-run Inbox conversation count, or a convid in the re-enumeration that
was never enumerated and never arrived, each render FAILED rather than a clean
PASS with a `verdict_source` sha on it. OWA's transcribed folder badge is
recorded separately because it counts message items, not conversations.

    python3 tools/cos_contract.py --pre pre.json --post post.json \
        --ledgers <vault>/cos-ops --run-id 41 --profile full --out block.json

`--run-id` is REQUIRED: it scopes the ledger scan to THIS run's rows, or
yesterday's ledgers in the same directory silently satisfy today's liveness
guard. Exit 0 = PASS, 1 = FAILED, 2 = malformed input.

A run whose safety guard correctly STOPPED still owes a bucket for every row it
enumerated: `stopped_by_guard` is a terminal, ACCOUNTED disposition (v5.52), and
it is refused unless the stop is both declared and corroborated by the run's own
ledgers. The owner's elected-lane pin (`overlay/cos/browser-lane.md`) is read
from the vault, never from the run, so a silent fallback to the other lane is a
named clause instead of an unremarked lane change.

ponytail: the output counts come from the ledgers and the eligible-input
counts from the run's own candidate records — the run may not hand this script
either total. Unknown ledger row shapes are ignored, never guessed, so the
output side under-counts before it over-counts.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_contract_ledger_scan import (  # noqa: E402,F401  batch-2 drain
    GUARD_STOPS, GUARD_STOP_GLOBS, TOOLSETS, _file_run, _guard_stop_shape,
    _run_token, counts_chip_clear, guard_stop_corroborated, lane_pin,
    run_scoped_rows)
from cos_contract_provenance import (  # noqa: E402,F401  batch-2 drain
    Malformed, _complete_enumeration, _counts, _load, _require,
    _sent_zero_send, _timestamp, _uses_new_count_schema,
    _validate_browser_election, _validate_browser_provenance,
    _validate_scan_provenance, _validate_sent_snapshot)
from cos_contract_snapshot_shape import candidate_shape_problem  # noqa: E402
from cos_contract_verdict_clauses import (  # noqa: E402
    accounting_reasons, candidate_scan, capability_liveness, degenerate_reasons,
    provenance_reasons)
from cos_reconcile_metrics import _rows, counts_archive, counts_draft  # noqa: E402

BUCKETS = ("archived", "held_non_drafted", "held_drafted", "chipped",
           "unaccounted", "stopped_by_guard")
PROFILES = ("full", "label-only")

#: WHICH buckets count as ACCOUNTED is profile-dependent, and that is
#: load-bearing: `full` refuses a bare P-chip (v5.26 wants a Held label on
#: anything not archived), `label-only` accepts it and forbids archiving.
#: `stopped_by_guard` is accounted under BOTH — a safety stop halts ACTION,
#: never ACCOUNTING (v5.48/v5.52), and the disposition a stopped run owes is a
#: terminal bucket that says so, not an absence.
ACCOUNTED = {
    "full": frozenset({"archived", "held_non_drafted", "held_drafted",
                       "stopped_by_guard"}),
    "label-only": frozenset({"chipped", "held_non_drafted", "held_drafted",
                             "stopped_by_guard"}),
}

CAPABILITIES = ("archives", "drafts", "chip_clears")

#: A guard may only be evaluated for a capability IN SCOPE for the profile —
#: under `label-only` archiving and drafting are forbidden BY DEFINITION, so
#: evaluating them would fail every correct midday run by construction.
IN_SCOPE = {
    "full": {"archives": True, "drafts": True, "chip_clears": True},
    "label-only": {"archives": False, "drafts": False, "chip_clears": True},
}

#: The OUTPUT side of capability liveness, per capability.
#:
#: ALL THREE ARE PRE-v7 MODEL-WRITTEN LEDGERS WITH NO v7 PRODUCER, AND THEY ARE
#: DELIBERATELY *NOT* REPOINTED AT `_cos_undo_ledger_*` (s10, 2026-08-16).
#: The reason is the ORDER of the night, not sentiment. The outcome contract is
#: a READ-lane instrument: `cos_driver` renders PRE/POST and runs this checker
#: hours before `cos_mutate apply` exists, so the undo ledger is not on disk
#: when the block is written. `cos_runverify.check_contract` then RE-EXECUTES
#: this checker after the apply and FAILS the run when the recomputation
#: disagrees with the recorded block. Reading an artifact written between the
#: two executions would make a deterministic re-derivation depend on the clock —
#: which is precisely the property `check_contract` exists to test.
#:
#: So under v7 `capability_liveness` reports `output: 0` for all three. That is
#: NOT a silent all-clear, and the distinction is the whole point of this note:
#: the FAIL clause is `in_scope and output == 0 and eligible_inputs > 0`, and
#: the v7 read lane declares EVERY archive candidate ineligible with a stated
#: `exclusion_reason` ("read-only night: the driver has no mutation lane"), so
#: `eligible_inputs` is 0 and the clause is unreachable by the run's own honest
#: declaration rather than by a missing file. Measured on the reference vault:
#: run 102 (pre-v7) `archives output 1 / eligible 1` — the clause was live and
#: this glob fed it; runs 145 and 148 (v7) `output 0 / eligible 0`.
#:
#: What holds the property under v7 instead: `cos_runverify.check_plan_binding`
#: joins every dispatched `conversation_id|verb` key to the host-private frozen
#: plan, and `check_mutation_counters` recounts the metrics row against the undo
#: ledger. Both run AFTER the apply, where a mutation-liveness question belongs.
LEDGER_GLOB = {
    "archives": "_cos_archive_ledger_*.jsonl",
    "drafts": "_cos_drafts_ledger_*.jsonl",
    "chip_clears": "_cos_chip_ledger_*.jsonl",
}


# --- ledger side: what the run actually PRODUCED, scoped to this run --------

COUNTERS = {"archives": counts_archive, "drafts": counts_draft,
            "chip_clears": counts_chip_clear}


# --- input validation -------------------------------------------------------

def validate(pre: dict, post: dict, profile: str, run_id: str,
             pin: str | None = None) -> None:
    if profile not in PROFILES:
        raise Malformed(f"unknown --profile {profile!r} (expected one of {PROFILES})")
    declared = pre.get("run_profile")
    if declared is not None and declared != profile:
        raise Malformed(
            f"pre declares run_profile={declared!r} but --profile={profile!r}")

    _require(pre, "enumerated_at", str, "pre")
    enumerated_list = _require(pre, "enumerated", list, "pre")
    if (not all(isinstance(convid, str) and convid for convid in enumerated_list)
            or len(set(enumerated_list)) != len(enumerated_list)):
        raise Malformed("pre: enumerated must contain unique non-empty conversation ids")
    _require(pre, "pre_run_holds", dict, "pre")
    _require(post, "post_run", dict, "post")
    _require(post, "arrived_during_run", list, "post")
    _require(post, "candidates", list, "post")
    _require(post, "capabilities", dict, "post")
    _counts(pre, post)
    if _uses_new_count_schema(pre, post):
        _validate_browser_provenance(pre, post, run_id, pin)
        if profile == "full":
            _validate_sent_snapshot(pre, "pre")
            _validate_sent_snapshot(post, "post")

    for convid, bucket in post["post_run"].items():
        if bucket not in BUCKETS:
            raise Malformed(f"post: convid {convid!r} carries unknown bucket "
                            f"{bucket!r} (expected one of {BUCKETS})")
    for cap in post["capabilities"]:
        if cap not in CAPABILITIES:
            raise Malformed(f"post: unknown capability {cap!r}")
    enumerated = set(enumerated_list)
    candidate_keys: set[tuple[str, str]] = set()
    for rec in post["candidates"]:
        problem = candidate_shape_problem(rec, enumerated, candidate_keys,
                                          CAPABILITIES)
        if problem is not None:
            raise Malformed(problem)


def preflight(pre: dict, profile: str, run_id: str,
              ledgers: Path | None = None) -> list[str]:
    """Validate the serialized PRE snapshot before any mailbox mutation.

    Pass `ledgers` (the ops dir) to check the owner's lane pin HERE, at 19:05,
    instead of discovering at 21:30 that the whole night ran on the wrong lane.
    """
    if profile not in PROFILES:
        raise Malformed(f"unknown --profile {profile!r} (expected one of {PROFILES})")
    if pre.get("run_profile") != profile:
        raise Malformed(
            f"pre declares run_profile={pre.get('run_profile')!r} "
            f"but --profile={profile!r}")
    _require(pre, "enumerated_at", str, "pre")
    enumerated = _require(pre, "enumerated", list, "pre")
    _require(pre, "pre_run_holds", dict, "pre")
    count = _require(pre, "inbox_conversation_count_before", int, "pre")
    folder_items = _require(pre, "owa_folder_item_count_before", int, "pre")
    if min(count, folder_items) < 0:
        raise Malformed("pre: conversation and folder-item counts must be non-negative")
    pin = lane_pin(ledgers) if ledgers is not None else None
    elected = _validate_browser_election(pre, pin)
    _validate_scan_provenance(pre, "pre", run_id, elected)
    if profile == "full":
        _validate_sent_snapshot(pre, "pre")

    reasons: list[str] = []
    if pin is not None and elected != pin:
        reasons.append("OC-lane-pin-not-honoured")
    if (not all(isinstance(convid, str) and convid for convid in enumerated)
            or len(set(enumerated)) != len(enumerated)
            or count != len(enumerated)):
        reasons.append("OC-provenance-pre-enumeration-count")
    if not _complete_enumeration(pre, count):
        reasons.append("OC-provenance-incomplete-enumeration")
    if profile == "full" and not pre["sent_zero_send"]["complete"]:
        reasons.append("ZS-incomplete")
    return reasons


# --- the contract ------------------------------------------------------------

def _sha() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]


def evaluate(pre: dict, post: dict, ledgers: Path, run_id: str, profile: str) -> dict:
    pin = lane_pin(ledgers)
    validate(pre, post, profile, run_id, pin)

    enumerated: list[str] = list(pre["enumerated"])
    enumerated_set = set(enumerated)
    pre_holds: dict = pre["pre_run_holds"]
    post_run: dict = post["post_run"]
    arrived: list[str] = list(post["arrived_during_run"])
    (conversation_before, folder_items_before, conversation_after,
     folder_items_after, legacy_counts) = _counts(pre, post)

    counts = dict.fromkeys(BUCKETS, 0)
    for convid in enumerated:
        bucket = post_run.get(convid)
        if bucket in BUCKETS:
            counts[bucket] += 1
    counts["enumerated"] = len(enumerated)

    # The clause families run in the order the verdict_reasons list has always
    # carried them: zero-send + provenance, clause (a) with the guard-stop
    # corroboration, the candidate scan, the anti-degenerate guard, liveness.
    (enumeration_complete, clause_reasons, zero_send_proof,
     stray) = provenance_reasons(
        pre, post, counts, BUCKETS, enumerated, enumerated_set, arrived,
        conversation_before, conversation_after, folder_items_after,
        legacy_counts, _complete_enumeration, _sent_zero_send, profile)
    unaccounted_convids, stopped_convids, guard_stop, a_reasons = (
        accounting_reasons(
            profile, ACCOUNTED[profile], post, pre, enumerated,
            enumerated_set, counts, legacy_counts, pin, ledgers, run_id,
            _guard_stop_shape, guard_stop_corroborated))
    reasons = clause_reasons + a_reasons
    eligible_seen, raw_seen, archive_candidates = candidate_scan(
        post, CAPABILITIES, reasons)
    reasons.extend(degenerate_reasons(
        profile, counts, pre_holds, enumerated, post_run, arrived,
        archive_candidates))
    liveness, unattributed_total = capability_liveness(
        post, profile, ledgers, run_id, eligible_seen, raw_seen, reasons,
        CAPABILITIES, IN_SCOPE, LEDGER_GLOB, COUNTERS, run_scoped_rows)

    block = {
        "run_profile": profile,
        "run_id": str(run_id),
        "enumerated_at": pre["enumerated_at"],
        "enumeration_complete": enumeration_complete,
        "enumerated": enumerated,
        "pre_run_holds": pre_holds,
        "post_run": post_run,
        "counts": counts,
        "arrived_during_run": arrived,
        "inbox_conversation_count_before": conversation_before,
        "inbox_conversation_count_after": conversation_after,
        "inbox_conversation_delta": conversation_after - conversation_before,
        "owa_folder_item_count_before": folder_items_before,
        "owa_folder_item_count_after": folder_items_after,
        "owa_folder_item_delta": folder_items_after - folder_items_before,
        # Compatibility aliases for metrics readers written against v5.28.
        "inbox_count_before": conversation_before,
        "inbox_count_after": conversation_after,
        "inbox_delta": conversation_after - conversation_before,
        "split": {"archive": counts["archived"], "hold": counts["held_non_drafted"],
                  "drafted": counts["held_drafted"]},
        "unaccounted_convids": unaccounted_convids,
        "stopped_by_guard_convids": stopped_convids,
        "guard_stop": guard_stop,
        "lane": {
            "elected": (pre["browser_election"]["elected"]
                        if not legacy_counts else None),
            "pin": pin,
            "pin_honoured": (None if pin is None or legacy_counts
                             else pre["browser_election"]["elected"] == pin),
        },
        "unknown_convids": stray,
        "unattributed_ledger_rows": unattributed_total,
        "capability_liveness": liveness,
        "zero_send_proof": zero_send_proof,
        "verdict": "FAILED" if reasons else "PASS",
        "verdict_reasons": reasons,
        "verdict_source": f"tools/cos_contract.py@{_sha()}",
    }
    return block


def render(block: dict) -> str:
    c = block["counts"]
    lines = [
        f"OUTCOME CONTRACT — {block['verdict']}  "
        f"(profile {block['run_profile']}, run {block['run_id']})",
        f"  enumerated {c['enumerated']} at {block['enumerated_at']}  "
        f"archive : hold : drafted = {block['split']['archive']} : "
        f"{block['split']['hold']} : {block['split']['drafted']}",
        f"  inbox conversations {block['inbox_conversation_count_before']} -> "
        f"{block['inbox_conversation_count_after']} "
        f"(delta {block['inbox_conversation_delta']}); OWA items "
        f"{block['owa_folder_item_count_before']} -> "
        f"{block['owa_folder_item_count_after']}  "
        f"arrived_during_run {len(block['arrived_during_run'])}",
    ]
    lane = block.get("lane") or {}
    lines.append(f"  lane: elected {lane.get('elected')}  pin {lane.get('pin')}"
                 f"  honoured {lane.get('pin_honoured')}")
    for cap, v in block["capability_liveness"].items():
        lines.append(f"  {cap}: output {v['output']} / eligible {v['eligible_inputs']}"
                     f" (raw {v['raw_inputs']}, in_scope {v['in_scope']})")
    if c.get("stopped_by_guard"):
        guard = (block.get("guard_stop") or {}).get("guard")
        lines.append(f"  stopped_by_guard {c['stopped_by_guard']} "
                     f"(guard {guard}) — accounted, no disposition written")
    if block["verdict_reasons"]:
        lines.append("  FAILED clauses: " + ", ".join(block["verdict_reasons"]))
    if block["unaccounted_convids"]:
        lines.append("  unaccounted: " + ", ".join(block["unaccounted_convids"]))
    lines.append(f"  {block['verdict_source']}")
    return "\n".join(lines)


def canonical_block_path(ledgers: Path, run_id: str) -> Path | None:
    """``<ops>/cos_contract_block_<host run id>.json`` — the name the HOST reads.

    WHY THIS EXISTS (measured, runs 63-64). SKILL.md writes the CLI as
    ``--out <block.json>``: a placeholder, never a name. Runs 40-62 happened to
    choose ``cos_contract_block_<run>.json``, which is what the host validator
    reads; runs 63 and 64 chose ``outcome_contract_<run>.json`` and the
    validator correctly reported "no readable OUTCOME CONTRACT block". A file
    name that only convention pins is a file name that drifts, so the checker
    now emits the canonical copy itself and ``--out`` stays the run's own.

    The full run id comes from the HOST pointer ``cos-run-begin`` writes, so
    this artifact is immune to the local-vs-UTC date drift as well: the run may
    pass ``--run-id 64`` or ``--run-id 2026-08-01-run64``, and either way the
    block lands under the id the host froze. No pointer, or a pointer naming a
    different run, means the host cannot tell WHICH run this block belongs to —
    then only ``--out`` is written and the validator says so.
    """
    want = re.search(r"(\d+)$", str(run_id))
    if want is None:
        return None
    try:
        from brain import cos                                # noqa: PLC0415
        pointer = json.loads(
            cos.current_run_path(ledgers.parent).read_text(encoding="utf-8"))
        host_run_id = str(pointer["run_id"])
    except Exception:                                        # noqa: BLE001
        return None
    m = re.search(r"run(\d+)$", host_run_id)
    if m is None or m.group(1) != want.group(1):
        return None
    return ledgers / f"cos_contract_block_{host_run_id}.json"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--pre", required=True, type=Path)
    p.add_argument("--post", type=Path)
    p.add_argument("--ledgers", type=Path)
    p.add_argument("--run-id", required=True)
    p.add_argument("--profile", required=True, choices=list(PROFILES))
    p.add_argument("--out", type=Path)
    p.add_argument("--preflight", action="store_true")
    args = p.parse_args(argv[1:])

    try:
        if args.preflight:
            reasons = preflight(_load(args.pre, "pre"), args.profile, args.run_id,
                                args.ledgers)
            if reasons:
                print("PRE-FLIGHT FAILED: " + ", ".join(reasons))
                return 1
            print(f"PRE-FLIGHT PASS (profile {args.profile}, run {args.run_id})")
            return 0
        if args.post is None or args.ledgers is None:
            raise Malformed("--post and --ledgers are required without --preflight")
        if not args.ledgers.is_dir():
            raise Malformed(f"--ledgers: no such directory {args.ledgers}")
        block = evaluate(_load(args.pre, "pre"), _load(args.post, "post"),
                         args.ledgers, args.run_id, args.profile)
    except Malformed as exc:
        print(f"MALFORMED INPUT: {exc}", file=sys.stderr)
        return 2

    payload = json.dumps(block, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        args.out.write_text(payload, encoding="utf-8")
    canonical = canonical_block_path(args.ledgers, args.run_id)
    if canonical is not None and canonical.resolve() != (
            args.out.resolve() if args.out else None):
        canonical.write_text(payload, encoding="utf-8")
    print(render(block))
    return 1 if block["verdict"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

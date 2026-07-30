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
from cos_reconcile_metrics import _rows, counts_archive, counts_draft  # noqa: E402

BUCKETS = ("archived", "held_non_drafted", "held_drafted", "chipped", "unaccounted")
PROFILES = ("full", "label-only")

#: WHICH buckets count as ACCOUNTED is profile-dependent, and that is
#: load-bearing: `full` refuses a bare P-chip (v5.26 wants a Held label on
#: anything not archived), `label-only` accepts it and forbids archiving.
ACCOUNTED = {
    "full": frozenset({"archived", "held_non_drafted", "held_drafted"}),
    "label-only": frozenset({"chipped", "held_non_drafted", "held_drafted"}),
}

CAPABILITIES = ("archives", "drafts", "chip_clears")

TOOLSETS = ("iab", "chrome-plugin")

#: A guard may only be evaluated for a capability IN SCOPE for the profile —
#: under `label-only` archiving and drafting are forbidden BY DEFINITION, so
#: evaluating them would fail every correct midday run by construction.
IN_SCOPE = {
    "full": {"archives": True, "drafts": True, "chip_clears": True},
    "label-only": {"archives": False, "drafts": False, "chip_clears": True},
}

LEDGER_GLOB = {
    "archives": "_cos_archive_ledger_*.jsonl",
    "drafts": "_cos_drafts_ledger_*.jsonl",
    "chip_clears": "_cos_chip_ledger_*.jsonl",
}


class Malformed(Exception):
    """Input the checker cannot read as a contract report at all (exit 2)."""


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise Malformed(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Malformed(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise Malformed(f"{label} must include a timezone")
    return parsed


def _validate_sent_snapshot(obj: dict, label: str) -> None:
    proof = obj.get("sent_zero_send")
    if not isinstance(proof, dict):
        raise Malformed(f"{label}: new-schema full run requires `sent_zero_send`")
    if proof.get("identity_field") != "item_id":
        raise Malformed(f"{label}: sent_zero_send.identity_field must be item_id")
    if proof.get("sort") != "newest-first":
        raise Malformed(f"{label}: sent_zero_send.sort must be newest-first")
    if not isinstance(proof.get("complete"), bool):
        raise Malformed(f"{label}: sent_zero_send.complete must be bool")

    window = _timestamp(proof.get("window_start"), f"{label}: sent_zero_send.window_start")
    captured = _timestamp(proof.get("captured_at"), f"{label}: sent_zero_send.captured_at")
    if captured < window:
        raise Malformed(f"{label}: sent proof was captured before its window")

    boundary = proof.get("boundary")
    boundary_value = proof.get("boundary_timestamp")
    if boundary == "older-than-window":
        if _timestamp(boundary_value, f"{label}: sent_zero_send.boundary_timestamp") >= window:
            raise Malformed(f"{label}: sent boundary must be older than window_start")
    elif boundary == "list-end":
        if boundary_value is not None:
            raise Malformed(f"{label}: list-end boundary_timestamp must be null")
    else:
        raise Malformed(
            f"{label}: sent_zero_send.boundary must be older-than-window or list-end")

    items = proof.get("items")
    if not isinstance(items, list):
        raise Malformed(f"{label}: sent_zero_send.items must be a list")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise Malformed(f"{label}: each sent item must be an object")
        item_id = item.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            raise Malformed(f"{label}: each sent item needs a non-empty item_id")
        if item_id in seen:
            raise Malformed(f"{label}: duplicate sent item_id {item_id!r}")
        seen.add(item_id)
        timestamp = _timestamp(
            item.get("timestamp"), f"{label}: sent item {item_id!r} timestamp")
        if timestamp < window or timestamp > captured:
            raise Malformed(
                f"{label}: sent item {item_id!r} falls outside the captured window")


def _sent_zero_send(pre: dict, post: dict) -> tuple[dict, list[str]]:
    before, after = pre["sent_zero_send"], post["sent_zero_send"]
    pre_ids = {item["item_id"] for item in before["items"]}
    post_ids = {item["item_id"] for item in after["items"]}
    new_ids = sorted(post_ids - pre_ids)
    reasons: list[str] = []
    if before["window_start"] != after["window_start"]:
        reasons.append("ZS-window-mismatch")
    if not before["complete"] or not after["complete"]:
        reasons.append("ZS-incomplete")
    if new_ids:
        reasons.append("ZS-new-sent-item")
    return {
        "identity_field": "item_id",
        "window_start": before["window_start"],
        "pre_item_count": len(pre_ids),
        "post_item_count": len(post_ids),
        "new_item_ids": new_ids,
        "complete": not reasons,
    }, reasons


def _counts(pre: dict, post: dict) -> tuple[int, int, int, int, bool]:
    """Return conversation/item counts before/after and whether input is legacy."""
    new_keys = (
        (pre, "inbox_conversation_count_before", "pre"),
        (pre, "owa_folder_item_count_before", "pre"),
        (post, "inbox_conversation_count_after", "post"),
        (post, "owa_folder_item_count_after", "post"),
    )
    if any(key in obj for obj, key, _ in new_keys):
        values = [_require(obj, key, int, label) for obj, key, label in new_keys]
        if any(value < 0 for value in values):
            raise Malformed("conversation and folder-item counts must be non-negative")
        return (*values, False)

    # Legacy v5.28 inputs used one ambiguous Inbox count for both units.
    before = _require(pre, "inbox_count_before", int, "pre")
    after = _require(post, "inbox_count_after", int, "post")
    folder_after = _require(post, "owa_folder_count", int, "post")
    if min(before, after, folder_after) < 0:
        raise Malformed("Inbox counts must be non-negative")
    return before, before, after, folder_after, True


def _complete_enumeration(obj: dict, expected: int) -> bool:
    evidence = obj.get("enumeration_evidence")
    return (
        obj.get("enumeration_complete") is True
        and isinstance(evidence, dict)
        and evidence.get("unique_ids") == expected
        and evidence.get("list_declared_size") == expected
        and isinstance(evidence.get("stagnant_scans"), int)
        and evidence["stagnant_scans"] >= 3
        and evidence.get("scroll_at_end") is True
    )


def _uses_new_count_schema(pre: dict, post: dict) -> bool:
    return any(
        key in obj
        for obj, key in (
            (pre, "inbox_conversation_count_before"),
            (pre, "owa_folder_item_count_before"),
            (post, "inbox_conversation_count_after"),
            (post, "owa_folder_item_count_after"),
        )
    )


def _validate_browser_election(pre: dict) -> str:
    election = pre.get("browser_election")
    if not isinstance(election, dict):
        raise Malformed("pre: new-schema snapshot requires browser_election")
    attempted = election.get("attempted")
    elected = election.get("elected")
    if (not isinstance(attempted, list) or not attempted or
            not all(isinstance(toolset, str) for toolset in attempted)):
        raise Malformed("pre: browser_election.attempted must be a non-empty string list")
    if attempted[0] != "iab":
        raise Malformed("pre: browser_election must attempt iab first")
    if len(set(attempted)) != len(attempted) or any(toolset not in TOOLSETS for toolset in attempted):
        raise Malformed("pre: browser_election contains an invalid or repeated toolset")
    if elected not in TOOLSETS or elected != attempted[-1]:
        raise Malformed("pre: browser_election.elected must be the final attempted toolset")
    return elected


def _validate_scan_provenance(
        obj: dict, label: str, run_id: str, elected: str) -> None:
    provenance = obj.get("scan_provenance")
    if not isinstance(provenance, dict):
        raise Malformed(f"{label}: new-schema snapshot requires scan_provenance")
    if _run_token(provenance.get("run_id")) != _run_token(run_id):
        raise Malformed(f"{label}: scan_provenance.run_id must match --run-id")
    if provenance.get("toolset") != elected:
        raise Malformed(f"{label}: scan_provenance.toolset must match the elected toolset")
    if provenance.get("folder") != "Inbox":
        raise Malformed(f"{label}: scan_provenance.folder must be Inbox")
    if provenance.get("identity_field") != "conversation_id":
        raise Malformed(f"{label}: scan_provenance.identity_field must be conversation_id")


def _validate_browser_provenance(pre: dict, post: dict, run_id: str) -> None:
    """Require fresh, same-lane scans and IAB-first election for v5.30 inputs."""
    elected = _validate_browser_election(pre)
    for label, obj in (("pre", pre), ("post", post)):
        _validate_scan_provenance(obj, label, run_id, elected)


# --- ledger side: what the run actually PRODUCED, scoped to this run --------

def counts_chip_clear(rows: list[dict]) -> int:
    """Verified chip CLEARS (LIF-01 `action: cleared`), never adds/re-levels."""
    n = 0
    for r in rows:
        if r.get("action") != "cleared":
            continue
        ver = r.get("verification")
        if isinstance(ver, dict):
            n += 1
        elif isinstance(ver, str) and ver.startswith(
            ("verified", "response-confirmed", "server-reread-confirmed", "PASS")
        ):
            n += 1
        elif str(r.get("status") or "").startswith(("verified", "response-confirmed")):
            n += 1
    return n


COUNTERS = {"archives": counts_archive, "drafts": counts_draft,
            "chip_clears": counts_chip_clear}


def _run_token(value: object) -> str:
    return re.sub(r"^run", "", str(value))


def _file_run(path: Path) -> str | None:
    m = re.search(r"-run([^.]+)\.jsonl$", path.name)
    return m.group(1) if m else None


def run_scoped_rows(ledgers: Path, glob: str, run_id: str) -> tuple[list[dict], int]:
    """Rows attributable to `run_id`, plus the count of unattributable rows.

    A row is this run's when it carries `run`/`run_id` matching, or when it
    lives in a `…-run<N>.jsonl` file for this run. A row with NO attribution in
    a file with NO run token cannot be proven to be this run's, so it is
    SKIPPED (and surfaced) — v5.27 already requires per-run attribution.
    """
    want = _run_token(run_id)
    keep: list[dict] = []
    unattributed = 0
    for path in sorted(ledgers.glob(glob)):
        file_run = _file_run(path)
        for row in _rows(path):
            rid = row.get("run", row.get("run_id"))
            if rid is not None:
                if _run_token(rid) == want:
                    keep.append(row)
            elif file_run is not None:
                if _run_token(file_run) == want:
                    keep.append(row)
            else:
                unattributed += 1
    return keep, unattributed


# --- input validation -------------------------------------------------------

def _load(path: Path, label: str) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise Malformed(f"{label}: no such file {path}") from exc
    except json.JSONDecodeError as exc:
        raise Malformed(f"{label}: not JSON ({exc})") from exc
    if not isinstance(obj, dict):
        raise Malformed(f"{label}: expected a JSON object")
    return obj


def _require(obj: dict, key: str, kind: type, label: str):
    if key not in obj:
        raise Malformed(f"{label}: missing required key `{key}`")
    if not isinstance(obj[key], kind):
        raise Malformed(f"{label}: `{key}` must be {kind.__name__}")
    return obj[key]


def validate(pre: dict, post: dict, profile: str, run_id: str) -> None:
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
        _validate_browser_provenance(pre, post, run_id)
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
        if not isinstance(rec, dict):
            raise Malformed("post: each candidate record must be an object")
        convid = rec.get("convid")
        if not isinstance(convid, str) or not convid:
            raise Malformed("post: each candidate record needs a non-empty `convid`")
        if convid not in enumerated:
            raise Malformed(f"post: candidate convid {convid!r} was not enumerated")
        capability = rec.get("capability")
        if capability not in CAPABILITIES:
            raise Malformed(f"post: candidate for unknown capability "
                            f"{capability!r}")
        key = (convid, capability)
        if key in candidate_keys:
            raise Malformed(f"post: duplicate candidate {key!r}")
        candidate_keys.add(key)
        if not isinstance(rec.get("eligible"), bool):
            raise Malformed("post: each candidate record needs a boolean `eligible`")


def preflight(pre: dict, profile: str, run_id: str) -> list[str]:
    """Validate the serialized PRE snapshot before any mailbox mutation."""
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
    elected = _validate_browser_election(pre)
    _validate_scan_provenance(pre, "pre", run_id, elected)
    if profile == "full":
        _validate_sent_snapshot(pre, "pre")

    reasons: list[str] = []
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
    validate(pre, post, profile, run_id)

    enumerated: list[str] = list(pre["enumerated"])
    enumerated_set = set(enumerated)
    pre_holds: dict = pre["pre_run_holds"]
    post_run: dict = post["post_run"]
    arrived: list[str] = list(post["arrived_during_run"])
    (conversation_before, folder_items_before, conversation_after,
     folder_items_after, legacy_counts) = _counts(pre, post)
    reasons: list[str] = []
    zero_send_proof = None
    if not legacy_counts and profile == "full":
        zero_send_proof, zero_send_reasons = _sent_zero_send(pre, post)
        reasons.extend(zero_send_reasons)

    counts = dict.fromkeys(BUCKETS, 0)
    for convid in enumerated:
        bucket = post_run.get(convid)
        if bucket in BUCKETS:
            counts[bucket] += 1
    counts["enumerated"] = len(enumerated)

    # --- provenance: the checker distrusts its own inputs -------------------
    enumeration_complete = (
        _complete_enumeration(pre, conversation_before)
        and _complete_enumeration(post, conversation_after)
    )
    if not legacy_counts and not enumeration_complete:
        reasons.append("OC-provenance-incomplete-enumeration")
    if not legacy_counts and conversation_before != len(enumerated):
        reasons.append("OC-provenance-pre-enumeration-count")
    bucket_sum = sum(counts[b] for b in BUCKETS)
    if bucket_sum != len(enumerated):
        reasons.append("OC-provenance-bucket-sum")
    resident = (counts["held_non_drafted"] + counts["held_drafted"]
                + counts["chipped"] + counts["unaccounted"] + len(arrived))
    if resident != conversation_after:
        reasons.append("OC-provenance-residency")
    if legacy_counts and folder_items_after != conversation_after:
        reasons.append("OC-provenance-folder-count")
    stray = sorted(set(post_run) - enumerated_set - set(arrived))
    if stray:
        reasons.append("OC-provenance-unknown-convid")

    # --- clause (a): accounted, per the run's declared profile --------------
    accounted = ACCOUNTED[profile]
    unaccounted_convids = sorted(
        c for c in enumerated if post_run.get(c) not in accounted)
    if unaccounted_convids:
        reasons.append("OC-a-unaccounted")

    # `label-only` forbids archiving: an archived row is a scope violation.
    if profile == "label-only" and counts["archived"]:
        reasons.append("OC-scope-violation-archived-under-label-only")

    # --- capability liveness -------------------------------------------------
    liveness: dict[str, dict] = {}
    unattributed_total = 0
    eligible_seen = {cap: 0 for cap in CAPABILITIES}
    raw_seen = {cap: 0 for cap in CAPABILITIES}
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

    # A newly classified hold is legitimate when its archive decision was
    # reported and rejected by a safety guard. Missing that per-conversation
    # evidence is the degenerate "label everything Held" shape.
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

    for cap in CAPABILITIES:
        declared = post["capabilities"].get(cap)
        in_scope = IN_SCOPE[profile][cap]          # computed, never read
        if not isinstance(declared, dict):
            reasons.append(f"OC-liveness-missing:{cap}")
        elif declared.get("in_scope") != in_scope:
            reasons.append(f"OC-liveness-in-scope:{cap}")
        rows, unattributed = run_scoped_rows(ledgers, LEDGER_GLOB[cap], run_id)
        unattributed_total += unattributed
        output = COUNTERS[cap](rows)
        liveness[cap] = {
            "in_scope": in_scope,
            "output": output,
            "eligible_inputs": eligible_seen[cap],
            "raw_inputs": raw_seen[cap],
        }
        if in_scope and output == 0 and eligible_seen[cap] > 0:
            reasons.append(f"OC-liveness:{cap}")

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
    for cap, v in block["capability_liveness"].items():
        lines.append(f"  {cap}: output {v['output']} / eligible {v['eligible_inputs']}"
                     f" (raw {v['raw_inputs']}, in_scope {v['in_scope']})")
    if block["verdict_reasons"]:
        lines.append("  FAILED clauses: " + ", ".join(block["verdict_reasons"]))
    if block["unaccounted_convids"]:
        lines.append("  unaccounted: " + ", ".join(block["unaccounted_convids"]))
    lines.append(f"  {block['verdict_source']}")
    return "\n".join(lines)


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
            reasons = preflight(_load(args.pre, "pre"), args.profile, args.run_id)
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

    if args.out:
        args.out.write_text(json.dumps(block, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
    print(render(block))
    return 1 if block["verdict"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

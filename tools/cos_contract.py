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
DISTRUSTS them: a bucket sum that does not equal the enumerated set, a
residency mismatch against the post-run Inbox count, a transcribed OWA folder
count that disagrees with the reported one, or a convid in the re-enumeration
that was never enumerated and never arrived, each render FAILED rather than a
clean PASS with a `verdict_source` sha on it.

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


def validate(pre: dict, post: dict, profile: str) -> None:
    if profile not in PROFILES:
        raise Malformed(f"unknown --profile {profile!r} (expected one of {PROFILES})")
    declared = pre.get("run_profile")
    if declared is not None and declared != profile:
        raise Malformed(
            f"pre declares run_profile={declared!r} but --profile={profile!r}")

    _require(pre, "enumerated_at", str, "pre")
    _require(pre, "enumerated", list, "pre")
    _require(pre, "pre_run_holds", dict, "pre")
    _require(post, "post_run", dict, "post")
    _require(post, "arrived_during_run", list, "post")
    _require(post, "candidates", list, "post")
    _require(post, "capabilities", dict, "post")
    for key in ("inbox_count_after", "owa_folder_count"):
        if not isinstance(post.get(key), int):
            raise Malformed(f"post: `{key}` must be an int (the OWA folder count "
                            f"is transcribed verbatim, never inferred)")

    for convid, bucket in post["post_run"].items():
        if bucket not in BUCKETS:
            raise Malformed(f"post: convid {convid!r} carries unknown bucket "
                            f"{bucket!r} (expected one of {BUCKETS})")
    for cap in post["capabilities"]:
        if cap not in CAPABILITIES:
            raise Malformed(f"post: unknown capability {cap!r}")
    for rec in post["candidates"]:
        if not isinstance(rec, dict):
            raise Malformed("post: each candidate record must be an object")
        if rec.get("capability") not in CAPABILITIES:
            raise Malformed(f"post: candidate for unknown capability "
                            f"{rec.get('capability')!r}")
        if not isinstance(rec.get("eligible"), bool):
            raise Malformed("post: each candidate record needs a boolean `eligible`")


# --- the contract ------------------------------------------------------------

def _sha() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]


def evaluate(pre: dict, post: dict, ledgers: Path, run_id: str, profile: str) -> dict:
    validate(pre, post, profile)

    enumerated: list[str] = list(pre["enumerated"])
    enumerated_set = set(enumerated)
    pre_holds: dict = pre["pre_run_holds"]
    post_run: dict = post["post_run"]
    arrived: list[str] = list(post["arrived_during_run"])
    inbox_after = int(post["inbox_count_after"])
    reasons: list[str] = []

    counts = dict.fromkeys(BUCKETS, 0)
    for convid in enumerated:
        bucket = post_run.get(convid)
        if bucket in BUCKETS:
            counts[bucket] += 1
    counts["enumerated"] = len(enumerated)

    # --- provenance: the checker distrusts its own inputs -------------------
    bucket_sum = sum(counts[b] for b in BUCKETS)
    if bucket_sum != len(enumerated):
        reasons.append("OC-provenance-bucket-sum")
    resident = (counts["held_non_drafted"] + counts["held_drafted"]
                + counts["chipped"] + counts["unaccounted"] + len(arrived))
    if resident != inbox_after:
        reasons.append("OC-provenance-residency")
    if int(post["owa_folder_count"]) != inbox_after:
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

    # --- anti-degenerate guard (`full` only) --------------------------------
    held_total = counts["held_non_drafted"] + counts["held_drafted"]
    if (profile == "full" and held_total > len(pre_holds)
            and counts["archived"] == 0 and not arrived):
        reasons.append("OC-degenerate")

    # --- capability liveness -------------------------------------------------
    liveness: dict[str, dict] = {}
    unattributed_total = 0
    eligible_seen = {cap: 0 for cap in CAPABILITIES}
    raw_seen = {cap: 0 for cap in CAPABILITIES}
    for rec in post["candidates"]:
        cap = rec["capability"]
        raw_seen[cap] += 1
        if rec["eligible"]:
            eligible_seen[cap] += 1
        elif not rec.get("exclusion_reason"):
            if "OC-candidate-no-exclusion-reason" not in reasons:
                reasons.append("OC-candidate-no-exclusion-reason")

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
        "enumerated": enumerated,
        "pre_run_holds": pre_holds,
        "post_run": post_run,
        "counts": counts,
        "arrived_during_run": arrived,
        "inbox_count_before": pre.get("inbox_count_before"),
        "inbox_count_after": inbox_after,
        "inbox_delta": (inbox_after - int(pre["inbox_count_before"])
                        if isinstance(pre.get("inbox_count_before"), int) else None),
        "split": {"archive": counts["archived"], "hold": counts["held_non_drafted"],
                  "drafted": counts["held_drafted"]},
        "unaccounted_convids": unaccounted_convids,
        "unknown_convids": stray,
        "unattributed_ledger_rows": unattributed_total,
        "capability_liveness": liveness,
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
        f"  inbox {block['inbox_count_before']} -> {block['inbox_count_after']} "
        f"(delta {block['inbox_delta']})  "
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
    p.add_argument("--post", required=True, type=Path)
    p.add_argument("--ledgers", required=True, type=Path)
    p.add_argument("--run-id", required=True)
    p.add_argument("--profile", required=True, choices=list(PROFILES))
    p.add_argument("--out", type=Path)
    args = p.parse_args(argv[1:])

    try:
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

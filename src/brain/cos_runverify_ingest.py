"""INGEST-01 belt 3 — ingestion is independent of disposition, recounted off the artifact.

The THIRD belt, and it exists because the two before it see different evidence
and neither is this one. `cos_judge_rules._r_ingest` recomputes the field from
the batch CONTEXT at judgment time; `cos_mutate_plan_marks
.mark_lane_disposition_blindness` differentially probes the MUTATION PLAN
before a single mark is dispatched; this recounts the WRITTEN LEDGER after the
fact, which is the only one of the three a later reader can reproduce.

WHY THE SHAPE OF THE FAILURE MATTERS. Commit f270700 records the near-identical
predecessor of this defect: a single-vocabulary design let 110 read-but-unowed
threads fall through with NO LANE AT ALL. Nothing said what had happened to
them, so no counter could count them and no check could fail on them. So the
first thing this check asks is not "is the answer right" but "is there an
answer at all" — a row whose `ingest` is missing or malformed is the failure
this file is named for, and it is reported before any disagreement is.

Split into its own module rather than added to `cos_runverify_checks`, which
sits at the 500-LOC bound.
"""
from __future__ import annotations

from typing import Any

#: A relevant row's content lane, closed. Kept as a literal tuple rather than
#: imported from `tools/`: this module runs inside the engine, on a host whose
#: `tools/` may not be importable, and a verifier that cannot load is a
#: verifier that reports INCONCLUSIVE on every night. `cos_judge_ingest
#: .CONTENT_LANES` is the producer's copy — keep them equal.
CONTENT_LANES = ("text", "attachments", "both")

#: Rows outside Phase-1.6 scope carry no staging verdict at all and are not
#: asked for one here.
_MARKER_DISPOSITION = "zero-eligible"


def _ingest_problems(rows: list[dict[str, Any]]) -> tuple[list[str], int, dict]:
    """(problems, relevant_count, relevant-by-verdict)."""
    missing: list[str] = []
    bad_lane: list[str] = []
    disagree: list[str] = []
    relevant = 0
    by_verdict: dict[str, int] = {}
    for r in rows:
        if str(r.get("disposition") or "") == _MARKER_DISPOSITION:
            continue
        cid = str(r.get("conversation_id"))[:16]
        ing = r.get("ingest")
        if not isinstance(ing, dict) or not isinstance(ing.get("relevant"), bool):
            missing.append(f"{cid}: ingest={ing!r}")
            continue
        # THE DISAGREEMENT THAT WOULD STARVE THE BRIDGE SILENTLY. The bridge
        # reads `ingest.relevant`; the counters (`ledger_counts`,
        # `candidate_count`) still read `disposition`. A row where the two
        # disagree is a candidate the run counts and never ingests, or an
        # ingestion the run never counted.
        if ing["relevant"] != (r.get("disposition") == "candidate"):
            disagree.append(f"{cid}: relevant={ing['relevant']} vs "
                            f"disposition={r.get('disposition')!r}")
            continue
        if not ing["relevant"]:
            if ing.get("content") is not None:
                bad_lane.append(f"{cid}: not relevant but names "
                                f"content={ing.get('content')!r}")
            continue
        relevant += 1
        by_verdict[str(r.get("verdict"))] = by_verdict.get(
            str(r.get("verdict")), 0) + 1
        if ing.get("content") not in CONTENT_LANES:
            bad_lane.append(f"{cid}: content={ing.get('content')!r}")
    problems = []
    if missing:
        problems.append(
            f"{len(missing)} in-scope row(s) carry no explicit `ingest` at all "
            f"— the f270700 shape, where 110 rows fell through with no lane and "
            f"nothing could count them: {missing[:4]}")
    if disagree:
        problems.append(
            f"{len(disagree)} row(s) whose `ingest.relevant` disagrees with "
            f"their staging disposition — the bridge reads one and the "
            f"counters read the other: {disagree[:4]}")
    if bad_lane:
        problems.append(
            f"{len(bad_lane)} row(s) name a content lane outside "
            f"{list(CONTENT_LANES)} or name one while claiming no relevance: "
            f"{bad_lane[:4]}")
    return problems, relevant, by_verdict


def _row(status: str, detail: str) -> dict[str, Any]:
    return {"check": "ingest_independence", "status": status,
            "reexecuted": True, "detail": detail}


def check_ingest_independence(run_id: str, rows: list[dict[str, Any]],
                              ) -> dict[str, Any]:
    """One check row — INGEST-01 belt 3 over this run's own ingestion ledger.

    AND IT STATES THE SPLIT BY VERDICT WHEN IT PASSES, which is not decoration.
    The whole item is "an act thread with a draft must not be skipped by the
    bridge", and a run whose relevant rows are ALL `read` is exactly the
    regression, with every row individually well-formed. Measured on run 188
    (2026-08-24) the split was 44 `act` and 13 `read`; printing it is what
    makes a future 0/57 a finding instead of a silence.
    """
    if not rows:
        return _row("inconclusive",
                    f"run {run_id} wrote no ingestion ledger rows, so ingest "
                    "independence was not exercised — an all-clear that equals "
                    "no input is not an all-clear")
    scoped = [r for r in rows
              if str(r.get("disposition") or "") != _MARKER_DISPOSITION]
    if scoped and not any(isinstance(r.get("ingest"), dict) for r in scoped):
        # A LEDGER FROM BEFORE THE FIELD EXISTED, and it is DEGRADED, not
        # FAILED. `check_body_order` handles a pre-v5.51 bundle the same way
        # and for the same reason: a replay of an old night must report what
        # it cannot recount, never convict the night of a defect that had not
        # been invented yet. The signature that IS a defect is a PARTIALLY
        # stamped ledger — some rows carrying the field and some not — which
        # falls through to `_ingest_problems` below and fails there.
        return _row("degraded",
                    f"none of run {run_id}'s {len(scoped)} in-scope row(s) "
                    "carries an `ingest` field, so this ledger predates "
                    "INGEST-01 and the split between relevance and disposition "
                    "cannot be recounted from it. The bridge read it on the "
                    "old `disposition == candidate` rule")
    problems, relevant, by_verdict = _ingest_problems(rows)
    if problems:
        return _row("fail", f"run {run_id}: " + "; ".join(problems))
    if not relevant:
        return _row("pass",
                    f"every one of this run's {len(rows)} ledger row(s) states "
                    "its ingest relevance explicitly and NONE was relevant — "
                    "nothing was offered to the bridge tonight, so the "
                    "independence of relevance from disposition was not "
                    "exercised")
    return _row("pass",
                f"{relevant} of {len(rows)} ledger row(s) are ingest-relevant, "
                "each naming a content lane, and every row states its "
                "relevance explicitly. Relevance by verdict: "
                f"{dict(sorted(by_verdict.items()))} — relevance is the staging "
                "pass's substance answer and is read off no bucket, archive "
                "claim or draft (INGEST-01)")

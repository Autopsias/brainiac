"""ATT-02 — the bridge leg cannot be skipped SILENTLY, recounted off the ledger.

WHAT THIS CONTROL EXISTS FOR. `tools/cos_ingest_bridge.py` is the ONLY path
from a staged ingestion-ledger row to a `cos.propose` drop, and the nightly
runs it inside `if [ "${COS_INGEST_BRIDGE:-0}" = "1" ]`. With the variable
unset the whole block is skipped: it invokes nothing, writes no `ingest
bridge:` log line, creates no `_cos_ingest_bridge_<run>.jsonl`, and the night
still logs `=== cos-nightly done ===` and exits 0. Measured as of run
2026-09-05-run260 over 151 ingestion ledgers
(`_evidence/porter-finishes/bridge-skip-census.txt`): 15 of the 49 runs that
staged an `act` + `ingest.relevant` row wrote no bridge file at all and 596 of
1656 such rows sit on them — 11 of those runs, holding 513 of the rows, were
the silent condition. **Nothing anywhere in those runs' artifacts says a
candidate was left behind**, which is why no counter could count them; that
absence, not the skip itself, is the defect.

So the rule this scores is ATT-02's first: every ingest-relevant row either
carries a drop stamp, or names why it got none — from the CLOSED vocabularies
`cos.BRIDGE_SETTLEMENT_KINDS` (the leg RAN and settled the row) and
`cos.BRIDGE_SKIP_REASONS` (the leg did NOT carry it). Reasons, not prose, so
this function can COUNT them instead of parsing a sentence.

AND IT STATES ITS SCOPE, structurally (2cc95fed, 2026-09-04). That commit
closed the near-identical shape one layer up: `run_host_checks` scored the
whole NIGHT from the end of the READ lane, a verdict it could not support,
and every later-lane check read "not yet". This control answers ONE question
over ONE artifact — this run's ingestion ledger — and its row says so in
words, in `scope`, and by returning INCONCLUSIVE (never a quiet pass) on a
run whose ledger does not exist yet, which is exactly what the read lane sees
when it scores itself.

Split into its own module rather than added to `cos_runverify_checks` or
`cos_runverify_contract`, both of which sit at the size bound.
"""
from __future__ import annotations

from typing import Any

from . import cos
from .cos_runverify_checks import FAIL, INCONCLUSIVE, PASS, _row
from .cos_runverify_stamps import settlement_claim

#: Rows outside Phase-1.6 scope carry no staging verdict and are not asked for
#: one here — the same marker `check_ingest_independence` skips on.
_MARKER_DISPOSITION = "zero-eligible"


def ingest_relevant(row: dict[str, Any]) -> bool:
    """The BRIDGE's one candidate predicate, over a ledger row.

    RESTATED, not imported: the producer's copy is
    ``tools/cos_judge_ingest.ingest_relevant`` and this module runs inside the
    engine, on a host whose ``tools/`` may not be importable — a verifier that
    cannot load is a verifier that reports INCONCLUSIVE on every night (the
    same reasoning, and the same duty to keep the two equal, as
    ``cos_runverify_ingest.CONTENT_LANES``).
    ``tests/test_cos_bridge_reach.py`` pins them equal over a row table.

    Deliberately the BRIDGE's predicate and not the deliverable's narrower
    "verdict `act` and `ingest.relevant` true": a `read` + relevant row is a
    bridge candidate too, and scoring the superset can only report MORE lost
    candidates, never fewer.
    """
    ing = row.get("ingest")
    if isinstance(ing, dict):
        return ing.get("relevant") is True
    return row.get("disposition") == "candidate"


def accounted(row: dict[str, Any]) -> tuple[str, str]:
    """``(bucket, reason)`` for one candidate — the whole classification.

    FOUR buckets, and the ORDER matters: a row carrying a drop stamp is
    delivered whatever else it says, so a skip reason can never be used to
    hide a duplicate id or a digest mismatch from E16.

    Also the PRODUCER's rule (`tools/cos_bridge_skip.py` asks it which rows
    still need a stamp), so the recorder and the check can never disagree
    about what "accounted for" means — the disagreement that would let a
    stamped run still fail, or an unstamped one pass.
    """
    from .cos_runverify_contract import _drop_stamped   # noqa: PLC0415
    # deferred: `cos_runverify_contract` binds names off `cos_runverify`,
    # which imports THIS module. A module-level import closes that cycle.
    if _drop_stamped(row):
        return "dropped", ""
    settled = settlement_claim(row)
    if settled:
        return "settled", settled
    skipped = cos.bridge_skip_reason(row)
    if skipped:
        return "skipped", skipped
    return "unaccounted", ""


def _tally(counts: dict[str, int]) -> str:
    return ", ".join(f"{k}: {n}" for k, n in sorted(counts.items()))


def check_bridge_reach(run_id: str, rows: list[dict[str, Any]],
                       ) -> dict[str, Any]:
    """One check row — ATT-02 over this run's own ingestion ledger.

    A run whose relevant rows all reached the bridge PASSES. A run holding a
    row that names no outcome at all FAILS, and so does a run whose rows name
    a SKIP: "the leg did not carry these" is precisely the state that used to
    score green, and a night that scored green over 40 unreached candidates is
    the failure this control is named for. A legitimate zero — a run with no
    ingest-relevant rows, or one whose every candidate the bridge settled
    without a drop (never-category, quarantined, duplicate) — passes, because
    the leg ran and answered for each row.
    """
    scoped = [r for r in rows
              if str(r.get("disposition") or "") != _MARKER_DISPOSITION]
    if not rows:
        return _row("bridge_reach", INCONCLUSIVE,
                    f"run {run_id} wrote no ingestion ledger rows, so no "
                    "candidate could have been staged for the bridge and its "
                    "reach cannot be recounted. Scope: this run's ingestion "
                    "ledger only — this is what the READ lane sees when it "
                    "scores itself, before the judgment leg has written one "
                    "row, and an all-clear that equals no input is not an "
                    "all-clear", reexecuted=True)

    candidates = [r for r in scoped if ingest_relevant(r)]
    scope = (f"Scope: run {run_id}'s ingestion ledger only "
             f"({len(candidates)} ingest-relevant of {len(scoped)} in-scope "
             f"row(s) of {len(rows)} total) — this control says nothing about "
             "the night's other lanes")
    if not candidates:
        return _row("bridge_reach", PASS,
                    f"none of run {run_id}'s {len(scoped)} in-scope ledger "
                    "row(s) is ingest-relevant, so the bridge was offered "
                    "nothing and no candidate can have been left behind. "
                    + scope, reexecuted=True)

    buckets: dict[str, int] = {}
    settled: dict[str, int] = {}
    skipped: dict[str, int] = {}
    unaccounted: list[str] = []
    for r in candidates:
        bucket, reason = accounted(r)
        buckets[bucket] = buckets.get(bucket, 0) + 1
        if bucket == "settled":
            settled[reason] = settled.get(reason, 0) + 1
        elif bucket == "skipped":
            skipped[reason] = skipped.get(reason, 0) + 1
        elif bucket == "unaccounted":
            unaccounted.append(str(r.get("conversation_id"))[:16])

    if unaccounted:
        return _row("bridge_reach", FAIL,
                    f"{len(unaccounted)} of run {run_id}'s {len(candidates)} "
                    "ingest-relevant row(s) carry NEITHER a drop stamp NOR "
                    "any reason they got none — the bridge is the only path "
                    "from a staged row to a proposal, so each of these is a "
                    "candidate the night triaged and nothing preserved, with "
                    "nothing on disk saying so. Reason vocabularies: "
                    f"{list(cos.BRIDGE_SETTLEMENT_KINDS)} (the leg settled "
                    f"it) and {list(cos.BRIDGE_SKIP_REASONS)} (the leg did "
                    f"not carry it). First: {unaccounted[:4]}. " + scope,
                    reexecuted=True)
    if skipped:
        return _row("bridge_reach", FAIL,
                    f"{sum(skipped.values())} of run {run_id}'s "
                    f"{len(candidates)} ingest-relevant row(s) name a bridge "
                    f"SKIP ({_tally(skipped)}) — the leg did not carry them, "
                    "so their candidates reached no proposal and the vault "
                    "gained nothing from those threads. This is a FAIL and "
                    "not a degrade on purpose: a skipped leg that scored "
                    "green is what let 596 rows across 15 runs go unnoticed "
                    "(census as of 2026-09-05-run260). Re-run the bridge for "
                    "this run, or run the night with COS_INGEST_BRIDGE=1. "
                    + (f"Also settled without a drop: {_tally(settled)}. "
                       if settled else "") + scope, reexecuted=True)
    return _row("bridge_reach", PASS,
                f"every one of run {run_id}'s {len(candidates)} "
                "ingest-relevant row(s) reached the bridge: "
                f"{buckets.get('dropped', 0)} carry a drop stamp"
                + (f" and {buckets.get('settled', 0)} were settled without a "
                   f"drop ({_tally(settled)})" if settled else "")
                + ". No row names a skip. " + scope, reexecuted=True)

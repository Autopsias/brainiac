"""The INGEST-MARK screen of the COS mutation plan (FIX-03, 2026-08-25).

Split out of `cos_mutate_plan.py` at the 500-LOC bound, exactly as
`cos_mutate_plan_aged` was. It decides which threads may carry
`Brainiac · Ingested`, and nothing else.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from brain import cos_chips  # noqa: E402


def ingested_conversations(run_id: str,
                           rows: list[dict[str, Any]],
                           vault: Path | None = None,
                           *, since_days: int | None = None) -> set[str]:
    """The conversation ids whose OWN candidate the vault SIGNED (FIX-03).

    THE AUTHORITY MOVED FROM THE DROP STAMP TO THE SIGNATURE. Until 2026-08-25
    this keyed on `cos.bridge_dropped_row`, the bridge's stamp that a candidate
    was OFFERED, so the `Brainiac · Ingested` chip landed on content the vault
    had merely been handed. MEASURED 2026-08-25 on the live ledger: run178
    drop-stamped 55 threads and signed 0 of them (its claims are still
    quarantined `run-inconclusive`), run188 stamped 57 and signed 0, while
    run171 (18) and run179 (61) signed every one. The engine owns the new rule,
    `cos.signed_ingested_catching_up`.

    IT ASKS ABOUT MORE THAN TONIGHT, and it has to: a candidate offered by
    tonight's ingest bridge is signed by a LATER maintenance drain, so a
    same-night question answers zero on every night forever — which is what
    the first cut of FIX-03 shipped. `cos.signed_ingested_catching_up` is the
    one definition of the widened rule, and E4 judges a dispatched mark by the
    same call: a planner and an E-check disagreeing about which nights are in
    reach would make every caught-up mark read as unjustified.

    `rows` still bounds the answer: `screen_ingest_marks` iterates TONIGHT'S
    rows, so a thread that has left the mailbox is never planned, and
    tonight's row is what carries the fresh `carries_ingest_mark` observation.

    `vault` is REQUIRED — no vault, nothing to have signed — and a caller that
    passes none gets the empty set and plans no marks. That is the fail-closed
    direction, and the old stamp needing no vault is exactly how it could claim
    ingestion with nothing behind it.
    """
    from brain import cos  # noqa: PLC0415
    if vault is None:
        return set()
    return cos.signed_ingested_catching_up(vault, run_id, rows,
                                           since_days=since_days)


def screen_ingest_marks(rows: list[dict[str, Any]], ingested: set[str],
                        exclude: Callable,
                        *, already_planned: list[dict[str, Any]]
                        ) -> list[dict[str, Any]]:
    """Plan the `Brainiac · Ingested` mark for every row the vault really took.

    A SECOND AXIS, so this is a SEPARATE screen rather than a branch inside
    `screen_ledger_rows`. Every gate there answers "how urgent is this and may
    we act on it" — the verdict, the tier, the ADD-ONLY rule that skips a
    thread already carrying a chip. NONE of them applies here. The mark answers
    "did the vault take this", the signature already answered it, and a thread
    that carries `P1 · Today` is exactly as ingested as one that carries
    nothing. Routing this through that screen is what excluded 178 of 210 rows
    on run164 for "already carries a chip".

    WHAT "INGESTED" MEANS (FIX-03): the vault SIGNED a note for this thread's
    own candidate — see `ingested_conversations` above. A candidate merely
    OFFERED is not ingested, which is what the chip used to claim. The
    signature arrives AFTER the night that offered the candidate, so most of
    what `ingested` names here was offered on an earlier run.

    It skips three rows, and the third is a MEASURED CONSTRAINT rather than a
    policy. The undo ledger's idempotency key is `conversation_id|verb`
    (`cos_mutate_ledger`), so two `categorize` mutations on ONE thread collide:
    `latest()` keeps one row per key, and the undo record for the priority chip
    would be overwritten by the mark's. Losing an undo row is the one failure
    this whole lane exists to prevent. So a thread taking a priority chip
    TONIGHT does not also take the mark tonight — it takes it on the next
    night, when the chip is already on and only the mark is planned.

    STATED LIMIT: the mark is therefore up to ONE NIGHT late on a thread that
    was chipped the same night, and it is late exactly once, because the chip
    lane is ADD-ONLY and never re-chips. It is the delay that depends on
    urgency, never whether the mark arrives. That holds on a thread the run is
    ARCHIVING too, and only because the priority chip is excluded before this
    screen runs: an archived thread is never enumerated again, so a mark
    deferred off one would never arrive at all. This lane does not know that
    and must not — `cos_mutate_plan_budget.screen_archiving_companions` is
    where it is stated, over the assembled plan, and it is the reason the mark
    is the ONE mutation allowed to ride an archive. Making it same-night means giving
    the mark its own verb, its own cap and its own metrics counter — a wider
    change than the deferral is worth, and a counter with no producer is its
    own defect.
    """
    chipping = {m["conversation_id"] for m in already_planned
                if m.get("verb") == "categorize"}
    planned: list[dict[str, Any]] = []
    for row in rows:
        cid = row["conversation_id"]
        if cid not in ingested:
            continue  # not ingested — silent, this is most of the mailbox
        if row.get("carries_ingest_mark"):
            exclude(cid, "categorize", "the thread already carries the "
                                       "ingestion mark; re-writing it is a "
                                       "no-op the page half refuses")
            continue
        if cid in chipping:
            exclude(cid, "categorize",
                    "this thread takes a priority chip tonight, and both "
                    "mutations would share the undo key `<cid>|categorize` — "
                    "the mark waits for the next night rather than overwrite "
                    "the chip's undo row")
            continue
        planned.append({"verb": "categorize", "conversation_id": cid,
                        "chip": cos_chips.CHIP_INGESTED, "mode": "add",
                        "received": row.get("received"),
                        "reason": "ingest mark: the vault SIGNED a note for "
                                  "this thread's own candidate (FIX-03)"})
    return planned


#: (INGEST-01 belt 2) Everything on a ledger row that says what the PORTER DID
#: with the thread, as opposed to what the thread IS. The mark lane answers
#: "did the vault take this" and must read none of them; belt 2 proves it by
#: re-screening with every one of them blanked.
DISPOSITION_FIELDS = ("auto_archive", "noise_signal", "verdict", "judged_tier",
                      "isDraft", "hold_category", "hold_verdict",
                      "disposition", "held_reason", "candidate_count")


def _noop_exclude(_cid: str, _verb: str, _why: str) -> None:
    """The blindness re-screen records nothing: its exclusions are a probe's,
    not the night's, and recording them twice would double every reason on the
    plan's `excluded` list."""


def mark_lane_disposition_blindness(rows: list[dict[str, Any]],
                                    ingested: set[str],
                                    already_planned: list[dict[str, Any]]
                                    ) -> str | None:
    """(INGEST-01 belt 2) Why the mark lane is NOT blind to disposition, or None.

    THE BELT IS A DIFFERENTIAL, not a re-statement of the rule. It runs
    `screen_ingest_marks` twice over the same rows — once as they are, once
    with every `DISPOSITION_FIELDS` value stripped — and refuses a difference.
    Today the two are identical by construction, and that is exactly what makes
    this worth running: the day someone adds `if row.get("auto_archive"):
    continue` to the mark lane, this is what says so, and it says it from the
    planner's own evidence rather than from a comment.

    It is the SECOND of INGEST-01's three belts and it sees what the others do
    not. Belt 1 (`cos_judge_rules._r_ingest`) recomputes the field off the
    batch context at judgment time; belt 3
    (`cos_runverify_checks.check_ingest_independence`) recounts the written
    ledger after the fact. Only this one sees the MUTATION PLAN — the artifact
    where "what the porter does" is actually decided.
    """
    with_disposition = screen_ingest_marks(
        rows, ingested, _noop_exclude, already_planned=already_planned)
    blinded_rows = [{k: v for k, v in r.items() if k not in DISPOSITION_FIELDS}
                    for r in rows]
    without = screen_ingest_marks(
        blinded_rows, ingested, _noop_exclude, already_planned=already_planned)
    if with_disposition == without:
        return None
    kept = {m["conversation_id"] for m in with_disposition}
    blind = {m["conversation_id"] for m in without}
    lost, gained = sorted(blind - kept)[:4], sorted(kept - blind)[:4]
    return ("the ingestion-mark lane is reading a DISPOSITION field: stripping "
            f"{list(DISPOSITION_FIELDS)} changes its plan "
            f"({len(with_disposition)} mark(s) -> {len(without)}; "
            f"{len(blind - kept)} thread(s) the lane skipped only because of "
            f"what the porter did to them, {len(kept - blind)} it planned only "
            f"because of it). Ingestion is independent of disposition "
            f"(INGEST-01): a thread is as worth remembering archived as held. "
            f"lost={lost} gained={gained}")

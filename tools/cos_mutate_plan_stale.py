"""The STALE-ACT lane's PLANNER belt (STALE-01) — belt 2 of three.

Split out of `cos_mutate_plan.py` at the 500-LOC bound, exactly as
`cos_mutate_plan_aged` was. It re-screens what the LEDGER ROW itself can prove,
and nothing else.

THE TWO BELTS SEE DIFFERENT EVIDENCE, which is the only reason a second one is
worth its lines. `cos_judge_rules_stale.stale_act_refusal` refused the claim at
judgment time off the batch CONTEXT — the ask detector, the deadline parse and
the spine, none of which survives onto the ledger. This half sees only the ROW:
the shape of the claim, the verdict it rides, the body that was opened, and the
row's own `isDraft` stamp (the ONE draft fact that does reach the ledger, and
a different reader of the same rule belt 1 applies through
`thread_carries_draft`). Neither belt is a copy of the other, so a bug in one
does not silently pass the other.

The signal itself is stated here again on purpose, exactly as
`cos_mutate_plan_aged` restates the aged-read one: the three belts each carry
their own copy so a rename cannot make one of them silently stop matching.
Keep them equal.
"""
from __future__ import annotations

from typing import Any

_STALE_ACT_SIGNAL = "stale-act-archived"
_STALE_REASONS = ("answered-by-other", "date-passed")


def stale_act_refusal(row: dict[str, Any],
                      signed_ingested: frozenset[str] | set[str] = frozenset()
                      ) -> str | None:
    """Why this ledger row may NOT ride the stale-act lane, or None.

    `signed_ingested` is accepted for a uniform belt signature (`lane_refusal`
    calls every belt the same way) and IGNORED here on purpose: this lane
    carries no substance gate, so there is nothing for the RULE 1 escape to
    open. It archives `act` threads the world moved past, a different case.
    """
    if row.get("noise_signal") != _STALE_ACT_SIGNAL:
        return None
    if row.get("verdict") != "act":
        return (f"`{_STALE_ACT_SIGNAL}` on a `{row.get('verdict')}` row — the "
                "lane archives ACTIONABLE threads the world moved past, and a "
                "read or noise thread has its own lane and its own evidence")
    st = row.get("stale")
    if not isinstance(st, dict) or st.get("is_stale") is not True:
        return (f"`{_STALE_ACT_SIGNAL}` on a row whose `stale` field is {st!r} "
                "— the signal and the field are one claim; a signal with no "
                "field behind it is a word with no producer")
    if st.get("reason") not in _STALE_REASONS:
        return (f"stale reason {st.get('reason')!r} is outside "
                f"{list(_STALE_REASONS)} — the checks key on the WORD, so an "
                "invented one is invisible to them")
    # THE DRAFT CENSUS, RE-SCREENED OFF THE ROW (review 2026-08-25, finding 5).
    # Belt 1 asks the batch context (`thread_carries_draft`, the union of this
    # census and the lane's own undo ledgers); this half asks the LEDGER ROW's
    # own `isDraft`, which `cos_driver_accounting._ledger_row` stamps from the
    # read pass's Drafts-folder enumeration. Two readers, two inputs, one rule:
    # work in progress is never archived, however confident.
    if row.get("isDraft") is True:
        return ("the row's own draft census reports an unsent draft on this "
                "thread — work in progress is never archived")
    if not row.get("body_opened"):
        return ("the stale-act lane needs the action screens to have RUN, and "
                "this row's body never opened — 'the world moved on' over "
                "screens that did not run is a guess, not a finding")
    # AND THIS BELT STOPS HERE, ON PURPOSE. Belt 1 also refuses a body that
    # opened and came back UNREADABLE (`unreadable_body_refusal`); this half
    # CANNOT, and the reason is a fact about the ledger rather than a choice:
    # the extraction result lives on the CORPUS row, and `_ledger_row` stamps
    # neither it nor a readability verdict — `body_open_outcome` is `None`
    # whenever the body opened at all, protected or not. Screening it here
    # would mean inventing the answer from `body_chars`, which is a second
    # producer for a sentence that already has one, and the drift the one-
    # producer rule exists to prevent. UPGRADE PATH: stamp the pair
    # `cos_signals_body.readable_newest_text` already computes onto the ledger
    # row, and this belt gains the clause by reading it.
    return None

"""The AGED-READ lane's PLANNER belt (v7.3, AGED-01) — belt 2 of three.

Split out of `cos_mutate_plan.py` at the 500-LOC bound. It re-screens what
the LEDGER ROW itself can prove, and nothing else.
"""
from __future__ import annotations

from typing import Any
import datetime as _dt

from cos_mutate_gates import _utcnow  # noqa: E402


#: (v7.3, AGED-01) The owner's floor, re-stated here on purpose. This is the
#: SECOND belt: `cos_judge_rules_aged.aged_read_refusal` already refused the
#: claim at judgment time off the batch context, and this re-screens what the
#: LEDGER ROW itself can prove, independently, exactly as the unread/tier/
#: verdict floors above are re-screened. The two belts see different evidence
#: — the judge half sees the action screens, this half sees only the row — so
#: neither is a copy of the other, and a bug in one does not silently pass the
#: other.
#:
#: `0` is the owner's live setting (2026-07-26, re-affirmed 2026-07-31: "if
#: it's read it's game") and means NO AGE GATE. It must never be coerced back
#: to a default by an absent/falsy check. The three belts each carry their own
#: copy on purpose; keep them equal.
_AGED_READ_SIGNAL = "aged-read-no-action"
_AGED_READ_MIN_DAYS = 0


def aged_read_refusal(row: dict[str, Any]) -> str | None:
    """Why this ledger row may NOT ride the aged-read lane, or None."""
    if row.get("noise_signal") != _AGED_READ_SIGNAL:
        return None
    if not row.get("body_opened"):
        return ("the aged-read lane needs the action screens to have RUN, and "
                "this row's body never opened — 'no action' over screens that "
                "did not run is a guess, not a finding")
    received = row.get("received")
    if not received:
        return ("the aged-read lane needs the server's `received`, and this row "
                "carries none — a row with no age is malformed, not merely young")
    # PARSE THE WHOLE TIMESTAMP, and compare it to an instant in the SAME
    # frame. This read `_utcnow().date() - date.fromisoformat(received[:10])`
    # until 2026-08-24. The server sends an offset-aware `received`
    # (`2026-08-16T07:08:23+01:00` on the live ledgers), so slicing off the
    # first ten characters takes its LOCAL calendar date and subtracts it from
    # a UTC one. East of Greenwich, a thread that arrives between midnight and
    # the offset measures -1 day and the belt refuses it as "the thread is -1
    # day(s) old and the owner's floor is 0 day(s)" — blaming the floor the
    # owner set to zero precisely so nothing would be held on age. The two
    # sibling readers (`cos_judge_rules._age_days`,
    # `cos_mutate_gates._within_recency`) already parse the whole stamp; this
    # was the one that did not. `max(0, ...)` follows `_age_days`: a `received`
    # ahead of the clock is skew, and skew is never a floor violation.
    try:
        when = _dt.datetime.fromisoformat(str(received))
    except (TypeError, ValueError):
        return f"unparseable `received` {received!r} — the age cannot be measured"
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    age = max(0, (_utcnow() - when).days)
    if age < _AGED_READ_MIN_DAYS:
        return (f"the thread is {age} day(s) old and the owner's floor is "
                f"{_AGED_READ_MIN_DAYS} day(s)")
    return None

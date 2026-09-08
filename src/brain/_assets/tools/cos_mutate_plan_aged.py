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


def aged_read_refusal(row: dict[str, Any],
                      signed_ingested: frozenset[str] | set[str] = frozenset(),
                      *, archive_over_draft: bool = False
                      ) -> str | None:
    """Why this ledger row may NOT ride the aged-read lane, or None.

    `signed_ingested` is the set of conversation ids whose OWN candidate the
    vault has already SIGNED (`cos.signed_ingested_catching_up`, the same set
    the `Brainiac · Ingested` chip and E4 use). It is the escape the substance
    gate below was written to take (RULE 1 follow-up): a thread whose substance
    is already in the vault has satisfied "ingested back" and may archive even
    when tonight's judge re-marks it `held`.

    `archive_over_draft` is the owner's `overlay/cos/auto-archive.md` lever
    (ruling 2026-09-02), read by `cos_mutate_gates.kill_switch` and threaded in
    from `build_plan`. It defaults False here, so a caller that forgets it gets
    the pre-ruling refusal — the safe direction. It waives ONLY the draft
    clause below; every other clause (body opened, age, RULE 1 substance) still
    binds, and the unread shield and what is left of the P0 floor sit outside
    this belt in `screen_ledger_rows`. Since 2026-09-04 that floor no longer
    reaches a `read` verdict at all — a P0 informational thread archives like
    any other (`cos_chips.p0_floor_refuses`).
    """
    if row.get("noise_signal") != _AGED_READ_SIGNAL:
        return None
    # THE DRAFT CENSUS, RE-SCREENED OFF THE ROW (review 2026-08-25, finding 5).
    # Belt 1 asks the batch context (`thread_carries_draft`, the union of this
    # census and the lane's own undo ledgers); this half asks the LEDGER ROW's
    # own `isDraft`, which `cos_driver_accounting._ledger_row` stamps from the
    # read pass's Drafts-folder enumeration. Two readers, two inputs, one rule:
    # work in progress is never archived, however confident.
    if row.get("isDraft") is True and not archive_over_draft:
        return ("the row's own draft census reports an unsent draft on this "
                "thread — work in progress is never archived")
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
    # (RULE 1, 2026-08-30) THE SUBSTANCE GATE. The owner's HARD invariant: an
    # informational thread is archived ONLY AFTER its substance is in the vault
    # ("always and only if everything was ingested back"). So a `read` thread
    # rides the aged-read lane ONLY when the ingest pass affirmatively found
    # NOTHING to vault — `disposition == "no-substance"`. A `held` thread
    # (substance suspected but unconfirmed — the under-detection limbo) or a
    # `candidate` (vault-worthy) WAITS: archiving it now would drop it from the
    # inbox with no vault copy. This is the FIRST belt that can screen it —
    # `disposition` is a judge OUTPUT written by `mark_candidates`, AFTER belt 1
    # (`cos_judge_rules_aged.aged_read_refusal`) runs in `accept_verdicts`, so
    # belt 1 sees it unset; this belt reads the finished ledger row. Belt 3
    # (`cos_runverify_join._aged_read_bad`) re-checks it as a soundness backstop.
    #
    # SHIPPED 2026-08-31 (RULE 1 follow-up). The gate clears on EITHER proof
    # the substance is safe: the ingest pass found nothing to vault
    # (`no-substance`), OR the vault already holds a SIGNED copy of it
    # (`signed_ingested`). The signature never lands the night that offered the
    # candidate — a LATER maintenance drain signs it — so this is the two-run
    # drain the owner's Rule 1 describes: run N vaults a `candidate` and holds
    # it, the drain signs it, run N+1 sees it in `signed_ingested` and archives
    # it. It was a no-op while the vault was empty; runs 216-221 filled it. A
    # thread the judge re-marks `held` still WAITS unless its substance is
    # already signed. Belt 3 (`cos_runverify_join._aged_read_bad`) carries the
    # identical escape so the two never disagree.
    disp = str(row.get("disposition") or "").strip().lower()
    cid = str(row.get("conversation_id") or "")
    if disp != "no-substance" and cid not in signed_ingested:
        return ("the ingest pass did not clear this thread as `no-substance` "
                f"(disposition={disp or 'unset'!r}) and the vault holds no signed "
                "copy of its substance yet — the owner's Rule 1 archives a read "
                "thread only after its substance is vaulted, so a held or "
                "vault-worthy thread waits for the vault, never archived blind")
    return None

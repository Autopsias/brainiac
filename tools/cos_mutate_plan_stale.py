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
_OWNER_REPLIED_SIGNAL = "owner-replied-archived"


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


# -- the two DRAFT holds (owner's first marks, 2026-09-07) -------------------
# Of 16 threads he marked, 7 drafts were "too late to answer" (the newest
# message was 32-53 days old at draft time) and the rest of that night's
# drafts sat at 3-25 days. Both facts below are HOST facts off the run's own
# artifacts — the ledger row's `received` and the night's sent baseline — so
# the judge cannot talk its way past either. Neither archives anything: a
# withheld draft leaves the thread exactly where it was, the safe direction.
DRAFT_MAX_AGE_ENV = "COS_DRAFT_MAX_AGE_DAYS"
DEFAULT_DRAFT_MAX_AGE_DAYS = 30


def draft_max_age_days() -> int:
    import os                                                    # noqa: PLC0415
    raw = os.environ.get(DRAFT_MAX_AGE_ENV, "") or ""
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_DRAFT_MAX_AGE_DAYS


def _when(value: Any):
    import datetime as _dt                                       # noqa: PLC0415
    try:
        when = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=_dt.timezone.utc)


def owner_sent_by_conversation(vault, run_id: str) -> dict[str, str]:
    """Newest sent-item timestamp per conversation, off THIS night's sent
    baseline (`_cos_sent_baseline_<run>.json`, written by the read pass before
    the plan is built). Missing or malformed ⇒ {} — the hold degrades to off,
    it never stops a night."""
    import json                                                  # noqa: PLC0415
    from brain import cos                                        # noqa: PLC0415
    out: dict[str, str] = {}
    try:
        path = cos.run_ops_dir(vault) / f"_cos_sent_baseline_{run_id}.json"
        items = json.loads(path.read_text(encoding="utf-8")).get("items") or []
    except (OSError, ValueError, AttributeError):
        return out
    for it in items:
        cid, ts = it.get("conv_id"), it.get("timestamp")
        if cid and ts and str(ts) > out.get(cid, ""):
            out[cid] = str(ts)
    return out


def owner_replied_last(row: dict[str, Any],
                       sent_by_cid: dict[str, str]) -> bool:
    """Is the owner's own newest sent reply NEWER than the thread's newest
    message? (owner ruling 2026-09-09, "1".)

    ONE DEFINITION, TWO READERS. `cos_judge_night.load_night` stamps this onto
    every ledger row so `archive_eligibility` can name the signal, and
    `owner_replied_refusal` re-reads the stamp as belt 2. Both call THIS
    function so the judge leg and the plan leg cannot disagree about the same
    night — the same reason `kill_switch` is read once per night.

    UNKNOWN EITHER SIDE ⇒ False, and that is the safe direction: a thread with
    no parseable date, or a conversation the sent window never covered, stays
    in the inbox exactly as it does today. The window is 30 days
    (`DEFAULT_SENT_WINDOW_HOURS`), so a reply older than that is invisible here
    and the thread keeps whatever lane it already had.
    """
    received = _when(row.get("received"))
    sent = _when(sent_by_cid.get(str(row.get("conversation_id"))))
    return bool(received is not None and sent is not None and sent > received)


def owner_replied_refusal(row: dict[str, Any],
                          signed_ingested: frozenset[str] | set[str] = frozenset()
                          ) -> str | None:
    """Why this ledger row may NOT ride the owner-replied lane, or None.

    `signed_ingested` is accepted for the uniform belt signature and IGNORED,
    as it is on the stale-act lane: this lane carries no substance gate. The
    ingest decision rides ABOVE the archive decision (`judged_row` writes
    `ingest` before `auto_archive`), so a thread whose substance is worth
    keeping is still ingested on the night it archives.

    WHAT THIS BELT CAN AND CANNOT SEE. The stamp is the fact belt 1 acted on,
    so re-reading it is not a second opinion about the DATES — it is the check
    that the signal was not written without the fact behind it, which is the
    failure mode a stamped-signal lane actually has. The independent evidence
    this half adds is the row's own `isDraft` census: an unsent draft on the
    thread is work in progress, and work in progress is never archived, however
    certain the reply is.
    """
    if row.get("noise_signal") != _OWNER_REPLIED_SIGNAL:
        return None
    if row.get("owner_replied_last") is not True:
        return (f"`{_OWNER_REPLIED_SIGNAL}` on a row whose own "
                "`owner_replied_last` stamp is "
                f"{row.get('owner_replied_last')!r} — the signal and the fact "
                "are one claim, and a signal with no fact behind it is a word "
                "with no producer")
    if row.get("isDraft") is True and not row.get("porter_drafted"):
        return ("the row's own draft census reports an unsent draft this lane "
                "never wrote — the OWNER'S work in progress, and archiving the "
                "thread under it would hide his own unfinished reply")
    return None


def screen_stale_drafts(planned: list[dict[str, Any]],
                        received_by_cid: dict[str, Any], exclude,
                        *, sent_by_cid: dict[str, str], now,
                        max_age_days: int) -> list[dict[str, Any]]:
    """Drop a planned draft the owner has said he does not want: one on a
    thread whose newest message is older than the window, or one he already
    answered himself (a sent item newer than that message). Unknown date ⇒
    KEPT, same rule as `_within_window`."""
    kept: list[dict[str, Any]] = []
    for m in planned:
        cid = m.get("conversation_id")
        received = _when(received_by_cid.get(cid)) if m.get("verb") == "draft" \
            else None
        if received is None:
            kept.append(m)
            continue
        age = (now - received).days
        sent = _when(sent_by_cid.get(cid))
        if age >= max_age_days:
            exclude(cid, "draft", f"the newest message is {age} days old, past "
                                  f"the {max_age_days}-day draft window — a "
                                  "reply this late is not wanted (owner marks "
                                  "2026-09-07)")
        elif sent is not None and sent > received:
            exclude(cid, "draft", "the owner's own sent reply is newer than the "
                                  "thread's newest message — he already "
                                  "answered it")
        else:
            kept.append(m)
    return kept

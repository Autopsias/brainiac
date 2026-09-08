"""The TWO PENS that write the owner-feedback record (FB-02) + the confirmation
detector the rule ranking needs (FB-03).

`feedback.py` froze WHAT a row is. This module is what turns two owner acts into
rows, and neither of them is the owner typing at a prompt:

  PEN 1 — THE SHEET. The owner opens the morning sheet, marks threads
  right/wrong/missed, writes a standing rule beside one, ticks `revoke` on a
  rule they are done with, and saves. `brain cos-feedback --from-sheet <html>`
  parses the page's embedded state and appends what the marks say.

  PEN 2 — OUTLOOK. The owner never opens the sheet and just drags a thread back
  out of Archive, or peels a chip off. The next run's ENUMERATION sees the
  mailbox and the previous undo ledgers say what the porter did to it; the
  difference is an owner ruling nobody typed.

THE ONE RULE PEN 2 MUST NOT GET WRONG. A conversation is back in the Inbox for
two completely different reasons: the owner pulled it back, or somebody REPLIED
and the thread came back on its own. The first is a reversal and mints a
permanent do-not-touch; the second is NEW WORK, and minting a do-not-touch for
it would silently retire a live thread forever. So a ruling is minted only when
the thread's message set is UNCHANGED — the newest message in the conversation
is no newer than the mutation being judged. Unparseable or missing timestamps on
either side are treated as new work, never as agreement: an unreadable input is
not the absence of evidence.

Imports flow one way: this module reads `feedback`, `feedback_render` and
`feedback_sheet`, and none of them reads it.
"""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._layout import _parse_ts, _utcnow
from .feedback import (ACTION_FOR_VERB, DO_NOT_TOUCH_VERDICT,
                       SHEET_STATE_ELEMENT_ID, WANTED_MORE_VERDICT,
                       FeedbackRowInvalid, feedback_path, read_record,
                       record_thread_ruling, subject_digest, thread_rows)
from .feedback_marks import record_marks
from .feedback_sheet import validate_sheet_state
from .sheet_marks import _is_marked, marks_from_state

#: The undo-ledger states that PROVE the porter's mutation landed. `sent` and
#: `unknown` are deliberately absent: a mutation whose outcome nobody confirmed
#: may never have reached the server, so a thread sitting in the Inbox next to
#: one of those rows is not evidence the owner reversed anything.
APPLIED_STATES = ("reconciled", "confirmed")

#: WHY A RETURNING THREAD MINTED NOTHING, as a CODE rather than only prose.
#: The lane's whole output for 64 nights was a sentence, so "how often does each
#: branch fire" could only be answered by matching substrings — and the FB-01
#: backtest (`tools/cos_moveback_backtest.py`) had to do exactly that. A code
#: per branch is what makes the next review a count.
NEW_WORK_NEWER_MESSAGE = "newer-message"
NEW_WORK_UNREADABLE_ACTION_TS = "unreadable-action-ts"
NEW_WORK_UNREADABLE_RECEIVED = "unreadable-received"
NEW_WORK_REASON_CODES = (NEW_WORK_NEWER_MESSAGE, NEW_WORK_UNREADABLE_ACTION_TS,
                         NEW_WORK_UNREADABLE_RECEIVED)

#: The undo-ledger verbs this diff can observe from an INBOX enumeration, and
#: what each one's reversal looks like. `draft` is absent on purpose: a draft
#: lives in the Drafts folder, which pass 1 never enumerates, so an owner who
#: deletes one leaves no trace here. Naming the ceiling beats a lane that
#: silently sees nothing.
OBSERVABLE_VERBS = ("archive", "categorize")

_STATE_RE = re.compile(
    r"<script[^>]*\bid=[\"']" + re.escape(SHEET_STATE_ELEMENT_ID)
    + r"[\"'][^>]*>(.*?)</script>", re.S | re.I)


# --------------------------------------------------------------------------
# PEN 1 — the sheet
# --------------------------------------------------------------------------
def sheet_state_from_html(html: str) -> dict[str, Any]:
    """The `<script id="cos-sheet-state">` payload of a SAVED sheet, validated.

    REFUSES rather than degrades, in all three ways it can fail — no element, no
    JSON, wrong shape. A sheet whose state cannot be found is not a sheet on
    which the owner marked nothing: it is a page this pen cannot read, and
    returning an empty state would record that silence as agreement.

    `validate_sheet_state` is the SAME function s07 calls before it writes the
    page. A shape checked only at write time still lets a truncated or
    hand-edited page look like an owner who marked nothing.
    """
    m = _STATE_RE.search(str(html))
    if m is None:
        raise FeedbackRowInvalid(
            f"no <script id={SHEET_STATE_ELEMENT_ID!r}> element in this page — "
            "a sheet whose state cannot be found is not a sheet with no marks")
    try:
        state = json.loads(m.group(1))
    except ValueError as exc:
        raise FeedbackRowInvalid(
            f"the sheet state did not parse ({exc}) — an unreadable page is "
            "not an empty one") from None
    return validate_sheet_state(state)


def record_from_sheet(vault: Any, html: str, *,
                      now: _dt.datetime | None = None) -> dict[str, Any]:
    """PEN 1, ARTIFACT TRANSPORT: one saved sheet page -> record rows.

    The Artifact route is KEPT and is now a transport rather than the only
    path: the page republishes itself with the owner's marks inside its own
    embedded state, and this reads them out. What a mark MEANS is decided in
    one place for both routes — `sheet_marks.marks_from_state` projects the
    page's state into the same payload the downloaded file carries, and
    `feedback_marks.record_marks` files it.

    THE OLD CEILING IS CLOSED. This pen used to say, in as many words, that
    reading the same saved sheet twice appended the marks twice, and that an
    unattended caller would need "a consumed-sheet marker keyed on the state's
    `generated_at`". That marker now exists: the sheet carries a deterministic
    `sheet_id` built from its date, its build stamp and its runs, and a second
    read of the same sheet is REFUSED rather than filed. Rebuilding the night
    produces a different sheet and a different id, which is correct — those are
    different sheets and their marks are different marks.
    """
    state = sheet_state_from_html(html)
    unmarked = sum(1 for row in state["threads"] if not _is_marked(row))
    payload = marks_from_state(state, transport="artifact")
    result = record_marks(vault, payload,
                          labels=tuple(state["label_vocabulary"]) or None,
                          rows_unmarked=unmarked, now=now)
    return result


# --------------------------------------------------------------------------
# PEN 2 — the Outlook diff
# --------------------------------------------------------------------------
def applied_mutations(
    ledger_rows: list[dict[str, Any]],
    *,
    verbs: tuple[str, ...] = OBSERVABLE_VERBS,
) -> dict[tuple[str, str], dict[str, Any]]:
    """The LATEST landed mutation per (conversation, verb), newest `action_ts`.

    Latest-wins and never file order: the undo ledgers are named
    `<date>-run<N>` and `run99` sorts after `run124` inside one day
    (`cos_mutate_ledger.threads_already_drafted` learned this the same way).
    Outlook's diff uses the observable default; other read-only consumers may
    name a wider closed verb set while retaining this one landed-fact fold.
    """
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in ledger_rows:
        cid = str(row.get("conversation_id") or "")
        verb = str(row.get("verb") or "")
        if not cid or verb not in verbs:
            continue
        if row.get("state") not in APPLIED_STATES:
            continue
        held = latest.get((cid, verb))
        if held is None or str(row.get("action_ts") or "") >= str(
                held.get("action_ts") or ""):
            latest[(cid, verb)] = row
    return latest


def _reversal_signal(row: dict[str, Any], enum: dict[str, Any],
                     chip_tier: dict[str, str]) -> str | None:
    """What the mailbox says about ONE landed mutation, or `None` for nothing.

    `enum` is tonight's INBOX enumeration row for the same conversation — its
    presence is already the archive signal, since an archived thread is not in
    the Inbox to be enumerated. The chip signal needs the two vocabularies
    joined: the undo ledger records the chip NAME it wrote (`P1 · Today`) and
    the enumeration reports the TIER it observes (`P1`), so the comparison goes
    through the driver's own `CHIP_TIER` rather than a second spelling here.
    """
    verb = str(row.get("verb"))
    if verb == "archive":
        return "the thread is back in the Inbox"
    if str(row.get("mode") or "") != "add":
        # The mutation lane only ever ADDS a chip today. A future removal mode
        # would make "the chip is gone" the expected outcome, not a reversal.
        return None
    wrote = str(row.get("chip") or "")
    if wrote not in chip_tier:
        # The ingestion mark and anything else outside the four priority chips
        # is invisible to the enumeration, whose `chip` field is the TIER.
        return None
    observed = enum.get("chip")
    if observed == chip_tier[wrote]:
        return None
    return (f"the {wrote} chip is gone (the thread now reads "
            f"{observed or 'no managed chip'})")


def outlook_reversals(ledger_rows: list[dict[str, Any]],
                      enumerated: list[dict[str, Any]], *,
                      chip_tier: dict[str, str],
                      recorded: dict[str, str] | None = None
                      ) -> dict[str, Any]:
    """PURE: which landed mutations the owner undid by hand, and which did not.

    THE MESSAGE-SET GUARD IS THE WHOLE POINT (fb-02). A conversation back in the
    Inbox is a reversal only when NOTHING NEW ARRIVED in it since the mutation:
    the newest message the enumeration reports (`received`, which
    `cos_driver_draw.conversations` takes from the newest message in the thread)
    must be no newer than the mutation's own `action_ts`. A newer message means
    somebody replied, the thread is back on its own, and it is NEW WORK — it
    re-enters tonight's draw like any other Inbox row and mints no permanent
    do-not-touch.

    An unparseable or missing timestamp on either side lands in `new_work` too,
    with its own reason. A pen that treats a blank stamp as "nothing arrived"
    would mint the strongest possible ruling on the weakest possible evidence.

    `recorded` is `{conversation_id: newest outlook-ruling ts}` — how this pen
    stays quiet on the second night. Once the owner pulls a thread back it stays
    in the Inbox forever, so without it every subsequent enumeration would mint
    the same ruling again. A LATER mutation on the same thread (a bigger
    `action_ts`) is a new act and can be reversed again.
    """
    recorded = recorded or {}
    by_cid = {str(r.get("conversation_id") or ""): r for r in enumerated}
    out: dict[str, Any] = {"reversals": [], "new_work": [],
                           "already_recorded": [], "considered": 0}
    for (cid, verb), row in sorted(applied_mutations(ledger_rows).items()):
        enum = by_cid.get(cid)
        if enum is None:
            continue
        signal = _reversal_signal(row, enum, chip_tier)
        if signal is None:
            continue
        out["considered"] += 1
        acted = str(row.get("action_ts") or "")
        item = {"conversation_id": cid, "verb": verb, "signal": signal,
                "action_taken": ACTION_FOR_VERB[verb], "action_ts": acted,
                "run": str(row.get("run") or ""),
                "subject": str(enum.get("subject") or ""),
                "received": str(enum.get("received") or "")}
        if recorded.get(cid, "") >= acted and acted:
            out["already_recorded"].append(item)
            continue
        code, why = _new_work_reason(acted, item["received"])
        if why:
            out["new_work"].append(dict(item, reason=why, reason_code=code))
        else:
            out["reversals"].append(item)
    return out


def _new_work_reason(action_ts: str, received: str) -> tuple[str, str]:
    """`("", "")` when the message set is unchanged; otherwise the reason CODE
    and the prose for why this is new work.

    THE EVENT KEY IS ALREADY ON THE ITEM, and that matters more than it looks.
    Measured over 64 nights (`tools/cos_moveback_backtest.py`, as of
    `2026-09-05-run263`): 435 reported observations are 18 conversations and 21
    distinct `(conversation_id, action_ts, received)` triples — three threads
    account for 192 of them by being re-reported unchanged every night for ten
    days. The lane is not seeing 435 events; it is seeing 21 and re-saying them.
    Both stamps ride the item, so a review can fold on that triple; the raw
    count must never be read as an event count.
    """
    acted = _parse_ts(action_ts)
    newest = _parse_ts(received)
    if acted is None:
        return (NEW_WORK_UNREADABLE_ACTION_TS,
                f"the undo row carries no readable action_ts ({action_ts!r}) — "
                "an unreadable stamp is not proof nothing arrived")
    if newest is None:
        return (NEW_WORK_UNREADABLE_RECEIVED,
                f"the enumeration carries no readable received time "
                f"({received!r}) — an unreadable stamp is not proof nothing "
                "arrived")
    if newest > acted:
        return (NEW_WORK_NEWER_MESSAGE,
                f"a newer message arrived at {received}, after the porter "
                f"acted at {action_ts} — new work, not an owner reversal")
    return "", ""


def outlook_rulings_recorded(vault: Any) -> dict[str, str]:
    """`{conversation_id: newest ts}` over the record's OUTLOOK thread rulings.

    The producer of `outlook_reversals`' `recorded` argument, so the "do not
    mint this again tomorrow" fact has one definition rather than one per
    caller.
    """
    seen: dict[str, str] = {}
    for row in thread_rows(read_record(vault)["rows"]):
        if row.get("source") != "outlook":
            continue
        cid = str(row.get("conversation_id") or "")
        ts = str(row.get("ts") or "")
        if ts > seen.get(cid, ""):
            seen[cid] = ts
    return seen


def record_outlook_reversals(vault: Any, ledger_rows: list[dict[str, Any]],
                             enumerated: list[dict[str, Any]], *,
                             chip_tier: dict[str, str],
                             now: _dt.datetime | None = None
                             ) -> dict[str, Any]:
    """PEN 2: run the diff and append a thread ruling for each real reversal.

    Only `reversals` are written. `new_work` is REPORTED — it rides the
    enumeration evidence so the night says out loud which threads came back
    because somebody replied — and mints nothing.
    """
    diff = outlook_reversals(ledger_rows, enumerated, chip_tier=chip_tier,
                             recorded=outlook_rulings_recorded(vault))
    for item in diff["reversals"]:
        record_thread_ruling(
            vault, source="outlook", conversation_id=item["conversation_id"],
            subject_sha256=subject_digest(item["subject"]),
            action_taken=item["action_taken"], verdict="wrong",
            note=item["signal"], run=item["run"], now=now)
    return {"path": str(feedback_path(vault)),
            "recorded": len(diff["reversals"]),
            "reversals": [{k: v for k, v in i.items() if k != "subject"}
                          for i in diff["reversals"]],
            "new_work": [{k: v for k, v in i.items() if k != "subject"}
                         for i in diff["new_work"]],
            "already_recorded": len(diff["already_recorded"]),
            "considered": diff["considered"]}


# --------------------------------------------------------------------------
# what the sheet needs back from the record
# --------------------------------------------------------------------------
def overturned_last_time(vault: Any, *, before: str = "") -> list[dict[str, Any]]:
    """The `overturned_last_time` block s07 renders — s05 supplies it (§7).

    Every ``wrong``/``missed`` thread ruling from the MOST RECENT day the record
    carries one, which is the night the owner last overturned the porter. A
    later ``right`` release lifts an old hold but is not itself an overturn.
    Hashes only: the block the sheet renders from this carries the digest, and
    s07 joins it to tonight's own display subjects if it wants text.
    """
    rows = [r for r in thread_rows(read_record(vault)["rows"])
            if r.get("verdict") in (DO_NOT_TOUCH_VERDICT, WANTED_MORE_VERDICT)
            and (str(r.get("ts") or "") < before or not before)]
    if not rows:
        return []
    day = max(str(r.get("ts") or "")[:10] for r in rows)
    return sorted((r for r in rows if str(r.get("ts") or "")[:10] == day),
                  key=lambda r: str(r.get("ts")))


__all__ = ['APPLIED_STATES', 'NEW_WORK_REASON_CODES',
           'OBSERVABLE_VERBS', 'sheet_state_from_html',
           'record_from_sheet', 'applied_mutations', 'outlook_reversals',
           'outlook_rulings_recorded', 'record_outlook_reversals',
           'overturned_last_time']

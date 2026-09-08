#!/usr/bin/env python3
"""STALE-01 — the two HOST facts the stale-act archive lane rests on.

A sibling of `cos_signals`, split out for the same reason `cos_judge_rules_aged`
was split out of `cos_judge_rules`: the 500-LOC bound. It obeys the same rule as
its parent — **the model may not certify its own input.** Everything here is
computed from the run's own captured message data and the driver's enumeration;
nothing here reads a model answer.

THE TWO FACTS

``deadline_passed`` — the thread names a due date and that date is BEHIND the
run. It is the exact complement of `cos_signals.live_deadline` and shares its
whole machinery (a deadline WORD plus a parseable date within `_DEADLINE_REACH`
characters of it, refusing an ambiguous numeric date whose two readings
straddle the run date). A date with no deadline word is a meeting; a deadline
word with no date is missed, and that is a false negative, which costs a thread
that stays in the owner's queue rather than one that leaves it.

``last_sender_is_owner`` — whether the newest message in the thread came from
the owner's own address.

WHAT THE SECOND ONE CAN AND CANNOT DO, STATED. The owner's address is not a
fact this pipeline has ever carried: no overlay setting names it, the run
manifest does not record it, and the driver's `sentitems` enumeration drops
`ConversationId` so a sent reply cannot be joined to a thread (see
`cos_signals`'s own "WHAT THIS FILE CANNOT SEE"). ``OWNER_ADDRESSES`` is
therefore an OPTIONAL knob, and when it is unset this fact is ``False`` for a
NAMED reason (`owner_address_known` is False beside it) rather than silently
asserting the newest message is not his. That is honest in the direction that
matters: the lane's refusals do not lean on it. What actually stands in its
place is `unanswered_direct_ask` — if the host still sees an ask standing in
the newest message's own words, "someone else already answered it" is refuted
by evidence the model does not control, whether or not an address is
configured.

UPGRADE PATH: an `overlay/cos/identity.md` setting read the way
`cos_ground_tenants.load_tenant_domains` reads `tenant-domains.md`, or
`ConversationId` carried on the `sentitems` enumeration — the one change that
would make an owner reply joinable to its thread.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cos_signals  # noqa: E402  the parent module; every marker list lives there
from cos_signals_body import readable_newest_text  # noqa: E402

#: The owner's own mail addresses, lowercased, from the environment. Empty is
#: the default and means "not configured" — never "the owner sent nothing".
OWNER_ADDRESSES_ENV = "BRAIN_COS_OWNER_ADDRESSES"


def owner_addresses() -> frozenset[str]:
    return frozenset(
        a.strip().casefold()
        for a in (os.environ.get(OWNER_ADDRESSES_ENV) or "").split(",")
        if a.strip())


def deadline_passed(*, subject: str | None, new_text: str, now: _dt.date
                    ) -> tuple[bool, str | None]:
    """`(fact, leg)` — a stated due date that is BEHIND the run.

    The mirror image of `cos_signals.live_deadline`, clause for clause, so the
    two cannot drift into disagreeing about the same sentence: same folded
    text, same marker offsets, same `_DEADLINE_REACH`, same refusal of an
    ambiguous numeric date whose readings straddle `now`. Only the comparison
    at the end is inverted.

    AND THE `new_text` THE TWO SCAN IS ONE PRODUCER'S, which is what makes the
    paragraph above true rather than merely intended: it comes from
    `cos_signals_body.readable_newest_text`, because this module used to build
    the pair itself and dropped the corpus row's `extraction.error` (review
    2026-08-25, finding 2).
    """
    for leg, raw in (("body", new_text), ("subject", subject or "")):
        hay = cos_signals.fold(raw)
        spots = cos_signals._marker_offsets(hay, cos_signals._DEADLINE_WORDS)
        if not spots:
            continue
        for off, val in cos_signals._dates_in(hay, now.year):
            if val is None:
                continue
            if not any(abs(off - s) <= cos_signals._DEADLINE_REACH
                       for s in spots):
                continue
            if isinstance(val, tuple):
                # Ambiguous d/m vs m/d: only usable when both readings agree.
                if (val[0] < now) != (val[1] < now):
                    continue
                val = val[0]
            if val < now:
                return True, leg
    return False, None


def stale_signals_for_row(row: dict[str, Any], corpus_row: dict[str, Any], *,
                          now: _dt.date) -> dict[str, Any]:
    """The stale-lane facts for ONE thread. Reads no model answer.

    `unanswered_direct_ask` and `live_deadline` are NOT recomputed here — the
    per-row context already carries `cos_signals.signals_for_row`'s answers and
    the lane's refusal function reads them from there. Two producers for one
    sentence is the "one rule, one rumour" state.
    """
    prov = ((corpus_row or {}).get("provenance") or {})
    sender = str(prov.get("sender") or "").strip()
    # THE PARENT'S OWN PRODUCER, not a second copy of it. This function used to
    # rebuild the readable-text pair itself and dropped the corpus row's
    # `extraction.error`, so it parsed a deadline out of a body the parent had
    # already declared unreadable (review 2026-08-25, finding 2). One call, one
    # answer, and the module header's "cannot drift into disagreeing about the
    # same sentence" is now true by construction rather than by care.
    new_text, _unreadable = readable_newest_text(row, corpus_row)
    passed, leg = deadline_passed(subject=prov.get("subject"),
                                  new_text=new_text, now=now)
    known = owner_addresses()
    return {
        # THE KEY IS NAMESPACED, and the reason is a real collision.
        # `cos_judge_rules.RESOLUTIONS` maps the HOLD lane's
        # `resolution_evidence: "deadline-passed"` onto a context flag named
        # `deadline_passed` — one of the four GAP-04 flags nothing has ever
        # produced. Publishing this fact under that name would silently arm a
        # DIFFERENT lane's RESOLVED verdict as a side effect of shipping this
        # one, which is exactly the "one word, two meanings" drift the closed
        # vocabularies exist to prevent. The hold lane keeps its unproduced
        # flag; this lane names its own.
        "stale_deadline_passed": passed,
        "stale_deadline_passed_leg": leg,
        "last_sender": sender or None,
        "owner_address_known": bool(known),
        "last_sender_is_owner": bool(known) and sender.casefold() in known,
    }

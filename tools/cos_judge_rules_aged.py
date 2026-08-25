"""The AGED-READ archive lane (v7.3, AGED-01) — belt 1, the judge-side rule.

Split out of `cos_judge_rules.py` at the 500-LOC bound. It is one owner
ruling with one refusal function, so it splits cleanly; `cos_judge_rules`
re-exports both names, and `_r_signal` still calls the refusal.
"""
from __future__ import annotations

from typing import Any


#: (v7.3, AGED-01) THE AGED-READ LANE, from the owner ruling of 2026-07-26:
#: "yes let's drop that 7 day rule, if it's read it's game" — re-affirmed
#: 2026-07-31. This is the ONE signal that may ride a `read` bucket — "worth
#: your eyes, no action" is precisely the thread the ruling describes, and
#: `noise` cannot carry it without lying about what the thread is. Every
#: condition it adds is a SERVER or HOST fact the model does not control.
AGED_READ_SIGNAL = "aged-read-no-action"
#: The MINIMUM age in days a thread must reach. `0` means NO AGE GATE and is
#: the owner's live setting — never coerce it back to a default by an
#: absent/falsy check.
#:
#: The lane originally shipped at 7, from the SUPERSEDED 2026-07-17 ruling
#: ("older than one week ... I might have seen it but not really read it").
#: The owner retired that on 2026-07-26 and re-affirmed the retirement on
#: 2026-07-31, in `<vault>/overlay/cos/auto-archive.md`, which says in terms:
#: "Anything (a run, a doctrine edit, a reviewer) that reads a 7-day floor out
#: of the 2026-07-17 prose is reading a retired ruling." This lane did exactly
#: that on the day it shipped, and held 18 of 60 qualifying threads for nothing
#: but age — the same complaint that caused the 07-26 change (53 of 125 held on
#: age alone). Measured cause, corrected the same day.
#:
#: Every OTHER screen is untouched and is what stands in place of the buffer:
#: read-observed-never-set, the body actually opened, no unanswered ask, no
#: live deadline, no open commitment, no unsent draft, P0/P1 never archived.
AGED_READ_MIN_DAYS = 0


def aged_read_refusal(v: dict[str, Any],
                      ctx: dict[str, Any]) -> str | None:
    """(v7.3, AGED-01) The aged-read lane's SEVEN conditions, none model-controlled.

    The owner's 2026-07-26 ruling in machine form. Every clause below is a fact
    the driver read off the server or the host computed for itself, so a model
    that claims this signal on a thread that does not qualify is refused by
    evidence it cannot write:

    1. the mailbox says he READ it (`read_state`, the standing unread shield);
    2. it has reached `AGED_READ_MIN_DAYS` (`ask_age_days`, off `received`).
       That floor is 0 — the owner's live ruling is "if it's read it's game" —
       so this clause gates nothing today. It is kept because the knob is the
       one-line revert to a buffer, and because a missing or unreadable age is
       still a malformed row and refused on its own;
    3. the deterministic action screens actually RAN — which means the body
       opened. This is the clause that keeps the lane honest and the one that
       bounds it: "no action" asserted over screens that never ran is a guess
       about the owner's obligations, and `screens_ran_unresolved` is exactly
       the flag `cos_signals` sets for that. It costs coverage on purpose —
       measured on run 178, 42 of 91 otherwise-eligible threads had an opened
       body, so the lane converges over several nights instead of clearing the
       backlog blind in one;
    4-6. no unanswered direct ask, no live deadline, no open spine commitment —
       the three screens the ruling named ("checking if there is an action
       classifier on said email");
    7. no unsent draft on the conversation ("and a draft"), the same
       draft-protection idiom `hold.draft_protected_keeps` already uses.

    Tier (P2/P3) and the P0/P1 refusal are NOT repeated here — the blast-radius
    floor already enforces them for every signal, and a condition checked twice
    is a condition that can be relaxed in one place and look enforced.
    """
    if ctx.get("read_state") != "read":
        return (f"`{AGED_READ_SIGNAL}` claimed on a thread the mailbox reports "
                f"as {ctx.get('read_state') or 'unknown'} — the UNREAD SHIELD "
                "stands under every lane (DOCTRINE §2.2/§4.2)")
    age = ctx.get("ask_age_days")
    if age is None:
        return (f"`{AGED_READ_SIGNAL}` claimed on a thread carrying no age — "
                "`received` is the server fact this lane measures against, and "
                "a row without one is malformed, not merely young")
    if int(age) < AGED_READ_MIN_DAYS:
        return (f"`{AGED_READ_SIGNAL}` claimed on a thread {age} day(s) old — "
                f"the owner's floor is {AGED_READ_MIN_DAYS} day(s)")
    if ctx.get("screens_ran_unresolved"):
        return (f"`{AGED_READ_SIGNAL}` claimed on a thread whose body never "
                "opened, so the action screens could not run — 'no action' "
                "over screens that did not run is a guess about the owner's "
                "obligations, not a finding")
    for key, what in (("unanswered_direct_ask", "an unanswered direct ask"),
                      ("live_deadline", "a live deadline"),
                      ("open_spine_commitment", "an open commitment")):
        if ctx.get(key):
            return (f"`{AGED_READ_SIGNAL}` claimed on a thread carrying {what} "
                    "— the owner's ruling archives read mail he owes NOTHING on")
    cid = v.get("conversation_id")
    if cid in (ctx.get("drafts_inventory") or []) \
            and cid not in (ctx.get("expired_cos_draft_convids") or []):
        return (f"`{AGED_READ_SIGNAL}` claimed on a thread carrying an unsent "
                "draft — work in progress is never archived, however confident")
    return None


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
#: live deadline, no open commitment, no unsent draft. (The tier floor is
#: gone from this lane as of the 2026-09-04 ruling: a `read` thread archives at
#: any tier, P0 included. `cos_chips.p0_floor_refuses` is the one definition.)
AGED_READ_MIN_DAYS = 0


def unreadable_body_refusal(signal: str, ctx: dict[str, Any]) -> str | None:
    """THE SHARED FLOOR OF EVERY AUTO-ARCHIVE LANE: a body this run could not
    read justifies NOTHING. The reason, or None.

    IT IS NOT `screens_ran_unresolved` (review 2026-08-25, attempt 2, finding
    1). That flag means the body never OPENED. This means it opened and came
    back UNUSABLE — a rights-protected placeholder, an empty extraction — and
    `cos_signals_body.readable_newest_text` then hands every content screen an
    EMPTY STRING. So `unanswered_direct_ask`, `live_deadline` and
    `stale_deadline_passed` all read False on such a row, and every lane clause
    that refuses on a True reads their silence as agreement.

    MEASURED on one rights-protected row (`body_opened: True`,
    `body_chars: 120`, corpus text "Please send me the annex by Friday. Can you
    confirm?", `extraction.error: "rights-protected"`, run date 2026-08-25):
    READABLE, both lanes refuse it on the standing ask; UNREADABLE, BOTH accept
    it — the aged-read lane archives a `read` thread and the stale-act lane an
    `act` one, on the ABSENCE of evidence rather than evidence of absence. It
    was found on the stale lane and it was never the stale lane's bug: the
    blanking happens in the shared producer, so both lanes inherited it.

    ONE DEFINITION, STATED BY EACH LANE UNDER ITS OWN SIGNAL NAME — the same
    idiom both lanes already use for the unread shield, so a refusal names the
    lane that refused. It lives in THIS module because `cos_judge_rules_stale`
    imports this one and nothing imports it back: one home, no ring.

    `cos_judge_rules.first_failed_screen` already answers `Held · protected`
    for exactly this thread. The fact had a producer and a name all along; the
    archive lanes were the readers that never asked for it.
    """
    if not ctx.get("body_unreadable"):
        return None
    return (f"`{signal}` claimed on a thread whose body this run itself "
            "declared UNREADABLE — the ask detector and both deadline parses "
            "scan an EMPTY string on such a row, so their silence is the "
            "ABSENCE of evidence, not evidence of absence (`Held · protected` "
            "is what the documented screen order does with this thread)")


def _draft_refusal(cid: str | None, ctx: dict[str, Any]) -> str | None:
    """The aged-read lane's draft clause — one rule read off two sources.

    Lifted out of `aged_read_refusal` (2026-09-02) so that function keeps
    its length bound. Nothing about the rule changed in the move.
    """
    # TWO SOURCES, BOTH REAL. `thread_carries_draft` is the HOST's own answer
    # (`cos_judge_night.load_night`: the driver's `isDraft` census of tonight's
    # enumeration, unioned with this lane's own undo ledgers via
    # `cos_mutate_ledger.threads_already_drafted`). It was added because the
    # `drafts_inventory` clause below CANNOT FIRE — `load_night` publishes that
    # key as a literal `[]` and nothing has ever published
    # `expired_cos_draft_convids` at all, so DOCTRINE's promise that "the host
    # re-checks the drafts inventory" was a guard with no producer (review
    # 2026-08-25, finding 5). The old clause stays: it is what the golden
    # fixtures and a future real inventory drive, and OR-ing the two can only
    # refuse more, never less.
    # THE OWNER'S LEVER (ruling 2026-09-02), off `overlay/cos/auto-archive.md`
    # via `load_night`. It waives BOTH draft clauses together, because they are
    # two readings of one fact and honouring one while waiving the other would
    # refuse on whichever source happened to see the draft. Absent from ctx ⇒
    # falsy ⇒ the pre-ruling refusal, so an old fixture keeps its old verdict.
    # Every other clause above still binds, and the ruling is scoped to THIS
    # lane: `cos_judge_rules_stale` keeps its own draft refusal untouched.
    if not ctx.get("archive_over_draft"):
        if ctx.get("thread_carries_draft"):
            return (f"`{AGED_READ_SIGNAL}` claimed on a thread the host's own draft census "
                    "reports as carrying an unsent draft — work in progress is "
                    "never archived, however confident")
        if cid in (ctx.get("drafts_inventory") or []) \
                and cid not in (ctx.get("expired_cos_draft_convids") or []):
            return (f"`{AGED_READ_SIGNAL}` claimed on a thread carrying an unsent "
                    "draft — work in progress is never archived, however confident")
    return None


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
    3b. and the body it opened came back READABLE — `unreadable_body_refusal`,
       the floor both archive lanes share. A body that opened and returned a
       rights-protected placeholder blanks the text every clause below scans,
       so 4-6 all read False and agree with the model by default;
    4-6. no unanswered direct ask, no live deadline, no open spine commitment —
       the three screens the ruling named ("checking if there is an action
       classifier on said email");
    7. no unsent draft on the conversation ("and a draft"), the same
       draft-protection idiom `hold.draft_protected_keeps` already uses.

    Tier is NOT repeated here — the blast-radius floor already enforces what is
    left of it for every signal, and a condition checked twice is a condition
    that can be relaxed in one place and look enforced. Since 2026-09-04 what
    is left of it for THIS lane is nothing: a `read` verdict archives at any
    tier, P0 included (`cos_chips.p0_floor_refuses`).
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
    # 3b. AND THE BODY THAT OPENED CAME BACK READABLE. Clause 3 asks whether
    # the screens RAN; this asks whether they had anything to run ON. The three
    # content clauses immediately below all read False over a blanked body, so
    # without this one they agree with the model by default.
    blanked = unreadable_body_refusal(AGED_READ_SIGNAL, ctx)
    if blanked:
        return blanked
    for key, what in (("unanswered_direct_ask", "an unanswered direct ask"),
                      ("live_deadline", "a live deadline"),
                      ("open_spine_commitment", "an open commitment")):
        if ctx.get(key):
            return (f"`{AGED_READ_SIGNAL}` claimed on a thread carrying {what} "
                    "— the owner's ruling archives read mail he owes NOTHING on")
    draft = _draft_refusal(v.get("conversation_id"), ctx)
    if draft:
        return draft
    # (RULE 1, 2026-08-30) NOTE: the owner's substance gate — a read thread
    # archives only when the ingest pass cleared it as `no-substance` — is NOT
    # here on purpose. `disposition` is a judge OUTPUT set by `mark_candidates`,
    # which runs AFTER `accept_verdicts` (where this belt fires), so `v` carries
    # no disposition yet at this point; reading it here would refuse every
    # aged-read thread on an absent field. The gate lives in belt 2
    # (`cos_mutate_plan_aged.aged_read_refusal`, off the finished ledger row) and
    # is re-checked by belt 3 (`cos_runverify_join._aged_read_bad`).
    return None


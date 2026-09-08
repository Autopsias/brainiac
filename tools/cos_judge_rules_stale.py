"""The STALE-ACT archive lane (STALE-01) — belt 1, the judge-side rule.

Split out of `cos_judge_rules.py` at the 500-LOC bound, exactly as
`cos_judge_rules_aged` was. `cos_judge_rules` re-exports every name and its
`triage.autoarchive_blast_floor` / `triage.noise_signal_required` read the two
tables below rather than restating the lane list a third time.

THE RULING THIS ENCODES. "An actionable thread that is past its date, or that
someone else already answered, does not need the owner. The porter archives it
and names it on the sheet." So this is the ONE signal that may ride an `act`
bucket — and that is precisely why every clause below is a SERVER or HOST fact
the model does not control. `act` is the highest-blast-radius bucket there is:
it means the judge already decided the owner is owed something, and this lane
says the world moved on. It gets the strictest evidence, not the loosest.

TWO REASONS, AND THEY REFUSE ON DIFFERENT EVIDENCE

``answered-by-other`` — someone spoke after the ask and left nothing standing.
Refused unless the host's OWN ask detector agrees: `unanswered_direct_ask` is
the marker scan over the newest message's own words and the subject line
(`cos_signals`), and if it still sees an ask aimed at the owner, "someone
answered it" is a claim the run's own artifacts contradict.

``date-passed`` — the date the thread names is behind the run.
`cos_signals_stale.deadline_passed` is that fact (it reaches the context as
`stale_deadline_passed`, namespaced away from the hold lane's identically-named
resolution flag), and `live_deadline` must be
False beside it: a thread naming both a passed date and a live one is not past
its date.

Both then take the same five floors, which are the aged-read lane's and are
kept in the same shape so the two cannot drift: the UNREAD SHIELD, the body
actually opened (so the action screens RAN — "nothing owed" asserted over
screens that never ran is a guess), the body that opened came back READABLE
(`cos_judge_rules_aged.unreadable_body_refusal`, the ONE definition both lanes
state under their own signal name — a blanked body makes every content clause
below read False and agree with the model by default), no open spine
commitment, and no unsent draft (`thread_carries_draft`, the host's real
census — see `stale_field_refusal`). Tier is NOT re-checked here:
`triage.autoarchive_blast_floor` refuses P0 for every signal EXCEPT the
`read` bucket (ruling 2026-09-04 — the stale lane is `act`, so the floor
still binds here in full), and a
condition checked twice is one that can be relaxed in one place and look
enforced.

AND THE FIELD, NOT THE SIGNAL, IS WHAT TRIGGERS THE EVIDENCE CHECK. The host
archives an `act` row off `stale` alone (`archive_eligibility`), so the
refusal is split: `stale_act_refusal` is the signal-side entry point and
`stale_field_refusal` — reached from `triage.stale_evidence` for EVERY row
whose field claims staleness — is the evidence. Keying only on the signal left
the trigger unexamined whenever the model set `auto_archive: false` beside a
populated `stale` (review 2026-08-25, finding 1).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_judge_rules_aged import (  # noqa: E402
    AGED_READ_SIGNAL, aged_read_refusal, unreadable_body_refusal)

#: STALE-01. The typed signal an act thread archives under.
STALE_ACT_SIGNAL = "stale-act-archived"

#: The closed reason vocabulary of the `stale` field. A word outside it is
#: refused, never read as a variant.
STALE_ANSWERED = "answered-by-other"
STALE_DATE_PASSED = "date-passed"
STALE_REASONS = (STALE_ANSWERED, STALE_DATE_PASSED)

#: Which BUCKETS each auto-archive signal may ride, in ONE table. `noise` is
#: the standing case; the aged-read ruling added `read` ("worth your eyes, no
#: action" is exactly the thread it describes) and STALE-01 adds `act` for its
#: own signal alone. Forcing a stale act thread through `noise` would make the
#: verdict lie about the thread, and `noise` is what the drift monitor watches.
_LANE_BUCKETS: dict[str, set[str]] = {
    AGED_READ_SIGNAL: {"noise", "read"},
    STALE_ACT_SIGNAL: {"act"},
}
_DEFAULT_BUCKETS = {"noise"}


def archive_buckets(signal: Any) -> set[str]:
    """The buckets this signal may auto-archive from. The ONE table."""
    return _LANE_BUCKETS.get(str(signal or ""), _DEFAULT_BUCKETS)


def stale_act_refusal(v: dict[str, Any], ctx: dict[str, Any]) -> str | None:
    """Why this act thread may NOT ride the stale lane, SIGNAL side.

    Reached from `triage.noise_signal_required`, so only for a verdict that
    actually claims the typed signal. It adds the one clause the FIELD side
    cannot state — a signal with no field behind it — and then defers to
    :func:`stale_field_refusal` for every piece of evidence.
    """
    st = v.get("stale")
    if not isinstance(st, dict) or st.get("is_stale") is not True:
        return (f"`{STALE_ACT_SIGNAL}` claimed on a row whose `stale` field is "
                f"{st!r} — the signal and the field are one claim, and a signal "
                "with no field behind it is a word with no producer")
    return stale_field_refusal(v, ctx)


def stale_field_refusal(v: dict[str, Any], ctx: dict[str, Any]) -> str | None:
    """Why this `stale` FIELD may not stand, or None — evidence, not shape.

    THE FIELD IS THE TRIGGER, NOT THE SIGNAL, and that split is the whole
    reason this function exists separately (review 2026-08-25, finding 1).
    `cos_judge_apply.archive_eligibility` promotes an `act` row to
    `auto_archive: True` / `stale-act-archived` off THIS FIELD ALONE — the
    model never has to claim either. But the only caller of the refusal used to
    be `triage.noise_signal_required`, which returns at
    `if not v.get("auto_archive"): return None`. So a verdict carrying
    `stale: {is_stale: true, …}` with `auto_archive: false` validated with ZERO
    violations against a context naming a standing direct ask, a live deadline,
    an open spine commitment AND an unsent draft — and was archived anyway,
    on a claim no belt had examined. Reproduced before the fix; the answer
    template in the prompt prints exactly that shape.

    `triage.stale_evidence` now calls this for every row whose field claims
    staleness, whatever the model said about archiving.
    """
    st = v.get("stale")
    if not isinstance(st, dict) or st.get("is_stale") is not True:
        return None
    reason = st.get("reason")
    if reason not in STALE_REASONS:
        return (f"stale reason {reason!r} is outside {list(STALE_REASONS)} — "
                "the checks key on the WORD, so an invented one is invisible "
                "to them")
    if ctx.get("read_state") != "read":
        return (f"`{STALE_ACT_SIGNAL}` claimed on a thread the mailbox reports "
                f"as {ctx.get('read_state') or 'unknown'} — the UNREAD SHIELD "
                "stands under every lane (DOCTRINE §2.2/§4.2)")
    if ctx.get("screens_ran_unresolved") or not ctx.get("body_opened"):
        return (f"`{STALE_ACT_SIGNAL}` claimed on a thread whose body never "
                "opened, so the action screens could not run — 'the world "
                "moved on' over screens that did not run is a guess about the "
                "owner's obligations, not a finding")
    # AND THE BODY THAT OPENED CAME BACK READABLE. The clause above asks
    # whether the screens RAN; this asks whether they had anything to run ON.
    # Both `_answered_refusal` and `_date_passed_refusal` below refuse on a
    # True and read False over a BLANKED body, so without this the two reasons
    # are at their most permissive on exactly the threads the run understood
    # least (review 2026-08-25, attempt 2, finding 1 — measured on a
    # rights-protected row carrying a standing direct ask).
    blanked = unreadable_body_refusal(STALE_ACT_SIGNAL, ctx)
    if blanked:
        return blanked
    if reason == STALE_ANSWERED:
        detail = _answered_refusal(ctx)
    else:
        detail = _date_passed_refusal(ctx)
    if detail:
        return detail
    if ctx.get("open_spine_commitment"):
        return (f"`{STALE_ACT_SIGNAL}` claimed on a thread carrying an open "
                "commitment — the spine says the owner still owes this one")
    cid = v.get("conversation_id")
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
    if ctx.get("thread_carries_draft"):
        return (f"`{STALE_ACT_SIGNAL}` claimed on a thread the host's own draft census "
                "reports as carrying an unsent draft — work in progress is "
                "never archived, however confident")
    if cid in (ctx.get("drafts_inventory") or []) \
            and cid not in (ctx.get("expired_cos_draft_convids") or []):
        return (f"`{STALE_ACT_SIGNAL}` claimed on a thread carrying an unsent "
                "draft — work in progress is never archived, however confident")
    return None


def _answered_refusal(ctx: dict[str, Any]) -> str | None:
    """`answered-by-other`: someone spoke last and left nothing standing."""
    if ctx.get("unanswered_direct_ask"):
        return (f"`{STALE_ANSWERED}` claimed on a thread where the host's own "
                "ask detector still sees a direct ask standing in the newest "
                f"message ({ctx.get('unanswered_direct_ask_leg') or 'unknown'} "
                "leg) — 'someone already answered it' is refuted by this run's "
                "own artifacts")
    if ctx.get("live_deadline"):
        return (f"`{STALE_ANSWERED}` claimed on a thread carrying a live "
                "deadline — something on it is still ahead of the owner")
    if not ctx.get("last_sender"):
        return (f"`{STALE_ANSWERED}` claimed on a thread whose newest message "
                "names no sender — 'from someone other than the owner' is the "
                "fact this reason rests on, and a row without one is malformed")
    if ctx.get("last_sender_is_owner"):
        return (f"`{STALE_ANSWERED}` claimed on a thread whose newest message "
                "is the OWNER'S OWN — he spoke last, so nobody answered for "
                "him")
    return None


def _date_passed_refusal(ctx: dict[str, Any]) -> str | None:
    """`date-passed`: the date the thread names is behind the run."""
    if not ctx.get("stale_deadline_passed"):
        return (f"`{STALE_DATE_PASSED}` claimed on a thread where the host "
                "found no stated due date behind the run date — a date with no "
                "deadline word is a meeting, and a deadline in prose with no "
                "date is not a date this lane can measure")
    if ctx.get("live_deadline"):
        return (f"`{STALE_DATE_PASSED}` claimed on a thread that ALSO names a "
                "deadline still ahead — a thread with a live date is not past "
                "its date")
    return None


#: (signal -> its lane refusal). `triage.noise_signal_required` dispatches
#: through this rather than growing a branch per lane, so adding a lane is
#: adding a row here and a refusal function, never an edit to the rule.
LANE_REFUSALS = {AGED_READ_SIGNAL: aged_read_refusal,
                 STALE_ACT_SIGNAL: stale_act_refusal}

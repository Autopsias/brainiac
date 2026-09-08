"""The OWNER'S OWN RULINGS, as a screen over the assembled mutation plan (FB-02).

Split out of `cos_mutate_plan` at the 500-LOC file-size bound, beside the aged
and stale belts, and re-imported by it so every name keeps its module path.

The seam is real rather than arbitrary: every other screen in that file
re-applies something the RUN can prove about a thread — its read state, its
tier, its age, what else the night already planned for it. This one re-applies
something the OWNER said, on a previous night, about a thread nobody is looking
at tonight. It is the only screen whose input comes from outside the run.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


#: THE VERDICT TEST LIVES IN ONE PLACE and it is not this file:
#: `brain.cos.feedback.DO_NOT_TOUCH_VERDICT`, applied by
#: `feedback_render.held_threads`, which this guard and the judge's rulings
#: block both call. A literal `"wrong"` here was the second spelling of that
#: fact, and the two readers drifted: the prompt rendered `missed` rulings under
#: a heading promising a host refusal this guard never performed.


def owner_rulings_state(vault: Path) -> tuple[set[str], dict[str, Any]]:
    """`({…ids…}, {"ruled": N, "unreadable_record_lines": N})` — the guard's
    INPUT, with the one number that says how much of it the reader could not see.

    THE COUNT TRAVELS WITH THE SET, and that is the point. `read_record` counts
    a line that will not parse rather than skipping it, and a lost line here
    means a do-not-touch the porter can no longer see. This does NOT stop the
    night: the failure is self-healing in the one direction that matters — the
    thread is archived once more, the owner pulls it back once more, and pen 2
    mints a fresh readable row — whereas halting the mailbox automation on one
    truncated byte is not. So it is REPORTED, on the plan itself and in the
    judge's rulings block, and never silently zero.
    """
    from brain.cos import feedback                                 # noqa: PLC0415

    record = feedback.read_record(vault)
    ruled = _ruled(record["rows"])
    return ruled, {"ruled": len(ruled),
                   "unreadable_record_lines": record["unreadable"]}


def _ruled(rows: list[dict[str, Any]]) -> set[str]:
    """Conversations the owner has ruled the porter must not touch (FB-02).

    THE LEDGER, NEVER THE PROJECTION. `feedback_render.projected_thread_rulings`
    decides how many rulings a single model MESSAGE may carry (80), because a
    prompt has an overflow cliff at ~250 rows. A GUARD has no such cliff, and
    reading the capped projection here would mean the eighty-first thread the
    owner ever pulled back quietly became archivable again — the exact
    "archives it again, nightly, forever" failure the second row kind exists to
    prevent. Storing and rendering are different things, and this is the
    storage side.

    Latest row per conversation wins, so a later `right` ruling releases a
    thread an earlier `wrong` one held: a do-not-touch is a standing ruling,
    not a life sentence. That release has a PRODUCER — a `right` mark on the
    morning sheet for a thread currently held (`feedback_cli._mark`) — because a
    documented way out that no shipped pen can reach is a life sentence with a
    paragraph denying it.

    `feedback_render.held_threads` is the fold AND the verdict test, shared with
    the prompt block, so the judge cannot be told this guard refuses something
    it does not: a `missed` ruling asks for MORE action and is refused by
    nothing.
    """
    from brain.cos import feedback_render                          # noqa: PLC0415

    return {str(r["conversation_id"])
            for r in feedback_render.held_threads(rows)}


def screen_owner_rulings(planned: list[dict[str, Any]], vault: Path,
                         exclude: Callable
                         ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop every mutation the owner ruled do-not-touch; report what was read.

    Returns the kept plan AND the report `build_plan` puts on the document, so
    a night whose record was partly unreadable says so on its own plan rather
    than reporting a guard that ran over less than it thought.
    """
    ruled, report = owner_rulings_state(vault)
    return _drop_ruled(planned, ruled, exclude), report


def _drop_ruled(planned: list[dict[str, Any]], ruled: set[str],
                exclude: Callable) -> list[dict[str, Any]]:
    """Drop EVERY mutation on a thread the owner ruled do-not-touch.

    OVER THE ASSEMBLED PLAN, AFTER EVERY LANE HAS SPOKEN, and that placement is
    the whole design. A row can be planned by three lanes at once — the archive
    screen, the chip lane and the ingestion-mark lane — and a ruling honoured in
    one of them while another still spends a slot on the same thread is not a
    guard, it is a lane that happens to agree. One screen over the finished
    plan cannot have that hole.
    """
    kept: list[dict[str, Any]] = []
    for m in planned:
        if str(m.get("conversation_id")) in ruled:
            exclude(str(m.get("conversation_id")), m["verb"],
                    "the owner ruled this thread do-not-touch — a standing "
                    "thread ruling in the COS feedback record")
            continue
        kept.append(m)
    return kept

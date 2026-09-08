"""The PER-THREAD MUTATION BUDGET of the COS plan (attempt-2 split, 2026-08-25).

Split out of `cos_mutate_plan.py` at the 500-LOC bound, exactly as
`cos_mutate_plan_aged`, `_stale` and `_marks` were. It holds the two rules that
decide what ONE ledger row may take BEYOND its archive: which priority chip it
earns (`_plan_chip`, the chip half of `screen_ledger_rows`), and what may ride
a thread the run is removing (`screen_archiving_companions`).

They belong together because the second exists only because the first was not
enough. Three lanes plan against one row and none of them sees the others, so a
rule about the THREAD's total cannot live inside any single lane — which is the
defect the companion belt closes and the reason a lane-local exclusion did not.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

# The sibling belts (`cos_mutate_plan_marks`) set the same two paths at import
# time. Doing it here too means this module can be imported FIRST — by a test,
# or by a future caller — without depending on `cos_mutate_plan` having run.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def p0_excluded(row: dict[str, Any]) -> str | None:
    """Why the P0 blast floor refuses this row an archive, or None.

    BOTH COLUMNS, because this belt is the OBSERVED-CHIP one and the ruling is
    about the THREAD: `tier` is the chip the mailbox already carries,
    `judged_tier` is tonight's verdict, and a floor read off one column alone
    lets the other through. The rule itself is `cos_chips.p0_floor_refuses` —
    the ONE definition all six enforcement sites now share (owner ruling
    2026-09-04, which retired the P0 exemption for the `read` bucket only).
    """
    from brain.cos_chips import p0_floor_refuses                 # noqa: PLC0415
    v = row.get("verdict")
    if p0_floor_refuses(v, row.get("tier")) \
            or p0_floor_refuses(v, row.get("judged_tier")):
        return (f"tier {row.get('tier') or row.get('judged_tier')} on a "
                f"`{v}` verdict — the 2026-09-04 ruling retired the P0 "
                "exemption for the `read` bucket only; every other lane "
                "keeps the blast floor")
    return None


def _plan_chip(row: dict[str, Any], cid: str, planned: list[dict[str, Any]],
               exclude: Callable, *, archiving: bool, chip_for: Callable,
               managed_chips: tuple[str, ...]) -> None:
    """The chip half of `screen_ledger_rows`, split out at the complexity
    bound. Same screen, same exclusions, same order — only the nesting moved."""
    # THE CHIP COMES FROM THE (bucket, tier) MATRIX, not from `hold_category`
    # (which is the judge's hold-REASON vocabulary — `Held · ask` and its
    # siblings — and could never match a managed chip name; wired to it, the
    # lane was unfireable by construction), and not from the tier ALONE
    # (DOCTRINE v7 §4.1: `read`/P2 → `P3 · Read` while `act`/P2 →
    # `P2 · This week`, same tier, different chip). `verdict` is the bucket
    # `apply_judgment` wrote; `judged_tier` is the tier. The lane is
    # ADD-ONLY: it puts a missing chip on, and never touches a thread that
    # already carries one, because clearing a chip is a shape this build has
    # not accepted from us.
    chip = chip_for(row.get("verdict"), row.get("judged_tier"))
    if chip:
        if archiving:
            # THE TWO LANES CONTRADICT EACH OTHER ON ONE ROW. The screen
            # planned the archive and then a chip on the SAME thread — on
            # the stale-act lane, `P2 · This week` on a thread the porter is
            # removing (review 2026-08-25, finding 3; reproduced as
            # `[('archive', None), ('categorize', 'P2 · This week')]`, no
            # exclusions). A REFUSED archive still earns its chip — the
            # thread stays — which is why this reads the PLANNED verb and
            # never `auto_archive`.
            #
            # IT SAYS CONTRADICTION AND NOT "a second mutation", because the
            # second half was never true and this lane cannot make it true:
            # dropping the chip here FREES the undo key `<cid>|categorize`,
            # and the ingest-mark lane — which must not read disposition at
            # all (INGEST-01) — takes it. That is deliberate, and
            # `screen_archiving_companions` is where the thread's WHOLE
            # budget is stated and enforced. The exclusion stays HERE, before
            # the mark lane runs, precisely so the mark is not blocked by a
            # key this chip would have taken and then lost anyway.
            exclude(cid, "categorize",
                    f"{chip!r} on a thread this run is ARCHIVING — a "
                    "priority chip claims the thread is due while the porter "
                    "is removing it")
        elif chip not in managed_chips:
            exclude(cid, "categorize", f"{chip!r} is not a managed priority "
                                       "chip; only the four may be written")
        elif row.get("judgment_pending"):
            exclude(cid, "categorize", "the row carries no verdict")
        elif row.get("read_state") != "read":
            # THE SHIELD IS NOT ARCHIVE-ONLY. The archive branch above says
            # an unread row is "untouchable by any lane", and the plan's own
            # census rule says an unread row can be neither archived NOR
            # categorized — but this branch never read `read_state`, so the
            # shield only ever covered half of what it claimed. Run188
            # (2026-08-24) chipped one unread thread `P3 · Read`; e-check E2
            # caught it after the mutation had already landed on the
            # mailbox. A post-apply check is a report, not a shield.
            exclude(cid, "categorize", "read_state is not `read` — an "
                                       "unread row is untouchable by any "
                                       "lane, the chip lane included")
        elif row.get("tier"):
            exclude(cid, "categorize",
                    f"the thread already carries a {row.get('tier')} chip and "
                    "this lane is ADD-ONLY — it cannot clear or replace one")
        else:
            planned.append({"verb": "categorize", "conversation_id": cid,
                            "chip": chip, "mode": "add",
                            "received": row.get("received"),
                            "reason": f"chip: judged "
                                      f"{row.get('verdict')}/"
                                      f"{row.get('judged_tier')} on a "
                                      "thread carrying none"})


def screen_archiving_companions(planned: list[dict[str, Any]],
                                exclude: Callable) -> list[dict[str, Any]]:
    """THE WHOLE PER-THREAD BUDGET OF A LEAVING THREAD, in ONE place.

    AN ALLOW-LIST OVER THE ASSEMBLED PLAN, not a condition inside a lane. THREE
    lanes can plan against one ledger row and none of them can see the others:
    `screen_ledger_rows` plans the archive and the priority chip,
    `screen_ingest_marks` plans the `Brainiac · Ingested` mark, `screen_drafts`
    plans a reply draft. Each was written with its own reason for what IT may
    do, so a rule about what a THREAD may take in total cannot live inside any
    of them — and a rule that tries to lands exactly where this one started.
    MEASURED 2026-08-25 (review attempt 2, finding 2): excluding the priority
    chip inside `_plan_chip` freed the undo key, the mark lane took it, and one
    stale-act thread came out of the screens as `[('archive', None),
    ('categorize', 'Brainiac · Ingested'), ('draft', None)]` — an archive, a
    mark, AND a reply draft written into the owner's real Drafts folder for a
    thread the porter was removing that same night. The reviewer saw two of
    those three; the draft lane was never probed because no lane was looking at
    the whole plan.

    THE ONE COMPANION AN ARCHIVE MAY CARRY IS THE INGEST MARK, and it is an
    exception with a named reason rather than a hole. The mark records that the
    VAULT SIGNED a note for this thread's own candidate; INGEST-01 makes that
    answer independent of what the porter did with the thread, and
    `mark_lane_disposition_blindness` FAILS THE WHOLE PLAN if the mark lane
    ever learns to read `auto_archive`. Tonight is also its LAST chance: the
    read pass enumerates the INBOX (`cos_driver`'s own `inbox_count`), so a
    thread archived tonight is never enumerated again and a mark deferred off
    it is a mark that never lands. The pair costs the ledger nothing it cannot
    record — `<cid>|archive` and `<cid>|categorize` are different undo keys and
    different per-verb caps.

    EVERYTHING ELSE ON A LEAVING THREAD IS REFUSED HERE. A priority chip claims
    the thread is due while the porter removes it; a reply draft answers a
    thread that is leaving. The chip is normally gone before this runs — its
    own exclusion has to precede the mark lane, for the undo key — so this is
    the belt that PROVES the budget rather than the one that usually spends it,
    and the one that catches a chip, a draft, or a verb this file has not met
    yet arriving from anywhere else.

    PERMISSION IS NOT DELIVERY. Letting the mark ride is only half the rule —
    `order_companions_before_archive` below is the other half, and until it
    existed the mark this function allows was dispatched AFTER the archive,
    resolved against an Inbox the thread had already left, and landed as
    `aborted-not-applied` against the run's absent cap. Measured 2026-08-25 on
    both real producers; the two functions are read together.
    """
    from brain import cos_chips                                  # noqa: PLC0415

    leaving = {m["conversation_id"] for m in planned if m["verb"] == "archive"}
    if not leaving:
        return planned
    kept: list[dict[str, Any]] = []
    for m in planned:
        cid = m["conversation_id"]
        if cid not in leaving or m["verb"] == "archive" or (
                m["verb"] == "categorize"
                and m.get("chip") == cos_chips.CHIP_INGESTED):
            kept.append(m)
            continue
        exclude(cid, m["verb"],
                f"this run is ARCHIVING the thread, and a {m['verb']!r} on a "
                "thread leaving the inbox either contradicts the archive or "
                "spends a slot on a thread that is gone. Only "
                f"{cos_chips.CHIP_INGESTED!r} may ride an archive — it records "
                "that the vault SIGNED this thread's candidate, it is blind to "
                "disposition by contract (INGEST-01), and tonight is its last "
                "chance because an archived thread is never enumerated again")
    return kept


def order_companions_before_archive(planned: list[dict[str, Any]]
                                    ) -> list[dict[str, Any]]:
    """THE ARCHIVE GOES LAST ON ITS OWN THREAD — which is what makes the one
    companion `screen_archiving_companions` allows actually LAND.

    THE ALLOW-LIST ABOVE DECIDED WHAT MAY RIDE; THIS DECIDES WHEN, and without
    it the permission is empty. `cos_mutate_apply.run_todo_row` walks the plan
    IN ORDER, and every categorize resolves against the folder named on its row
    (`cos_mutate_page.js` `prepareCategorize`). The ingest mark's row names
    none, so it resolves against the INBOX — and the archive it was allowed to
    ride is precisely the mutation that takes the thread out of the Inbox.
    Measured 2026-08-25 on the real producers: `build_plan` emitted
    `[('archive', None), ('categorize', 'Brainiac · Ingested')]`, and the page
    half run against the simulated mailbox in that order returns the mark
    `dispatched: false`, `absence_conclusive: true`, `verified-failed`
    (`tests/js/cos_mutate_page.test.mjs`, `markRidingAnArchive`, arm 1). The
    host then writes `aborted-not-applied` and counts the row against
    `absent_cap` (`cos_mutate_apply.record_outcome`), so a night of marked
    archives eventually STOPS blaming a starved folder — and none of it is
    visible to the rehearsal, which runs before any mutation moves anything.

    THE PAGE IS NOT WHERE THIS IS FIXED. A thread really IS gone from the Inbox
    once it is archived, so teaching the mark a `folder` would mean asserting
    where the thread ended up — a second producer for a fact the archive
    already owns — and would leave the mark's undo row (`original_folder:
    "Inbox"`, its before-image, its resolve) describing a mailbox state that no
    longer existed when it was written. Dispatched BEFORE the archive, every
    one of those records is simply true, and no other lane, verb or reversal
    has to learn anything.

    STATED LIMIT, and it is new because the mark can now really land. A thread
    that ends the night ARCHIVED AND MARKED cannot be `unchip`-ed on its own:
    `unchip_pass` builds a bare `categorize/remove` and the page resolves it
    against the Inbox, so it reports `target-not-found`, `verified-failed`,
    nothing dispatched — honest, never a false reversal, and it takes no chip
    off the wrong thread. The reversal that works is the one an operator would
    reach for anyway: `undo` the run's archives first, then `unchip`. They are
    two separate commands (`cos_ctl.sh`), never chained by anything, so no
    automated path hits this order backwards. Upgrade path if a single-command
    reversal is ever wanted: `undo_pass` before `unchip_pass` in one caller —
    not a `folder` on the reversal, for the reason above.

    IT RUNS LAST, over what the caps and the window actually left. A companion
    the cap dropped means the archive is no longer carrying anything, and only
    the final list knows that. `sorted` is stable, so the plan's worst-first
    chip order (`CHIP_RANK`) and enumeration order survive untouched: the ONLY
    rows that move are the archives of threads that still have a rider.
    """
    leaving = {m["conversation_id"] for m in planned if m["verb"] == "archive"}
    riding = {m["conversation_id"] for m in planned
              if m["verb"] != "archive" and m["conversation_id"] in leaving}
    if not riding:
        return planned
    return sorted(planned, key=lambda m: 1 if (m["verb"] == "archive"
                                               and m["conversation_id"]
                                               in riding) else 0)

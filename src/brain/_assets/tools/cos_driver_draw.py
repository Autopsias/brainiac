"""The draw logic of `cos_driver` — tier chips, read-order ranking, the body draw (batch-2 drain)

Moved verbatim out of `cos_driver` (batch-2 drain) and re-imported by it, so
every name keeps its `cos_driver` module path; the parent's night orchestration
calls these through its own globals exactly as before, so a test that
monkeypatches one on `cos_driver` still steers the callers.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_driver_transport import DriverStop  # noqa: E402


#: The three managed priority chips, read off the SERVER's own category list.
#: An observed chip is a fact about the mailbox, not a judgment about the mail —
#: which is exactly why the driver may use it to order the draw and may not use
#: it to decide what any thread MEANS.
CHIP_TIER = {"P0 · Now": "P0", "P1 · Today": "P1", "P2 · This week": "P2",
             "P3 · Read": "P3"}

#: `P3 · Read` reads back as tier P3 for the two things that need a tier — the
#: ADD-ONLY screen ("this thread already carries one of ours") and the draw
#: order — but it ASSERTS NO TIER, and that difference is load-bearing. The
#: v7 matrix writes it for `read`/P2, `read`/P3 AND `act`/P3 (DOCTRINE §4.1),
#: so reading it back as an assertion that the thread IS P3 would make
#: `triage.tier_vocabulary` reject tonight's honest `read`/P2 verdict as
#: "contradicts the row's own managed chip" — the ratchet, running backwards.
#: The ledger therefore SOURCES the tier differently for it, and
#: `cos_judge.load_night` only feeds `chip_tier` from the unambiguous source.
TIER_SOURCE_PRIORITY_CHIP = "outlook-priority-chip"
TIER_SOURCE_READ_CHIP = "outlook-read-chip"
AMBIGUOUS_TIER_CHIPS = ("P3 · Read",)


def _observed_chip(categories: list[str]) -> str | None:
    """The ONE managed chip on this thread, strongest first."""
    for name in ("P0 · Now", "P1 · Today", "P2 · This week", "P3 · Read"):
        if name in (categories or []):
            return name
    return None


def _tier(categories: list[str]) -> str | None:
    name = _observed_chip(categories)
    return CHIP_TIER[name] if name else None


def _tier_source(categories: list[str]) -> str:
    """Which KIND of managed chip the tier above was read off — derived from
    the SAME pick `_tier` made, never from a second scan that could disagree."""
    name = _observed_chip(categories)
    return (TIER_SOURCE_READ_CHIP if name in AMBIGUOUS_TIER_CHIPS
            else TIER_SOURCE_PRIORITY_CHIP)


def _draw_rank(tier: str | None) -> int:
    return {"P0": 0, "P1": 1}.get(tier or "", 2)


def conversations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per CONVERSATION, newest message winning, in a stable order.

    Deterministic ordering matters more than which order: the ledger is diffed
    byte-for-byte against a replay, so the sort has to be total. Received time
    then conversation id is total (two messages can share a timestamp; two
    conversations cannot share an id).
    """
    newest: dict[str, dict[str, Any]] = {}
    for it in items:
        cid = it.get("convId")
        if not cid:
            continue
        prev = newest.get(cid)
        if prev is None or str(it.get("received") or "") > str(prev.get("received") or ""):
            newest[cid] = it
    rows = list(newest.values())
    rows.sort(key=lambda r: (str(r.get("received") or ""), str(r.get("convId"))),
              reverse=True)
    return rows


def body_draw(convs: list[dict[str, Any]], cap: int,
              exclude: set[str] | frozenset[str] = frozenset()
              ) -> list[dict[str, str]]:
    """P0 before P1 before the rest, newest-first inside a group, unread EXCLUDED.

    The unread filter is applied HERE, before a single id reaches the fetcher:
    the page-side `fetchBody` refuses an unread message as a second gate, but a
    gate that is the only gate is one edit away from being none.

    `exclude` IS THE CATEGORY GATE, and it is the reason this parameter exists
    (JDG-01, 2026-08-10). SKILL.md rule 1¾ excludes a `never`-category thread on
    the DRAW, before its body is opened — "a `never` thread that was OPENED is a
    FAIL even when it is ledgered correctly afterwards", because it spent one of
    the twenty opens the cap owed to actionable material. The category is a
    JUDGMENT over typed fields, so the driver cannot compute it: the caller runs
    the category batch after enumeration and hands the excluded ids back here.
    Measured on run 115, whose draw had no such gate: 5 of 20 opens went to
    `never` threads (runs 103 and 108 lost 11 of 19 and 3 of 19 the same way).
    """
    eligible = [c for c in convs
                if c.get("isRead") is True and c.get("convId") not in exclude]
    eligible.sort(key=lambda c: (_draw_rank(_tier(c.get("categories"))),
                                 [-ord(ch) for ch in str(c.get("received") or "")],
                                 str(c.get("convId"))))
    return [{"convId": c["convId"], "itemId": c["itemId"]} for c in eligible[:cap]]


def starvation_stop(convs: list[dict[str, Any]], cap: int,
                    excluded: set[str] | frozenset[str]) -> str | None:
    """Did the gate take a drawable mailbox down to ZERO opens? (item 2, s09)

    THE DEGENERATE CASE, AND IT NEEDS NO CALIBRATION. A category pass that
    stamped everything `never` would blind the night, and the cited backstop —
    `_CATEGORY_DOMINANCE_MAX_SHARE`, 0.75, in `cos_runverify.check_category_stamp`
    — is evadable by construction: split the stamps across two `never` ids at
    50/50 and 100 % of the inbox is excluded while no single category reaches
    the bar. Two ids blind the night and pass.

    What share is TOO MUCH is an owner decision wanting data nobody has yet
    (`_evidence/s09/excluded-share.json`). What is unambiguous today is zero,
    and this is the one number a wrong pre-draw `never` cannot hide behind: no
    body was read, so an incorrectly excluded row is byte-identical to a
    correct one on this run's own artifacts — the only observable left is that
    nothing was drawn at all.

    Compared against the SAME `body_draw` with no exclusions, so the two calls
    differ in exactly ONE input. A mailbox that draws zero for its own reasons
    — every thread unread, an empty inbox — draws zero either way and is left
    alone; only a zero the gate CAUSED stops the night.

    A pure function on purpose: the caller runs inside a browser session, and a
    guard only reachable through a live mailbox is a guard no test can prove
    fires (`hardening-prose-is-not-a-mechanism`).
    """
    if not convs:
        return None
    ungated = body_draw(convs, cap)
    if not ungated or body_draw(convs, cap, exclude=excluded):
        return None
    held = sum(1 for c in convs if c["convId"] in excluded)
    return (f"the category gate excluded EVERY drawable thread: {len(ungated)} "
            f"conversation(s) would have had their bodies opened and {held} of "
            f"{len(convs)} enumerated row(s) were held out, leaving nothing. A "
            "night that reads no body judges nothing, and a pre-draw `never` "
            "is unfalsifiable from this run's own artifacts — no body was "
            "read, so the row is byte-identical to a correct exclusion. "
            "Stopping for review instead of reporting a quiet mailbox. Re-run "
            "without --categories to draw ungated, or fix the taxonomy at "
            "<vault>/overlay/cos/ingest.md")


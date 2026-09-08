"""The typed-field enumeration of `cos_driver` — the categoriser's field projection, the stamp binding, the pass-1-only mode (batch-2 drain).

Moved verbatim out of `cos_driver` and re-imported by it, so
`cos_driver.enumerate_only`, `enumeration_row`, `row_digest` and
`bind_categories` keep their module path; `_run_night` passes
`bind_categories` into the gate through the parent's globals exactly as before.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_driver_accounting import _persist  # noqa: E402
from cos_driver_completeness import assert_complete, completeness  # noqa: E402
from cos_driver_draw import CHIP_TIER, _tier, conversations  # noqa: E402
from cos_driver_transport import (  # noqa: E402
    DriverStop, _ts, _utcnow, capture_night, load_sheet,
    open_tab)


#: The typed fields the CATEGORY batch is allowed to see. Phase 1.5 judges from
#: typed fields and nothing else (INJ-03) and no body exists yet at this point in
#: the night, so this list is the whole surface — it is stated here rather than
#: assembled ad hoc so that adding a field is a decision someone makes on purpose.
ENUMERATION_FIELDS = ("conversation_id", "subject", "sender", "received",
                      "read_state", "chip")


def enumeration_row(conv: dict[str, Any]) -> dict[str, Any]:
    """One capture item projected onto the typed fields the categoriser sees.

    ONE PROJECTION, TWO CALLERS. `enumerate_only` writes these rows out and
    `_run_night` re-derives them from its OWN enumeration to compare — and a
    comparison between two spellings of "the typed fields" would report drift
    that is really a formatting difference, or miss drift that is real.
    """
    return {"conversation_id": conv["convId"],
            "subject": conv.get("subject") or "",
            "sender": conv.get("sender") or None,
            "received": conv.get("received"),
            "read_state": "read" if conv.get("isRead") else "unread",
            "chip": _tier(conv.get("categories"))}


def row_digest(row: dict[str, Any]) -> str:
    """The identity of ONE enumerated conversation, over every typed field.

    Every field, because every field is an INPUT to the category judgment: a
    thread whose subject changed, whose sender changed, or which was read since
    the stamp was made is a thread the model judged from data that no longer
    describes it.
    """
    return hashlib.sha256(json.dumps(
        {k: row.get(k) for k in ENUMERATION_FIELDS},
        sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def bind_categories(categories: dict[str, str],
                    prior_rows: list[dict[str, Any]],
                    live_convs: list[dict[str, Any]]) -> dict[str, Any]:
    """Bind stamps judged on enumeration A to the enumeration B about to draw.

    TWO ENUMERATIONS, ONE SET OF STAMPS (review 2026-08-13, round 1, HIGH).
    `cos_nightly.sh` enumerates, runs the model, then starts the driver AGAIN
    and it re-enumerates. Stamps were applied by conversation id to whatever
    the second read returned — so a thread that arrived during the model call
    entered the draw with no stamp at all, and a thread whose subject, sender
    or read state CHANGED was excluded on data the model never saw. Nothing
    compared the two, so the gate's own metrics still read valid.

    Two passes are structural here and carrying A whole into B is not
    available: the body fetch needs a fresh `itemId` (OWA re-issues one when an
    item moves) and the completeness cross-check has to be a live read. So the
    delta is RESOLVED rather than wished away, and every part of it is counted:

    * `honored` — the thread is still here and its typed fields are unchanged.
      Only these can exclude anything.
    * `stale` — still here, fields CHANGED. The stamp is dropped: it was judged
      from data that no longer describes the thread, and the conservative
      reading of a stale `never` is to leave the row in the draw. Withholding a
      body on an obsolete judgment is silent blindness; opening one that did
      not need opening costs a slot and is visible.
    * `arrivals` — in B, never in A. Nothing judged them, so they draw UNGATED,
      and the count says so on the report rather than being inferred.
    * `departed` — in A, gone from B. Excluding nothing is already correct.
    """
    prior = {str(r.get("conversation_id")): row_digest(r) for r in prior_rows}
    live = {c["convId"]: row_digest(enumeration_row(c)) for c in live_convs}
    both = set(prior) & set(live)
    stale = sorted(c for c in both if prior[c] != live[c])
    honored = {c: v for c, v in categories.items()
               if c in both and c not in stale}
    return {"honored": honored,
            "scope": sorted(both),
            "stale": stale,
            "arrivals": sorted(set(live) - set(prior)),
            "departed": sorted(set(prior) - set(live)),
            "stamps_dropped_as_stale": sorted(c for c in stale
                                              if c in categories)}




def owner_reversals(vault: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """PEN 2 of FB-02 — what the owner UNDID by hand, read off this enumeration.

    The undo ledgers say what the porter did to each thread; `rows` is what the
    Inbox looks like now. A thread the ledger says was archived and that this
    enumeration can see is back; a thread whose managed chip the ledger wrote
    and the enumeration no longer reports has been unchipped. Either is an owner
    ruling nobody typed, and `brain.cos.feedback_cli` appends it as a
    do-not-touch keyed on the conversation.

    IT ONLY MINTS ONE WHEN THE MESSAGE SET IS UNCHANGED. A conversation back in
    the Inbox because somebody REPLIED is new work, not a reversal, and a
    permanent do-not-touch on it would retire a live thread forever. The guard
    lives in `feedback_cli.outlook_reversals`; this function's whole job is to
    hand it the two inputs and the ONE chip-name-to-tier mapping
    (`cos_driver_draw.CHIP_TIER`), so the ledger's `P1 · Today` and the
    enumeration's `P1` are joined through the driver's own table rather than a
    second spelling.

    EVERY ledger in the ops dir, not just last night's: an owner may reverse a
    thread days later, and `applied_mutations` takes the latest landed row per
    (conversation, verb) by `action_ts` — never by file name, which sorts
    `run99` after `run124` inside one day.

    AN UNREACHABLE RECORD DEGRADES THIS PEN, IT DOES NOT KILL THE PASS.
    `feedback_dir` refuses outright when `$BRAIN_INDEX_DIR` puts the host-private
    base back inside a VM-visible root — a configuration fact, and one that must
    not take the whole Inbox enumeration down with it. The judge lane already
    degrades on exactly this (`cos_judge_grounding.owner_rulings`); the two now
    agree. Nothing is minted, and the report says `unreachable` rather than a
    zero that reads as "the owner reversed nothing".
    """
    from brain import config, cos                                # noqa: PLC0415
    from brain.cos import feedback_cli                           # noqa: PLC0415

    ledger_rows: list[dict[str, Any]] = []
    for ledger in sorted(cos.run_ops_dir(vault).glob("_cos_undo_ledger_*.jsonl")):
        for line in cos._read_nofollow(ledger).decode(
                "utf-8", "replace").splitlines():
            if line.strip():
                try:
                    ledger_rows.append(json.loads(line))
                except ValueError:
                    continue
    try:
        return feedback_cli.record_outlook_reversals(
            vault, ledger_rows, rows, chip_tier=CHIP_TIER)
    except (config.HostPathUnsafe, OSError) as exc:
        return {"state": "unreachable", "detail": f"{type(exc).__name__}: {exc}",
                "recorded": 0, "reversals": [], "new_work": [],
                "already_recorded": 0, "considered": 0}


def enumerate_only(vault: Path, tab_id: int | None, *,
                   evidence_path: Path | None,
                   poll_seconds: float = 3.0, max_wait: float = 900.0,
                   use_cdp: bool = False,
                   use_ego: bool = False) -> dict[str, Any]:
    """Pass 1 alone: enumerate, cross-check, and STOP before the draw.

    THIS IS THE SEAM THE CATEGORY GATE NEEDED (GAP 9). `body_draw`'s `exclude`
    parameter — the gate itself — has existed since JDG-01 and has never once
    been fed, because the category is a MODEL judgment and the model ran after
    every body was already open. Measured on runs 126, 129 and 130 alike: 8 of
    ~228 rows carried a `never` category and 8 of the night's 20 body opens went
    to them, while `category_gate.state` reported `not-run` on every run ever
    scored.

    `capture_night` already runs pass 1 with an EMPTY draw, so nothing new is
    read here — this only stops afterwards and writes the typed fields out, so
    a caller can put a category batch between the two passes. It writes NO
    ledger, NO contract and NO corpus: those belong to the night that opens
    bodies, and a half-night that wrote them would be a second run.
    """
    now = _utcnow()
    sheet = load_sheet(vault)
    try:
        tab, transport = open_tab(tab_id, use_cdp=use_cdp, use_ego=use_ego)
        capture = capture_night(tab, cap=0, poll_seconds=poll_seconds,
                                max_wait=max_wait, now=now)
        report = completeness(capture)
        assert_complete(report)
    except DriverStop as exc:
        # SAME CONTRACT AS `run_night`: a stop is written to the evidence file,
        # not swallowed. The nightly's failure message names this path, and a
        # path it names has to exist when it points there.
        _persist(evidence_path, {"run_id": sheet["run_id"], "rows": [],
                                 "stopped": str(exc),
                                 "stopped_at": _ts(_utcnow())})
        raise
    convs = conversations(capture["enumeration"].get("items", []))
    out = {
        "run_id": sheet["run_id"],
        "enumerated_at": capture["enumeration"].get("at") or _ts(now),
        "window_start": capture["window_start"],
        "driver_transport": transport,
        "fields": list(ENUMERATION_FIELDS),
        "completeness": report,
        "rows": [enumeration_row(c) for c in convs],
    }
    # THE OWNER'S OTHER PEN, RUN HERE AND PERSISTED WITH THE ENUMERATION
    # (FB-02). This is the one pass that holds both halves of the diff — the
    # mailbox as it is now, and the vault's own undo ledgers — and it runs
    # before any judgment, so tonight's plan is built against a record that
    # already knows what the owner reversed. `new_work` rides the evidence
    # rather than the record: a thread back in the Inbox because somebody
    # replied is named on the night's own artifact and mints nothing.
    out["owner_reversals"] = owner_reversals(vault, out["rows"])
    _persist(evidence_path, out)
    return out



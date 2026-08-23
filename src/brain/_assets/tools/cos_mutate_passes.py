"""The locked mutation passes of `cos_mutate` — the apply body, undo, unchip, canary drill (batch-2 drain).

Moved verbatim out of `cos_mutate` on the s18 lane convention: each pass
receives ``lane`` — the loaded ``cos_mutate`` module's own namespace, passed by
the parent's same-signature wrappers — so every parent name it touches
(``_bridge_for``, ``load_shapes``, ``UndoLedger``, ``MutationStop``, …) is
looked up at CALL time and a test that monkeypatches one on ``cos_mutate``
keeps governing this code exactly as before.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_mutate_gates import _ts, short  # noqa: E402
from cos_mutate_ledger import _write_text_atomic  # noqa: E402
from cos_mutate_policy import MANAGED_CHIPS, _OPS_MODE  # noqa: E402
import cos_mutate_apply as apply_stages                       # noqa: E402
from cos_mutate_gates import MUTATION_LANE, PRIMITIVE   # noqa: E402
import cos_mutate_canary as canary_stages                     # noqa: E402
import cos_mutate_plan as plan_stages                         # noqa: E402

#: The canary drill (E17) — the undo path, exercised on ONE disposable row.
CANARY_STEPS = ("chip_roundtrip", "archive", "undo", "replay")


def _apply_pass_locked(vault: Path, run_id: str, tab_id: int, *,
                       caps: dict[str, int] | None = None,
                       since_days: int | None = None,
                       allow_draft_resume: bool = False,
                       plan_path: Path | None = None,
                       rehearsal_path: Path | None = None,
                       use_cdp: bool = False, use_ego: bool = False,
                       lane=None) -> dict[str, Any]:
    # The row-handling sub-steps live in cos_mutate_apply (s18) and receive
    # THIS module's namespace, so a monkeypatch on cos_mutate keeps governing
    # them; they run inside this pass's lane lock and never take it themselves.
    # `lane` is the PARENT `cos_mutate` module, passed by its wrapper —
    # every parent name below resolves through it at CALL time.
    if since_days is None:
        since_days = lane.DEFAULT_SINCE_DAYS
    pre = apply_stages.preflight(vault, lane)
    root, ks, e17, shapes = pre["root"], pre["kill_switch"], pre["e17"], pre["shapes"]

    ledger = lane.UndoLedger(vault, run_id)
    done_keys = set(ledger.latest())
    bound = apply_stages.bind_plan(
        vault, run_id, ledger, done_keys, caps=caps, since_days=since_days,
        plan_path=plan_path, rehearsal_path=rehearsal_path, lane=lane)
    plan, todo, binding = bound["plan"], bound["todo"], bound["binding"]

    bridge = lane._bridge_for(tab_id, shapes["shapes"], run_id, use_cdp=use_cdp,
                             use_ego=use_ego)
    init = {"transport": lane._transport_name(use_cdp=use_cdp, use_ego=use_ego)}
    account = str(root["BRAIN_VAULT"])
    # THE WINDOW THE 401 DIAGNOSTIC IS ABOUT: "fresher" means captured after
    # this pass started replaying the seed it was handed. The lane's own reads
    # go out through `cap.rawFetch` and are never captured, so anything the
    # buffer holds after this instant is the APP's own traffic — the only kind
    # a re-seed could use.
    pass_started_at = lane._ts()

    results: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    stop_reason = None
    absent: list[dict[str, Any]] = []
    absent_cap = lane.absent_skip_cap(len(todo))

    # Resume before starting anything new: a row left mid-flight is the server's
    # question to answer, not ours to guess.
    for row in ledger.unfinished():
        entry, row_stop = apply_stages.resume_row(
            row, ledger, bridge, run_id,
            allow_draft_resume=allow_draft_resume, lane=lane)
        if entry is not None:
            results.append(entry)
        if row_stop:
            stop_reason = row_stop
            break

    for m in todo:
        # A resume that could not reconcile has already stopped the run.
        if stop_reason:
            break
        stop_reason, skip = apply_stages.run_todo_row(
            m, vault, run_id, bridge, todo, ledger, account, results,
            transitions, absent, absent_cap, pass_started_at, lane=lane)
        if stop_reason:
            break
        if skip:
            continue

    # TELEMETRY MAY NOT ERASE THE RECORD. This is the last of the three
    # unguarded bridge calls: it reports the page half's runtime counters, and
    # a timeout in it used to throw away the whole report of a completed night.
    try:
        runtime = bridge.call("state")["out"]
    except lane.MutationStop as exc:
        runtime = {"unavailable": str(exc)[:200]}
    return apply_stages.final_report(
        root, ks, e17, shapes, init, binding, plan, results, stop_reason,
        absent, absent_cap, transitions, runtime, ledger, lane=lane)



#: Verification words only a DISPATCHED mutation that the server ANSWERED can
#: wear. They are the dispatch proof for a legacy row that predates the
#: `dispatched` field. `verified-failed` is deliberately NOT here: the absent
#: -target skips wear it too, so it proves nothing either way.
DISPATCH_PROVEN_BY = ("verified-archived", "verified-categorized",
                      "verified-draft-saved", "verified-failed-noop")

#: The three answers `_reversal_eligibility` can give. `manual` is the one that
#: did not exist before 2026-08-12, and its absence is what made a missing
#: field mean "yes" on one path and "not eligible" on another.
REVERSAL_YES, REVERSAL_NO, REVERSAL_MANUAL = "yes", "no", "manual"

#: States a reversal may CONSIDER. `intent` joined them in the review of
#: 2026-08-12: it is written BEFORE the call, so a death between dispatch and
#: result leaves a real mutation in it — and every reversal command targeted
#: only the states written AFTER a result, so no command could ever reach it.
#: Considering is not acting: `_reversal_eligibility` routes `intent` to


REVERSIBLE_STATES = ("intent", "reconciled", "confirmed", "sent", "unknown")


def _reversal_eligibility(row: dict[str, Any]) -> str:
    """May a REVERSAL touch this row — and if it cannot tell, does it say so?

    A reversal is destructive on someone else's mailbox state: `unchip` takes a
    category off a live thread and `undo` moves a thread back into the Inbox.
    Both selected on the ledger's STATE, and two rows reach a reversible-looking
    state without anything ever leaving the machine — the "already applied"
    skip (`reconciled`, because the chip was already there when we looked) and
    an absent target. Reversing either removes a chip, or un-archives a thread,
    the OWNER put there.

    MISSING EVIDENCE WAS BEING READ BOTH WAYS (review 2026-08-12). A row with no
    `dispatched` field counted as "this run sent it" — so a legacy or resumed
    row with no dispatch evidence at all could remove a chip the owner set —
    while a row in `intent`, which is the one state that really might hold an
    unrecorded live mutation, was not selected by any reversal command. The same
    absence of proof meant yes in one place and not-eligible in the other.

    So there are THREE answers, not two:

    * `no`   — this run recorded that nothing left the machine (`dispatched:
      False`). Never touched.
    * `yes`  — this run recorded a dispatch, or a legacy row carries a
      verification word only an answered dispatch can wear.
    * `manual` — a row whose evidence cannot answer the question: a legacy row
      with no field and no proving word, or an `intent` row. It is REPORTED,
      not guessed at in either direction.

    `intent` briefly answered `yes` (2026-08-13, same day), on the argument
    that the reversal re-reads the mailbox before acting — but the re-read
    proves the thread's CURRENT state, not who caused it (review round 3).
    Crash after the write-ahead row but before dispatch, owner archives the
    thread by hand, undo runs: the "reversal" un-archives the OWNER's action —
    the same class as removing a chip the owner set. So `intent` is reachable
    (it lands in `needs_manual_resolution`, which the status page surfaces),
    and never auto-acted-on: the ledger cannot distinguish a death before
    dispatch from a death after it, and a human can.
    """
    if row.get("state") == "intent":
        return REVERSAL_MANUAL
    dispatched = row.get("dispatched")
    if dispatched is True:
        return REVERSAL_YES
    if dispatched is False:
        return REVERSAL_NO
    return (REVERSAL_YES if row.get("verification") in DISPATCH_PROVEN_BY
            else REVERSAL_MANUAL)




def undo_pass(vault: Path, run_id: str, tab_id: int | None, *,
              use_cdp: bool = False, use_ego: bool = False,
              limit: int | None = None, lane=None) -> dict[str, Any]:
    """Put a run's archives back in the Inbox — every one of them, by conversation.

    WHY IT EXISTS. The archive cap is what used to bound a bad night: three
    threads, and a human sees it before there is a fourth. The owner lifted that
    cap on 2026-08-11 ("be aggressive, do them all"), which is a legitimate call
    — an archive is reversible and the noise rules are narrow — but only while
    the reversal is ONE COMMAND rather than a morning of clicking. This is that
    command.

    It reads the run's own undo ledger, so it can only undo what that run
    recorded doing, and it keys on `conversation_id` exactly as doctrine v4.7
    requires (a move-time ItemId is a session handle, not an identity). Each
    restore is verified by re-reading, appended as its own `restore` row, and a
    thread already back in the Inbox is reported as such and NOT dispatched.
    """
    root = lane.assert_vault(vault)
    shapes = lane.load_shapes(vault)
    ledger = lane.UndoLedger(vault, run_id)
    candidates = [r for r in ledger.latest().values()
                  if r["verb"] == "archive" and r["state"] in REVERSIBLE_STATES]
    already = {r["conversation_id"] for r in ledger.latest().values()
               if r["verb"] == "restore" and r["state"] == "reconciled"}
    candidates = [r for r in candidates if r["conversation_id"] not in already]
    targets = [r for r in candidates
               if _reversal_eligibility(r) == REVERSAL_YES]
    # NEVER GUESSED AT IN EITHER DIRECTION: a legacy row with no dispatch
    # evidence is reported for a human, not silently reversed and not silently
    # dropped (review 2026-08-12).
    manual = [lane.short(r["conversation_id"]) for r in candidates
              if _reversal_eligibility(r) == REVERSAL_MANUAL]
    if limit is not None:
        targets = targets[:limit]
    if not targets:
        return {"run_id": run_id, "restored": 0, "results": [],
                "needs_manual_resolution": manual,
                "why": "this run's ledger records no archive left to put back",
                "vault_root_asserted": root}

    # `_bridge_for` has already staged and initialised whichever transport this
    # is. The second `_init_page` call that used to be here reached for
    # `bridge.tab`, which the CDP transport does not have — so this raised
    # AttributeError before its first restore on the exact transport
    # `cos_ctl.sh undo` drives it with. The tests never saw it: they patch
    # `_init_page` out.
    bridge = lane._bridge_for(tab_id, shapes["shapes"], run_id, use_cdp=use_cdp,
                             use_ego=use_ego)
    results: list[dict[str, Any]] = []
    for row in targets:
        m = {"verb": "archive", "conversation_id": row["conversation_id"],
             "restore": True}
        # ITS OWN KEY. Reusing the archive's would make the restore row SUPERSEDE
        # the archive in `latest()`, erasing the record of what the run did and
        # making the "already restored" check answer about the wrong thing.
        base = dict(row, verb="restore",
                    idempotency_key=f"{row['conversation_id']}|restore",
                    reason=f"undo of this run's archive ({run_id})",
                    action_ts=lane._ts())
        # THE INTENT ROW IS ON DISK BEFORE THE CALL — `apply_pass` step 2's rule,
        # and this lane dispatched first and recorded afterwards (review
        # 2026-08-12). A response lost after the server took the move left no
        # durable trace that this command had ever touched the thread.
        # `dispatched=None`, EXPLICITLY. `base` is a copy of the forward archive
        # row, which carries `dispatched: True` — so the reversal's write-ahead
        # row inherited a dispatch claim about a request this loop had not made
        # yet (review 2026-08-12). A reversal intent gets its own field.
        ledger.append(dict(base, state="intent", connector_result=None,
                           verification=None, dispatched=None))
        out = bridge.call("apply", {"mutation": m})["out"]
        applied = out.get("verification") in ("verified-archived",
                                              "response-confirmed")
        ledger.append(dict(base, state="reconciled" if applied else "sent",
                           connector_result=out.get("outcome"),
                           verification=out.get("verification"),
                           dispatched=out.get("dispatched")))
        results.append({"conversation_id": lane.short(row["conversation_id"]),
                        "verification": out.get("verification"),
                        "outcome": out.get("outcome")})
    return {"run_id": run_id, "restored": sum(1 for r in results
                                              if r["verification"] in
                                              ("verified-archived",
                                               "response-confirmed")),
            "attempted": len(results), "results": results,
            "needs_manual_resolution": manual,
            "vault_root_asserted": root}




def unchip_pass(vault: Path, run_id: str, tab_id: int | None, *,
                use_cdp: bool = False, use_ego: bool = False,
                limit: int | None = None, lane=None) -> dict[str, Any]:
    """Take every managed chip this run put on back off — verified per thread.

    WHY IT EXISTS. The same argument as `undo_pass`, one lane over: the chip cap
    came off with the archive cap, and an uncapped write is only safe while its
    reversal is ONE COMMAND. Until 2026-08-12 the chip lane had no reversal at
    all — a reduced `Categories` updates the forward RULE and leaves the chip on
    the thread — so this rides the captured `CategoriesToRemove` shape instead.

    Same discipline as the undo: it reads only THIS run's ledger, keys on
    `conversation_id`, verifies each removal by re-reading, appends its own
    `unchip` row (never superseding the chip row it reverses), and skips a thread
    already unchipped. A chip row with no chip NAME is not guessed at — removing
    "the chip" is not an instruction anything can follow — it is reported and
    left for a human.
    """
    root = lane.assert_vault(vault)
    shapes = lane.load_shapes(vault)
    ledger = lane.UndoLedger(vault, run_id)
    latest = ledger.latest()
    candidates = [r for r in latest.values()
                  if r["verb"] == "categorize" and r.get("mode") != "remove"
                  and r["state"] in REVERSIBLE_STATES]
    already = {r["conversation_id"] for r in latest.values()
               if r["verb"] == "unchip" and r["state"] == "reconciled"}
    candidates = [r for r in candidates if r["conversation_id"] not in already]
    targets = [r for r in candidates
               if _reversal_eligibility(r) == REVERSAL_YES]
    manual = [lane.short(r["conversation_id"]) for r in candidates
              if _reversal_eligibility(r) == REVERSAL_MANUAL]
    unnamed = [r for r in targets if not r.get("chip")]
    targets = [r for r in targets if r.get("chip")]
    if limit is not None:
        targets = targets[:limit]
    if not targets:
        return {"run_id": run_id, "unchipped": 0, "results": [],
                "no_chip_name_on_row": [lane.short(r["conversation_id"])
                                        for r in unnamed],
                "needs_manual_resolution": manual,
                "why": "this run's ledger records no chip left to take off",
                "vault_root_asserted": root}

    bridge = lane._bridge_for(tab_id, shapes["shapes"], run_id, use_cdp=use_cdp,
                             use_ego=use_ego)
    results: list[dict[str, Any]] = []
    for row in targets:
        m = {"verb": "categorize", "conversation_id": row["conversation_id"],
             "chip": row["chip"], "mode": "remove"}
        # ITS OWN KEY, for the reason `restore` has its own: reusing the chip's
        # would make the reversal supersede the record of what the run did.
        base = dict(row, verb="unchip",
                    idempotency_key=f"{row['conversation_id']}|unchip",
                    mode="remove",
                    reason=f"unchip of this run's chip ({run_id})",
                    action_ts=lane._ts())
        # WRITE-AHEAD, like the archive lane and for its reason (review
        # 2026-08-12): this loop dispatched a category removal and only then
        # recorded that it had, so a lost response left the chip gone from the
        # mailbox and nothing at all on disk saying who took it off.
        # Its own `dispatched`, for `undo_pass`'s reason one lane over.
        ledger.append(dict(base, state="intent", connector_result=None,
                           verification=None, observed_after=None,
                           dispatched=None))
        out = bridge.call("apply", {"mutation": m})["out"]
        applied = out.get("verification") in ("verified-categorized",
                                              "response-confirmed")
        ledger.append(dict(base, state="reconciled" if applied else "sent",
                           connector_result=out.get("outcome")
                           or out.get("response_code"),
                           verification=out.get("verification"),
                           observed_after=out.get("observed_after"),
                           dispatched=out.get("dispatched")))
        results.append({"conversation_id": lane.short(row["conversation_id"]),
                        "chip": row["chip"],
                        "verification": out.get("verification"),
                        "observed_after": out.get("observed_after"),
                        "outcome": out.get("outcome")})
    return {"run_id": run_id,
            "unchipped": sum(1 for r in results
                             if r["verification"] in ("verified-categorized",
                                                      "response-confirmed")),
            "attempted": len(results), "results": results,
            "no_chip_name_on_row": [lane.short(r["conversation_id"]) for r in unnamed],
            "needs_manual_resolution": manual,
            "vault_root_asserted": root}




def canary_drill(vault: Path, run_id: str, tab_id: int, conv_id: str,
                 *, use_cdp: bool = False, use_ego: bool = False,
                 lane=None) -> dict[str, Any]:
    """Archive one disposable row, undo it, replay the undo, round-trip a chip.

    Every step produces a RECEIPT read back from the server, and the canary file
    is written only when all four are present — "writing the canary file without
    executing every drill step on live rows is an E17 FAIL; the file asserts
    receipts, never bare fields".
    """
    root = lane.assert_vault(vault)
    shapes = lane.load_shapes(vault)
    if shapes["missing"]:
        raise lane.MutationStop(f"no approved shapes at {shapes['path']}")
    bridge = lane._bridge_for(tab_id, shapes["shapes"], run_id, use_cdp=use_cdp,
                             use_ego=use_ego)

    chip_open = bool((shapes["shapes"] or {}).get("UpdateItem"))
    receipts, before, arch, undo = canary_stages.drill_receipts(
        bridge, conv_id, chip_open, MANAGED_CHIPS[2], short=short)

    missing, ok = canary_stages.drill_ok(receipts, arch, undo, CANARY_STEPS)
    if not ok:
        return {"written": False, "receipts": receipts, "missing_steps": missing,
                "why": "a drill step produced no verification receipt; the canary "
                       "file is NOT written (a written file is not a run drill)",
                "vault_root_asserted": root}

    written = canary_stages.write_canary_file(
        vault, receipts, before, chip_open, ts=_ts,
        write_atomic=_write_text_atomic, ops_mode=_OPS_MODE,
        mutation_lane=MUTATION_LANE, primitive=PRIMITIVE)
    return {**written, "receipts": receipts, "vault_root_asserted": root}

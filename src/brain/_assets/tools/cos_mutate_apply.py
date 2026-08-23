"""Row-handling sub-steps of the locked mutation apply pass (s18 drain).

Every sub-step is moved verbatim out of ``cos_mutate._apply_pass_locked`` and
receives ``lane`` — the loaded ``cos_mutate`` module's own namespace — so each
parent name it touches (``stopped``, ``_undo_row``, ``MutationStop``, …) is
looked up at CALL time and a test that monkeypatches one on the parent keeps
governing this code. The sub-steps run INSIDE the caller's mutation-lane lock
and never take it themselves.
"""
from __future__ import annotations

from typing import Any


def preflight(vault, lane) -> dict[str, Any]:
    """Assert the four gates a mutation pass refuses to run without."""
    root = lane.assert_vault(vault)
    ks = lane.kill_switch(vault)
    if not ks["enabled"]:
        raise lane.MutationStop(f"the kill switch at {ks['source']} reads "
                                f"enabled: false ({ks['state']}) — no mutation runs")
    e17 = lane.canary_status(vault)
    if not e17["valid"]:
        raise lane.MutationStop(
            f"E17: the {lane.MUTATION_LANE!r}-lane undo canary does not satisfy "
            f"the gate ({e17['why']}). Run the drill (`cos_mutate.py canary`) "
            "before any mutation — guard condition 5 exists to make an "
            "unverified undo path impossible to mutate through.")
    shapes = lane.load_shapes(vault)
    if shapes["missing"] or not shapes["shapes"]:
        raise lane.MutationStop(
            f"no approved mutation shapes at {shapes['path']}. A mutation "
            "request is a REPLAY of a shape the server already accepted for "
            "that verb (doctrine v4.7); there is no path that builds one.")
    return {"root": root, "kill_switch": ks, "e17": e17, "shapes": shapes}


def bind_plan(vault, run_id: str, ledger, done_keys: set, *,
              caps, since_days, plan_path, rehearsal_path, lane) -> dict[str, Any]:
    """Freeze what this pass will dispatch: the plan and its binding record."""
    if plan_path is not None:
        frozen = lane._frozen_todo(vault, run_id, plan_path, rehearsal_path,
                                   done_keys)
        plan, todo = frozen["plan"], frozen["todo"]
        binding = {"source": "frozen", "plan": str(plan_path),
                   "rehearsal": str(rehearsal_path),
                   "plan_digest": plan["plan_digest"],
                   "planned": len(plan["mutations"]),
                   "already_carried_out_this_checkpoint":
                       len(plan["mutations"]) - len(todo)}
    else:
        plan = lane.build_plan(vault, run_id, caps=caps, since_days=since_days,
                               applied=ledger.applied_counts(), skip_keys=done_keys)
        todo = list(plan["mutations"])
        binding = {"source": "rebuilt-by-the-apply",
                   "plan_digest": plan["plan_digest"],
                   "why": "no --plan was given, so this pass planned for "
                          "itself and no rehearsal is bound to what it "
                          "dispatched. The CLI refuses this; only an "
                          "in-process caller reaches it"}
    # WRITTEN WHERE A VERDICT CAN READ IT (review 2026-08-13, round 5). This
    # value existed only in the `--out` report, which lives in the repo's
    # evidence directory — so `cos_runverify`, which scores a run from the
    # VAULT's artifacts, could never see it, and nothing anywhere failed on
    # `rebuilt-by-the-apply`. A field written and read by nothing is a comment
    # (`hardening-prose-is-not-a-mechanism`). `check_plan_binding` reads this
    # file and FAILS the run.
    import json                                                  # noqa: PLC0415
    lane._write_text_atomic(lane.plan_binding_path(vault, run_id),
                            json.dumps(binding, indent=2, ensure_ascii=False)
                            + "\n")
    return {"plan": plan, "todo": todo, "binding": binding}


def resume_row(row: dict[str, Any], ledger, bridge, run_id: str, *,
               allow_draft_resume: bool, lane) -> tuple[dict[str, Any] | None,
                                                        str | None]:
    """Reconcile ONE unfinished row; return (result entry, stop reason)."""
    if row["verb"] == "draft" and not allow_draft_resume:
        ledger.append(dict(row, state="unknown",
                           connector_result="manual-resolution-required",
                           verification=lane.DRAFT_RESUME_POLICY))
        return None, None
    # A BRIDGE CALL OUTSIDE THE `try` IS A REPORT THAT NEVER GETS WRITTEN
    # (review 2026-08-12). Three of them sat outside — this resume
    # reconcile, the per-row `resolve` below, and the closing `state` — and
    # a timeout in any one raised straight past `apply_pass` into `main`,
    # whose stop handler writes a report hard-coding `results: []`. A night
    # that had applied fifty mutations then produced an artifact claiming
    # it had done nothing.
    try:
        rec = bridge.call("reconcile", {"mutation": {
            "verb": row["verb"], "conversation_id": row["conversation_id"],
            "chip": row.get("chip"), "mode": row.get("mode"),
            "signature": lane.draft_signature(run_id,
                                              row["conversation_id"])}})["out"]
    except lane.MutationStop as exc:
        ledger.append(dict(row, state="unknown",
                           connector_result="reconcile-unavailable",
                           verification=f"the resume reconcile could not "
                                        f"run: {str(exc)[:200]}"))
        return None, (f"the resume reconcile for {row['verb']} on "
                      f"{lane.short(row['conversation_id'])} failed: "
                      f"{str(exc)[:200]}")
    ledger.append(dict(row,
                       state=("reconciled" if rec.get("applied")
                              else "aborted-not-applied" if rec.get("conclusive")
                              else "unknown"),
                       connector_result=rec.get("observed"),
                       verification=f"reconciled by re-read: {rec.get('query')}"))
    return {"resumed": True, **rec}, None


def resolve_failure_stop(m: dict[str, Any], exc, bridge, transitions: list,
                         pass_started_at: str, *,
                         lane) -> str:
    """Name why the pre-dispatch mailbox read died (401 diagnostics included)."""
    # A 401 HERE IS THE STALE BEARER, AND IT IS NAMED (run 130) — but
    # NOT as a recovery that was tried and failed (review 2026-08-13,
    # round 2). This used to read "one re-prime did not recover it",
    # asserting an attempt the page cannot make.
    #
    # AND NOT AS AN ABSOLUTE EITHER (round 3). The corrected text said
    # the app issues its FindItem "at boot only", which is what s03
    # concluded from a SETTLED tab and what the capture hook's own
    # header corrects: measured three times on 2026-08-11, a hook
    # installed at `readyState=complete` does capture an authenticated
    # `FindItem` once the tab becomes ACTIVE and the list settles.
    #
    # SO IT IS MEASURED NOW, NOT ASSERTED (round 4, Claude MEDIUM). The
    # corrected text still said "no fresher envelope had been captured
    # by the time it failed" while NOTHING in this build read the
    # capture buffer at 401 time — one unverified absolute swapped for
    # another. The page has the query (`cap.freshestSeed`), so the host
    # asks it, over the window that starts at this transition, and
    # prints the ANSWER. A probe that cannot run is reported as
    # unmeasured; it is never silently read as "none".
    auth401 = bool(getattr(exc, "auth401", False))
    seed = lane._seed_probe(bridge, pass_started_at) if auth401 else None
    if auth401:
        transitions.append({"at": lane._ts(), "reason": "http-401",
                            "leg": "resolve",
                            "recovery": "host-only-requires-reprepare",
                            "fresher_seed": seed.get("fresher_seed"),
                            "fresher_seed_measured": seed.get("measured"),
                            "conversation_id": lane.short(m["conversation_id"])})
    return (f"the mailbox read before {m['verb']} on "
            f"{lane.short(m['conversation_id'])} failed: "
            f"{str(exc)[:200]}"
            + (" — the replayed envelope's bearer aged out "
               "part-way through this pass. "
               + lane._seed_probe_sentence(seed)
               + " This lane does not retry a 401 "
               "automatically: re-seeding is the host's call, "
               "deliberately. So the lane stopped here; "
               "everything logged above applied and verified. "
               "Re-run the night — `cos_cdp_capture.py "
               "--prepare` navigates and takes a fresh envelope"
               if auth401 else ""))


def apply_failure(m: dict[str, Any], exc, intent: dict[str, Any], bridge,
                  ledger, results: list, transitions: list, *,
                  lane) -> str:
    """Handle a dispatch that died: reconcile the possibly-applied mutation."""
    in_flight = bool(getattr(exc, "mutation_in_flight", False))
    if getattr(exc, "canary449", False):
        transitions.append({"at": lane._ts(), "reason": "http-449",
                            "mutation_in_flight": in_flight,
                            "conversation_id": lane.short(m["conversation_id"])})
    if getattr(exc, "auth401", False):
        transitions.append({"at": lane._ts(), "reason": "http-401",
                            "leg": "apply",
                            "recovery": "never-a-mutation-is-re-issued",
                            "mutation_in_flight": in_flight,
                            "conversation_id": lane.short(m["conversation_id"])})
    # The response is lost, not the outcome: ASK THE SERVER.
    #
    # AND GUARD THE ASKING (review 2026-08-13, round 3). This is the
    # FOURTH bridge call — the round-2 fix guarded the three sitting
    # outside the `try` and missed the one inside its own `except`.
    # The transport death that raised `exc` is exactly the failure
    # most likely to kill this call too, and an unguarded raise here
    # propagated to `main`, whose stop handler writes `results: []`
    # over a night of applied mutations — the precise erased-report
    # failure the round-2 fix claimed to close.
    try:
        rec = bridge.call("reconcile", {"mutation": {
            "verb": m["verb"], "conversation_id": m["conversation_id"],
            "chip": m.get("chip"), "mode": m.get("mode"),
            "signature": m.get("signature")}})["out"]
    except lane.MutationStop as rexc:
        # Nothing is known: the mutation MAY have applied and the
        # re-read could not run. `unknown` is the only honest state,
        # and the report of everything BEFORE this row must survive.
        ledger.append(dict(intent, state="unknown",
                           connector_result=str(exc)[:400],
                           verification=f"the lost-response reconcile "
                                        f"could not run: "
                                        f"{str(rexc)[:200]}"))
        results.append({"conversation_id": lane.short(m["conversation_id"]),
                        "verb": m["verb"], "stopped": True,
                        "reconciliation": None,
                        "reconcile_unavailable": str(rexc)[:200]})
        return (f"{str(exc)[:200]} — and the reconcile after it "
                f"also failed: {str(rexc)[:200]}")
    ledger.append(dict(intent,
                       state=("reconciled" if rec.get("applied")
                              else "aborted-not-applied"
                              if rec.get("conclusive") else "unknown"),
                       connector_result=str(exc)[:400],
                       verification=f"reconciliation after a lost "
                                    f"response: {rec.get('observed')}"))
    if transitions:
        transitions[-1]["in_flight_449_outcome"] = (
            "stopped-and-reconciled" if in_flight else "none-in-flight")
        transitions[-1]["reconciliation"] = rec
    results.append({"conversation_id": lane.short(m["conversation_id"]),
                    "verb": m["verb"], "stopped": True,
                    "reconciliation": rec})
    return str(exc)[:400]


def final_report(root, ks, e17, shapes, init, binding, plan, results,
                 stop_reason, absent, absent_cap, transitions, runtime,
                 ledger, *, lane) -> dict[str, Any]:
    """Assemble the apply pass's report of everything it did and stopped at."""
    return {
        "vault_root_asserted": root, "kill_switch": ks, "e17": e17,
        "shapes_path": shapes["path"],
        "shape_fingerprints": {k: v.get("fingerprint")
                               for k, v in shapes["shapes"].items()},
        "capture": (init.get("out") or {}).get("capture"),
        # WHAT THIS PASS WAS BOUND BY (K1). A report that does not say whether
        # the plan it dispatched was the rehearsed artifact cannot be audited
        # for the one property the whole gate exists to give.
        "plan_binding": binding,
        "plan": plan, "results": results, "stopped": stop_reason,
        "skipped_absent": absent, "skipped_absent_cap": absent_cap,
        "http_449_transitions": transitions,
        "runtime": runtime,
        "ledger": str(ledger.path),
        "final_states": {k: v["state"] for k, v in ledger.latest().items()},
    }


def record_outcome(m: dict[str, Any], intent: dict[str, Any],
                   res: dict[str, Any], ledger, results: list,
                   absent: list, absent_cap: int, todo_total: int, *,
                   lane) -> tuple[str | None, bool]:
    """Write ONE dispatched row's outcome; return (stop reason, skip row)."""
    state = res.get("state")
    # A CONCLUSIVE ABSENCE IS TERMINAL, AND IT IS NOT `sent` (review
    # 2026-08-12). The page's skip carries the state word it would have
    # used had it dispatched, and this row wrote that word to disk before
    # the skip rule below ever ran — so a chip that never left the machine
    # was recorded `sent`, and `unchip` selects on `sent`. The run did not
    # apply it, so the ledger says so: `aborted-not-applied`, which is
    # terminal, spends no cap, and is not eligible for a reversal.
    absent_skip = (res.get("dispatched") is False
                   and res.get(lane.ABSENT_TARGET_FLAG) is True
                   and res.get("absence_conclusive") is True)
    if absent_skip:
        state = "aborted-not-applied"
    ledger.append(dict(intent, state=state if state in lane.STATES else "unknown",
                       connector_result=res.get("response_code")
                       or res.get("outcome"),
                       verification=res.get("verification"),
                       new_item_id=res.get("new_item_id"),
                       # WHETHER ANYTHING LEFT THE MACHINE, on the row. The
                       # reversals (`undo`, `unchip`) may only touch what
                       # this run actually sent, and the ledger was the one
                       # place that fact was not written down.
                       dispatched=res.get("dispatched"),
                       receipts=res.get("receipts")))
    # `state` and not `res["state"]`: the page reports the word it would
    # have used had it dispatched, and the row on disk says what this run
    # actually did. Two records of one event may not disagree.
    results.append({**res, "state": state,
                    "conversation_id": lane.short(m["conversation_id"])})
    if state != "reconciled":
        # A TARGET THAT ISN'T THERE IS NOT A FAILED MUTATION. The page half
        # labels "I looked in the folder and the thread was gone" with the
        # same `verified-failed` word it uses for "I sent a change and could
        # not confirm it", and this loop stopped the whole night on either.
        # Measured on run 125, the first unattended run: one thread had
        # moved between the plan and the apply, and the run halted at chip
        # 30 of 65 having archived nothing and drafted nothing — while its
        # last log line still said `done`. On a mailbox the owner is
        # actually using, a thread moving mid-run is ordinary, so that halt
        # would have fired most mornings.
        #
        # The discriminator is NOT the word, it is `dispatched` AND proof
        # that the folder was read to the end. Nothing left the machine, so
        # there is no outcome in doubt and nothing to reconcile — but only
        # if the thread was really gone. `dispatched: false` alone cannot
        # tell "it moved" from "I could not see it" (review 2026-08-12): a
        # truncated enumeration or a throttled GetItem produced the same
        # row, so a browser-side read failure could drop every row of a
        # night and still report a completed run. The page half now returns
        # `absence_conclusive` from the enumeration's own
        # last-item-in-range flag, and an INCONCLUSIVE absence keeps its own
        # outcome word and falls through to the stop below. A row that WAS
        # dispatched and did not reconcile still stops the run, on the
        # original reasoning and unchanged.
        if absent_skip:
            absent.append({"conversation_id": lane.short(m["conversation_id"]),
                           "verb": m["verb"], "outcome": res.get("outcome"),
                           # The evidence the skip was decided on, so the
                           # report can be audited instead of believed.
                           "enumeration_terminated":
                               res.get("enumeration_terminated"),
                           "enumeration_pages": res.get("enumeration_pages"),
                           "enumeration_folder":
                               res.get("enumeration_folder")})
            if len(absent) > absent_cap:
                return (f"{len(absent)} of {todo_total} planned rows were absent "
                        f"from the mailbox, past this run's ceiling of "
                        f"{absent_cap} — that many threads do not move in one "
                        f"night, so this is the browser reading a starved "
                        f"folder and not a busy mailbox", False)
            return None, True
        return (f"verification failed for {m['verb']} on "
                f"{lane.short(m['conversation_id'])}: "
                f"{res.get('verification')}", False)
    return None, False


def run_todo_row(m: dict[str, Any], vault, run_id: str, bridge, todo, ledger,
                 account: str, results: list, transitions: list,
                 absent: list, absent_cap: int, pass_started_at: str, *, lane=None):
    """One mutation row of `_apply_pass_locked`'s todo loop (batch-2 drain).

    Runs INSIDE the caller's lane lock and never takes it itself — same
    contract as every sub-step here. Returns ``(stop_reason, skip)``:
    ``stop_reason`` set ends the whole pass; ``skip`` only this row.
    """
    if lane.stopped(vault, run_id):
        return f"the stop file {lane.stop_file(vault, run_id)} appeared", False
    try:
        resolved = bridge.call("resolve", {
            "conversation_id": m["conversation_id"],
            "folder": "inbox"})["out"]
    except lane.MutationStop as exc:
        return resolve_failure_stop(
            m, exc, bridge, transitions, pass_started_at, lane=lane), False
    # 2. THE UNDO ROW IS ON DISK BEFORE THE CALL.
    intent = lane._undo_row(m, resolved, state="intent", run_id=run_id,
                            account=account)
    ledger.append(intent)
    try:
        res = bridge.call("apply", {"mutation": m})["out"]
    except lane.MutationStop as exc:
        return apply_failure(
            m, exc, intent, bridge, ledger, results, transitions, lane=lane), False
    return record_outcome(
        m, intent, res, ledger, results, absent, absent_cap, len(todo),
        lane=lane)

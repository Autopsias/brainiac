"""Sub-steps of the run validator's PLAN BINDING check (s18 extraction).

One function per stage of ``check_plan_binding`` (K1): the applicability
decision (no binding, no ledger, what do the counters say), the binding
document's shape validation, and the plan↔digest↔rehearsal↔ledger join. The
check itself stays in :mod:`brain.cos_runverify` with an unchanged signature;
this module never imports it. The lane module's own callables
(``mutation_counts``, ``_cos_mutate``, ``_read_jsonl``) arrive as arguments,
so their definitions stay single and a monkeypatch on the parent keeps
governing them.

The check's full round-history prose (rounds 5-7: H-forge, H-disarm, the
gap-05 mount move) moved here with the code it explains — see
``_applicability_row`` and ``_join_row`` docstrings and comments.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .cos_runverify_checks import FAIL, INCONCLUSIVE, PASS, _row


def _applicability_row(vault, run_id: str, binding_p: Path, ledger_p: Path,
                       *, mutation_counts: Callable) -> dict[str, Any]:
    """Decide whether a mutating pass even happened; a read-only night passes.

    APPLICABILITY IS NOT DECIDED BY A VM-WRITABLE FILE (review 2026-08-13,
    round 7, H-disarm). Round 6 moved the binding host-private and joined its
    digest, which closed FORGERY — but the line that decided whether any of
    that ran still asked `<vault>/cos-ops` for the undo ledger, a zone
    `cos.run_ops_dir` documents as "VM-writable by nature". A VM does not have
    to author a plan, a rehearsal and a digest to beat that: it DELETES one file
    in a directory it owns, and a run that dispatched real mutations under
    `rebuilt-by-the-apply` scores PASS. The removed FAIL takes RUN_INVALID with
    it. It is the same shape `unledgered_mutations` exists to close one screen
    up — an absent ledger read as "nothing acted" is an instrument that cannot
    fail (measured, run 106) — so it takes the same two answers:

      * the HOST-PRIVATE binding's own existence makes this control APPLY. The
        apply writes that file on every mutating pass, into `runs_dir` — which
        is off the VirtioFS mount since gap-05 (2026-08-16) and so is a
        directory the VM genuinely cannot reach. Round 7 wrote that sentence
        while `runs_dir` was still `<vault>/.brain/cos/host/runs`, inside the
        workspace: true of the VM's RULES, false of the filesystem, and a
        comment that overstates a protection is how the gap survived.
      * a missing undo ledger is CORROBORATED against the run's own mutation
        counters. Counters above zero with no ledger is INCONCLUSIVE — the run
        says it mutated and the artifact that would say under which plan is
        gone. Only counters-all-zero AND no binding AND no ledger is the
        read-only night this clause is for.
    """
    if not binding_p.exists() and not ledger_p.exists():
        did = mutation_counts(vault, run_id)
        if did:
            return _row("plan_binding", INCONCLUSIVE,
                        "this run has NO plan binding and NO undo ledger, but "
                        "its own metrics row records "
                        + ", ".join(f"{k}={v}" for k, v in sorted(did.items()))
                        + ". The ledger lives in the VM-writable `cos-ops` zone,"
                          " so its absence beside non-zero mutation counters is"
                          " a removed artifact, not a quiet night — and which"
                          " plan reached the mailbox is unknown",
                        reexecuted=True)
        return _row("plan_binding", PASS,
                    "this run dispatched no mutation (no plan binding, no undo "
                    "ledger, and zero mutation counters), so no plan was bound "
                    "and there is nothing here to check",
                    reexecuted=True)
    return {}


def _binding_doc_row(path: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Read and shape-validate the binding; ``(row, None)`` means proceed.

    HOST-PRIVATE, and JOINED — nothing a VM can write moves this toward PASS
    (review 2026-08-13, round 6 H-forge, wording corrected round 7). The
    binding lives beside the run manifest now (``cos.runs_dir``, off the mount
    since gap-05), and ``source: frozen`` is only the door: this control
    re-verifies the SUBSTANCE the same way the category check re-joins its
    label, by re-hashing the frozen plan the binding names and confirming the
    binding's every claim about it. A one-key ``{"source":"frozen"}`` is
    malformed, not a pass.

    FULL SCHEMA — a ``frozen`` source with nothing behind it is the forgery
    shape, not a pass. The binding must NAME the plan, the rehearsal, the
    digest and the count, or there is nothing to re-join it to.
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _row("plan_binding", INCONCLUSIVE,
                    "this run dispatched mutations and the apply's plan "
                    f"binding at {path.name} could not be read "
                    f"({str(exc)[:120]}). Which plan reached the mailbox is "
                    "unknown, and an unknown is not a pass",
                    reexecuted=True), None
    if not isinstance(doc, dict):
        return _row("plan_binding", FAIL,
                    f"the plan binding at {path.name} is not a JSON object — a "
                    "binding that is not even a record cannot say which plan "
                    "reached the mailbox",
                    reexecuted=True), None
    source = doc.get("source")
    if source != "frozen":
        return _row("plan_binding", FAIL,
                    f"this run dispatched mutations under plan binding "
                    f"{source!r}, not `frozen`. The plan that reached the "
                    "mailbox is not the plan any rehearsal validated — which is "
                    "the one thing the frozen artifact exists to make "
                    "impossible",
                    reexecuted=True), None
    plan_ref = doc.get("plan")
    reh_ref = doc.get("rehearsal")
    stamped = doc.get("plan_digest")
    planned = doc.get("planned")
    bad = [k for k, v in (("plan", plan_ref), ("rehearsal", reh_ref),
                          ("plan_digest", stamped))
           if not isinstance(v, str) or not v.strip()]
    # `type(... ) is int`, not `isinstance` — `True` IS an `int` in Python, so a
    # binding carrying `"planned": true` satisfied a one-mutation join (round 7,
    # Codex LOW). A negative count is not a count either.
    if type(planned) is not int or planned < 0:
        bad.append("planned")
    if bad:
        return _row("plan_binding", FAIL,
                    "the plan binding claims `source: frozen` but is missing or "
                    f"malformed on {', '.join(sorted(bad))} — a frozen binding "
                    "must carry the plan it dispatched, the rehearsal that "
                    "validated it, that plan's digest and its planned count, or "
                    "it is a bare assertion with nothing behind it",
                    reexecuted=True), None
    return {}, {"plan": plan_ref, "rehearsal": reh_ref,
                "plan_digest": stamped, "planned": planned}


def _rejoined_plan_row(run_id: str, doc: dict[str, Any], *,
                       cos_mutate: Callable) -> tuple[dict[str, Any] | None,
                                                      dict[str, Any] | None]:
    """Re-hash the named frozen plan; ``(row, None)`` refuses, ``(None, plan)`` proceeds.

    THE JOIN. Re-hash the frozen plan the binding names, through the SAME
    loader the apply used (`load_frozen_plan` re-verifies the plan hashes to
    its own stamp), and confirm every claim the binding makes about it. The
    digest hash is imported, never restated — the engine holds ONE notion of
    a plan's identity.
    """
    plan_ref, stamped, planned = doc["plan"], doc["plan_digest"], doc["planned"]
    mutate = cos_mutate()
    if mutate is None:
        return _row("plan_binding", INCONCLUSIVE,
                    "this run dispatched mutations but the mutation toolchain is "
                    "not on disk beside the engine, so the host cannot re-hash "
                    "the frozen plan to confirm the binding's digest — an "
                    "unverifiable binding is not a pass",
                    reexecuted=True), None
    plan_p = Path(plan_ref)
    if not plan_p.is_file():
        return _row("plan_binding", INCONCLUSIVE,
                    f"this run's frozen plan at {plan_ref} is not on disk "
                    "(evidence pruned, or the path the binding names is wrong), "
                    "so the host cannot re-hash it to confirm the binding — an "
                    "unverifiable binding is not a pass",
                    reexecuted=True), None
    try:
        plan = mutate.load_frozen_plan(plan_p)
    except Exception as exc:                              # noqa: BLE001
        return _row("plan_binding", FAIL,
                    f"the frozen plan the binding names ({plan_ref}) is on disk "
                    f"but does not hash to its own stamp: {str(exc)[:160]}. A "
                    "binding that points at a tampered plan proves nothing about "
                    "what reached the mailbox",
                    reexecuted=True), None
    if plan.get("plan_digest") != stamped:
        return _row("plan_binding", FAIL,
                    "the plan binding's digest does not match the frozen plan it "
                    "names — the binding claims a plan the plan file is not, so "
                    "it is not evidence of which plan was dispatched",
                    reexecuted=True), None
    if plan.get("run_id") != run_id:
        return _row("plan_binding", FAIL,
                    "the frozen plan the binding names was built for run "
                    f"{plan.get('run_id')!r}, not {run_id!r} — a plan bound to "
                    "another run is not this run's dispatched plan",
                    reexecuted=True), None
    if len(plan.get("mutations") or []) != planned:
        return _row("plan_binding", FAIL,
                    "the plan binding's planned count disagrees with the frozen "
                    "plan it names — the two do not describe one plan",
                    reexecuted=True), None
    return None, plan


def _rehearsal_row(reh_ref: str, stamped: str) -> tuple[dict[str, Any] | None,
                                                       dict[str, Any] | None]:
    """Read the named rehearsal and confirm it validated THIS digest."""
    reh_p = Path(reh_ref)
    if not reh_p.is_file():
        return _row("plan_binding", INCONCLUSIVE,
                    f"the rehearsal the binding names ({reh_ref}) is not on disk, "
                    "so the host cannot confirm the dispatched plan was "
                    "rehearsed — an unverifiable binding is not a pass",
                    reexecuted=True), None
    try:
        reh = json.loads(reh_p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _row("plan_binding", FAIL,
                    f"the rehearsal the binding names ({reh_ref}) could not be "
                    f"read ({str(exc)[:120]}) — an unreadable rehearsal cannot "
                    "prove the dispatched plan was rehearsed",
                    reexecuted=True), None
    if not isinstance(reh, dict) or reh.get("plan_digest") != stamped:
        return _row("plan_binding", FAIL,
                    "the rehearsal the binding names validated a different plan "
                    "digest — it proves nothing about the plan that reached the "
                    "mailbox",
                    reexecuted=True), None
    return None, reh


def _join_row(vault, run_id: str, doc: dict[str, Any], ledger_p: Path, *,
              mutation_counts: Callable, cos_mutate: Callable,
              read_jsonl: Callable) -> dict[str, Any]:
    """Re-hash the named plan and join it to what the ledger says was sent."""
    reh_ref = doc["rehearsal"]
    stamped, planned = doc["plan_digest"], doc["planned"]
    row, plan = _rejoined_plan_row(run_id, doc, cos_mutate=cos_mutate)
    if row:
        return row
    mutate = cos_mutate()
    assert mutate is not None  # _rejoined_plan_row already refused the None case
    # The rehearsal must have named THIS digest — the same equality
    # `_frozen_todo` enforces in `cos_mutate` before it dispatches.
    row, reh = _rehearsal_row(reh_ref, stamped)
    if row:
        return row
    # AND THE PLAN IS JOINED TO WHAT WAS ACTUALLY DISPATCHED (review
    # 2026-08-13, round 7). Everything above authenticates the binding against
    # the plan file it NAMES — plan↔digest↔rehearsal, a closed triangle that
    # never once looks at the mailbox rows. So a binding could name a real,
    # rehearsed, correctly-hashed plan while the undo ledger recorded
    # dispatches from a different one, and this control would call it FROZEN.
    # The undo ledger is the record of what the lane sent, keyed exactly as the
    # plan is: `conversation_id|verb`, per v4.7 (a move-time ItemId is a
    # session handle, never an identity). Every dispatched row must be IN the
    # plan.
    #
    # Only the plan's own verbs are joined. `restore` and `unchip` are the
    # REVERSAL lane (`cos_mutate.undo_pass`/`unchip_pass`), which appends to the
    # same run's ledger by design and is never in a plan — joining those would
    # fail every run an operator undid.
    #
    # The ledger is append-only with one row per state TRANSITION, so it is
    # counted in distinct KEYS — `intent` then `reconciled` for one archive is
    # one mutation, not two.
    # The key is DERIVED from `(conversation_id, verb)`, never read from the
    # row's own `idempotency_key` (round 7, Codex). The ledger lives in the
    # VM-writable ops zone, and preferring a supplied key let a row carry
    # `conversation_id: cEVIL, idempotency_key: "cA|archive"` — an off-plan
    # dispatch wearing an in-plan key, joined as covered. The honest producer
    # writes `idempotency_key = f"{conv}|{verb}"` (cos_mutate.py), so deriving
    # loses nothing on an honest row and refuses the forge. `verb` is still the
    # trusted discriminator for the reversal-lane exclusion below, which is fine
    # — an attacker relabelling a real mutation's verb to `restore` to duck the
    # join makes it disappear from `plan_keys` too, changing nothing.
    acted = {f"{r.get('conversation_id')}|{r.get('verb')}"
             for r in read_jsonl(ledger_p) if isinstance(r, dict)
             and str(r.get("verb") or "") in mutate.MUTATION_VERBS}
    plan_keys = {f"{m.get('conversation_id')}|{m.get('verb')}"
                 for m in (plan.get("mutations") or []) if isinstance(m, dict)}
    unbound = sorted(acted - plan_keys)
    if unbound:
        return _row("plan_binding", FAIL,
                    f"{len(unbound)} mutation(s) in this run's undo ledger are "
                    "NOT in the frozen plan the binding names "
                    f"({', '.join(unbound[:5])}"
                    + (", …" if len(unbound) > 5 else "")
                    + "). The binding is bound to a plan the mailbox did not "
                      "receive, so what reached the mailbox was never rehearsed",
                    reexecuted=True)
    # A frozen binding that PLANNED mutations but whose undo ledger records NONE
    # of them is not a clean pass (round 7, Codex): the ledger lives in the
    # VM-writable ops zone, so an empty join beside a >0 planned count and a
    # run whose own metrics counters say it mutated is a REMOVED record, not a
    # quiet apply. The join cannot see what a deleted or truncated ledger no
    # longer holds, so it says so rather than acquitting on absence. A run that
    # genuinely dispatched nothing (all blocked/449) has zero counters and is a
    # real pass.
    if planned > 0 and not acted:
        did = mutation_counts(vault, run_id)
        if did:
            return _row("plan_binding", INCONCLUSIVE,
                        f"this run's binding claims {planned} planned "
                        "mutation(s) and its own metrics row records "
                        + ", ".join(f"{k}={v}" for k, v in sorted(did.items()))
                        + ", but its undo ledger joins ZERO of them — the "
                          "record of what reached the mailbox is empty or gone, "
                          "and an unverifiable dispatch is not a pass",
                        reexecuted=True)
    return _row("plan_binding", PASS,
                f"the apply dispatched the FROZEN plan ({str(stamped)[:16]}…, "
                f"{planned} planned), re-hashed from the frozen plan file, "
                "bound by digest to the rehearsal that validated it, and joined "
                f"to the {len(acted)} mutation(s) its undo ledger records",
                reexecuted=True)

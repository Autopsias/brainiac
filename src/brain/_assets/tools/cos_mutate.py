#!/usr/bin/env python3
"""The COS mutation module — guards kept, clicks gone (MUT-01, S04).

WHAT THIS IS. The host half of the three mailbox mutations a COS night makes:
archive, priority-chip write, and reply-draft SAVE. It owns the ledger, the
caps, the kill switch and the state machine; `tools/cos_mutate_page.js` owns the
payloads and the auth, because auth may not leave the page. Neither half can act
without the other, which is the containment.

WHY IT IS A SEPARATE FILE FROM `cos_driver.py`. The read driver's proven
property is that no mutation verb appears anywhere in it or in its page half,
and `tests/test_cos_driver.py` asserts that mechanically. Bolting mutations onto
it would have deleted a proof to satisfy a filename. The mutation module keeps
its own, different property — every outgoing payload is validated at RUNTIME
against an allowlist — and shares the read driver's transport, staging and
bridge rather than its guarantees.

THE ORDER IS THE SAFETY. Per mutation, in this order and no other:

  1. resolve the target by CONVERSATION ID (ids rotate; a list-view ItemId is a
     session handle, never an identity — v4.7);
  2. write the undo row, state `intent`, WITH the before-image, BEFORE the call;
  3. re-fetch the ChangeKey immediately before dispatch, validate the payload
     against the allowlist, dispatch;
  4. verify by RE-READ, and treat a response that changed nothing as a
     verification FAILURE rather than a success;
  5. record `reconciled`, or STOP the run.

RESUME, NEVER RESTART (G7). Caps count what is applied THIS CHECKPOINT, read
off the undo ledger and not off a process counter, and a mutation whose ledger
row is already terminal is skipped. A half-applied batch is a legitimate state
and the evidence says exactly which rows are applied, which are rolled back and
which are unknown — "unknown" is an honest value, a silent second pass is not.

THE LIVE PASS IS THE MAIN SESSION'S (owner ruling 2026-08-08). A dispatched
subagent is classifier-refused on live mailbox mutation and returns zero writes.
`--dry-run` exists so the live pass can be inspected before it is run: it walks
the whole thing against the live mailbox read-only and stops one line before
`fetch`, printing exactly what would be sent.

    python3 tools/cos_mutate.py plan     --vault <v> --run-id <r>
    python3 tools/cos_mutate.py dry-run  --vault <v> --run-id <r> --tab-id <id>
    python3 tools/cos_mutate.py apply    --vault <v> --run-id <r> --tab-id <id>
    python3 tools/cos_mutate.py canary   --vault <v> --run-id <r> --tab-id <id> \\
                                         --canary-convid <id>
    python3 tools/cos_mutate.py capture-shapes --vault <v> --tab-id <id>
    python3 tools/cos_mutate.py selfcheck
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cos_driver as drv                                          # noqa: E402
import cos_mutate_apply as apply_stages                           # noqa: E402
import cos_mutate_plan as plan_stages                             # noqa: E402
import cos_mutate_canary as canary_stages                          # noqa: E402
import cos_mutate_cli as cli_stages                               # noqa: E402
import cos_mutate_evidence as evidence_stages                       # noqa: E402
import cos_mutate_shapes as shape_stages                          # noqa: E402
from brain import cos_chips as chips                              # noqa: E402
# The undo ledger's counting definition. It MOVED to `cos_reconcile_metrics`
# (s10, 2026-08-16) and is imported back here under its original names, so every
# existing caller of `cos_mutate.applied_counts` / `MUTATION_VERBS` /
# `APPLIED_STATES` / `VERB_COUNTER` is unchanged. It had to move because the
# ledger↔metrics join needs the SAME definition and this module is deliberately
# absent from the engine asset mirror — see the note beside it over there.
# Imported EAGERLY and unguarded on purpose: the cap accounting is built on it,
# and a missing counter must stop the apply, never silently count zero.
from cos_reconcile_metrics import (                                # noqa: E402
    APPLIED_STATES as APPLIED_STATES,  # noqa: F401  deliberate re-export
    MUTATION_VERBS, VERB_COUNTER, applied_counts,
)



chip_for = chips.chip_for

#: The three mutation verbs, in one place, for iterating the counters —
#: imported above from `cos_reconcile_metrics` beside `applied_counts`.

#: THE TYPED FACT that says THE TARGET WAS NOT THERE, nothing was dispatched,
#: and the night should carry on. They are skips wearing a failure word — the
#: page half returns `verification: "verified-failed"` for them because it has
#: one word for "no" — so the host discriminates on `dispatched` and on this
#: flag, never on the word (run 125, 2026-08-12).
#:
#: IT USED TO BE A LIST OF OUTCOME STRINGS, and that is exactly how it failed
#: (review 2026-08-12). The list held `target-not-found` and
#: `source-thread-not-found` — the categorize and draft lanes' words — while
#: the ARCHIVE lane mints its own, `already-absent-from-<folder>`. So no number
#: of vanished archive targets ever matched, none of them reached the skip cap,
#: and a night that found nothing to archive reported completion. A list two
#: producers must both remember to join is the defect; a flag every absence
#: site sets cannot be forgotten by a lane that does not appear in it.
#: `tests/test_cos_mutate.py::test_the_absent_target_flag_is_the_page_halfs_own`
#: reads the page half and asserts every skip site sets THIS key.
ABSENT_TARGET_FLAG = "absent_target"

# ABSENT_SKIP_FRACTION / ABSENT_SKIP_FLOOR live in `cos_mutate_policy` (the cap
# math itself is `cos_mutate_gates.absent_skip_cap`, imported below) — no
# separate copy here.


# batch-2 drain: the vault gates, the plan builders, the undo ledger, the
# Chrome bridge, the shape store, the rehearsal lane, the locked passes, the
# evidence builder and the self-check moved verbatim to siblings; every name is
# re-imported here so its `cos_mutate` module path — the one the tests, the
# nightly and `cos_runverify` re-execution name — is unchanged.
from cos_mutate_evidence import (  # noqa: E402,F401
    build_evidence, fault_injection_report)
from cos_mutate_bridge import (  # noqa: E402,F401
    HOOK_JS, HOOK_SRC_ID, HOOK_STAT_ID, IN_ID, OUT_ID, PAGE_JS, SRC_ID,
    WRONG_WORLD,
    Bridge, CdpBridge, EgoBridge,
    stage_hook, verify_capture_world, _bridge_for, _init_page,
    _transport_name)
from cos_mutate_gates import (  # noqa: E402,F401
    CANARY_MAX_AGE_DAYS, DRAFT_FORMS, MUTATION_LANE, PRIMITIVE, MutationStop,
    absent_skip_cap, assert_vault,
    canary_status, draft_form, draft_signature, kill_switch, receipts_shape_ok,
    short, stop_file, stopped, _fold, _ledger_path, _read_jsonl, _ts, _utcnow,
    _within_window)
from cos_mutate_ledger import (  # noqa: E402,F401
    LEDGER_ROW_KEYS, UndoLedger, dispatched_counters, mutation_lane_lock_path,
    record_mutation_counters, _mutation_lane_lock, _reconcile_module,
    _run_suffix, _undo_row, _write_text_atomic)
# --- denylist: named here so a payload carrying one is REFUSED --------------
# These literals exist in this file only to be rejected. `tests/test_cos_mutate.py`
# reads this block by its markers, so the audit can tell a denial from a use.
BANNED_ACTIONS = ("SendItem", "DeleteItem", "MarkAsJunk", "MarkAllItemsAsRead",
                  "EmptyFolder", "ExportItems", "UploadItems", "CreateAttachment")
BANNED_DISPOSITIONS = ("SendOnly", "SendAndSaveCopy", "SendToNone",
                       "SendOnlyToAll", "SendOnlyToChanged",
                       "SendToAllAndSaveCopy", "SendToChangedAndSaveCopy")
# --- end denylist ------------------------------------------------------------
from cos_mutate_policy import (  # noqa: E402,F401
    CHIP_RANK, DEFAULT_CAPS, DEFAULT_SINCE_DAYS, MANAGED_CHIPS, RECEIPT_KEYS,
    STATES, TERMINAL,
    DRAFT_FOLDER, DRAFT_RESUME_POLICY, PENDING,
    PERMITTED_ACTIONS, PERMITTED_CONVERSATION_ACTIONS, PERMITTED_FOLDERS,
    REFUSED_CONVERSATION_ACTIONS, SAVE_ONLY)
from cos_mutate_passes import (  # noqa: E402,F401
    DISPATCH_PROVEN_BY, REVERSAL_MANUAL, REVERSAL_NO, REVERSAL_YES,
    REVERSIBLE_STATES, _reversal_eligibility)
from cos_mutate_plan import (  # noqa: E402,F401
    build_plan, load_frozen_plan, plan_binding_path, plan_digest)
from cos_mutate_rehearsal import (  # noqa: E402,F401
    _DIGEST_RE, _DISPATCH_EVIDENCE,
    _dispatch_evidence_problems, _frozen_todo, _rehearsal_key, _row_problems,
    _seed_probe, _seed_probe_sentence, rehearsal_gate, rehearsal_verdict)
from cos_mutate_selfcheck import selfcheck  # noqa: E402,F401
from cos_mutate_shapestore import (  # noqa: E402,F401
    capture_shapes, load_shapes, shapes_from_capture, _fingerprints,
    _merge_shapes)
import cos_mutate_passes  # noqa: E402



#: Permission for the artifacts this tool writes under `<vault>/cos-ops` and
#: into the run's evidence directory. `cos._write_atomic` defaults to 0o600
#: (host-private), which these were NOT before round 7 — they were plain
#: umask-derived 0o644 — and a hardening pass that also silently narrows a
#: permission is two changes wearing one commit. 0o644 keeps them exactly as
#: they are today; the plan binding, which really is host-private, takes the
#: helper's own default.
_OPS_MODE = 0o644





#: NO ARTIFICIAL NUMERIC CAP (owner ruling 2026-08-11: "the content and emails
#: and context should drive that, not pre-established artificial limits"). A cap
#: of `None` is unlimited; the SCOPE is what bounds a night now — `since_days`
#: below, plus per-lane self-exclusion (an archived thread leaves the inbox, a
#: chipped thread is skipped, a thread with a draft is skipped). A number can
#: still be passed per verb for a deliberately small hand-run.

#: `APPLIED_STATES` and `VERB_COUNTER` are imported at the top of this file from
#: `cos_reconcile_metrics`. ONE definition, because THREE sides now read it: the
#: apply WRITES these counters from what it dispatched,
#: `cos_runverify.check_metrics_row` RECOUNTS them from the same ledger, and the
#: ledger↔metrics join re-counts them per date. Two spellings of one mapping is
#: how the row and the recount drift apart, which is the defect this closes.

# The undo ledger's CLOSED FIELD SET (grounding design D14, sink 11) and the
# nested `receipts` shape rule both live on their real siblings —
# `LEDGER_ROW_KEYS` beside `UndoLedger.append` in `cos_mutate_ledger` (the
# write site that enforces it), `RECEIPT_KEYS` beside the other mutation
# policy constants in `cos_mutate_policy` — and are re-imported below so this
# module's path for both names is unchanged.



def dry_run(vault: Path, run_id: str, tab_id: int, *,
            caps: dict[str, int] | None = None,
            since_days: int | None = DEFAULT_SINCE_DAYS,
            plan_path: Path | None = None,
            use_cdp: bool = False,
            use_ego: bool = False) -> dict[str, Any]:
    """The whole pass, READ-ONLY, stopping one line before `fetch`.

    Everything the live pass does except the dispatch: the same resolve, the
    same freshly re-fetched ChangeKey, the same builder, the same allowlist —
    and then the payload is printed instead of sent. The undo rows are written
    to a DRY-RUN ledger and never to the real one: a real undo row claiming a
    move that never happened is precisely the hazard the 449 rule exists to
    prevent, and a rehearsal must not be able to create one.

    `plan_path` REHEARSES A FROZEN PLAN (K1). Without it this built its own
    plan, so what it rehearsed was only coincidentally what the apply would
    dispatch. With it, the artifact on disk is the one thing rehearsed, and the
    digest it was written under is carried out to the report so the apply can
    refuse anything else.
    """
    root = assert_vault(vault)
    shapes = load_shapes(vault)
    ledger = UndoLedger(vault, run_id)
    plan = (load_frozen_plan(plan_path) if plan_path is not None
            else build_plan(vault, run_id, caps=caps, since_days=since_days,
                            applied=ledger.applied_counts()))
    bridge = _bridge_for(tab_id, shapes["shapes"], run_id, use_cdp=use_cdp,
                         use_ego=use_ego)
    init = {"transport": _transport_name(use_cdp=use_cdp, use_ego=use_ego)}

    out = []
    for m in plan["mutations"]:
        # A REHEARSAL REPORTS, IT DOES NOT ABORT. One lane whose approved shape
        # cannot be built (measured 2026-08-11: the captured CreateItem names no
        # SavedItemFolderId, and v4.7 forbids filling one in) used to take the
        # whole dry run with it — so the owner saw an error instead of the other
        # lanes' payloads, which is the opposite of what a dry run is for.
        try:
            out.append(bridge.call("dry", {"mutation": m})["out"])
        except MutationStop as exc:
            out.append({"verb": m["verb"],
                        "conversation_id": short(m["conversation_id"]),
                        "blocked": str(exc), "would_dispatch": False,
                        "dispatched": False})
    dry_ledger = ledger.path.with_name(ledger.path.name.replace(
        "_cos_undo_ledger_", "_cos_undo_DRYRUN_"))
    _write_text_atomic(dry_ledger, "".join(
        json.dumps(_undo_row(m, r.get("resolved") or {}, state="intent",
                             run_id=run_id, account=str(root["BRAIN_VAULT"]),
                             dry_run=True), sort_keys=True) + "\n"
        for m, r in zip(plan["mutations"], out)), mode=_OPS_MODE)
    return {"vault_root_asserted": root, "shapes": {k: v.get("fingerprint")
                                                    for k, v in shapes["shapes"].items()},
            "shapes_path": shapes["path"],
            "capture": (init.get("out") or {}).get("capture"),
            # THE ONE FIELD THE APPLY IS BOUND BY. Top-level rather than only
            # inside `plan`, because the rehearsal gate and the apply both read
            # this file for exactly this and nothing else about the plan.
            "plan_digest": plan.get("plan_digest"),
            "rehearsed_frozen_plan": str(plan_path) if plan_path else None,
            "plan": plan, "dry": out, "dry_run_ledger": str(dry_ledger),
            "e17": canary_status(vault), "kill_switch": kill_switch(vault),
            "dispatched": 0}



def apply_pass(vault: Path, run_id: str, tab_id: int, *,
               caps: dict[str, int] | None = None,
               since_days: int | None = DEFAULT_SINCE_DAYS,
               allow_draft_resume: bool = False,
               plan_path: Path | None = None,
               rehearsal_path: Path | None = None,
               use_cdp: bool = False,
               use_ego: bool = False) -> dict[str, Any]:
    """The live mutating pass. Every guard is checked HERE, in code.

    `plan_path`/`rehearsal_path` are how the NIGHTLY runs it, and the CLI
    refuses `apply` without both (K1): the plan the apply dispatches is the
    frozen artifact the rehearsal validated, by digest, or nothing is
    dispatched. Omitting them builds a fresh plan — the pre-s09 behaviour, kept
    only for the in-process callers that construct their own ledger — and the
    report says so in `plan_binding` rather than leaving it to be inferred.
    """
    with _mutation_lane_lock(vault, run_id):
        # THE COUNTERS ARE WRITTEN ON EVERY EXIT, INCLUDING THE BAD ONES. A run
        # that stopped early, refused, or died mid-plan is exactly the run whose
        # metrics row must not still read `archived: 0` — everything dispatched
        # before the stop is dispatched, and it is what the anti-vacuity guards
        # corroborate a missing artifact against. It runs INSIDE the lane lock,
        # so it cannot race a concurrent pass on the same run.
        #
        # ponytail: a failure to WRITE the counters is swallowed rather than
        # masking the apply's own outcome — it fails CLOSED, because
        # `check_metrics_row` reads an all-zero row beside a non-empty undo
        # ledger as INCONCLUSIVE, never as a pass.
        try:
            out = _apply_pass_locked(
                vault, run_id, tab_id, caps=caps, since_days=since_days,
                allow_draft_resume=allow_draft_resume, plan_path=plan_path,
                rehearsal_path=rehearsal_path, use_cdp=use_cdp,
                use_ego=use_ego)
        except BaseException:
            try:
                record_mutation_counters(vault, run_id)
            except Exception:                                     # noqa: BLE001
                pass
            raise
        try:
            out["mutation_counters"] = record_mutation_counters(vault, run_id)
        except Exception as exc:                                  # noqa: BLE001
            out["mutation_counters"] = {"appended": "failed",
                                        "error": str(exc)[:300]}
        return out


# ---------------------------------------------------------------------------
# batch-2 drain: the four locked passes moved verbatim to `cos_mutate_passes`
# on the s18 lane convention — each receives THIS module's namespace, so the
# monkeypatch surface (cm._apply_pass_locked, cm._bridge_for, cm.load_shapes,
# cm._init_page, cm.Bridge, cm.record_mutation_counters,
# cm._mutation_lane_lock) keeps governing them from here, exactly as before.


# ---------------------------------------------------------------------------
# batch-2 drain: the four locked passes moved verbatim to `cos_mutate_passes`
# on the s18 lane convention — each receives THIS module's namespace, so the
# monkeypatch surface (cm._apply_pass_locked, cm._bridge_for, cm.load_shapes,
# cm._init_page, cm.Bridge, cm.record_mutation_counters,
# cm._mutation_lane_lock) keeps governing them from here, exactly as before.
# ---------------------------------------------------------------------------
def _apply_pass_locked(vault: Path, run_id: str, tab_id: int, *,
                       caps: dict[str, int] | None = None,
                       since_days: int | None = DEFAULT_SINCE_DAYS,
                       allow_draft_resume: bool = False,
                       plan_path: Path | None = None,
                       rehearsal_path: Path | None = None,
                       use_cdp: bool = False,
                       use_ego: bool = False) -> dict[str, Any]:
    return cos_mutate_passes._apply_pass_locked(
        vault, run_id, tab_id, caps=caps, since_days=since_days,
        allow_draft_resume=allow_draft_resume, plan_path=plan_path,
        rehearsal_path=rehearsal_path, use_cdp=use_cdp, use_ego=use_ego,
        lane=sys.modules[__name__])


def undo_pass(vault: Path, run_id: str, tab_id: int | None, *,
              use_cdp: bool = False, use_ego: bool = False,
              limit: int | None = None) -> dict[str, Any]:
    return cos_mutate_passes.undo_pass(
        vault, run_id, tab_id, use_cdp=use_cdp, use_ego=use_ego, limit=limit,
        lane=sys.modules[__name__])


def unchip_pass(vault: Path, run_id: str, tab_id: int | None, *,
                use_cdp: bool = False, use_ego: bool = False,
                limit: int | None = None) -> dict[str, Any]:
    return cos_mutate_passes.unchip_pass(
        vault, run_id, tab_id, use_cdp=use_cdp, use_ego=use_ego, limit=limit,
        lane=sys.modules[__name__])


def canary_drill(vault: Path, run_id: str, tab_id: int, conv_id: str,
                 *, use_cdp: bool = False,
                 use_ego: bool = False) -> dict[str, Any]:
    return cos_mutate_passes.canary_drill(
        vault, run_id, tab_id, conv_id, use_cdp=use_cdp, use_ego=use_ego,
        lane=sys.modules[__name__])


def main(argv: list[str]) -> int:
    p = cli_stages.build_parser(
        __doc__.splitlines()[0], since_days_default=DEFAULT_SINCE_DAYS,
        draft_resume_policy=DRAFT_RESUME_POLICY)
    args = p.parse_args(argv[1:])

    if args.command == "selfcheck":
        return selfcheck()

    # NO VAULT, NO BROWSER, NO LEDGER — two files and an exit status. Placed
    # before the vault resolution below on purpose: the nightly gates REAL
    # MUTATIONS on this status, and a gate that needs more than the artifacts
    # it judges is a gate with more ways to fail open.
    if args.command == "rehearsal-gate":
        if args.plan is None or args.dry_run_json is None:
            print("rehearsal-gate needs --plan and --dry-run-json",
                  file=sys.stderr)
            return 2
        rc, reason = rehearsal_gate(args.plan, args.dry_run_json)
        print(reason)
        return rc

    vault = args.vault or Path(os.environ.get("BRAIN_VAULT", "")).expanduser()

    def _cap(v: int | None) -> int | None:
        return None if v is None or v <= 0 else v         # <= 0 ⇒ unlimited
    caps = {"archive": _cap(args.cap_archive),
            "categorize": _cap(args.cap_categorize),
            "draft": _cap(args.cap_draft)}
    since_days = None if args.all else args.since_days

    try:
        out = cli_stages.dispatch_command(
            args, vault, caps, since_days, lane=sys.modules[__name__])
    except cli_stages.AttendedCapRefused:
        return 4
    except MutationStop as exc:
        print(f"MUTATION STOP: {exc}", file=sys.stderr)
        cli_stages.write_stop_report(
            args, exc, ts=_ts, write_atomic=_write_text_atomic,
            ops_mode=_OPS_MODE)
        return 3

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(args.out,
                           json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                           mode=_OPS_MODE)
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

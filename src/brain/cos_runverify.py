"""INS-01 — the HOST-side validator for a COS run's own artifacts.

WHY THIS EXISTS (measured, 2026-07-31). Run 59 skipped its ENTIRE self-eval:
zero E-check output across its 16 artifacts. E16 — the check written to catch a
candidate with no stamps — never executed, so the miss was invisible, and 8
unstamped candidates went on to be claimed. Doctrine cannot police itself: a
model working under a 3,566-line skill will sometimes skip a step, and the
report it writes about itself is the one artifact that cannot detect that.

So the check lives where it cannot be skipped: HOST-side, in the hourly broker
fold, over the run's artifacts as they sit on disk.

THREE THINGS MAKE THIS MORE THAN THE NEXT INSTRUMENT THAT CANNOT FAIL:

1. FOUR STATES, NOT PASS/FAIL. ``VALID`` / ``VALID_DEGRADED`` / ``INVALID`` /
   ``INCONCLUSIVE`` (the constants live in :mod:`brain.cos`, beside the claim
   gate that reads them). ``VALID_DEGRADED`` never collapses into an ordinary
   pass — it is the state for "correctly-reported degradation", and for a check
   the host could not re-execute. ``INCONCLUSIVE`` — the validator could not
   run — is surfaced as loudly as ``INVALID`` and blocks claiming just as hard,
   because a validator that could not run is not a validator that passed.

2. RE-EXECUTION, NOT MARKER-TRUST. Reading a self-eval block's shape proves
   only that a string was printed. Where a control is mechanically
   re-executable host-side, this RE-EXECUTES it and compares:

     * the OUTCOME CONTRACT verdict is recomputed with ``tools/cos_contract.py``
       from the run's own raw PRE/POST snapshots and ledgers, and compared to
       the block the run recorded;
     * the metrics row's three ingestion counters are RECOUNTED from the run's
       ingestion ledger;
     * the expected self-eval check count is re-derived from THE RUN MANIFEST'S
       skill digest — never from whatever ``SKILL.md`` happens to be deployed at
       validation time, which is the same claim-time-vs-production-time error
       the manifest exists to prevent;
     * the substance verdicts in the ingestion ledger are checked against the
       body reads they assert (``check_body_pass``) — run 64 reported a whole
       Phase 1.6 it never ran, by copying the previous run's ledger;
     * every evidence artifact is checked against the run id the HOST assigned
       (``check_artifact_naming``) — run 64 named its ledgers from the local
       clock, crossed midnight, and left two dated copies of each.

   Where re-execution is genuinely impossible (the run's skill bytes are gone,
   the checker is not installed beside the engine), the row says so and scores
   ``degraded`` — never ``pass``.

3. THE DEGRADE EXEMPTION IS CROSS-ARTIFACT, NOT A MARKER. Run 58 (2026-07-31)
   was a LEGITIMATE degrade: Outlook signed out, the mail leg correctly stopped,
   an honest ``zero-eligible`` ledger marker, and a contract that honestly
   FAILED. Scoring that as a validator failure is how a guard gets muted — which
   is exactly how E16 stayed trusted while vacuous. So a FAILED contract is
   exempt ONLY when the degrade is TOTAL and CONSISTENT across every artifact of
   the run, corroborated against host-observed state (how many candidates the
   host actually received from that run). The run writes the marker, so the
   marker alone proves nothing: a ``zero-eligible`` marker beside a populated
   ledger, non-zero counters, an enumerating mail leg, or candidates the host
   really received is an outright FAIL.

AND IT BLOCKS. The verdict is not a counter. ``brain.cos.claim_drops`` refuses
to bind a candidate whose producing run is not ``VALID``/``VALID_DEGRADED``
(STA-01/STA-02, s01), so an INVALID run's candidates are quarantined rather than
claimed — they never reach the owner batch, the evidence keys, or the signed
drain. A validator that only wrote counters would make "FAILED" cosmetic.

ONLY COMPLETED RUNS ARE SCORED. A run whose manifest-declared artifact set is
incomplete, or whose artifacts are still being written, is PENDING — no verdict
is recorded at all (and an unscored run is INCONCLUSIVE to the claim gate, so
its candidates wait rather than flow). Every verdict records the digest of the
inputs it was computed over, so a changed manifest or a substituted artifact
re-validates on the next pass instead of resting on a cached verdict.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from . import cos, cos_deploy
# The four ledger vocabularies, the two dominance-bar constants and
# `_is_refusal` below are RE-EXPORTS on purpose: `tools/cos_judge.py` does
# `from brain.cos_runverify import _DEDUP_CHECKS, _HELD_REASONS,
# _LEDGER_DISPOSITIONS`, `brain.cos_echecks` reads
# `rv._HELD_REASONS | rv._HOST_HELD_REASONS`, `tools/cos_category_share_report.py`
# and `tests/test_cos_driver.py` read the dominance-bar pair, and the
# chief-of-staff doctrine text pins the name `cos_runverify._is_refusal` — all
# off THIS module's namespace, which these imports keep true.
from .cos_runverify_checks import (DEGRADED, FAIL, INCONCLUSIVE, PASS,
                                   _CATEGORY_DOMINANCE_MAX_SHARE,   # noqa: F401
                                   _CATEGORY_DOMINANCE_MIN_ROWS,    # noqa: F401
                                   _DEDUP_CHECKS,             # noqa: F401
                                   _HELD_REASONS,             # noqa: F401
                                   _HOST_HELD_REASONS,        # noqa: F401
                                   _LEDGER_DISPOSITIONS,      # noqa: F401
                                   _MARKER_DISPOSITION, _READ_IMPLYING_REASON,
                                   _category_dominance,
                                   _category_dominance_problem,
                                   _category_stamp_counts,
                                   _category_stamp_problems, _declares,
                                   _empty_shell_problem, _never_opened_problem,
                                   _row, _sequence_verdict, _starved_problem,
                                   _vocabulary_counts, _vocabulary_problems)
from .cos_runverify_identity import (_NON_IDENTITY_EVENTS,
                                     _attempt_problems, _cascade_problems,
                                     _forged_refusal_problems,
                                     _forged_unreachable_row,
                                     _identity_partitions,
                                     _is_refusal,             # noqa: F401
                                     _mislabel_problems,
                                     _missing_pre_problem_row,
                                     _post_mismatch_mutation_row,
                                     _recovery_problems, _refusal_problem_row,
                                     _undetected_problem_row)
from .cos_runverify_chipdraw import (_bundle_at_least, _chip_draw_state,
                                      _chip_epoch0_verdict,
                                      _chip_late_stamp_row,
                                      _chip_left_behind_row,
                                      _chip_missing_census_row,
                                      _denominator_row, _is_reeval_row)
from .cos_runverify_corpus import (_corpus_join_problems, _corpus_missing_row,
                                   _corpus_missing_thread_row,
                                   _corpus_opened_no_text_row)
from . import cos_runverify_planbinding as planbinding
from . import cos_runverify_stamps as stamps
from . import cos_runverify_preamble as preamble

# -- check-row states ---------------------------------------------------------
#: The four states and the row shape live in :mod:`brain.cos_runverify_checks`
#: (the ONE definition) and are imported above, so the sub-step modules and
#: this module can never drift on what a check row is.

#: A run must be quiet for this long before it is scored — a nightly writes its
#: ledgers, its report and its metrics row over ~30 minutes, and scoring a
#: half-written run is both a false-alarm generator and a substitution window.
QUIESCE_ENV = "BRAIN_COS_RUN_QUIESCE_SECONDS"
DEFAULT_QUIESCE_SECONDS = 900

#: E-check DEFINITIONS in a chief-of-staff SKILL.md (`- **E16** · …`). Defined
#: in :mod:`brain.cos_deploy` beside the other facts read off a bundle, so the
#: count the manifest FREEZES and the count derived here can never disagree.
_SKILL_ECHECK_RE = cos_deploy.ECHECK_RE
#: E-check RESULTS in a run report. Line-anchored and list-item-anchored on
#: purpose: an Outlook conversation id is base64 and full of `E8XWki0=`-shaped
#: noise, and a substring match over the report counts that as a self-eval.
_REPORT_ECHECK_RE = re.compile(
    r"^\s*[-*]\s*\*{0,2}E0?(\d{1,2})\b[^\n]*?\b(PASS|FAIL|N/?A)\b",
    re.MULTILINE | re.IGNORECASE)

#: placeholder category values a producer invents when the taxonomy did not
#: match (SKILL.md E16: the value is the real id or it is absent — never a
#: stand-in, because `unclassified` is the host's own never-graduable default)
_PLACEHOLDER_CATEGORIES = {"uncategorized", "unclassified", "none", "n/a",
                           "na", "unknown", "null", "-"}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_NUMBER_RE = re.compile(r"run([0-9]+)")

#: The ledger vocabularies and per-check thresholds live in
#: :mod:`brain.cos_runverify_checks` (the ONE definition) and are
#: imported above — `tools/cos_judge.py` and :mod:`brain.cos_echecks`
#: read them off THIS module's namespace, which the import keeps true.

#: Artifacts deliberately named for the MORNING the owner reads them, which is
#: the day AFTER a run that starts before midnight. Everything else a run
#: writes is EVIDENCE and carries the host-assigned run id's own date.
_MORNING_DATED_PREFIXES = ("_briefing_", "_decision_card_")




#: THE BAR IS SEVENTEEN CONTROLS (s08, 2026-08-16), and the three that left it
#: are named here rather than deleted quietly. All three re-execute over a
#: MODEL-WRITTEN ledger — `_cos_action_ledger_<run>.jsonl`,
#: `_cos_chip_ledger_<run>.jsonl`, `_cos_hold_ledger_<run>.jsonl` — from the
#: pre-v7 lane, where the model drove the browser and wrote its own record of
#: what it had done. Under v7 no producer for those files exists or can exist:
#: the model legs run `--tools "Read,Glob"` with `Edit(//**)` denied
#: (`cos_nightly.sh` MODEL_TOOLS), so they cannot write a file at all, and the
#: mutation lane is a deterministic host program that records what it dispatched
#: in `_cos_undo_ledger_<run>.jsonl`. Measured on run 145 (2026-08-16): all
#: three read 0 rows, took their "nothing acted, so nothing could act wrongly"
#: branch, and reported PASS on a night that applied 16 mutations.
#:
#: A control whose all-clear equals no input is the failure mode this validator
#: exists to prevent, so they are OFF the bar rather than left green. What each
#: one guarded is held by a live control with a non-zero denominator, named
#: below — and the anti-vacuity job itself moved to `check_mutation_counters`,
#: which recounts the metrics row against the artifact this lane DOES write and
#: is proven able to fail on it.
#:
#: The functions and their tests are kept, unscored: they encode the measured
#: failures of runs 100-111 and are the implementation a browser-driven lane
#: would need again. Nothing calls them, so nothing can report on them.
RETIRED_CONTROLS = {
    "unread_touch": (
        "re-executes over `_cos_action_ledger_<run>.jsonl` (no v7 producer). "
        "The unread shield is now a PRE-DISPATCH refusal in `cos_mutate.py` "
        "(`read_state != 'read'` excludes the row), the only categorize "
        "primitive is the non-touching `rest-categorize`, and E2 recounts every "
        "mutated thread's screened read state host-side from the ingestion + "
        "undo ledgers"),
    "target_identity": (
        "re-executes over `_cos_action_ledger_<run>.jsonl` (no v7 producer). It "
        "scores the CLICK-era identity risk — a virtualized row recycled between "
        "verify and click — which the REST lane cannot have: it addresses a "
        "conversation by id, and `check_plan_binding` joins every dispatched "
        "`conversation_id|verb` key to the frozen plan"),
    "chip_reeval_draw": (
        "re-executes over `_cos_chip_ledger_<run>.jsonl` against a population "
        "recounted from `_cos_hold_ledger_<run>.jsonl` (no v7 producer for "
        "either). E4 recounts that the chips applied are managed names on the "
        "correct (bucket, tier) matrix and on bare threads"),
}

#: Every control `verify_run` scores, in order. PINNED so the size of the bar is
#: a fact a test can read: an owner ruling that reads "twenty controls" against
#: a validator that runs seventeen is exactly the drift s08 was dispatched to
#: close, and a control added or dropped in silence is how it recurs.
SCORED_CONTROLS = (
    "completion", "self_eval", "repairs", "metrics_row", "ledger_vocabulary",
    "category_stamp", "ingestion_ledger", "body_pass", "body_order",
    "body_open_count", "open_instrumentation", "plan_binding", "corpus_join",
    "candidate_stamps", "artifact_naming", "degrade_consistency", "contract",
)


# -- the re-execution toolchain ------------------------------------------------
# `tools/cos_contract.py` (the OUTCOME CONTRACT checker) and
# `tools/cos_reconcile_metrics.py` (the ledger join + the observation guard)
# are the two controls this validator re-executes rather than reads. They are
# scripts, not package modules, so they are resolved from disk: the dev
# checkout's `tools/`, the copy mirrored into the wheel at `_assets/tools/`
# (tools/package_clients.py keeps it in lockstep), or an explicit override.
# Without them the two most load-bearing re-executions cannot run at all, which
# is INCONCLUSIVE — never a quiet pass.
TOOLS_DIR_ENV = "BRAIN_COS_TOOLS_DIR"



# -- the individual controls ---------------------------------------------------

def verify_run(vault, run_id: str, *, now: _dt.datetime | None = None,
               quiesce_seconds: int | None = None) -> dict[str, Any]:
    """Score ONE run against its own artifacts. Never writes anything."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    quiesce = _quiesce_seconds(quiesce_seconds)
    out: dict[str, Any] = {"run_id": run_id, "verdict": None, "state": "pending",
                           "reason": "", "checks": [], "inputs_digest": None}

    # The three pre-scoring gates (gap-05 intruders, manifest, ops dir) live
    # in :mod:`brain.cos_runverify_preamble` (s18); each returns the scored
    # row to return with, or None to proceed.
    gate = preamble.intruder_row(vault, run_id)
    if gate:
        out.update(gate)
        return out
    gate = preamble.manifest_missing_row(vault, run_id)
    if gate:
        out.update(gate)
        return out
    manifest = cos.run_manifest(vault, run_id)
    out["inputs_digest"] = inputs_digest(vault, run_id, manifest)

    ops = cos.run_ops_dir(vault)
    gate = preamble.ops_dir_row(ops)
    if gate:
        out.update(gate)
        return out

    done = completion(vault, run_id, manifest, now=now, quiesce=quiesce)
    if not done["complete"]:
        out["reason"] = done["reason"]
        out["checks"] = [_row("completion", INCONCLUSIVE, done["reason"],
                              reexecuted=True)]
        return out                                   # PENDING: no verdict at all

    contract_mod, recon, tools_reason = checkers()
    checks = [_row("completion", PASS, done["reason"], reexecuted=True)]
    if contract_mod is None:
        checks.append(_row("checkers", INCONCLUSIVE, tools_reason,
                           reexecuted=False))

    rows = ledger_rows(vault, run_id)
    row = metrics_row(vault, run_id)
    block = _load_json(ops / f"cos_contract_block_{run_id}.json")
    evidence = degrade_evidence(vault, run_id,
                                block if isinstance(block, dict) else None,
                                row, rows, recon)

    checks.append(check_self_eval(vault, run_id, manifest))
    checks.append(check_repairs(vault, run_id, manifest))
    checks.append(check_metrics_row(vault, run_id, manifest, rows, recon))
    checks.append(check_ledger_vocabulary(run_id, rows))
    checks.append(check_category_stamp(vault, run_id, rows))
    checks.append(check_ingestion_ledger(vault, run_id, rows, recon))
    checks.append(check_body_pass(run_id, rows))
    checks.append(check_body_order(run_id, rows))
    checks.append(check_body_open_count(run_id, rows, row))
    acts = action_rows(vault, run_id)
    checks.append(check_open_instrumentation(vault, run_id, rows, acts))
    checks.append(check_plan_binding(vault, run_id))
    checks.append(check_corpus_join(vault, run_id, rows))
    checks.append(check_candidate_stamps(vault, run_id, rows))
    checks.append(check_artifact_naming(vault, run_id))
    checks.append(check_degrade_consistency(evidence))
    contract_row, _ = check_contract(vault, run_id, contract_mod, recon, evidence)
    checks.append(contract_row)

    verdict, reason = _verdict_from(checks)
    out.update(state="scored", verdict=verdict, reason=reason, checks=checks,
               degrade=evidence)
    return out


__all__ = [
    "PASS", "DEGRADED", "FAIL", "INCONCLUSIVE",
    "alert", "checkers", "completion", "expected_check_count", "hot_entry",
    "inputs_digest", "known_run_ids", "ledger_counts", "recent_verdicts",
    "run_artifacts", "stalled_runs", "verify_pending_runs", "verify_run",
]

# The size ratchet of 2026-08-16 moved the verify pipeline's sub-steps into
# sibling modules (io -> metrics -> selfeval -> touch -> ledger -> join ->
# contract -> alerts). Everything is re-exported below so every
# `brain.cos_runverify.<name>` caller and monkeypatch target is unchanged;
# `check_self_eval` itself moved VERBATIM (cos_runverify_selfeval.py).
from .cos_runverify_io import (  # noqa: E402,F401  (facade re-export)
    DEFAULT_RUN_WINDOW as DEFAULT_RUN_WINDOW,
    STALLED_LOOKBACK_DAYS as STALLED_LOOKBACK_DAYS,
    STALLED_PENDING_HOURS as STALLED_PENDING_HOURS,
    _SUPERSEDES as _SUPERSEDES,
    _load_json as _load_json,
    _load_script as _load_script,
    _quiesce_seconds as _quiesce_seconds,
    _read_jsonl as _read_jsonl,
    _run_number as _run_number,
    checkers as checkers,
    completion as completion,
    host_received_candidates as host_received_candidates,
    inputs_digest as inputs_digest,
    ledger_counts as ledger_counts,
    ledger_rows as ledger_rows,
    metrics_row as metrics_row,
    metrics_rows as metrics_rows,
    run_artifacts as run_artifacts,
    tools_dir as tools_dir,
)

from .cos_runverify_metrics import (  # noqa: E402,F401  (facade re-export)
    _BULLET_RE as _BULLET_RE,
    _REPAIRS_SECTION_RE as _REPAIRS_SECTION_RE,
    _REPAIR_HEADER_RE as _REPAIR_HEADER_RE,
    check_ledger_vocabulary as check_ledger_vocabulary,
    check_metrics_row as check_metrics_row,
    check_mutation_counters as check_mutation_counters,
    check_repairs as check_repairs,
    expected_check_count as expected_check_count,
)

from .cos_runverify_selfeval import (  # noqa: E402,F401  (facade re-export)
    check_self_eval as check_self_eval,
)

from .cos_runverify_touch import (  # noqa: E402,F401  (facade re-export)
    _MUTATION_COUNTERS as _MUTATION_COUNTERS,
    _NON_TOUCHING_CATEGORIZE_PRIMITIVE as _NON_TOUCHING_CATEGORIZE_PRIMITIVE,
    _UNREAD_DEFER_REASON as _UNREAD_DEFER_REASON,
    action_rows as action_rows,
    check_open_instrumentation as check_open_instrumentation,
    check_target_identity as check_target_identity,
    check_unread_touch as check_unread_touch,
    mutation_counts as mutation_counts,
    unledgered_mutations as unledgered_mutations,
)
# The five chip names this block also carried are re-exported by `_touch` from
# `_chips`, and the `_chips` block further down re-exports them again — so the
# same five objects were bound twice and the second binding silently won. They
# stay on the `_chips` import, beside the module that defines them.

from .cos_runverify_ledger import (  # noqa: E402,F401  (facade re-export)
    check_body_order as check_body_order,
    check_body_pass as check_body_pass,
    check_category_stamp as check_category_stamp,
    check_ingestion_ledger as check_ingestion_ledger,
)

from .cos_runverify_join import (  # noqa: E402,F401  (facade re-export)
    check_artifact_naming as check_artifact_naming,
    check_body_open_count as check_body_open_count,
    check_corpus_join as check_corpus_join,
)

from .cos_runverify_contract import (  # noqa: E402,F401  (facade re-export)
    _DROP_STAMP_KEYS as _DROP_STAMP_KEYS,
    _cos_mutate as _cos_mutate,
    _drop_stamped as _drop_stamped,
    _verdict_from as _verdict_from,
    check_candidate_stamps as check_candidate_stamps,
    check_contract as check_contract,
    check_degrade_consistency as check_degrade_consistency,
    check_plan_binding as check_plan_binding,
    degrade_evidence as degrade_evidence,
)

from .cos_runverify_alerts import (  # noqa: E402,F401  (facade re-export)
    alert as alert,
    hot_entry as hot_entry,
    known_run_ids as known_run_ids,
    recent_verdicts as recent_verdicts,
    stalled_runs as stalled_runs,
    verify_pending_runs as verify_pending_runs,
)

from .cos_runverify_chips import (  # noqa: E402,F401  (facade re-export)
    _written_before as _written_before,
    check_chip_reeval_draw as check_chip_reeval_draw,
    chip_rows as chip_rows,
    hold_rows as hold_rows,
    prior_reeval_stamps as prior_reeval_stamps,
)


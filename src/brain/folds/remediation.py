"""FIX-01/FIX-02 — the remediation umbrella fold.

The two repair BRANCHES live in :mod:`brain.remediation_folds`; this is the
umbrella that rides the existing hourly daily flow, inside the maintain lock
the orchestrator already holds. It is one fold, not two date-gated branches,
because ``maintenance_escalation``'s scheduler understands only
``daily``/``graphify``/weekday names and cannot express hourly at all — the
adversarial-review scheduler ruling. Cadence, liveness, convergence, the
shadow countdown and the per-finding coverage binding therefore live in the
HOST-PRIVATE state :mod:`brain.remediation_state` owns.

Nothing here is throttled by the registry cadence. ``BRANCH_CADENCE_DAYS`` is
what LIVENESS is judged against — ``last_run`` older than two cadences is an
escalation — exactly as ``daily`` (cadence 1) runs on every hourly firing. A
throttle would be actively wrong here: the routing layer suppresses a finding
only while the branch can prove it covered THIS run's targets, so a branch
that ran at 02:00 and skipped 03:00 would banner for the other 23 hours.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Callable

from .context import MaintenanceRun
from .remediation_bookkeeping import (
    apply_extract_retry_intents,
    apply_intents,
    heal_streaks,
    in_shadow,
    record_extract_retry_run,
    record_failure,
    record_run,
    record_shadow,
    render_hot_entry,
)
from .. import maintenance, remediation, remediation_folds as rf
from .. import remediation_branches as rb
from .. import remediation_state as rs
from ..lock import WriterLockBusy, vault_writer_lock

#: The mount-visible projection key. ``_``-prefixed so no consumer that walks
#: ``maintain-state.json`` for branch rows can mint a phantom branch from it —
#: the authoritative rows are host-private.
STATE_KEY = "_remediation"

def _projection(branches: dict[str, Any], mode: str) -> dict[str, Any]:
    """The health-report row for a run that took no branch decisions.

    Without it ``_remediation_html`` finds no ``branches`` key and renders
    NOTHING, so one busy lock — or switching the lane off — made the
    "Automatic repairs" section vanish and a working feature became
    indistinguishable from a deleted one (llm-review, 2026-08-21)."""
    out: dict[str, Any] = {}
    for name in _ALL_BRANCH_NAMES:
        row = branches.get(name)
        row = row if isinstance(row, dict) else {}
        out[name] = {"mode": mode, "targets": "?", "healed": 0, "skipped": 0,
                     "remaining": int(row.get("remaining_target_count", 0) or 0)}
    return out


_BRANCHES: tuple[tuple[str, str, Callable[[Any, int], Any]], ...] = (
    (rf.SIGN_REPAIR, rf.KEY_UNSIGNED, rb.plan_sign_repair),
    (rf.REGUARD, rf.KEY_UNGUARDED, rb.plan_reguard),
)

#: Every branch name this fold owns a row for — ``_BRANCHES``' one-key-per-
#: branch shape plus ``extract_retry`` (FIX-03), which serves four registry
#: keys at once and so runs through its own driver (``_run_extract_retry``)
#: rather than the generic loop above. Anything that iterates branch NAMES
#: only (never a (name, key, planner) triple) reads this instead.
_ALL_BRANCH_NAMES: tuple[str, ...] = tuple(
    name for name, _key, _planner in _BRANCHES) + (rf.EXTRACT_RETRY,)


class RemediationFoldsMixin:
    """Provide the REG-04 automatic-repair umbrella."""

    def remediation_fold(self, run: MaintenanceRun) -> None:
        """Run each repair branch under the SINGLE-WRITER LOCK, persist its
        host-private evidence, and publish the target signatures the routing
        layer binds suppression to.

        The lock is not decoration. ``core.write_note`` does not take it, so
        without this the branch could rewrite notes and the audit chain while
        a concurrent ``sync`` held it — that sync would return
        ``skipped-writer-busy`` while remediation had already recorded a live
        run and its coverage, leaving index and snapshot stale against a vault
        it believes it repaired (adversarial review 2026-08-21). Planning,
        validation, writes and the state persist all happen inside ONE
        acquisition, exactly as ``supersede`` does.

        A BUSY lock costs this fold and nothing else. Letting
        ``WriterLockBusy`` escape would abort the rest of the daily block —
        version_chain, auto_dedup, auto_para, navigation, corpus_invariants,
        the watchdogs AND ``publish_daily_fold`` — *after* the sync has already
        moved the index, so the VM's snapshot would go unrepublished because
        the newest and least critical fold in the block could not get a lock a
        rebuild can legitimately hold for 90 minutes. The round-1 ruling was to
        refuse the remediation RUN, not the daily one (adversarial review round
        2, 2026-08-21). So it is caught here, recorded as a skip in the lane's
        own host-private state, and no target signature is published — a fold
        that could not run suppresses nothing."""
        if rf.disabled():
            self._remediation_disabled(run)
            return
        try:
            with vault_writer_lock(self.vault, verb="remediation"):
                self._remediation_locked(run)
        except WriterLockBusy as exc:
            self._remediation_writer_busy(run, exc)

    def _remediation_disabled(self, run: MaintenanceRun) -> None:
        """Switched OFF must QUIET the lane, not hand the owner a dead alarm.

        Two halves. No target signature is published, so nothing this lane is
        no longer repairing can be suppressed by its silence — the invariants
        banner on their own, as they did before any of this existed.

        And the branch rows are put back to NEVER-RUN, because simply returning
        left ``last_run`` frozen: ``branch_liveness`` escalates a branch whose
        ``last_run`` is older than two cadences (cadence 1 here), and
        ``_route_auto`` then banners ``branch-escalate:<branch>``, which is an
        UNSUPPRESSIBLE prefix. Three days after an operator flipped the switch
        the vault would report the branch permanently broken, with nothing the
        operator could answer (llm-review, 2026-08-21). ``never-run``
        suppresses nothing and escalates nothing, which is exactly what an
        off lane should look like. The date is kept as
        ``last_run_before_disabled`` so the history is not destroyed, and the
        shadow countdown is left alone — nights already proven stay proven."""
        run.results["remediation"] = {"target_signatures": {}, "branches": {},
                                      "disabled": True}
        if run.dry_run:
            return
        state = rs.read_state(Path(self.vault))
        branches = dict(state.get("branches") or {})
        changed = False
        for name in list(branches):
            row = branches[name]
            if not isinstance(row, dict) or "last_run" not in row:
                continue
            row = dict(row)
            row["last_run_before_disabled"] = row.pop("last_run")
            row["disabled_since"] = run.date.isoformat()
            row.pop("covered", None)   # stale coverage may never suppress
            branches[name] = row
            changed = True
        if changed:
            rs.write_state(Path(self.vault), {"branches": branches})
        run.state[STATE_KEY] = {"status": "disabled",
                                "disabled_since": run.date.isoformat(),
                                "branches": _projection(branches, "disabled")}

    def _remediation_writer_busy(
        self, run: MaintenanceRun, exc: WriterLockBusy,
    ) -> None:
        """Record a run the lane could not take, and let the day continue.

        Exactly the ``skipped-writer-busy`` contract every other branch uses
        (CC-01): ``last_attempt`` is refreshed, ``consecutive_skips`` counts up
        with ``writer_busy_since`` marking the start of the streak, and
        ``last_run``/``consecutive_failures`` are NOT touched — those mean work
        completed. The lane's existing liveness ladder escalates a streak on
        its own, so a wedged lock is loud without being fatal.

        ``consecutive_skips`` is the ONE counter here deliberately left
        PER-INVOCATION rather than routed through ``once_per_day`` (audit A,
        row 6): its threshold is ``SKIP_ESCALATE_THRESHOLD`` (6), which the
        pre-existing ``daily`` branch already feeds once per hourly run, so
        day-gating it would make this lane's leaked-lock alarm six DAYS where
        the engine's own is six runs. ``writer_busy_since`` is the wall-clock
        half of the same check, and is a ``setdefault``."""
        holder = getattr(exc, "holder", {}) or {}
        iso = run.date.isoformat()
        run.results["remediation"] = {
            "target_signatures": {}, "branches": {},
            "status": "skipped-writer-busy", "held_by": holder}
        if run.dry_run:
            return
        state = rs.read_state(Path(self.vault))
        branches = dict(state.get("branches") or {})
        for name in _ALL_BRANCH_NAMES:
            row = dict(branches.get(name) or {})
            row["last_attempt"] = iso
            row["consecutive_skips"] = int(row.get("consecutive_skips", 0) or 0) + 1
            row.setdefault("writer_busy_since", iso)
            branches[name] = row
        rs.write_state(Path(self.vault), {"branches": branches})
        run.state[STATE_KEY] = {
            "last_attempt": iso, "status": "skipped-writer-busy",
            "branches": _projection(branches, "skipped-writer-busy")}

    def _remediation_locked(self, run: MaintenanceRun) -> None:
        try:
            state = rs.read_state(Path(self.vault))
        except Exception as exc:  # noqa: BLE001 — an unreadable store is loud, not fatal
            run.blocked.append(maintenance.blocked_item(
                f"remediation state unreadable: {exc}", "host-private store",
                "next maintain run"))
            return
        branches = dict(state.get("branches") or {})
        results: dict[str, Any] = {"target_signatures": {}, "branches": {}}
        # Owner-class exception key -> the targets that raised it, in ONE
        # place: every key here gets a signature below, so a third one cannot
        # be added without its subject — which is exactly how
        # `tamper:redirected-write` reopened the defect `tamper:unsigned-note`
        # had already closed (llm-review, final).
        exceptions: dict[str, list[str]] = {rf.TAMPER_KEY: [], rf.REDIRECT_KEY: []}
        for name, key, planner in _BRANCHES:
            entry = branches.get(name)
            row = dict(entry) if isinstance(entry, dict) else {}
            outcome, found = self._run_remediation_branch(run, name, key, planner, row)
            exceptions[rf.TAMPER_KEY].extend(found)
            if outcome.redirected:
                exceptions[rf.REDIRECT_KEY].append(outcome.redirected)
            branches[name] = row
            results["branches"][name] = outcome.report()
            # AUDIT B (2026-08-21): a shadow run has repaired NOTHING, so it
            # publishes no claim a suppression decision could read. The
            # enforcement is `binding_matches` (the module that DECIDES
            # suppression refuses a coverage row not stamped `live`); this is
            # the producer half, so a rehearsal's claim never even travels.
            if outcome.enumerated and outcome.repairing:
                results["target_signatures"][key] = rf.signature(outcome.targets)
        self._run_extract_retry(run, branches, results, exceptions)
        # Without its own signature a key's owner question is a bare
        # constant: one answer enters the ledger forever and every later
        # occurrence is skipped as decided, with no banner fallback. The
        # safety valve silences itself.
        for exception_key, targets in exceptions.items():
            results["target_signatures"][exception_key] = rf.signature(targets)
        run.results["remediation"] = results
        if not run.dry_run:
            rs.write_state(Path(self.vault), {"branches": branches})
            run.state[STATE_KEY] = {"last_run": run.date.isoformat(),
                                    "branches": results["branches"]}
            self._report_remediation(run, results, exceptions[rf.TAMPER_KEY])

    def _run_remediation_branch(
        self, run: MaintenanceRun, name: str, key: str,
        planner: Callable[[Any, int], Any], row: dict[str, Any],
    ) -> tuple[rf.BranchOutcome, list[str]]:
        """One branch: plan, decide shadow vs live, apply, then book-keep."""
        try:
            outcome, intents, tamper = planner(self, rf.batch_cap())
        except Exception as exc:  # noqa: BLE001 — one broken branch is not a broken run
            outcome = rf.BranchOutcome(name, key, available=False,
                                       error=f"{type(exc).__name__}: {exc}")
            record_failure(row, outcome, run.date)
            run.blocked.append(maintenance.blocked_item(
                f"remediation branch '{name}' raised: {outcome.error}",
                "repair branch", "next maintain run"))
            return outcome, []
        outcome.intents = [i.record() for i in intents]
        if in_shadow(row, key):
            outcome.mode = "shadow"
            record_shadow(row, intents, run.date)
        elif not run.dry_run:
            apply_intents(self, outcome, intents)
        else:
            outcome.mode = "dry-run"
        if outcome.redirected:
            run.action_required.append({
                **maintenance.action_required_item(
                    f"remediation/{name} stopped its batch: the bytes at "
                    f"`{outcome.redirected}` are not the bytes that were just "
                    "signed for that path",
                    "a write landed somewhere else while the fold held the "
                    "single-writer lock — the vault is being written "
                    "concurrently by something outside the audited path",
                    "stop the lane (BRAIN_REMEDIATION_DISABLED=1) and find the "
                    "writer before re-enabling it",
                    "brain verify-audit --check-content"),
                "notify_key": rf.REDIRECT_KEY,
            })
        if not run.dry_run:
            record_run(row, outcome, run.date)
        return outcome, tamper

    def _run_extract_retry(
        self, run: MaintenanceRun, branches: dict[str, Any],
        results: dict[str, Any], exceptions: dict[str, list[str]],
    ) -> None:
        """FIX-03 — its own driver, not the ``_BRANCHES`` loop above.

        ``extract_retry`` serves FOUR registry keys at once (one sub-floor
        invariant, three mechanical quarantine reasons) and writes to
        ``inbox/`` rather than through ``audited_write``, so it needs its own
        wiring rather than stretching the sign_repair/reguard shape to fit."""
        entry = branches.get(rf.EXTRACT_RETRY)
        row = dict(entry) if isinstance(entry, dict) else {}
        try:
            outcome, intents = rb.plan_extract_retry(
                self, rf.batch_cap(), row, run.date)
        except Exception as exc:  # noqa: BLE001 — one broken branch is not a broken run
            outcome = rf.BranchOutcome(rf.EXTRACT_RETRY, rf.KEY_SUBFLOOR,
                                       available=False,
                                       error=f"{type(exc).__name__}: {exc}")
            record_failure(row, outcome, run.date)
            run.blocked.append(maintenance.blocked_item(
                f"remediation branch '{rf.EXTRACT_RETRY}' raised: {outcome.error}",
                "repair branch", "next maintain run"))
            branches[rf.EXTRACT_RETRY] = row
            results["branches"][rf.EXTRACT_RETRY] = outcome.report()
            return
        outcome.intents = [i.record() for i in intents]
        if run.dry_run:
            outcome.mode = "dry-run"
        else:
            apply_extract_retry_intents(self, outcome, intents, row, run.date)
            record_extract_retry_run(row, outcome, run.date)
        branches[rf.EXTRACT_RETRY] = row
        results["branches"][rf.EXTRACT_RETRY] = outcome.report()
        if outcome.enumerated and outcome.repairing:
            for key, targets in outcome.by_key.items():
                results["target_signatures"][key] = rf.signature(targets)
        for key, targets in outcome.exhausted.items():
            # De-duplicated, ordered: two runs in the same hour must not
            # double-list a file, and the union across runs is what a
            # per-key `rf.signature` needs to stay stable while the set of
            # exhausted files does not change.
            merged = list(dict.fromkeys(exceptions.get(key, []) + targets))
            exceptions[key] = merged
        if not run.dry_run and outcome.exhausted:
            self._report_extract_exhaustions(run, outcome.exhausted)

    def _report_extract_exhaustions(
        self, run: MaintenanceRun, exhausted: dict[str, list[str]],
    ) -> None:
        """One owner-class finding per exhausted key, quoting that key's own
        remedy text back to the owner (s01-review finding: retrying an
        encrypted PDF forever must not banner the whole branch — it must name
        the file and say what to do about IT)."""
        from .. import maintenance_retention as _mr

        for key, targets in exhausted.items():
            if not targets:
                continue
            reason = key.split(":", 1)[1] if ":" in key else key
            if reason == "subfloor":
                remedy = (
                    "this document's extraction keeps failing after "
                    f"{rf.EXTRACT_RETRY_MAX_ATTEMPTS} retries. Manually re-extract "
                    "or replace the archived original (see the note's `origin:` "
                    "frontmatter) with a better-quality source, then move it "
                    "into `inbox/` and run `brain sync`")
            else:
                remedy = _mr._QUARANTINE_REMEDY.get(
                    reason, "inspect the file and its `.reason.txt` sidecar, fix "
                            "the cause, then move it back to `inbox/` and run "
                            "`brain sync`")
            shown = ", ".join(sorted(targets)[:5])
            more = f" (+{len(targets) - 5} more)" if len(targets) > 5 else ""
            run.action_required.append({
                **maintenance.action_required_item(
                    f"extract_retry retried {len(targets)} file(s) under `{key}` "
                    f"{rf.EXTRACT_RETRY_MAX_ATTEMPTS} times and they still fail: "
                    f"{shown}{more}",
                    "retrying forever would never converge, so this is now an "
                    "owner decision for these specific files — the branch keeps "
                    "working on everything else",
                    remedy,
                    str(Path(self.vault) / "inbox" / "_quarantine")),
                "notify_key": key,
            })

    def _report_remediation(
        self, run: MaintenanceRun, results: dict[str, Any], tamper: list[str],
    ) -> None:
        """One hot.md block per run, and the TAMPER exception as a finding."""
        reports = list(results["branches"].values())
        if any(r["intents"] or r["healed"] for r in reports):
            self._append_hot_once(
                f"maintain:remediation-run:{run.date.isoformat()}",
                render_hot_entry(reports, run.date))
        if tamper:
            run.action_required.append({
                **maintenance.action_required_item(
                    f"{len(tamper)} unsigned note(s) carry no host record of the "
                    "bytes on disk, so they cannot be signed automatically: "
                    + ", ".join(sorted(tamper)[:5]),
                    "an unsigned note with no matching audit-chain content hash "
                    "was not written by this host's audited path",
                    "rule on each file: admit it through `brain write`, or "
                    "remove it — a signature is never granted on trust",
                    "brain doctor --json"),
                "notify_key": rf.TAMPER_KEY,
            })


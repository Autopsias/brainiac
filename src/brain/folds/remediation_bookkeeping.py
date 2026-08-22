"""Shadow mode, application, and the host-private book-keeping for the
FIX-01/FIX-02 remediation umbrella fold.

Split out of :mod:`brain.folds.remediation` (the ``RemediationFoldsMixin``
stays there; this holds the free functions it calls) purely to keep that file
under the file-size ratchet — no behaviour change. See that module's
docstring for the umbrella's own design rationale.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from .. import remediation, remediation_folds as rf
from .. import remediation_branches as rb
from .. import remediation_state as rs


def registry_shadow_nights(key: str) -> int:
    try:
        remedy = remediation.resolve(key)
    except remediation.RegistryError:
        return 0
    return int(getattr(remedy, "shadow_nights", 0) or 0) if remedy else 0


def in_shadow(row: dict[str, Any], key: str) -> bool:
    """Whether this run must be report-only. Seeds the countdown on first sight."""
    remaining = row.get("shadow_nights_remaining")
    if not isinstance(remaining, int):
        remaining = registry_shadow_nights(key)
        row["shadow_nights_remaining"] = remaining
    return remaining > 0


def record_shadow(
    row: dict[str, Any], intents: list[rf.Intent], today: datetime.date,
) -> None:
    """Count a shadow night down — but only one, and only one that proved
    something.

    Three separate reasons not to decrement, and the FIRST one is what the
    window is actually specified in. The registry says ``shadow_nights=3`` and
    this fold rides the HOURLY daily flow, so a per-invocation countdown makes
    the acceptance window three HOURS: a vault with targets at 09:00 would be
    signing notes and raising classifications by noon (adversarial review round
    2, 2026-08-21). ``shadow_last_night`` is the same date guard
    :func:`heal_streaks` already applies for the same reason — at most one
    night is consumed per calendar day, whatever the hourly job does in
    between.

    The other two: a night with no targets advances nothing (a zero-target
    shadow run is not evidence the branch does the right thing), and neither
    does a night whose intended actions contain a write class nobody
    expected. Both are checked BEFORE the day is claimed — a run that claimed
    the day and then declined to count would silently eat that night."""
    if not intents:
        return
    if any(i.write_class not in rf.EXPECTED_WRITE_CLASSES for i in intents):
        return
    if not rs.once_per_day(row, "shadow_nights", today):
        return
    row["shadow_nights_remaining"] = max(
        0, int(row.get("shadow_nights_remaining", 0) or 0) - 1)


def apply_intents(
    core: Any, outcome: rf.BranchOutcome, intents: list[rf.Intent],
) -> None:
    """Do the repairs.

    An ordinary refusal is a SKIP — never a crash, never a retry: one note
    that would lower a tier or sits behind a symlink must not block the
    repairs queued behind it.

    A :class:`rf.RedirectedWrite` is NOT ordinary and does not continue. It
    means a write landed somewhere other than the path it was signed for while
    this fold held the writer lock, so every remaining target in the batch
    would be writing into the same race. The batch stops, the target is
    recorded, and ``_run_remediation_branch`` turns it into an owner-class
    exception — which is what ``remediation_folds``' own docstring has always
    said this control does (llm-review, 2026-08-21: the code said otherwise)."""
    for intent in intents:
        try:
            rf.audited_write(core, intent.target, intent.content,
                             f"remediation/{outcome.branch}: {intent.detail}",
                             write_class=intent.write_class)
        except rf.RedirectedWrite as exc:
            outcome.redirected = intent.target
            outcome.skipped.append({"target": intent.target,
                                    "reason": f"BATCH STOPPED — {exc}"})
            return
        except Exception as exc:  # noqa: BLE001 — one refusal must not stop the batch
            outcome.skipped.append({"target": intent.target,
                                    "reason": f"{type(exc).__name__}: {exc}"})
            continue
        outcome.healed.append(intent.target)


def apply_extract_retry_intents(
    core: Any, outcome: rf.BranchOutcome, intents: list[rf.Intent],
    row: dict[str, Any], today: datetime.date,
) -> None:
    """FIX-03's own apply step: COPY (never move) the retried bytes into
    ``inbox/`` for the existing ingest retry lane (26dbd97) to pick up.

    Never goes through ``rf.audited_write`` — ``inbox/`` is the drop zone,
    not the audited ``vault/brain``/``vault/raw`` zone, so nothing here signs
    anything or touches the index. The source (a quarantined file, or an
    archived original under ``raw/originals/``) is never deleted or moved;
    only a copy is written, so a failed retry leaves the original exactly
    where it was for the next attempt or an owner to find by hand."""
    import shutil

    inbox = Path(core.vault) / "inbox"
    for intent in intents:
        source = Path(intent.content)
        try:
            from ..snapshot import _sha256_file

            content_hash = _sha256_file(source)
        except OSError as exc:
            outcome.skipped.append({"target": intent.target,
                                    "reason": f"source unreadable: {exc}"})
            continue
        try:
            inbox.mkdir(parents=True, exist_ok=True)
            dest = inbox / f"retry-{content_hash[:12]}-{source.name}"
            shutil.copy2(source, dest)
        except OSError as exc:
            outcome.skipped.append({"target": intent.target,
                                    "reason": f"copy failed: {exc}"})
            continue
        entry = rb._attempt_entry(row, content_hash)
        # Through the shared day-claim primitive (audit A, `once_per_day`'s
        # class): the planner already excludes a target retried today from
        # ever reaching an intent, so this claim is expected to succeed every
        # time it is reached — but the INCREMENT itself is what the class
        # guard requires route through here, not a hand-rolled date compare.
        if rs.once_per_day(entry, "count", today):
            entry["count"] = int(entry.get("count", 0) or 0) + 1
        entry["target"] = intent.target
        outcome.healed.append(intent.target)


def record_failure(
    row: dict[str, Any], outcome: rf.BranchOutcome, today: datetime.date,
) -> None:
    """Count ONE failed day, not one failed hour.

    ``branch_liveness`` compares this against the registry's
    ``escalate_after``, which is declared in runs at the branch's CADENCE
    (1 day) — so on this hourly fold a bare increment raised the
    unsuppressible ``branch-escalate:`` banner after three hours. Third site
    of the class ``once_per_day`` now owns; the error text is a full-state
    overwrite and is refreshed every run regardless."""
    row["last_error"] = outcome.error
    if rs.once_per_day(row, "consecutive_failures", today):
        row["consecutive_failures"] = int(
            row.get("consecutive_failures", 0) or 0) + 1


def record_run(
    row: dict[str, Any], outcome: rf.BranchOutcome, today: datetime.date,
) -> None:
    """Persist one run into the branch's host-private row."""
    row["cadence"] = remediation.BRANCH_CADENCE_DAYS.get(outcome.branch, 1)
    row["last_attempt"] = today.isoformat()
    row["last_cost_usd"] = outcome.cost_usd   # a MEASURED value, design-freeze (d)
    if not outcome.available:
        record_failure(row, outcome, today)
        return
    row["last_run"] = today.isoformat()
    # Through the primitive, so the day claim is RELEASED with the counter:
    # fail -> success -> fail on one day left the marker standing and the
    # second failure did not count until tomorrow (llm-review, final).
    rs.reset_daily(row, "consecutive_failures")
    row["consecutive_skips"] = 0   # per-invocation by design — audit A, row 6
    row.pop("writer_busy_since", None)
    healed = set(outcome.healed)
    remaining = [t for t in outcome.targets if t not in healed]
    _record_convergence(row, outcome, remaining, today)
    row["heal_streaks"] = heal_streaks(
        row.get("heal_streaks"), outcome.healed, today)


def record_extract_retry_run(
    row: dict[str, Any], outcome: rf.BranchOutcome, today: datetime.date,
) -> None:
    """``record_run``, minus ``heal_streaks``.

    ``extract_retry`` already carves an exhausted target out of the branch
    population via its OWN content-hash attempt counter and its OWN owner
    exception (``BranchOutcome.exhausted``, reported by
    ``_report_extract_exhaustions``). Feeding its retry ATTEMPTS into the
    generic ``THRASH_NIGHTS`` detector too would raise a SECOND, less
    specific owner question ("this branch keeps re-healing the same target")
    about the exact file the first question already named with its own
    remedy — retrying a target for up to ``EXTRACT_RETRY_MAX_ATTEMPTS``
    consecutive nights is this branch's designed behaviour, not thrashing."""
    row["cadence"] = remediation.BRANCH_CADENCE_DAYS.get(outcome.branch, 1)
    row["last_attempt"] = today.isoformat()
    row["last_cost_usd"] = outcome.cost_usd
    if not outcome.available:
        record_failure(row, outcome, today)
        return
    row["last_run"] = today.isoformat()
    rs.reset_daily(row, "consecutive_failures")
    row["consecutive_skips"] = 0
    row.pop("writer_busy_since", None)
    healed = set(outcome.healed)
    remaining = [t for t in outcome.targets if t not in healed]
    _record_convergence(row, outcome, remaining, today)


def _record_convergence(
    row: dict[str, Any], outcome: rf.BranchOutcome, remaining: list[str],
    today: datetime.date,
) -> None:
    digest = rf.target_set_hash(remaining)
    if outcome.mode == "live":
        # Non-convergence is a property of a branch that IS writing. A shadow
        # night deliberately changes nothing, so counting it would escalate
        # every branch exactly as it earns promotion.
        _count_unchanged(row, outcome, remaining, digest, today)
    else:
        row.setdefault("unchanged_runs", 0)
    row["remaining_target_hash"] = digest
    row["remaining_target_count"] = len(remaining)
    covered = dict(row.get("covered") or {})
    remaining_set = set(remaining)
    # ``by_key`` is FIX-03's addition: extract_retry serves four registry
    # keys from one branch row, so its per-key convergence is recorded here
    # too rather than once for the branch as a whole. Every other branch
    # leaves ``by_key`` empty and falls back to its single ``key`` exactly as
    # before this existed.
    for key, key_targets in (outcome.by_key or {outcome.key: outcome.targets}).items():
        previous = covered.get(key)
        previous_remaining = (int(previous.get("remaining", 0) or 0)
                              if isinstance(previous, dict) else len(key_targets))
        key_remaining = [t for t in key_targets if t in remaining_set]
        covered[key] = {
            "detector_generation": rf.DETECTOR_GENERATION,
            "target_set_hash": rf.target_set_hash(key_targets),
            "remaining": len(key_remaining),
            "previous_remaining": previous_remaining,
            # Written HONESTLY on every run, including a rehearsal, and CHECKED by
            # `binding_matches`. Recording the mode beats omitting the row: an
            # absent row is indistinguishable from a branch that never ran, while
            # a row stamped `shadow` says exactly what happened and still cannot
            # suppress anything (audit B, 2026-08-21).
            "mode": outcome.mode,
        }
    row["covered"] = covered


def _count_unchanged(
    row: dict[str, Any], outcome: rf.BranchOutcome, remaining: list[str],
    digest: str, today: datetime.date,
) -> None:
    """Advance ``unchanged_runs`` at most once per CALENDAR DAY.

    Second site of the class ``remediation_state.once_per_day`` owns; only the
    INCREMENT is gated (see ``reset_daily``). PROGRESS means healing
    something, not merely a different leftover set: a run that healed a target
    while the remainder happened to hash the same is converging, and counting
    it would report a working branch as stuck. Found by this fix's own
    probe."""
    if outcome.healed or not remaining or row.get(
            "remaining_target_hash") != digest:
        rs.reset_daily(row, "unchanged_runs")
        return
    if rs.once_per_day(row, "unchanged_runs", today):
        row["unchanged_runs"] = int(row.get("unchanged_runs", 0) or 0) + 1


def heal_streaks(
    previous: Any, healed: list[str], today: datetime.date,
) -> dict[str, dict[str, Any]]:
    """Per-target heal history, pruned, for ``remediation_state.thrashing_targets``."""
    iso, out = today.isoformat(), {}
    yesterday = (today - datetime.timedelta(days=1)).isoformat()
    cutoff = (today - datetime.timedelta(days=rf.HEAL_STREAK_RETENTION_DAYS)).isoformat()
    for target, row in (previous or {}).items():
        if isinstance(row, dict) and str(row.get("last") or "") >= cutoff:
            out[str(target)] = dict(row)
    for target in healed:
        prior = out.get(target) or {}
        last, nights = str(prior.get("last") or ""), int(prior.get("nights", 0) or 0)
        if last == iso:
            continue                      # the fold runs hourly; one night, one night
        out[target] = {"last": iso,
                       "nights": nights + 1 if last == yesterday else 1}
    return out


def render_hot_entry(reports: list[dict[str, Any]], today: datetime.date) -> str:
    """ONE dated block per run — a log, never a queue item."""
    lines = [f"## {today.isoformat()} — Remediation branches"]
    for report in reports:
        verb = "would repair" if report["mode"] == "shadow" else "repaired"
        lines.append(
            f"- **{report['branch']}** ({report['mode']}): {verb} "
            f"{len(report['intents']) if report['mode'] == 'shadow' else report['healed']}"
            f", skipped {report['skipped']}, remaining {report['remaining']} "
            f"of {report['targets']}")
        for intent in report["intents"][:5]:
            lines.append(f"  - `{intent['target']}` [{intent['write_class']}] "
                         f"{intent['detail']}")
    lines.append("- **Next:** nothing. A branch that stops converging, dies, or "
                 "is shown targets it has not processed banners on its own.")
    return "\n".join(lines) + "\n"

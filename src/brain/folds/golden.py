"""Schedule the Sunday golden-probe execution."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any

from .context import MaintenanceRun
from .. import config, maintenance
from ..atomic_temp import atomic_temp_path

#: Stable ``notify_key`` of the refusal below. Without one the action item never
#: reaches ``degradation_findings`` -> ``notify-sent/current.json`` -> ``brain
#: alerts`` and dies in the log; registered as a banner in ``remediation.py``.
PROBES_CHANGED_KEY = "golden-probes-changed"


class GoldenFoldsMixin:
    """Provide the WD-03 golden-probe fold."""

    def golden_fold(self, run: MaintenanceRun) -> None:
        """Execute the due golden probe with attempt-persisted backoff."""
        if "golden" not in run.branches:
            return
        try:
            probes_path = maintenance.golden_probes_path(Path(self.vault))
            if not probes_path.is_file():
                self._record_missing_golden_probes(run, probes_path)
                return
            marker = run.state.get("_golden_attempt")
            marker = marker if isinstance(marker, dict) else None
            now = dt.datetime.now(dt.timezone.utc)
            if not maintenance.golden_attempt_due(marker, now):
                run.results["golden"] = {
                    "score": None,
                    "runner": None,
                    "degraded": False,
                    "skipped": "cooldown",
                    "next_retry_at": (marker or {}).get("next_retry_at"),
                }
                return
            if run.dry_run:
                run.results["golden"] = {
                    "score": None,
                    "runner": None,
                    "degraded": False,
                    "dry_run": True,
                    "would_run": True,
                    "probes_path": str(probes_path),
                }
                run.mark("golden", True)
                return
            self._execute_golden_probe(run, probes_path, marker, now)
        except Exception as exc:
            self._persist_golden_exception_backoff(run)
            detail = f"{type(exc).__name__}: {exc}"
            run.blocked.append(
                maintenance.blocked_item(
                    "golden branch raised",
                    detail,
                    "re-run maintain after the underlying error is fixed",
                )
            )
            run.mark("golden", False, detail)

    def _record_missing_golden_probes(
        self, run: MaintenanceRun, probes_path: Path
    ) -> None:
        """Report the absent per-vault probes file as an owner action."""
        run.results["golden"] = {
            "score": None,
            "runner": None,
            "degraded": False,
            "skipped": "no probes file",
        }
        run.action_required.append(
            maintenance.action_required_item(
                f"golden-probe branch skipped: no probes file at {probes_path}",
                "WD-03 cross-family execution needs a per-vault "
                "eval/golden-probes.json (WD-02) to score",
                "author a probes file (see `brain-golden-probe --help` "
                "/ docs/operations/s06-evidence.md)",
                str(probes_path),
            )
        )
        run.mark("golden", True)

    def _execute_golden_probe(
        self,
        run: MaintenanceRun,
        probes_path: Path,
        marker: dict[str, Any] | None,
        now: dt.datetime,
    ) -> None:
        """Persist an attempt, execute the scorer, then classify its result."""
        frozen = self._frozen_golden_probes(run, probes_path)
        if frozen is None:
            return
        original_failures, provisional = self._provisional_golden_marker(marker, now)
        run.state["_golden_attempt"] = provisional
        self._save_maintain_state(run.state)
        runner = run.golden_runner or self._run_golden_probe
        result = runner(probes_path=frozen)
        run.results["golden"] = result
        exit_code = result.get("exit_code")
        transient = exit_code not in (
            maintenance.GOLDEN_EXIT_OK,
            maintenance.GOLDEN_EXIT_REGRESSION,
            maintenance.GOLDEN_EXIT_ACTION_REQUIRED,
        )
        run.state["_golden_attempt"] = maintenance.update_golden_attempt_marker(
            {
                **provisional,
                "consecutive_transient_failures": original_failures,
            },
            now,
            transient=transient,
        )
        self._save_maintain_state(run.state)
        if transient:
            self._record_transient_golden(run, result)
            return
        self._record_deterministic_golden(run, probes_path, result, exit_code)

    def _frozen_golden_probes(
        self, run: MaintenanceRun, probes_path: Path
    ) -> Path | None:
        """Return the host-private copy of the probes file to score, or None
        (recorded as an owner action) when the live file no longer matches it.

        The live file sits in the vault, which the Cowork VM can write, so
        scoring it directly let the VM swap in trivially-passing probes and
        record a healthy golden_score over a real regression (F127). The first
        run pins the file as it stands (trust on first use, so a vault that
        already has one keeps running; shortcut: a file tampered with BEFORE
        that run is pinned as is). Later runs refuse a changed file, and the
        scorer reads only the pinned bytes, so there is no check-then-read
        race. To accept an edit the owner deletes the pin the action item
        names; the next run re-pins. An unprovable or unwritable pin store
        raises into golden_fold's handler: nothing is scored, nothing crashes.
        """
        pin = config.golden_probes_pin_path(self.vault)
        live = probes_path.read_bytes()
        if not pin.is_file():
            pin.parent.mkdir(parents=True, exist_ok=True)
            config.secure_file_permissions(pin.parent, 0o700)
            tmp = atomic_temp_path(pin)
            try:
                tmp.write_bytes(live)
                os.replace(tmp, pin)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        elif pin.read_bytes() != live:
            run.results["golden"] = {
                "score": None,
                "runner": None,
                "degraded": False,
                "disposition": "action_required",
                "exit_code": maintenance.GOLDEN_EXIT_ACTION_REQUIRED,
                "skipped": "probes file changed since pinned",
            }
            run.action_required.append({
                **maintenance.action_required_item(
                    f"golden-probe branch refused: {probes_path} changed since "
                    "it was pinned; it was NOT scored",
                    "the probes file lives in the vault, where a Cowork VM can "
                    "write it; a rewritten file could score 1.0 over a real "
                    "retrieval regression",
                    f"if you made the change, delete {pin} (the next golden "
                    "run pins the file as it stands); if not, restore the "
                    "probes file from backup",
                    str(probes_path),
                ),
                "notify_key": PROBES_CHANGED_KEY,
            })
            return None  # not marked done: stays due, so it is re-raised until resolved
        return pin

    def _provisional_golden_marker(
        self, marker: dict[str, Any] | None, now: dt.datetime
    ) -> tuple[int, dict[str, Any]]:
        """Build the escalating marker written before the scorer starts."""
        base_minutes = maintenance.DEFAULT_GOLDEN_RETRY_BASE_MINUTES
        original_failures = int((marker or {}).get("consecutive_transient_failures", 0))
        provisional_failures = original_failures + 1
        provisional = dict(marker or {})
        provisional["last_attempt"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        provisional["consecutive_transient_failures"] = provisional_failures
        provisional["next_retry_at"] = (
            now
            + dt.timedelta(
                minutes=maintenance.golden_retry_backoff_minutes(
                    base_minutes, provisional_failures
                )
            )
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        return original_failures, provisional

    def _record_transient_golden(
        self, run: MaintenanceRun, result: dict[str, Any]
    ) -> None:
        """Record a retryable scorer result without advancing last_run."""
        error = (
            result.get("error")
            or result.get("codex_error")
            or "no deterministic result"
        )
        run.blocked.append(
            maintenance.blocked_item(
                f"golden-probe run was transient (runner={result.get('runner')}): "
                f"{error}",
                "the brain CLI itself failed/emitted non-JSON, or codex could not "
                "be validated and the self-run fallback also failed",
                "bounded backoff will retry automatically once the cooldown elapses",
            )
        )
        run.mark("golden", False, "transient")

    def _record_deterministic_golden(
        self,
        run: MaintenanceRun,
        probes_path: Path,
        result: dict[str, Any],
        exit_code: object,
    ) -> None:
        """Surface regression, configuration, or degraded deterministic results."""
        if exit_code == maintenance.GOLDEN_EXIT_ACTION_REQUIRED:
            run.action_required.append(
                maintenance.action_required_item(
                    f"golden-probe run is config-invalid (score={result.get('score')})",
                    "a deterministic problem in the probes file/vault anchors — "
                    "never retried before next Sunday",
                    "fix the probes file, then re-run `brain-golden-probe` manually "
                    "to confirm",
                    str(probes_path),
                )
            )
        elif exit_code == maintenance.GOLDEN_EXIT_REGRESSION:
            run.action_required.append(
                maintenance.action_required_item(
                    f"golden-probe regression: score {result.get('score')}",
                    "retrieval quality regressed below the probes-file threshold",
                    "run the autoresearch skill or review recent promotions/curation "
                    "findings",
                    str(probes_path),
                )
            )
        if result.get("degraded"):
            run.action_required.append(
                maintenance.action_required_item(
                    "golden-probe ran in DEGRADED (self) mode — codex execution "
                    f"unavailable/unvalidated: {result.get('codex_error')}",
                    "cross-family EXECUTION requires codex to actually run the scorer; "
                    "a persistent degraded state means that isn't happening",
                    "check codex CLI availability/auth on this host",
                    str(probes_path),
                )
            )
        run.mark("golden", True)

    def _persist_golden_exception_backoff(self, run: MaintenanceRun) -> None:
        """Best-effort persist a retry floor after an unexpected branch raise."""
        if run.dry_run:
            return
        try:
            current = run.state.get("_golden_attempt")
            current = dict(current) if isinstance(current, dict) else {}
            now = dt.datetime.now(dt.timezone.utc)
            existing = self._parse_golden_retry_at(current.get("next_retry_at"))
            if existing is not None and existing > now:
                return
            base_minutes = maintenance.DEFAULT_GOLDEN_RETRY_BASE_MINUTES
            current["last_attempt"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            current["next_retry_at"] = (
                now + dt.timedelta(minutes=base_minutes)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            run.state["_golden_attempt"] = current
            self._save_maintain_state(run.state)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _parse_golden_retry_at(value: object) -> dt.datetime | None:
        """Parse the UTC retry marker with the branch's original tolerance."""
        if not value:
            return None
        try:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed

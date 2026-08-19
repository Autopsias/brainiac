"""Enforce scheduled retention windows."""

from __future__ import annotations

from pathlib import Path

from .context import MaintenanceRun
from .. import cos_corpus, maintenance, querylog


class RetentionFoldsMixin:
    """Provide daily host-private retention folds."""

    def retention_schedule_fold(self, run: MaintenanceRun) -> None:
        """Run each retention lane at most once per maintenance date."""
        marker = run.state.get("_retention")
        marker = marker if isinstance(marker, dict) else {}
        if marker.get("last_run") == run.date.isoformat():
            return
        self.duplicate_retention_fold(run)
        self.query_capture_retention_fold(run)
        self.cos_corpus_retention_fold(run)

    def duplicate_retention_fold(self, run: MaintenanceRun) -> None:
        """Prune only provenance-verified aged duplicates."""
        try:
            result = maintenance.retention_fold(Path(self.vault), run.date)
            run.results["retention"] = result
            if not run.dry_run:
                run.state["_retention"] = {"last_run": run.date.isoformat()}
            if result["pruned"]:
                run.auto_fixed.append(
                    maintenance.auto_fixed_item(
                        "duplicate-retention",
                        str(Path(self.vault) / "inbox" / "_duplicate"),
                        f"pruned {len(result['pruned'])} duplicate(s) older than "
                        f"{result['retention_days']}d (provenance-verified)",
                    )
                )
            self._record_retention_skips(run, result["skipped"])
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"duplicate-retention fold failed: {exc}",
                    "filesystem/manifest read",
                    "next maintain run",
                )
            )

    def _record_retention_skips(self, run: MaintenanceRun, skipped: list[dict]) -> None:
        """Distinguish conservative provenance holds from filesystem failures."""
        provenance_skips = [
            item for item in skipped if item.get("kind") == "provenance"
        ]
        filesystem_skips = [
            item for item in skipped if item.get("kind") in {"delete", "stat"}
        ]
        location = str(Path(self.vault) / "inbox" / "_duplicate")
        if provenance_skips:
            run.action_required.append(
                maintenance.action_required_item(
                    f"{len(provenance_skips)} aged duplicate(s) kept — their "
                    "provenance chain does not verify",
                    "an unverifiable duplicate is never auto-deleted (its archived "
                    "original may be missing/changed)",
                    "inspect inbox/_duplicate and the referenced manifest/raw/originals entries",
                    location,
                )
            )
        if filesystem_skips:
            run.action_required.append(
                maintenance.action_required_item(
                    f"{len(filesystem_skips)} aged duplicate(s) could not be "
                    "stat'd/deleted (filesystem error)",
                    "a real I/O/permission error, NOT a provenance failure — the "
                    "file was not removed",
                    "check inbox/_duplicate permissions/mount",
                    location,
                )
            )

    def query_capture_retention_fold(self, run: MaintenanceRun) -> None:
        """Unlink only whole expired host query-ledger month files."""
        try:
            result = querylog.prune_expired_months(
                self.vault, role=self.role, today=run.date
            )
            run.results["query_capture_retention"] = result
            if result.get("pruned"):
                run.auto_fixed.append(
                    maintenance.auto_fixed_item(
                        "query-log-retention",
                        "host query ledger",
                        f"pruned {len(result['pruned'])} expired whole month file(s)",
                    )
                )
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"query-log retention fold failed: {exc}",
                    "host query ledger",
                    "next maintain run",
                )
            )

    def cos_corpus_retention_fold(self, run: MaintenanceRun) -> None:
        """Prune whole expired COS corpus runs using the real UTC clock."""
        try:
            result = cos_corpus.prune(self.vault)
            run.results["cos_corpus_retention"] = result
            if not run.dry_run and not result["errors"]:
                run.state[cos_corpus.PRUNE_MARKER] = {
                    "last_run": cos_corpus.cos.utcnow().date().isoformat()
                }
            if result["errors"]:
                run.blocked.append(
                    maintenance.blocked_item(
                        "COS capture-corpus retention did not complete: "
                        f"{'; '.join(result['errors'][:3])} — expired mail bodies "
                        "are still on disk and status will keep reporting retention "
                        "as not run here",
                        "COS capture corpus",
                        "next maintain run",
                    )
                )
            if result["pruned"]:
                run.auto_fixed.append(
                    maintenance.auto_fixed_item(
                        "cos-corpus-retention",
                        "COS capture corpus",
                        f"pruned {len(result['pruned'])} corpus file(s) older than "
                        f"{result['retention_days']}d",
                    )
                )
            if result["held"]:
                run.action_required.append(
                    maintenance.action_required_item(
                        f"{len(result['held'])} expired capture corpus/corpora are "
                        "past retention but never closed",
                        "an unclosed corpus is never auto-deleted — its capture stage "
                        "may still hold the file open",
                        "confirm no run is writing them, then `brain cos-corpus-close "
                        "<run>` so they age out",
                        "COS capture corpus",
                    )
                )
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"COS capture-corpus retention fold failed: {exc}",
                    "COS capture corpus",
                    "next maintain run",
                )
            )

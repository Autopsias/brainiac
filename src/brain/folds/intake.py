"""Stage external intake before the nightly sync."""

from __future__ import annotations

from .context import MaintenanceRun
from .. import config, maintenance


class IntakeFoldsMixin:
    """Provide pre-sync maintenance intake folds."""

    def future_artifact_fold(self, run: MaintenanceRun) -> None:
        """Remove future-dated derived brief artifacts before regeneration."""
        reaped = maintenance.reap_future_dated_artifacts(
            config.brief_dir(self.vault), run.date
        )
        if reaped:
            run.auto_fixed.append(
                maintenance.auto_fixed_item(
                    "reap-future-artifacts",
                    "brief/",
                    f"removed {len(reaped)} future-dated brief/digest file(s): "
                    f"{', '.join(reaped)}",
                )
            )

    def workspace_sweep_fold(self, run: MaintenanceRun) -> None:
        """Sweep settled workspace files into the ingestion drop zone."""
        sweep_dirs, sweep_age = maintenance.workspace_sweep_config()
        if not sweep_dirs:
            return
        try:
            result = maintenance.sweep_workspace(
                sweep_dirs, self.vault / "inbox", sweep_age
            )
            run.results["workspace_sweep"] = result
            if result["swept"]:
                run.auto_fixed.append(
                    maintenance.auto_fixed_item(
                        "workspace-sweep",
                        str(self.vault / "inbox"),
                        f"swept {len(result['swept'])} settled workspace file(s) "
                        f"into inbox/ (age>{sweep_age}d)",
                    )
                )
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"workspace sweep failed: {exc}",
                    "filesystem",
                    "next maintain run",
                )
            )

    def provision_drain_fold(self, run: MaintenanceRun) -> None:
        """Drain pending new-vault provision requests written by a Cowork session.

        PRV-10 (VM-request → host-drain, owner ruling 2026-08-16: automatic,
        loudly reported). Rides the hourly daily branch instead of a new
        scheduled task (AGENTS.md §6). Cheap no-op scan when no request is
        pending."""
        try:
            from .. import provision as _provision
            _provision.maintain_fold(
                run.results, run.auto_fixed, run.action_required)
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"provision drain failed: {exc}",
                    "workspace registry / filesystem",
                    "next maintain run",
                )
            )

    def cos_ingest_sweep_fold(self, run: MaintenanceRun) -> None:
        """Quarantine manifest-named downloads for the current owner batch."""
        try:
            result = self.cos_ingest_sweep()
            run.results["cos_ingest_sweep"] = result
            if result.get("moved"):
                run.auto_fixed.append(
                    maintenance.auto_fixed_item(
                        "cos-ingest-sweep",
                        str(self.vault),
                        f"quarantined {len(result['moved'])} manifest-named "
                        "download(s) for an owner verdict",
                    )
                )
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"COS ingest sweep failed: {exc}",
                    "downloads dir / cos ops dir",
                    "next maintain run",
                )
            )

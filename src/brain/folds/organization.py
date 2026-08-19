"""Apply automatic vault organization after sync."""

from __future__ import annotations

from pathlib import Path

from .context import MaintenanceRun
from .. import maintenance


class OrganizationFoldsMixin:
    """Provide post-sync metadata organization folds."""

    def sync_reconcile_fold(self, run: MaintenanceRun) -> None:
        """Drain and reconcile the writable index before organization folds."""
        result = self.sync(drain=True, publish=False)
        run.results["sync"] = result
        added = result.get("added", 0)
        updated = result.get("updated", 0)
        deleted = result.get("deleted", 0)
        rebased = result.get("rebased", 0)
        if added or updated or deleted or rebased:
            rebased_text = (
                f" ={rebased} path-rebased (move, no re-embed)" if rebased else ""
            )
            run.auto_fixed.append(
                maintenance.auto_fixed_item(
                    "sync",
                    str(self.vault),
                    f"index reconciled +{added} ~{updated} -{deleted}{rebased_text}",
                )
            )
        drain = result.get("drain", {}) or {}
        if drain.get("promoted"):
            run.auto_fixed.append(
                maintenance.auto_fixed_item(
                    "drain",
                    str(self.capture_inbox_dir()),
                    f"drained {drain['promoted']} pending capture(s)",
                )
            )
        # A quarantined drop is a document the owner MEANT to
        # ingest and did not get — reported the run it happens
        # (see `ingest_quarantine_findings` for why neither the
        # trend metric nor the monthly triage can catch it).
        run.action_required.extend(
            maintenance.ingest_quarantine_findings(
                result.get("ingest", {}), Path(self.vault)
            )
        )

    def version_chain_fold(self, run: MaintenanceRun) -> None:
        """Stamp explicit version families without overriding manual links."""
        try:
            result = maintenance.auto_version_chains(self)
            run.results["version_chains"] = result
            if result["chained"]:
                run.auto_fixed.append(
                    maintenance.auto_fixed_item(
                        "version-chain",
                        str(self.vault),
                        f"stamped {len(result['chained'])} supersession link(s) "
                        "across explicit version families",
                    )
                )
            for family in result["skipped_conflict"]:
                run.action_required.append(
                    maintenance.action_required_item(
                        f"version family '{family}' has a manual chain that "
                        "disagrees with the computed order",
                        "auto-chaining never overrides a human supersede",
                        "inspect the family and fix the chain with `brain supersede` "
                        "if the manual link is wrong",
                        family,
                    )
                )
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"auto version-chain fold failed: {exc}",
                    "index/write path",
                    "next maintain run",
                )
            )

    def auto_dedup_fold(self, run: MaintenanceRun) -> None:
        """Retire only DDP-01's safe sha256-identical duplicate pairs."""
        try:
            result = maintenance.auto_dedup_tier1(self)
            run.results["autodedup"] = result
            previous = (
                run.state.get("daily")
                if isinstance(run.state.get("daily"), dict)
                else {}
            )
            run.state["daily"] = {
                **previous,
                "autodedup_retired": len(result["retired"]),
                "autodedup_skipped_short_body": len(
                    result.get("skipped_short_body", [])
                ),
                "autodedup_skipped_classification": len(
                    result["skipped_classification"]
                ),
                "autodedup_skipped_recurring": len(result["skipped_recurring"]),
                "autodedup_skipped_trust": len(result["skipped_trust"]),
            }
            self._record_auto_dedup_outcomes(run, result)
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"auto-dedup fold failed: {exc}",
                    "index/write path",
                    "next maintain run",
                )
            )

    def _record_auto_dedup_outcomes(self, run: MaintenanceRun, result: dict) -> None:
        """Project dedup decisions and trust/classification refusals into outcomes."""
        if result["retired"]:
            run.auto_fixed.append(
                maintenance.auto_fixed_item(
                    "auto-dedup",
                    str(self.vault),
                    f"auto-superseded {len(result['retired'])} sha256-identical "
                    "duplicate pair(s) (DDP-01)",
                )
            )
        if result["skipped_classification"]:
            run.action_required.append(
                maintenance.action_required_item(
                    f"{len(result['skipped_classification'])} sha256-identical "
                    "pair(s) span different classifications",
                    "classification decisions are never automated",
                    "review the pair and `brain supersede` by hand if the duplicate "
                    "really is retired content",
                    "auto-dedup",
                )
            )
        if result["skipped_trust"]:
            run.action_required.append(
                maintenance.action_required_item(
                    f"{len(result['skipped_trust'])} sha256-identical pair(s) span "
                    "different trust levels (one side is a draft/untrusted-provenance note)",
                    "an untrusted draft must never automatically retire a trusted note "
                    "(codex 2026-07-22)",
                    "review the pair and `brain supersede` by hand if the duplicate "
                    "really is retired content",
                    "auto-dedup",
                )
            )
        if not run.dry_run and (result["retired"] or result["truncated"]):
            self._record_auto_dedup_hot_entry(run, result)

    def _record_auto_dedup_hot_entry(self, run: MaintenanceRun, result: dict) -> None:
        """Log a completed dedup pass without turning a hot-write failure into failure."""
        try:
            self._append_hot_once(
                f"maintain:autodedup:{run.date.isoformat()}:"
                f"{len(result['retired'])}:{result['truncated']}",
                maintenance.render_autodedup_hot_entry(result, run.date),
            )
        except Exception as exc:
            run.action_required.append(
                maintenance.action_required_item(
                    "auto-dedup hot-queue entry could not be written",
                    f"{type(exc).__name__}: {exc}",
                    "check .brain/memory/hot.md writability; the dedup pass itself "
                    "completed fine",
                    "auto-dedup",
                )
            )

    def auto_para_fold(self, run: MaintenanceRun) -> None:
        """File notes into PARA zones according to their metadata."""
        try:
            result = maintenance.auto_para(Path(self.vault), audit=self.audit)
            run.results["auto_para"] = result
            if result["moved"]:
                run.auto_fixed.append(
                    maintenance.auto_fixed_item(
                        "auto-para",
                        str(Path(self.vault) / "brain"),
                        f"filed {len(result['moved'])} note(s) into their PARA zone "
                        "by metadata",
                    )
                )
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"auto-PARA fold failed: {exc}",
                    "filesystem",
                    "next maintain run",
                )
            )

    def navigation_fold(self, run: MaintenanceRun) -> None:
        """Regenerate backlinks and zone catalogs from canonical notes."""
        try:
            result = maintenance.refresh_navigation(Path(self.vault))
            run.results["navigation"] = result
            run.auto_fixed.append(
                maintenance.auto_fixed_item(
                    "navigation",
                    str(Path(self.vault) / "brain"),
                    f"regenerated backlinks ({result['backlink_targets']} targets) + "
                    f"{len(result['catalog_counts'])} zone catalogs",
                )
            )
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    f"navigation refresh failed: {exc}",
                    "filesystem",
                    "next maintain run",
                )
            )

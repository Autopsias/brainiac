"""Publish the reconciled nightly view."""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

from .context import MaintenanceRun
from .. import config, maintenance


class PublishFoldsMixin:
    """Provide final daily publication folds."""

    def _daily_note_fold(self, today: Any, brief_result: Any = None) -> dict[str, Any]:
        """Create today's linked ``type: daily`` note exactly once."""
        note_id = f"daily-{today.isoformat()}"
        if self.get(note_id) is not None:
            return {"created": False, "id": note_id}
        lines = [
            f"# {today.isoformat()} ({today.strftime('%A')})",
            "",
            "## Session Summary",
        ]
        try:
            for note in (brief_result or {}).get("recent_notes") or []:
                title = (
                    (note.get("title") or note.get("id") or "").strip()
                    if isinstance(note, dict)
                    else str(note).strip()
                )
                if title:
                    lines.append(f"- {title}")
        except Exception:
            pass
        lines += [
            "",
            "## Work Done",
            "",
            "## Open Threads",
            "",
            "## Next Session",
            "",
        ]
        previous_id = f"daily-{(today - dt.timedelta(days=1)).isoformat()}"
        if self.get(previous_id) is not None:
            lines += ["## Related", f"- [[{previous_id}]]", ""]
        body = "\n".join(lines).rstrip() + "\n"
        self.capture(
            body,
            note_id=note_id,
            note_type="daily",
            classification="Confidential",
            reason="brain-nightly daily-note fold",
        )
        return {"created": True, "id": note_id}

    def _recommendations_aging_fold(self, today: Any) -> dict[str, Any]:
        """Surface each aged open recommendation into hot.md exactly once."""
        open_path = config.recommendations_open_path(self.vault)
        if not open_path.exists():
            return {"scanned": 0, "surfaced": 0, "appended_to_hot": 0}
        entries = maintenance.parse_recommendation_lines(
            open_path.read_text(encoding="utf-8")
        )
        updated, newly = maintenance.recommendations_aging_scan(entries, today)
        appended = 0
        for entry in newly:
            if self._append_hot_once(
                f"rec:{entry.get('id')}",
                maintenance.render_recommendation_hot_entry(entry, today),
            ):
                appended += 1
        if newly:
            open_path.write_text(
                maintenance.render_recommendation_lines(updated), encoding="utf-8"
            )
        return {
            "scanned": len(entries),
            "surfaced": len(newly),
            "appended_to_hot": appended,
        }

    def publish_daily_fold(self, run: MaintenanceRun) -> None:
        """Reconcile fold mutations, publish the snapshot, and render daily artifacts."""
        synced = self.sync(drain=False, publish=True)
        run.results["sync_publish"] = {
            key: synced.get(key) for key in ("added", "updated", "deleted")
        }
        snapshot = synced.get("snapshot")
        if snapshot:
            run.auto_fixed.append(
                maintenance.auto_fixed_item(
                    "snapshot",
                    str(snapshot.get("snapshot_db", "")),
                    f"published snapshot gen {snapshot.get('generation')}",
                )
            )
        self._brief_fold(run)
        recommendation_result = self._recommendations_aging_fold(run.date)
        run.results["recommendations_aging"] = recommendation_result
        if recommendation_result.get("surfaced"):
            run.auto_fixed.append(
                maintenance.auto_fixed_item(
                    "recommendations-aging",
                    str(config.recommendations_open_path(self.vault)),
                    f"surfaced {recommendation_result['surfaced']} aged "
                    "recommendation(s) into hot.md",
                )
            )
        self._optional_daily_note_fold(run)
        run.mark("daily", True)

    def _brief_fold(self, run: MaintenanceRun) -> None:
        """Render the structured and HTML morning brief independently."""
        try:
            run.results["brief"] = self.brief(drain=False)
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    "could not generate the morning brief",
                    f"{type(exc).__name__}: {exc}",
                    "re-run after the underlying error is fixed",
                )
            )
        try:
            run.results["brief_html"] = self.brief_html(drain=False, today=run.date)
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    "could not write the HTML morning brief",
                    f"{type(exc).__name__}: {exc}",
                    "re-run after the underlying error is fixed",
                )
            )

    def _optional_daily_note_fold(self, run: MaintenanceRun) -> None:
        """Create a signed daily note only when BRAIN_DAILY_NOTE opts in."""
        try:
            result = (
                self._daily_note_fold(run.date, run.results.get("brief"))
                if os.environ.get("BRAIN_DAILY_NOTE")
                else {"created": False, "skipped": "BRAIN_DAILY_NOTE unset"}
            )
            run.results["daily_note"] = result
            if result.get("created"):
                run.auto_fixed.append(
                    maintenance.auto_fixed_item(
                        "daily-note",
                        str(self.vault),
                        f"created daily note {result['id']}",
                    )
                )
        except Exception as exc:
            run.blocked.append(
                maintenance.blocked_item(
                    "could not create today's daily note",
                    f"{type(exc).__name__}: {exc}",
                    "create it manually with tools/brain_daily.py",
                )
            )

"""Carry shared state for one locked maintenance run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import maintenance


@dataclass
class MaintenanceRun:
    """Explicit state passed between independently testable maintenance folds."""

    core: Any
    dry_run: bool
    date: Any
    state: dict[str, Any]
    branches: list[str]
    min_score: float
    near_dup_k: int
    graphify_runner: Any = None
    golden_runner: Any = None
    results: dict[str, Any] = field(default_factory=dict)
    auto_fixed: list[dict[str, Any]] = field(default_factory=list)
    action_required: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)

    def mark(self, branch: str, ok: bool, error: str | None = None) -> None:
        """Persist one branch attempt without conflating failure and busy skips."""
        if self.dry_run:
            return
        previous = (
            self.state.get(branch) if isinstance(self.state.get(branch), dict) else {}
        )
        entry = dict(previous)
        entry["last_attempt"] = self.date.isoformat()
        if ok:
            entry["last_run"] = self.date.isoformat()
            entry["status"] = "ok"
            entry["failed"] = False
            entry["consecutive_failures"] = 0
            entry.pop("error", None)
            entry["consecutive_skips"] = 0
            entry.pop("writer_busy_since", None)
            entry.pop("writer_busy_holder", None)
        else:
            entry["status"] = "failed"
            entry["failed"] = True
            entry["consecutive_failures"] = (
                int(previous.get("consecutive_failures", 0)) + 1
            )
            entry["error"] = error
        self.state[branch] = entry

    def outcomes(self) -> dict[str, Any]:
        """Build the canonical outcome buckets from current fold state."""
        return maintenance.build_outcomes(
            self.auto_fixed, self.action_required, self.blocked
        )

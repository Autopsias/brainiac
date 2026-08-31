"""What a leak scan RETURNS, and nothing else.

Its own module so the per-class detectors (`cowork_leak_notes`,
`cowork_leak_databases`) and the walk that drives them
(`cowork_leak_scan`) can all name the type without importing each other in a
cycle. One dataclass; no logic, no dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Artifact:
    """One artefact inside a workspace from which a note body is recoverable."""

    # vault_tree | snapshot | derived_index | staged_original | escaping_symlink
    kind: str
    path: Path
    detail: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return f"{self.kind}: {self.path} ({self.detail})"

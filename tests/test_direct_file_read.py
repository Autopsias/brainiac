"""Consensus hardening: the classification filter is NOT containment.

Proves, on each supported surface, the security boundary:
  1. The cooperative `brain` path WITHHOLDS restricted/MNPI (egress decision).
  2. A direct file read of the FULL vault CAN see restricted content — so the
     filter is not containment (this documents the threat, not a regression).
  3. The REAL control — a projected workspace (brain.projection) — physically
     EXCLUDES restricted/MNPI, so a direct file read on THAT surface cannot
     surface them. This is option (a) from the hardening note.
"""
from __future__ import annotations

from pathlib import Path

from brain.core import BrainCore
from brain.index import BrainIndex
from brain.projection import project_workspace
from brain.vectors import get_backend

SENSITIVE = ("restricted-deal", "mnpi-merger", "confidential-pricing", "unlabelled")


def _read_all_text(root: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in root.rglob("*.md"))


def test_cli_path_withholds_but_files_on_disk_expose(sample_vault, tmp_path):
    # (1) cooperative path withholds
    idx = BrainIndex(db_path=tmp_path / "i.sqlite", backend=get_backend("brute-force"))
    core = BrainCore(vault=sample_vault, index=idx)
    core.rebuild()
    # core.search is intentionally UNFILTERED (in-process bypasses the filter)
    in_process_ids = {h.id for h in core.search("deal merger pricing", k=20)}
    # (2) direct file read of the FULL vault exposes the secrets -> NOT containment
    raw = _read_all_text(sample_vault)
    assert "secret Meridian counterparty" in raw          # restricted leaked on disk
    assert "material non-public merger" in raw             # MNPI leaked on disk
    # in-process core sees them too (documents the bypass)
    assert "restricted-deal" in in_process_ids or "mnpi-merger" in in_process_ids


def test_projection_excludes_sensitive_on_disk(sample_vault, tmp_path):
    # (3) the REAL control: projected workspace physically omits sensitive files.
    dest = tmp_path / "vm-workspace"
    res = project_workspace(sample_vault, dest, max_tier="Internal")
    assert res.copied >= 1 and res.excluded >= 1
    projected = _read_all_text(dest)
    # direct file read on the projected surface cannot surface the secrets
    assert "secret Meridian counterparty" not in projected
    assert "material non-public merger" not in projected
    assert "Confidential pricing model" not in projected
    # and the unlabelled (default-deny) note is excluded too
    assert "must be default-denied" not in projected
    # the projected files that DO exist are all <= Internal
    from brain import classification as cls
    from brain.notes import scan_vault
    for note in scan_vault(dest):
        assert cls.rank(note.classification) <= cls.RANK["Internal"]


def test_projection_recreates_clean_each_run(sample_vault, tmp_path):
    dest = tmp_path / "vm-workspace"
    # First project at high cap (includes sensitive), then re-project low.
    project_workspace(sample_vault, dest, max_tier="MNPI")
    assert "secret Meridian counterparty" in _read_all_text(dest)
    project_workspace(sample_vault, dest, max_tier="Internal")
    # re-projection must not retain the previously-copied sensitive file
    assert "secret Meridian counterparty" not in _read_all_text(dest)

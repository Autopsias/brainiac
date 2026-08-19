"""Scan release artifacts for contamination."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from tools.publish_release import (
    PublishError,
    REPO_ROOT,
    _write_split_mirror,
)


def _clean_denylist(denylist: Path) -> Path:
    if not denylist.exists():
        raise PublishError(f"denylist not found: {denylist} (external, never committed — see runbook §5)")
    terms = [line for line in denylist.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if not terms:
        raise PublishError(f"denylist {denylist} has no usable terms after stripping comments/blanks")
    with tempfile.NamedTemporaryFile("w", suffix=".denylist", delete=False,
                                     encoding="utf-8") as handle:
        handle.write("\n".join(terms) + "\n")
        return Path(handle.name)


def _rg_scan(target: Path, clean_denylist: Path) -> list[str]:
    proc = subprocess.run(
        ["rg", "-Foiw", "--hidden", "--no-ignore", "-f", str(clean_denylist),
         str(target)],
        capture_output=True, text=True,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _grep_scan(target: Path, clean_denylist: Path) -> list[str]:
    proc = subprocess.run(
        ["grep", "-rFoiI", "-f", str(clean_denylist), str(target)],
        capture_output=True, text=True,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _scan_target(
    target: Path, clean_denylist: Path, scan: object
) -> tuple[int, int]:
    """Return direct and split-only contamination hits for one target."""
    scan_target = scan
    if not target.exists():
        return (0, 0)
    direct = len(scan_target(target, clean_denylist))
    with tempfile.TemporaryDirectory(prefix="contamination-split-") as tmp:
        mirror = _write_split_mirror(target, Path(tmp))
        split_total = len(scan_target(mirror, clean_denylist))
    return (direct, max(0, split_total - direct))


def step_contamination_scan(export_dir: Path, denylist: Path) -> dict:
    """Runbook §5 — HARD GATE on the export tree (no override); the companion
    ``_evidence/`` pass is informational-only (`_evidence` never ships) and is
    NOT gated — it carries known-benign synthetic-fixture/eval-golden-set
    terms that a hard gate would trip on every release. Redacted counts only,
    never the matched term or line (same posture as the runbook's own scan
    command)."""
    clean_denylist = _clean_denylist(denylist)
    one_pass = _rg_scan if shutil.which("rg") else _grep_scan
    try:
        export_direct, export_split = _scan_target(
            export_dir, clean_denylist, one_pass
        )
        evidence_direct, evidence_split = _scan_target(
            REPO_ROOT / "_evidence", clean_denylist, one_pass
        )
    finally:
        clean_denylist.unlink(missing_ok=True)
    return {
        "export_hit_count": export_direct + export_split,   # the hard gate
        "export_direct_hits": export_direct,
        "export_split_hits": export_split,
        "evidence_hit_count": evidence_direct + evidence_split,
    }

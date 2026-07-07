"""S09 (D-4/ADR-0001) — clean-room exporter contract.

Asserts the exported tree carries no _archive/_plans/_evidence/_workspace
paths and no gitignored content EXCEPT the named dist/cowork-skills/*.skill
allowlist exception (release-contract Cowork bundles, regenerated fresh).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_export_excludes_internal_dirs_and_allows_only_cowork_zips(tmp_path):
    output_dir = tmp_path / "export"
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "export_cleanroom.py"), "--output", str(output_dir)],
        check=True,
        cwd=REPO_ROOT,
    )

    manifest = json.loads((output_dir / "manifest.json").read_text())["files"]
    assert manifest, "manifest must not be empty"

    for excluded in ("_archive/", "_plans/", "_evidence/", "_workspace/"):
        assert not any(f.startswith(excluded) for f in manifest), f"{excluded} leaked into export"

    # Every gitignored path in the export must be a dist/cowork-skills/*.skill.
    tracked = set(
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files"], check=True, capture_output=True, text=True
        ).stdout.splitlines()
    )
    for f in manifest:
        if f not in tracked:
            assert f.startswith("dist/cowork-skills/") and f.endswith(".skill"), (
                f"untracked/gitignored path leaked into export outside the cowork-zip allowlist: {f}"
            )

    # The allowlist exception must actually be present (regenerated, not silently dropped).
    cowork_zips = [f for f in manifest if f.startswith("dist/cowork-skills/")]
    assert cowork_zips, "dist/cowork-skills/*.skill bundles must be regenerated into the export"

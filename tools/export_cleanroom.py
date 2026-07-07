#!/usr/bin/env python3
"""S09 (D-4/ADR-0001) — deterministic clean-room export.

ADR 0001 ("Publish via clean-room export, not history rewrite") requires the
public release to be produced from a single, explicit include/exclude policy
rather than a working-tree grep. This script IS that policy:

1. Enumerate every **git-tracked** file at HEAD (`git ls-files`) — this
   already excludes `.git` history and everything gitignored.
2. Drop any path under an explicit exclude prefix (`_archive/`, `_plans/`,
   `_evidence/`, `_workspace/`) as defense-in-depth, in case one is ever
   accidentally tracked.
3. Copy the survivors into ``--output``.
4. Regenerate ``dist/cowork-skills/*.skill`` via ``tools/package_clients.py``
   (dist/ is gitignored, but these zips are release-contract artifacts the
   Cowork install docs point at) and copy them in under ``dist/cowork-skills/``
   — the ONLY gitignored path allowed into the export.
5. Write ``manifest.json`` listing every exported file.

Usage:
    python3 tools/export_cleanroom.py --output /path/to/export-dir
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

EXCLUDE_PREFIXES = ("_archive/", "_plans/", "_evidence/", "_workspace/")

# The one gitignored allowlist exception: release-contract Cowork bundles,
# regenerated fresh at export time rather than shipped stale.
COWORK_SKILL_GLOB = "dist/cowork-skills/*.skill"


def tracked_files(repo_root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    paths = [p for p in out.decode("utf-8").split("\0") if p]
    return [p for p in paths if not p.startswith(EXCLUDE_PREFIXES)]


def build_cowork_zips(repo_root: Path) -> list[Path]:
    subprocess.run(
        [sys.executable, str(repo_root / "tools" / "package_clients.py")],
        check=True,
        cwd=repo_root,
    )
    cowork_dir = repo_root / "dist" / "cowork-skills"
    return sorted(cowork_dir.glob("*.skill"))


def export(repo_root: Path, output_dir: Path) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[str] = []

    for rel in tracked_files(repo_root):
        src = repo_root / rel
        dst = output_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        manifest.append(rel)

    for zip_path in build_cowork_zips(repo_root):
        rel = f"dist/cowork-skills/{zip_path.name}"
        dst = output_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(zip_path, dst)
        manifest.append(rel)

    assert_exported_version_stamp(output_dir)

    manifest.sort()
    (output_dir / "manifest.json").write_text(
        json.dumps({"files": manifest, "count": len(manifest)}, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def assert_exported_version_stamp(output_dir: Path) -> str:
    """ADR-0005 Ruling 1 export gate: the EXPORTED tree must carry a committed
    src/brain/_version.py whose stamp equals the exported pyproject version.
    Checked against the export, never only the working tree — a stamp that
    exists only dev-side (untracked) or went stale must stop the release here,
    otherwise the shipped zero-install VM reports 0.0.0+unknown (or worse, a
    confidently wrong version)."""
    pyproject = output_dir / "pyproject.toml"
    stamp = output_dir / "src" / "brain" / "_version.py"
    pv = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"))
    if not pv:
        raise SystemExit(f'FAIL: exported {pyproject} has no version = "..."')
    if not stamp.exists():
        raise SystemExit(
            "FAIL: exported tree is missing src/brain/_version.py — the version stamp "
            "is not git-tracked, so the shipped VM would report 0.0.0+unknown (ADR-0005 Ruling 1)"
        )
    sv = re.search(r'(?m)^__version__ = "([^"]+)"$', stamp.read_text(encoding="utf-8"))
    if not sv or sv.group(1) != pv.group(1):
        raise SystemExit(
            f"FAIL: exported version stamp {sv.group(1) if sv else None!r} != exported "
            f"pyproject version {pv.group(1)!r} — stale committed stamp (ADR-0005 Ruling 1)"
        )
    return pv.group(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="destination directory for the export tree")
    parser.add_argument("--repo-root", default=str(REPO_ROOT), help="repo root to export from (default: this repo)")
    args = parser.parse_args()

    manifest = export(Path(args.repo_root).resolve(), Path(args.output).resolve())
    print(f"Exported {len(manifest)} files to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
4. Write ``manifest.json`` listing every exported file.

NOTHING gitignored is exported. Until 0.19.10 this script also regenerated
``dist/cowork-skills/*.skill`` and copied them in as a deliberate "release
contract" exception — but they never actually reached the public repo, whose
own ``.gitignore`` carries ``dist/``, so ``git add -A`` dropped them silently
on all twelve releases. Nobody missed them: ``tools/cowork_workspace_install.sh``
rebuilds the bundles from source on every stage (deliberately, not
only-if-absent), and both the packager and the SKILL.md sources ship. Shipping
prebuilt zips would only reintroduce the stale-bundle class of bug that
v0.10.5 had to fix.

Usage:
    python3 tools/export_cleanroom.py --output /path/to/export-dir
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

EXCLUDE_PREFIXES = (
    "_archive/", "_plans/", "_evidence/", "_workspace/",
    # Internal-only, corpus-derived (excluded 2026-07-12): the test suite and
    # the eval golden set were built USING the owner's real vault as example
    # data, so they carry vault-shaped content that is not needed publicly and
    # is a standing contamination risk. Tests aren't needed to run the package;
    # the golden set is the owner's private vault. The generic eval harness
    # (harness/gate/stats/capture_run/path_normalize) stays — it's clean.
    "tests/",
    "eval/build_golden_set.py",
    # One-time, owner-specific corpus-migration scripts + doc (excluded
    # 2026-07-12, owner decision): they map the owner's real Obsidian vault
    # taxonomy and are not general-purpose. Not useful
    # publicly, and they carry vault-shaped content.
    "docs/corpus-migration.md",
    "tools/migrate_corpus.py",
    "tools/apply_live_migration.py",
    # Owner-cutover artifacts carrying real vault provenance (excluded
    # 2026-07-12, owner decision): a corpus-cutover checklist and an ADR whose
    # provenance table lists real vault file paths + content hashes. Kept in
    # the private repo as the internal record; not shipped publicly.
    "docs/dependency-inventory.md",
    "docs/adr/0003-parity-architecture.md",
)

# Suffix excludes (fixed 2026-07-12): session evidence artifacts carry raw
# operational content (real names/counterparties) and are internal-only — the
# `_evidence/` dir is already prefix-excluded, but sibling `*-evidence.md`
# files landed under `docs/operations/` (tracked by build sessions) and leaked
# real terms into the export tree. Any `-evidence.md` is an internal artifact;
# never ship it. (Root-cause fix accompanies scrubbing the tracked files.)
EXCLUDE_SUFFIXES = ("-evidence.md",)

def tracked_files(repo_root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    paths = [p for p in out.decode("utf-8").split("\0") if p]
    return [p for p in paths
            if not p.startswith(EXCLUDE_PREFIXES)
            and not p.endswith(EXCLUDE_SUFFIXES)]


def export(repo_root: Path, output_dir: Path) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[str] = []

    for rel in tracked_files(repo_root):
        src = repo_root / rel
        dst = output_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
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

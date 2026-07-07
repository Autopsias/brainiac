#!/usr/bin/env python3
"""HARDENED:claude-f4 — files-in=rows-out disposition/count reconciliation
against the reference vault, run BEFORE a fresh capture may be recorded as
the schema-3 baseline. Mirrors src/brain/notes.py::scan_vault's own walk
predicate exactly (top-level inbox/, .brain/, raw/originals/, backlinks.md
excluded) plus load_note's frontmatter-required skip, so "mismatch" means a
REAL indexing gap, not an expected exclusion.

Usage:
    .venv-embed/bin/python3 scripts/eval-vault-reconcile.py \
        --vault "$BRAIN_REFERENCE_VAULT" --indexed-count 2254
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True)
    ap.add_argument("--indexed-count", type=int, required=True,
                     help="notes count reported by core.rebuild()/status()")
    args = ap.parse_args()

    from brain import frontmatter

    vault = Path(args.vault).resolve()
    excluded_inbox = excluded_raw_originals = excluded_backlinks = 0
    skipped_no_frontmatter = skipped_unreadable = 0
    indexed = 0
    total_md = 0

    for p in sorted(vault.rglob("*.md")):
        total_md += 1
        sp = p.as_posix()
        if "/.brain/" in sp:
            continue  # not a candidate at all (runtime cache, never a source file)
        rel_parts = p.relative_to(vault).parts
        if rel_parts and rel_parts[0] == "inbox":
            excluded_inbox += 1
            continue
        if rel_parts[:2] == ("raw", "originals"):
            excluded_raw_originals += 1
            continue
        if p.name == "backlinks.md":
            excluded_backlinks += 1
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            skipped_unreadable += 1
            continue
        meta, _ = frontmatter.parse_text(text)
        if not meta:
            skipped_no_frontmatter += 1
            continue
        indexed += 1

    accounted = (indexed + excluded_inbox + excluded_raw_originals
                 + excluded_backlinks + skipped_no_frontmatter + skipped_unreadable)

    print(f"vault: {vault}")
    print(f"total *.md files (rglob, incl. .brain/): {total_md}")
    print(f"  excluded (top-level inbox/):      {excluded_inbox}")
    print(f"  excluded (raw/originals/):        {excluded_raw_originals}")
    print(f"  excluded (backlinks.md):          {excluded_backlinks}")
    print(f"  skipped (unreadable):             {skipped_unreadable}")
    print(f"  skipped (no frontmatter):         {skipped_no_frontmatter}")
    print(f"  candidate-indexable (this scan):  {indexed}")
    print(f"  accounted-for total:              {accounted}")
    print(f"reported indexed count (--indexed-count): {args.indexed_count}")

    ok = indexed == args.indexed_count
    print(f"RECONCILE: {'OK' if ok else 'MISMATCH'} "
          f"(candidate-indexable={indexed} vs reported={args.indexed_count})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

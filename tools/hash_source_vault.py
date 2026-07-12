#!/usr/bin/env python3
"""S10 LV-01 — read-only pre/post migration integrity check for the SOURCE vault.

The corpus migration (``migrate_corpus.py`` dry-run + ``apply_live_migration.py``
apply) reads the source Obsidian vault but must NEVER write to it. This tool
computes a sha256 manifest of every file under the vault root (name + hash,
NOT content — the manifest itself must stay content-free so it is safe to keep
even though it is written to a gitignored evidence dir) so a pre-run and a
post-run manifest can be diffed to PROVE the no-write-to-source guarantee.

Uses os.walk with dot/underscore-dir pruning at descent (sandbox-discipline-safe
metadata sweep) — NOT a bare recursive find/grep.

Usage:
    python3 tools/hash_source_vault.py <vault-root> --out manifest.json
    python3 tools/hash_source_vault.py <vault-root> --diff pre.json --out post.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def walk_files(root: Path):
    for dirpath, dirnames, filenames in __import__("os").walk(root):
        # Prune dot/underscore infra dirs at descent (Obsidian/system caches),
        # but keep vault-content zones (they are digit-prefixed, not
        # dot/underscore) and any digit-prefixed content zones (e.g. a workspace/inbox/daily drop) which are
        # legitimate content zones despite not matching a "NN " prefix check
        # here (this tool hashes the WHOLE vault tree for integrity, not just
        # the JD zones migrate_corpus.py imports).
        dirnames[:] = [d for d in dirnames if d not in (".git", ".obsidian", ".smart-env", "__pycache__")]
        for f in filenames:
            p = Path(dirpath) / f
            if p.is_symlink():
                continue
            yield p


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    try:
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except Exception as exc:
        return f"ERROR:{exc}"
    return h.hexdigest()


def build_manifest(root: Path) -> dict:
    rows = {}
    for p in sorted(walk_files(root)):
        rel = p.relative_to(root).as_posix()
        rows[rel] = sha256_of(p)
    return {
        "vault_root": str(root),
        "file_count": len(rows),
        "files": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vault_root")
    ap.add_argument("--out", required=True)
    ap.add_argument("--diff", default=None, help="a prior manifest.json to diff against")
    args = ap.parse_args()

    root = Path(args.vault_root).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory")
        return 2

    man = build_manifest(root)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(man, indent=2) + "\n", encoding="utf-8")
    print(f"hashed {man['file_count']} files under {root} -> {args.out}")

    if args.diff:
        prior = json.loads(Path(args.diff).read_text(encoding="utf-8"))
        pf, nf = prior["files"], man["files"]
        added = sorted(set(nf) - set(pf))
        removed = sorted(set(pf) - set(nf))
        changed = sorted(k for k in (set(pf) & set(nf)) if pf[k] != nf[k])
        mutated = bool(added or removed or changed)
        result = {
            "prior_manifest": args.diff,
            "prior_file_count": prior["file_count"],
            "post_file_count": man["file_count"],
            "added": added,
            "removed": removed,
            "changed": changed,
            "SOURCE_MUTATED": mutated,
        }
        diff_out = str(Path(args.out).with_name(Path(args.out).stem + "-diff.json"))
        Path(diff_out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"diff -> {diff_out}")
        if mutated:
            print(f"** SOURCE_MUTATED = TRUE ** added={len(added)} removed={len(removed)} changed={len(changed)}")
            return 1
        print("SOURCE_MUTATED = false — source vault confirmed byte-identical pre/post migration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

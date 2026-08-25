#!/usr/bin/env python3
"""Bind a content hash to chain paths that never carried one — witnessed only.

WHY THIS EXISTS. `AuditChain.content_drift` can only speak for a path whose
write entry carried a `content_sha256`. Entries written before INT-02 carry
none, so an edit to those notes is undetectable. Measured on the live
reference vault 2026-08-24: 1636 of 3212 live chain paths (51%) were unbound.

WHY IT IS NOT A BLANKET BACKFILL. Signing today's bytes as the baseline would
bless every edit already made. So this binds ONLY a note that a SECOND,
INDEPENDENT witness verifies: the `sha256:` field written into the note's own
frontmatter at capture time, which the audit chain never touched. If the body
still hashes to that field, the body is the capture body and binding it
asserts nothing new.

STATED LIMIT. The capture hash covers the BODY. Frontmatter is outside it, so
a frontmatter edit — supersession keys, a tier raise, or a hand repair like
F10 (2026-08-23) — is invisible to the witness, and binding seals whatever
frontmatter is on disk now. That is accepted: frontmatter changes are the
normal output of the version-link and supersession folds, while a body edit to
an immutable `raw/` source is not supposed to happen at all.

The verb is `bind`, never `write`. Nothing was written; only the chain's
knowledge of these bytes is new, and the log says so.

Dry by default. `--apply` signs and appends.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

#: The capture-time hash, quoted or bare. An earlier probe of this same field
#: captured the surrounding quotes and reported 158 false "changed bodies";
#: the quotes are optional in the corpus, so they are optional here.
FIELD = re.compile(r'^sha256:\s*["\']?([0-9a-fA-F]{64})["\']?\s*$', re.M)


def _witness_ok(vault: Path, note: Path) -> bool:
    """Does the note's own capture-time hash still describe its body?

    Four body conventions and the archived original are all in live use across
    the ingest lanes; a single convention scores real notes as damaged.
    """
    text = note.read_text(encoding="utf-8", errors="replace")
    m = FIELD.search(text)
    if not m or not text.startswith("---\n"):
        return False
    want = m.group(1).lower()
    try:
        body = text[text.index("\n---\n", 3) + 5:]
    except ValueError:
        return False
    for cand in (body.lstrip("\n"), body.strip(), body, text):
        if hashlib.sha256(cand.encode("utf-8")).hexdigest() == want:
            return True
    originals = vault / "raw" / "originals" / note.stem
    if originals.is_dir():
        for f in originals.iterdir():
            if f.is_file() and hashlib.sha256(f.read_bytes()).hexdigest() == want:
                return True
    return False


def unbound_paths(vault: Path, chain) -> list[str]:
    """Live chain paths carrying no content hash, under their RELATIVE key.

    An absolute key is normalized here so the three historical entries written
    before the absolute-path fix (6bb15be) are not re-bound as if unbound.
    """
    prefix = str(vault) + "/"
    live: set[str] = set()
    covered: set[str] = set()
    for line in chain._lines():
        s = line.strip()
        if not chain._is_entry(s):
            continue
        try:
            obj = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            continue
        path, verb = obj.get("path"), obj.get("verb")
        if not isinstance(path, str):
            continue
        rel = path[len(prefix):] if path.startswith(prefix) else path
        if verb in ("write", "ingest", "bind"):
            live.add(rel)
            if isinstance(obj.get("content_sha256"), str):
                covered.add(rel)
        elif verb in ("delete", "write_failed"):
            live.discard(rel)
            covered.discard(rel)
    return sorted(live - covered)


def main(argv: list[str] | None = None) -> int:
    from brain import audit as audit_mod
    from brain import config
    from brain.audit_chain import AuditChain

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("vault", type=Path)
    ap.add_argument("--apply", action="store_true",
                    help="sign and append; without it nothing is written")
    args = ap.parse_args(argv)

    vault = args.vault.resolve()
    chain = AuditChain(config.index_dir(vault) / "audit_chain.jsonl")
    if not chain.log_path.is_file():
        print(f"no audit chain at {chain.log_path}", file=sys.stderr)
        return 2

    todo, skipped = [], {"gone": 0, "no_witness": 0}
    for rel in unbound_paths(vault, chain):
        note = vault / rel
        if not note.is_file():
            skipped["gone"] += 1
            continue
        if not _witness_ok(vault, note):
            skipped["no_witness"] += 1
            continue
        # Hash EXACTLY as content_drift will read it back, or every entry this
        # writes reports as drift on the next doctor run.
        todo.append((rel, audit_mod._sha256(note.read_text(encoding="utf-8"))))

    print(f"bindable: {len(todo)}   skipped: {skipped['gone']} gone, "
          f"{skipped['no_witness']} without a capture-time witness")
    if not args.apply:
        print("dry run — nothing written. Re-run with --apply.")
        return 0

    for i, (rel, sha) in enumerate(todo, 1):
        chain.append("bind", rel,
                     "content hash bound from the note's own capture-time "
                     "sha256; bytes unchanged, only the chain's knowledge of "
                     "them is new (INT-02 coverage backfill 2026-08-24)",
                     content_sha256=sha)
        if i % 200 == 0:
            print(f"  {i}/{len(todo)}")
    print(f"appended {len(todo)} bind entry(ies)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

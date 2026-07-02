#!/usr/bin/env python3
"""Normalise each system's returned doc id to the CANONICAL SOURCE PATH.

The golden-set qrels key on the real example-vault relative path (the canonical
key). The two systems return paths in DIFFERENT namespaces:

  * current  (Smart Connections) returns the real vault path AS-IS  -> identity.
  * new      (brain) returns its own vault path (``vault/raw/<slug>.md`` or
             ``vault/brain/<bucket>/<slug>.md``) -> mapped back to the source
             via the materialisation sidecar produced when the eval corpus was
             built (brain_path -> source_path).

For TEMPORAL queries the canonical doc id is ``<source_path>#<version_state>``
so a retriever that surfaces the WRONG version does not score green
(HARDENED:codex). ``resolve_version`` reads the note's frontmatter
(``is_latest_version`` / ``document_date``); when absent it falls back to the
date embedded in the path and records ``by-path-date`` so the scorecard can
flag the resolution method.
"""
from __future__ import annotations

import re
from pathlib import Path

_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def normalize(raw_path: str, mapping: dict[str, str] | None = None) -> str:
    p = raw_path.strip()
    if p.startswith("./"):
        p = p[2:]
    if mapping and p in mapping:
        return mapping[p]
    return p


def resolve_version(source_path: str, vault_root: str | None) -> tuple[str, str]:
    """Return (version_state, method) for a note. version_state in
    {'current','superseded'}; method in {'frontmatter','by-path-date','default'}."""
    if vault_root:
        fp = Path(vault_root) / source_path
        if fp.exists():
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                text = ""
            if text.startswith("---"):
                fm = text.split("---", 2)[1] if text.count("---") >= 2 else ""
                m = re.search(r"^\s*is_latest_version\s*:\s*(true|false)\s*$", fm, re.I | re.M)
                if m:
                    return ("current" if m.group(1).lower() == "true" else "superseded",
                            "frontmatter")
    return ("current", "by-path-date" if _DATE.search(source_path) else "default")


def path_date(source_path: str) -> str | None:
    m = _DATE.search(source_path)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


if __name__ == "__main__":
    import sys
    print(normalize(sys.argv[1] if len(sys.argv) > 1 else "vault/raw/x.md"))

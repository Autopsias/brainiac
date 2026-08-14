#!/usr/bin/env python3
"""Normalise each system's returned doc id to the CANONICAL SOURCE PATH.

The golden-set qrels key on the real owner-vault relative path (the canonical
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

import json
import re
from pathlib import Path

_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

# --------------------------------------------------------------------------
# HYG-02 — the score-time CODENAME map (FOLLOWUPS item 3)
# --------------------------------------------------------------------------
# The private fixtures name entities by an anonymization scheme that drifts
# from the corpus (a note renamed in the vault, a codename retired). When it
# drifts, every qrel path carrying that name stops resolving and the score
# silently understates quality — that is what produced the 0.000 Spanish
# artifact. This is deliberately NOT the existing 72-entry path map
# (``ne-upgrade-established-path-map.json``, applied at CAPTURE time to map a
# brain path back to its source path): this reconciles the ANONYMIZATION
# SCHEME itself, so ONE entry (``Northwind`` -> the corpus name) repairs every
# path containing it instead of thirty path rewrites.
#
# ponytail: plain longest-key-first string substitution. No regex, no aliases
# per zone. Upgrade to per-field rules only if a codename ever needs to mean
# different things in different path segments.
CODENAME_MAP_PATH = Path(__file__).with_name("codename-map.json")


def load_codename_map(path: str | Path | None = None) -> dict[str, str]:
    """Load the gitignored fixture-codename -> corpus-name map.

    An ABSENT or EMPTY map is identity ({}), which is exactly today's behaviour
    — the map may never be required to score, and ``--codename-map /dev/null``
    is the way to turn it off deliberately. A map that is PRESENT and non-empty
    but malformed raises: silently degrading to identity is the same silent
    understatement it exists to prevent. Keys beginning with ``_`` are comments.
    """
    p = Path(path) if path is not None else CODENAME_MAP_PATH
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    if not text.strip():
        return {}
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: codename map must be a JSON object of "
                         f"fixture-name -> corpus-name, got {type(raw).__name__}")
    out = {}
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        if not isinstance(v, str) or not k:
            raise ValueError(f"{p}: codename map entry {k!r} must map to a string")
        out[k] = v
    return out


def apply_codenames(path: str, cmap: dict[str, str]) -> str:
    """Rewrite a FIXTURE doc key into the current corpus namespace.

    Longest key first, so a longer codename is never shadowed by a shorter one
    that is its prefix. Each key is applied once over the already-rewritten
    string; a map whose replacements feed each other is a map bug, not a
    feature to support.
    """
    for k in sorted(cmap, key=len, reverse=True):
        if k in path:
            path = path.replace(k, cmap[k])
    return path


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

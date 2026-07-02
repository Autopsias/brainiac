#!/usr/bin/env python3
"""EF-03 (s02) — shared query-scope helper for the PT golden set (ef-02).

The golden set (`_evidence/s01/pt-golden-set.json`, 102 queries) is split into
four folds by `_evidence/s01/pt-split.json` (H34/H36/H37): train, dev,
adoption-validation, held-out. **held-out is LABEL-FIRST-barriered** — it may
be scored exactly ONCE, by s11b, across every metric family. Every earlier
session (including this one, EF-03/s02) must score only the non-held-out
folds (train + dev + adoption-validation).

This module is the single place that enforces the barrier so no capture
script can accidentally include a held-out query id — import `nonheldout_qids`
or `load_scope`, never re-derive the fold union by hand.
"""
from __future__ import annotations

import json
from pathlib import Path

NONHELDOUT_FOLDS = ("train", "dev", "adoption-validation")
HELDOUT_FOLD = "held-out"


def nonheldout_qids(split_path: str | Path) -> set[str]:
    """Union of train + dev + adoption-validation query ids (H37 barrier)."""
    split = json.loads(Path(split_path).read_text(encoding="utf-8"))
    folds = split["folds"]
    held = set(folds[HELDOUT_FOLD])
    scope: set[str] = set()
    for name in NONHELDOUT_FOLDS:
        scope |= set(folds[name])
    overlap = scope & held
    if overlap:
        raise AssertionError(
            f"H37 barrier violated: {sorted(overlap)} appear in both a "
            "non-held-out fold and held-out — refusing to build scope."
        )
    return scope


def load_scope(golden_path: str | Path, split_path: str | Path) -> dict:
    """Return a golden-set dict (same schema) with `queries` filtered to the
    non-held-out scope. Raises if any held-out id would leak through."""
    golden = json.loads(Path(golden_path).read_text(encoding="utf-8"))
    scope = nonheldout_qids(split_path)
    held = set(json.loads(Path(split_path).read_text(encoding="utf-8"))["folds"][HELDOUT_FOLD])
    filtered = [q for q in golden["queries"] if q["id"] in scope]
    leaked = [q["id"] for q in filtered if q["id"] in held]
    if leaked:
        raise AssertionError(f"H37 barrier violated: held-out ids leaked into scope: {leaked}")
    out = dict(golden)
    out["queries"] = filtered
    out["_scope"] = {
        "folds_included": list(NONHELDOUT_FOLDS),
        "fold_excluded": HELDOUT_FOLD,
        "n_scope": len(filtered),
        "n_total_golden": len(golden["queries"]),
        "barrier": "H37 LABEL-FIRST — held-out scored ONCE, by s11b, across all metric families",
    }
    return out


if __name__ == "__main__":
    import sys

    g = load_scope(sys.argv[1], sys.argv[2])
    print(f"scope: {g['_scope']['n_scope']}/{g['_scope']['n_total_golden']} queries "
          f"(excludes {HELDOUT_FOLD})")

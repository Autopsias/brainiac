#!/usr/bin/env python3
"""ENF-03 — measure the cross-tier detector's COVERAGE against an exhaustive
all-pairs scan, and probe it with a known positive and a known negative.

    export BRAIN_VAULT=/path/to/your/vault
    .venv/bin/python tools/crosstier_coverage.py --json out.json

Why this file exists at all: `invariants.cross_tier_duplicates` screens
candidate pairs with a bottom-k sketch, because holding every document's
5-word-shingle set costs ~760 MB on the reference vault. A screen can LOSE
pairs. A coverage number that is asserted from the arithmetic of the screen
instead of measured against a scan that has no screen is exactly the
self-referential number s12 rejected — so this tool re-finds every pair the
dumb way, over all N*(N-1)/2 of them, and reports what the detector missed.

INDEPENDENCE BOUNDARY, stated so it can be argued with: this tool imports the
DEFINITION (`_ct_tokens`, `_ct_shingles`, `_jaccard`, the thresholds, the
population rules) and re-implements the SEARCH. The definition is what the two
must share or the comparison is meaningless; the search is the only part that
can drop a pair, and it is written twice.

Coverage is reported as a fraction with its denominator named, per s03's
pre-stated bar (>= 0.90, `_evidence/invariants/s03-twin-buckets.md` §5).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def vault_guard(vault: Path) -> int:
    """Assert we are pointed at the reference vault. The repo itself contains a
    `vault/` with 2 raw sources, and `$BRAIN_VAULT` unset resolves to it."""
    db = vault / ".brain/snapshot/index.snapshot.sqlite"
    if not db.exists():
        raise SystemExit(f"no snapshot at {db} — wrong vault?")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        n = conn.execute(
            "SELECT count(*) FROM notes WHERE path LIKE '%/raw/%'").fetchone()[0]
    finally:
        conn.close()
    if n <= 1000:
        raise SystemExit(f"WRONG VAULT: {n} raw sources at {vault}")
    return n


def brute_force(conn: Any) -> dict[str, Any]:
    """Every cross-tier near-duplicate pair, found with no screen of any kind."""
    from brain import invariants as inv
    from brain.classification import normalize as tier_of
    from brain.index import _family_min_body
    from brain.maintenance import _floor_bytes

    floor = _family_min_body()
    rows = conn.execute(
        "SELECT id, classification, zone, path, is_latest_version, body FROM notes"
    ).fetchall()

    docs = []
    population = too_short = subfloor = 0
    excluded: dict[str, int] = {}
    for nid, cls, zone, path, ilv, body in rows:
        reason = inv.link_coverage_exclusion(
            path=str(path or ""), zone=str(zone or ""), is_latest_version=ilv)
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
            if reason in inv.CROSS_TIER_SKIP_REASONS:
                continue
        population += 1
        toks = inv._ct_tokens(body or "")
        short = len(toks) < inv.CROSS_TIER_MIN_TOKENS
        below = _floor_bytes(body or "") < floor
        too_short += short
        subfloor += below
        if short or below:
            continue
        docs.append((str(nid), tier_of(cls), set(toks), inv._ct_shingles(toks)))

    conflicts, unclassified = [], []
    t0 = time.time()
    n = len(docs)
    for i in range(n):
        aid, atier, awords, ashing = docs[i]
        for j in range(i + 1, n):
            bid, btier, bwords, bshing = docs[j]
            if atier == btier:
                continue
            wj = inv._jaccard(awords, bwords)
            if wj < inv.CROSS_TIER_CANDIDATE:
                continue
            sj = inv._jaccard(ashing, bshing)
            rec = {"a": aid, "a_tier": atier, "b": bid, "b_tier": btier,
                   "shingle_jaccard": round(sj, 4), "word_jaccard": round(wj, 4)}
            (conflicts if sj >= inv.CROSS_TIER_SAME_DOC else unclassified).append(rec)
        if i % 400 == 0:
            print(f"  brute force {i}/{n}  {time.time() - t0:.0f}s", file=sys.stderr,
                  flush=True)
    return {
        "population": population, "comparable": n, "too_short": too_short,
        "subfloor": subfloor, "floor": floor, "excluded_by_reason": excluded,
        "conflicts": conflicts, "unclassified": unclassified,
        "seconds": round(time.time() - t0, 1),
    }


def key(rec: dict[str, Any]) -> tuple[str, str]:
    return tuple(sorted((rec["a"], rec["b"])))  # type: ignore[return-value]


def coverage(found: list[dict[str, Any]], truth: list[dict[str, Any]]) -> dict[str, Any]:
    f, t = {key(r) for r in found}, {key(r) for r in truth}
    missed = sorted(t - f)
    return {
        "detected": len(f), "truth": len(t), "hit": len(f & t),
        "coverage": round(len(f & t) / len(t), 4) if t else None,
        "missed": [list(m) for m in missed],
        "extra": [list(m) for m in sorted(f - t)],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", default=None, help="default: $BRAIN_VAULT")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args(argv)

    from brain import config
    from brain import invariants as inv

    vault = Path(os.path.expanduser(
        args.vault or os.environ.get("BRAIN_VAULT", ""))).resolve()
    n_raw = vault_guard(vault)
    db = config.index_dir(vault) / "index.sqlite"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    print(f"# vault ok: {n_raw} raw sources   index: {db}")

    t0 = time.time()
    det = inv.cross_tier_duplicates(conn, detail=True)
    det_seconds = round(time.time() - t0, 1)
    truth = brute_force(conn)

    conf = coverage(det["conflicts"], truth["conflicts"])
    cand = coverage(det["unclassified"], truth["unclassified"])
    both_found = det["conflicts"] + det["unclassified"]
    both_truth = truth["conflicts"] + truth["unclassified"]
    allp = coverage(both_found, both_truth)

    out = {
        "vault": str(vault), "raw_sources": n_raw,
        "params": {"shingle": inv.CROSS_TIER_SHINGLE,
                   "min_tokens": inv.CROSS_TIER_MIN_TOKENS,
                   "same_doc": inv.CROSS_TIER_SAME_DOC,
                   "candidate": inv.CROSS_TIER_CANDIDATE,
                   "sketch": inv.CROSS_TIER_SKETCH,
                   "screen": inv.CROSS_TIER_SCREEN,
                   "floor_bytes": truth["floor"]},
        "detector": {k: v for k, v in det.items()
                     if k not in ("conflicts", "unclassified")},
        "detector_seconds": det_seconds,
        "brute_force": {k: v for k, v in truth.items()
                        if k not in ("conflicts", "unclassified")},
        "pair_coverage": {"conflicts": conf, "unclassified": cand, "all": allp},
        "truth_conflicts": truth["conflicts"],
        "truth_unclassified": truth["unclassified"],
    }
    if args.json:
        args.json.write_text(json.dumps(out, indent=1), encoding="utf-8")

    fp = det["comparable"] / det["population"] if det["population"] else 0
    print()
    print("THREE NUMBERS, kept separate")
    print(f"  1 detected conflicts        : {det['value']}")
    print(f"  2 unclassified candidates   : {det['candidates']}")
    print(f"  3 detector coverage (pairs) : {allp['coverage']}  "
          f"= {allp['hit']}/{allp['truth']} of the exhaustive scan's pairs")
    print(f"    - of which conflicts      : {conf['coverage']}  "
          f"({conf['hit']}/{conf['truth']})")
    print(f"    - of which unclassified   : {cand['coverage']}  "
          f"({cand['hit']}/{cand['truth']})")
    print(f"    fingerprintable fraction  : {fp:.4f}  "
          f"= {det['comparable']}/{det['population']} documents")
    print()
    print(f"  population {det['population']}  comparable {det['comparable']}  "
          f"too_short {det['too_short']}  sub-floor {det['subfloor']} "
          f"(floor {truth['floor']}B)")
    print(f"  excluded by design: {det['excluded_by_reason']}  "
          f"retained superseded: {det['retained_superseded']}")
    print(f"  detector {det_seconds}s   brute force {truth['seconds']}s "
          f"over {det['comparable'] * (det['comparable'] - 1) // 2} pairs")
    if allp["missed"]:
        print("\n  MISSED BY THE DETECTOR:")
        for m in allp["missed"]:
            print(f"    {m}")
    if allp["extra"]:
        print("\n  FOUND BY THE DETECTOR, NOT BY BRUTE FORCE (a bug either way):")
        for m in allp["extra"]:
            print(f"    {m}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

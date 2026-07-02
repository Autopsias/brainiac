#!/usr/bin/env python3
"""EM-01 (s06) — build the SCOPED candidate corpus for the BGE-M3 A/B.

H9 feasibility gate (measured, `_evidence/pt-bench/bge-m3-speed.json`):
BGE-M3 fp32 ONNX-CPU on this Apple M4 Pro runs ~441 ms/chunk warm — a full
96,568-chunk corpus rebuild would take ~11.8 hours, infeasible in-session and
almost certainly infeasible on the corporate CPU fleet as a routine rebuild.
So this script selects a SCOPED chunk universe sufficient to score the
adoption-validation split (s01 pt-split.json) fairly:

  * ALL chunks of every note that is a qrels-locked GOLD for an
    adoption-validation query (guarantees the dense leg CAN find every gold).
  * A stratified-by-zone random SAMPLE of additional "distractor" notes
    (chunks capped per note) so the dense candidate window has realistic
    competition, not just golds — proportional to the real corpus's zone mix.

Chunk rowids/text are read directly from the LIVE index's `chunks` table
(read-only) — so the scoped corpus is a real subset of the real corpus. The
budget defaults to ~3,500 chunks (~1,290 s / ~21.5 min at the measured warm
throughput, an acceptable in-session cost); override with --budget.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))
import path_normalize as pn  # noqa: E402


def _load(p: str) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--split", required=True)
    ap.add_argument("--qrels", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--index", required=True, help="path to the live index.sqlite (read-only)")
    ap.add_argument("--map", default=None)
    ap.add_argument("--budget", type=int, default=3500, help="target total scoped chunks")
    ap.add_argument("--cap-per-distractor-note", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20260702)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    split = _load(args.split)["folds"]
    qrels_all = _load(args.qrels)["qrels"]
    mapping = _load(args.map) if args.map else None

    av_qids = split["adoption-validation"]
    forbidden = set(split.get("held-out", []))
    assert not (set(av_qids) & forbidden), "H36 barrier: adoption-validation must not touch held-out"

    gold_paths: set[str] = set()
    for q in av_qids:
        for p, g in qrels_all.get(q, {}).items():
            if g > 0:
                gold_paths.add(p)

    vault_root = str(Path(args.vault).resolve())
    conn = sqlite3.connect(f"file:{args.index}?mode=ro", uri=True)
    notes = conn.execute("SELECT rowid, zone, path FROM notes").fetchall()

    canon_of: dict[int, str] = {}
    zone_of: dict[int, str] = {}
    for rid, _zone_col, p in notes:
        rel = os.path.relpath(p, vault_root) if os.path.isabs(p) else p
        canon = pn.normalize(rel, mapping) if mapping else rel
        canon_of[rid] = canon
        zone_of[rid] = canon.split("/")[0] if "/" in canon else canon

    canon_to_rowid = {c: r for r, c in canon_of.items()}
    gold_rowids = {canon_to_rowid[p] for p in gold_paths if p in canon_to_rowid}
    missing_gold = sorted(gold_paths - set(canon_to_rowid))
    print(f"gold notes for adoption-validation: {len(gold_paths)} "
          f"(resolved {len(gold_rowids)}, missing {len(missing_gold)})")
    if missing_gold:
        print("  MISSING:", missing_gold)

    # chunk counts per note (for budgeting + distractor sampling)
    chunk_counts = defaultdict(int)
    for rid, cnt in conn.execute("SELECT note_rowid, count(*) FROM chunks GROUP BY note_rowid"):
        chunk_counts[int(rid)] = cnt

    gold_chunk_total = sum(chunk_counts.get(r, 0) for r in gold_rowids)
    remaining_budget = max(0, args.budget - gold_chunk_total)
    print(f"gold chunk total: {gold_chunk_total}; remaining distractor budget: {remaining_budget}")

    # Stratify distractor pool by zone, proportional to corpus-wide zone mix.
    all_rowids = [r for r, *_ in notes]
    non_gold = [r for r in all_rowids if r not in gold_rowids and chunk_counts.get(r, 0) > 0]
    by_zone: dict[str, list[int]] = defaultdict(list)
    for r in non_gold:
        by_zone[zone_of.get(r, "?")].append(r)

    rng = random.Random(args.seed)
    for z in by_zone:
        rng.shuffle(by_zone[z])

    zone_sizes = {z: len(v) for z, v in by_zone.items()}
    total_pool = sum(zone_sizes.values())
    distractor_rowids: list[int] = []
    distractor_chunk_total = 0
    ptrs = {z: 0 for z in by_zone}
    # Round-robin proportional draw until budget exhausted or pool empty.
    order = sorted(by_zone, key=lambda z: -zone_sizes[z])
    while distractor_chunk_total < remaining_budget:
        progressed = False
        for z in order:
            if ptrs[z] >= len(by_zone[z]):
                continue
            r = by_zone[z][ptrs[z]]
            ptrs[z] += 1
            progressed = True
            take = min(chunk_counts.get(r, 0), args.cap_per_distractor_note)
            if take <= 0:
                continue
            distractor_rowids.append(r)
            distractor_chunk_total += take
            if distractor_chunk_total >= remaining_budget:
                break
        if not progressed:
            break

    scope_rowids = sorted(gold_rowids | set(distractor_rowids))
    per_note_cap = {r: (None if r in gold_rowids else args.cap_per_distractor_note) for r in scope_rowids}

    # Pull the actual chunk rows for the scoped notes.
    scoped_chunks = []
    for r in scope_rowids:
        cap = per_note_cap[r]
        rows = conn.execute(
            "SELECT rowid, text FROM chunks WHERE note_rowid=? ORDER BY rowid", (r,)
        ).fetchall()
        if cap is not None and len(rows) > cap:
            idxs = sorted(rng.sample(range(len(rows)), cap))
            rows = [rows[i] for i in idxs]
        for crid, text in rows:
            if text:
                scoped_chunks.append({"chunk_rowid": int(crid), "note_rowid": int(r), "text": text})

    zone_hist = defaultdict(int)
    for r in scope_rowids:
        zone_hist[zone_of.get(r, "?")] += 1

    out = {
        "session": "s06", "item": "em-01",
        "seed": args.seed,
        "budget_target_chunks": args.budget,
        "adoption_validation_n_queries": len(av_qids),
        "gold_notes": len(gold_rowids),
        "gold_chunks": gold_chunk_total,
        "distractor_notes": len(distractor_rowids),
        "distractor_chunks": distractor_chunk_total,
        "cap_per_distractor_note": args.cap_per_distractor_note,
        "total_scope_notes": len(scope_rowids),
        "total_scope_chunks": len(scoped_chunks),
        "full_corpus_notes": len(all_rowids),
        "full_corpus_chunks": sum(chunk_counts.values()),
        "coverage_pct_notes": round(100.0 * len(scope_rowids) / len(all_rowids), 2),
        "coverage_pct_chunks": round(100.0 * len(scoped_chunks) / sum(chunk_counts.values()), 2),
        "zone_histogram_scope": dict(sorted(zone_hist.items())),
        "zone_histogram_full_corpus": dict(sorted(zone_sizes.items())),
        "gold_note_paths": sorted(gold_paths),
        "missing_gold_note_paths": missing_gold,
        "scope_note_rowids": scope_rowids,
        "chunks": scoped_chunks,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = {k: v for k, v in out.items() if k not in ("chunks", "gold_note_paths", "scope_note_rowids")}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"wrote {args.out} ({len(scoped_chunks)} chunks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

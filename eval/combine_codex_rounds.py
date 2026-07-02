#!/usr/bin/env python3
"""Combine Codex round-1 (snippet-only) and round-2 (enriched-passages) verdicts.

Round 2 re-judged ONLY the pairs not locked by round-1 agreement (disagreement
or either-side unsure), with richer evidence from the same note — still blind
to Claude's labels. For those pairs the round-2 verdict SUPERSEDES round 1
(better evidence, same judge, same blindness). All other pairs keep round 1.

Usage:
  python3 eval/combine_codex_rounds.py \
    --round1 _evidence/s01/judgments_codex.json \
    --round2 _evidence/s01/_judgments_codex_r2_shard0.json [more shards...] \
    --out _evidence/s01/judgments_codex.json
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--round1", required=True)
    ap.add_argument("--round2", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    r1 = json.loads(Path(args.round1).read_text(encoding="utf-8"))
    r2: dict[tuple[str, str], str] = {}
    for f in args.round2:
        for row in json.loads(Path(f).read_text(encoding="utf-8")):
            r2[(row["qid"], row["note_id"])] = row["label"]

    n_override = 0
    out = []
    for row in r1:
        key = (row["qid"], row["note_id"])
        new = dict(row)
        if key in r2:
            new["label_round1"] = row["label"]
            new["label"] = r2[key]
            new["round"] = 2
            n_override += 1
        else:
            new["round"] = 1
        out.append(new)

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"rows={len(out)} round2-overrides={n_override} "
          f"final-dist={Counter(r['label'] for r in out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Dump a compact, human-readable view of all_candidates_snippets.json for the
Claude labeler to read in chunks, plus a stable (qid, idx) -> note_id index.

Writes:
  _evidence/s01/_compact_snippets.txt   — one block per query, candidates numbered
  _evidence/s01/_pair_index.json        — {"qid|idx": note_id} for label expansion
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("_evidence/s01/all_candidates_snippets.json")
    outtxt = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("_evidence/s01/_compact_snippets.txt")
    outidx = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("_evidence/s01/_pair_index.json")
    data = json.loads(src.read_text(encoding="utf-8"))
    lines = []
    idx_map = {}
    for q in data:
        qid = q["qid"]
        lines.append(f"\n===== {qid} | {q['lang']} | {q['qclass']}{' | ANCHOR' if q.get('anchor') else ''} =====")
        lines.append(f"QUERY: {q['query']}")
        for i, c in enumerate(q["candidates"]):
            idx_map[f"{qid}|{i}"] = c["note_id"]
            snip = " ".join(c["snippet"].split())
            if len(snip) > 600:
                snip = snip[:600] + "…"
            zone = c["note_id"].split("/", 1)[0]
            lines.append(f"  [{i}] ({zone}) {c['title']}")
            lines.append(f"      {snip}")
    outtxt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    outidx.write_text(json.dumps(idx_map, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"queries={len(data)} pairs={len(idx_map)} wrote {outtxt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

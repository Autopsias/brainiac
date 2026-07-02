#!/usr/bin/env python3
"""Convert any golden_set.json -> ranx qrels.json ({query_id: {doc_id: grade}}).

Temporal queries get version-stamped doc ids (``<path>#<version_state>``) so a
retriever surfacing the wrong version cannot score green (HARDENED:codex).

Usage: python3 eval/make_qrels.py --golden <golden.json> --out <qrels.json>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    qrels: dict[str, dict[str, int]] = {}
    for q in golden["queries"]:
        d: dict[str, int] = {}
        for r in q["qrels"]:
            key = r["path"]
            if q["stratum"] == "temporal" and "version_state" in r:
                key = f"{r['path']}#{r['version_state']}"
            d[key] = int(r["grade"])
        qrels[q["id"]] = d
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(qrels, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(qrels)} qrels -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

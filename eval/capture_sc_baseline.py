#!/usr/bin/env python3
"""FREEZE the current-system (Smart Connections) baseline into a committed ranx Run.

HARDENED:consensus — the current-SC baseline is captured ONCE into a
version-committed run file (qrels + run + SC index-state hash + date). The CI
gate compares NEW against THIS FROZEN FILE, never a live SC MCP call.

The Smart Connections MCP can only be driven by the agent (it is an MCP tool,
not a Python API). So the agent collects the SC `lookup` ranking for each query
into a small JSON (``--sc-results {query_id: [ranked source paths]}``) plus the
SC ``stats`` output (``--sc-stats``), and THIS script freezes them into the
committed run file with an index-state hash + capture date.

FAIL-LOUD (HARDENED:consensus): if the SC results or stats are missing/empty,
this script EXITS NON-ZERO and writes nothing — a re-baseline must never
silently skip when SC is unavailable.

Usage:
  python3 eval/capture_sc_baseline.py --golden eval/golden_set.json \
      --sc-results eval/runs/_sc_lookup_raw.json --sc-stats eval/runs/_sc_stats.json \
      --source-vault /Users/user/Downloads/Example-Vault \
      --out eval/runs/current_sc.frozen.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import path_normalize as pn  # noqa: E402


def _die(msg: str) -> int:
    print(f"FATAL (fail-loud baseline): {msg}", file=sys.stderr)
    print("Refusing to write a baseline. SC must be available for a (re)baseline.",
          file=sys.stderr)
    return 2


def _index_hash(stats: dict) -> str:
    keys = ["vault_path", "active_model", "active_model_dimensions",
            "searchable_entries", "note_entries", "embedded_active_entries",
            "newest_index_mtime", "index_files"]
    payload = json.dumps({k: stats.get(k) for k in keys}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--sc-results", required=True)
    ap.add_argument("--sc-stats", required=True)
    ap.add_argument("--source-vault", default=None)
    ap.add_argument("--latency", default=None, help="optional JSON {qid: ms}")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rp, sp = Path(args.sc_results), Path(args.sc_stats)
    if not rp.exists():
        return _die(f"--sc-results not found: {rp}")
    if not sp.exists():
        return _die(f"--sc-stats not found: {sp}")
    try:
        sc_results = json.loads(rp.read_text(encoding="utf-8"))
        stats = json.loads(sp.read_text(encoding="utf-8"))
    except Exception as exc:
        return _die(f"could not parse SC inputs: {exc}")
    if not sc_results:
        return _die("--sc-results is EMPTY (SC returned nothing / unavailable)")
    if not stats or not stats.get("active_model"):
        return _die("--sc-stats is empty or missing active_model (SC unavailable)")

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    qmeta = {q["id"]: q for q in golden["queries"]}
    latency = json.loads(Path(args.latency).read_text(encoding="utf-8")) if args.latency else {}

    runs: dict[str, dict[str, float]] = {}
    for qid, ranked in sc_results.items():
        if qid not in qmeta:
            print(f"warn: SC result for unknown query id {qid} — skipping", file=sys.stderr)
            continue
        q = qmeta[qid]
        doc_scores: dict[str, float] = {}
        for rank, raw in enumerate(ranked):
            src = pn.normalize(raw)
            if q["stratum"] == "temporal":
                vstate, _ = pn.resolve_version(src, args.source_vault)
                src = f"{src}#{vstate}"
            score = 1.0 / (rank + 1)  # SC has no comparable scores -> reciprocal rank
            if src not in doc_scores or score > doc_scores[src]:
                doc_scores[src] = score
        runs[qid] = doc_scores

    if not runs:
        return _die("no SC results matched golden-set query ids")

    out = {
        "system": "current-sc",
        "captured": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "frozen": True,
        "index_state": {
            "active_model": stats.get("active_model"),
            "dimensions": stats.get("active_model_dimensions"),
            "searchable_entries": stats.get("searchable_entries"),
            "note_entries": stats.get("note_entries"),
            "newest_index_mtime": stats.get("newest_index_mtime"),
            "index_hash": _index_hash(stats),
            "vault_path": stats.get("vault_path"),
        },
        "k": max((len(v) for v in runs.values()), default=0),
        "runs": runs,
        "latency_ms": latency,
        "scope": {"queries_captured": sorted(runs), "n": len(runs),
                  "note": "frozen SC baseline; this IS the 'today' incumbent for the gate"},
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"FROZEN SC baseline: {len(runs)} queries -> {args.out}")
    print(f"  index_hash={out['index_state']['index_hash'][:16]}…  model={out['index_state']['active_model']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

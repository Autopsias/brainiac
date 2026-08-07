#!/usr/bin/env python3
"""TEL-01 (napmem-adoption S02) — navigation-behavior counters from a trace.

Turns the tool-call trace the vault-eval qualitative cascade records into
four numbers:

  * avg_tool_calls     — mean number of tool calls per query (cost telemetry,
                          reported only — search effort and answer quality
                          are only weakly aligned, arXiv 2608.01913).
  * multi_level_rate    — share of queries whose calls span >=2 tool
                          FAMILIES (lexical/semantic/structured/graph/fetch/
                          other) — a diversity-of-retrieval-modality signal,
                          not NapMem's abstraction levels.
  * evidence_hit_ratio  — per-CALL precision: 100 * (calls whose
                          returned_ids intersect gold_ids) / total calls,
                          computed only over queries with non-empty
                          gold_ids. Structurally depressed by breadth (a
                          map-note read that never hits gold still counts).
  * gold_coverage       — per-QUERY recall: 100 * (gold-bearing queries
                          where ANY call hit gold) / (gold-bearing
                          queries). The quality-correlated navigation
                          metric (cumulative recall tracks answer quality
                          better than per-call precision does).

All four are DESCRIPTIVE / non-confirmatory signal — the golden set +
eval/gate.py stays the only *gate*.

Trace schema (see .claude/skills/vault-eval/SKILL.md for the runner-side
recording contract):

    {
      "run": "2026-08-07",
      "queries": [
        {"qid": "q01", "gold_ids": ["note-a"],
         "calls": [
           {"tool": "grep", "returned_ids": ["note-b"]},
           {"tool": "search", "returned_ids": ["note-a", "note-c"]},
           {"tool": "get", "returned_ids": ["note-a"], "status": "ok"}
         ]}
      ]
    }

A call that failed is still recorded, with `returned_ids: []` and a
`status` field naming the failure (e.g. not_found, egress-withheld).

CLI:

    python3 eval/navigation_stats.py --trace <file.json>

Prints one JSON object: {n_queries, n_calls, avg_tool_calls,
multi_level_rate, evidence_hit_ratio, gold_coverage}.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Known tool -> family mapping. Any tool name not listed here maps to the
# single shared "other" bucket (never one bucket per unknown name — that
# would silently inflate multi_level_rate whenever the tool surface grows).
_FAMILIES = {
    "grep": "lexical",
    "search": "semantic",
    "hybrid-search": "semantic",
    "bases-query": "structured",
    "graph-expand": "graph",
    "get": "fetch",
    "read": "fetch",
}


def family(tool_name: str) -> str:
    """Map a tool name to its family; unknown names share one "other" bucket."""
    return _FAMILIES.get(tool_name, "other")


def compute_stats(trace: dict) -> dict:
    queries = trace.get("queries", [])
    n_queries = len(queries)

    all_calls = [call for q in queries for call in q.get("calls", [])]
    n_calls = len(all_calls)

    if n_calls == 0:
        return {
            "n_queries": n_queries,
            "n_calls": 0,
            "avg_tool_calls": 0,
            "multi_level_rate": 0,
            "evidence_hit_ratio": 0,
            "gold_coverage": 0,
        }

    avg_tool_calls = n_calls / n_queries if n_queries else 0

    multi_family_queries = 0
    for q in queries:
        families = {family(c.get("tool", "")) for c in q.get("calls", [])}
        if len(families) >= 2:
            multi_family_queries += 1
    multi_level_rate = 100 * multi_family_queries / n_queries if n_queries else 0

    # Only queries with non-empty gold_ids participate in the hit-ratio and
    # coverage denominators (calibration/refusal questions carry no gold).
    gold_queries = [q for q in queries if q.get("gold_ids")]
    n_gold_queries = len(gold_queries)

    gold_calls = [c for q in gold_queries for c in q.get("calls", [])]
    n_gold_calls = len(gold_calls)

    if n_gold_calls == 0:
        evidence_hit_ratio = 0
    else:
        hits = 0
        for q in gold_queries:
            gold = set(q["gold_ids"])
            for c in q.get("calls", []):
                if gold.intersection(c.get("returned_ids", [])):
                    hits += 1
        evidence_hit_ratio = 100 * hits / n_gold_calls

    if n_gold_queries == 0:
        gold_coverage = 0
    else:
        covered = 0
        for q in gold_queries:
            gold = set(q["gold_ids"])
            if any(gold.intersection(c.get("returned_ids", [])) for c in q.get("calls", [])):
                covered += 1
        gold_coverage = 100 * covered / n_gold_queries

    return {
        "n_queries": n_queries,
        "n_calls": n_calls,
        "avg_tool_calls": avg_tool_calls,
        "multi_level_rate": multi_level_rate,
        "evidence_hit_ratio": evidence_hit_ratio,
        "gold_coverage": gold_coverage,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", required=True, type=Path, help="path to a trace JSON file")
    args = ap.parse_args()

    trace = json.loads(args.trace.read_text(encoding="utf-8"))
    stats = compute_stats(trace)
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

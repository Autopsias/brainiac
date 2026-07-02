#!/usr/bin/env python3
"""S05 EVAL-02 — ranx A/B harness: current (frozen SC) vs new (brain).

Scores BOTH systems against the same golden-set qrels and emits a scorecard:
Recall@5/10/20, nDCG@10, MRR@10 + latency p50/p95 — OVERALL, PER-LANGUAGE and
PER-CLASS (stratum). Per-query Recall@10 for each system is carried through so
the gate (eval/gate.py) can run the paired bootstrap + Fisher test on the delta.

It compares the NEW run against the FROZEN, committed current-SC run file
(HARDENED:consensus) — never a live SC call. Comparison is PAIRED: only query
ids present in BOTH run files are scored, and the scored set is reported so a
reduced/smoke scope is explicit, never hidden.

Usage:
    python3 eval/harness.py \
        --golden eval/golden_set.json --qrels eval/qrels/qrels.json \
        --current eval/runs/current_sc.frozen.json --new eval/runs/new_brain.json \
        --out _evidence/s05/scorecard.json --md _evidence/s05/scorecard.md
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from ranx import Qrels, Run, evaluate

METRICS = ["recall@5", "recall@10", "recall@20", "ndcg@10", "mrr@10"]


def _load(p: str) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _pctl(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 2)


_NO_HIT = {"__no_hit__": 0.0}  # sentinel so ranx accepts a query that returned nothing


def _safe_run(run_d: dict, qids) -> dict:
    """ranx.Run rejects a query with an empty doc list — inject a non-relevant
    sentinel so an empty retrieval scores 0 instead of crashing the harness."""
    return {q: (run_d.get(q) or dict(_NO_HIT)) for q in qids}


def _seg_eval(qrels_d: dict, run_d: dict, qids: list[str]) -> dict:
    """Evaluate the metrics over a subset of query ids. Returns {} if the subset
    has no scorable query (no qrels)."""
    qd = {q: qrels_d[q] for q in qids if q in qrels_d and qrels_d[q]}
    rd = _safe_run(run_d, qd)
    if not qd:
        return {}
    qrels, run = Qrels(qd), Run(rd)
    out = {}
    for m in METRICS:
        out[m] = round(float(evaluate(qrels, run, m)), 4)
    out["n"] = len(qd)
    return out


def _per_query_recall(qrels_d: dict, run_d: dict, qids: list[str], k: int = 10) -> dict:
    """Per-query Recall@k keyed by qid (for the gate's paired bootstrap)."""
    out = {}
    for q in qids:
        if q not in qrels_d or not qrels_d[q]:
            continue
        r = evaluate(Qrels({q: qrels_d[q]}), Run({q: (run_d.get(q) or dict(_NO_HIT))}), f"recall@{k}")
        out[q] = round(float(r), 6)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--qrels", required=True)
    ap.add_argument("--current", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--md", default=None)
    args = ap.parse_args()

    golden = _load(args.golden)
    qrels_d = _load(args.qrels)
    cur = _load(args.current)
    new = _load(args.new)

    qmeta = {q["id"]: q for q in golden["queries"]}

    cur_runs, new_runs = cur["runs"], new["runs"]
    cur_lat, new_lat = cur.get("latency_ms", {}), new.get("latency_ms", {})

    # PAIRED scope: only queries present in BOTH systems' runs AND in qrels.
    scored = sorted(set(cur_runs) & set(new_runs) & set(qrels_d))
    missing_current = sorted(set(qrels_d) - set(cur_runs))
    missing_new = sorted(set(qrels_d) - set(new_runs))

    def segments(qids):
        segs = {"overall": qids}
        for lng in sorted({qmeta[q]["lang"] for q in qids if q in qmeta}):
            segs[f"lang:{lng}"] = [q for q in qids if qmeta[q]["lang"] == lng]
        for st in sorted({qmeta[q]["stratum"] for q in qids if q in qmeta}):
            segs[f"class:{st}"] = [q for q in qids if qmeta[q]["stratum"] == st]
        segs["held_out"] = [q for q in qids if qmeta.get(q, {}).get("held_out")]
        return segs

    segs = segments(scored)
    cov = golden.get("coverage", {})

    def power_of(seg_name: str, n: int) -> str:
        if seg_name == "overall":
            return "gate"
        if seg_name.startswith("class:"):
            return cov.get("strata", {}).get(seg_name[6:], {}).get("power", "smoke")
        if seg_name.startswith("lang:"):
            return cov.get("languages", {}).get(seg_name[5:], {}).get("power", "smoke")
        return "smoke"

    scorecard = {
        "session": "s05",
        "golden_set": {"schema": golden.get("schema_version"), "total_queries": len(golden["queries"])},
        "current_system": {"label": cur.get("system"), "captured": cur.get("captured"),
                           "index_state": cur.get("index_state"), "scope": cur.get("scope")},
        "new_system": {"label": new.get("system"), "captured": new.get("captured"),
                       "index_state": new.get("index_state"), "scope": new.get("scope")},
        "paired_scope": {"scored_n": len(scored), "scored_ids": scored,
                         "missing_from_current": missing_current,
                         "missing_from_new": missing_new},
        "metrics": {"by_segment": {}},
        "latency_ms": {},
        "per_query_recall@10": {
            "current": _per_query_recall(qrels_d, cur_runs, scored),
            "new": _per_query_recall(qrels_d, new_runs, scored),
        },
        # maps the gate uses for per-language / per-class bootstrap
        "_qlang": {q: qmeta[q]["lang"] for q in scored if q in qmeta},
        "_qstratum": {q: qmeta[q]["stratum"] for q in scored if q in qmeta},
    }

    for seg, qids in segs.items():
        cur_m = _seg_eval(qrels_d, cur_runs, qids)
        new_m = _seg_eval(qrels_d, new_runs, qids)
        if not cur_m and not new_m:
            continue
        delta = {}
        for m in METRICS:
            if m in cur_m and m in new_m:
                delta[m] = round(new_m[m] - cur_m[m], 4)
        scorecard["metrics"]["by_segment"][seg] = {
            "n": new_m.get("n", cur_m.get("n", 0)),
            "power": power_of(seg, new_m.get("n", 0)),
            "current": cur_m, "new": new_m, "delta": delta,
        }

    # latency over the scored set
    cl = [cur_lat[q] for q in scored if q in cur_lat]
    nl = [new_lat[q] for q in scored if q in new_lat]
    scorecard["latency_ms"] = {
        "current": {"p50": _pctl(cl, 0.50), "p95": _pctl(cl, 0.95), "n": len(cl)},
        "new": {"p50": _pctl(nl, 0.50), "p95": _pctl(nl, 0.95), "n": len(nl)},
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(scorecard, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")

    if args.md:
        lines = [f"# S05 A/B scorecard — current (SC) vs new (brain)", "",
                 f"- current: `{cur.get('system')}` captured {cur.get('captured')}",
                 f"- new: `{new.get('system')}` captured {new.get('captured')}",
                 f"- paired scored set: **{len(scored)}** queries "
                 f"(missing from current: {len(missing_current)}, from new: {len(missing_new)})",
                 "", "## Metrics by segment", "",
                 "| segment | n | power | R@10 cur | R@10 new | Δ R@10 | nDCG@10 cur | nDCG@10 new | MRR@10 cur | MRR@10 new |",
                 "|---|--:|---|--:|--:|--:|--:|--:|--:|--:|"]
        for seg, d in scorecard["metrics"]["by_segment"].items():
            c, n, dl = d["current"], d["new"], d["delta"]
            lines.append(
                f"| {seg} | {d['n']} | {d['power']} | {c.get('recall@10','-')} | "
                f"{n.get('recall@10','-')} | {dl.get('recall@10','-')} | "
                f"{c.get('ndcg@10','-')} | {n.get('ndcg@10','-')} | "
                f"{c.get('mrr@10','-')} | {n.get('mrr@10','-')} |")
        lat = scorecard["latency_ms"]
        lines += ["", "## Latency (ms)", "",
                  f"- current p50={lat['current']['p50']} p95={lat['current']['p95']}",
                  f"- new p50={lat['new']['p50']} p95={lat['new']['p95']}", ""]
        Path(args.md).write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"scored {len(scored)} paired queries; segments={len(scorecard['metrics']['by_segment'])}")
    print(f"wrote {args.out}" + (f" + {args.md}" if args.md else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

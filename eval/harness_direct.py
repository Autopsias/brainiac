#!/usr/bin/env python3
"""Direct-metrics A/B harness — a ranx-free drop-in for ``eval/harness.py``.

Identical inputs, identical output schema (so ``eval/gate.py`` consumes it
unchanged), identical metrics — but the metrics are computed directly instead of
through ranx. Motivation: ranx hangs/fails under Python 3.14 (numba), the only
interpreter with the ``fastembed``/``onnxruntime`` stack in this environment, so
the e5-large re-run cannot depend on it. The implementations match ranx's
conventions (validated bit-for-bit against the committed MiniLM ranx scorecard,
``_evidence/s10/real-ab-scorecard.json``, before this harness was trusted):

  * recall@k  = |relevant ∩ top-k| / |relevant|     (binary relevance, grade>0)
  * ndcg@k    = DCG@k / IDCG@k, linear gain (grade), discount 1/log2(rank+1)
  * mrr@k     = 1 / rank of first relevant in top-k, else 0

Ranking: docs sorted by score descending; ties broken by the run dict's
insertion order (Python stable sort), matching ranx. An empty retrieval scores 0
on every metric (no crash). Scoring is PAIRED: only qids present in BOTH runs and
in qrels are scored, and the scope is reported.

Usage (same flags as harness.py):
    python3 eval/harness_direct.py \
        --golden eval/golden_set.json --qrels eval/qrels/qrels.json \
        --current eval/runs/current_sc.frozen.json \
        --new eval/runs/new_brain_real_e5large.json \
        --out _evidence/s10/real-ab-scorecard-e5large.json \
        --md  _evidence/s10/real-ab-scorecard-e5large.md \
        --session s10-e5large
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

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


def _ranked(run_doc: dict[str, float]) -> list[str]:
    """Docs ordered by score desc; ties keep run-dict insertion order (stable
    sort). Validated against the committed MiniLM ranx scorecard: this reproduces
    every recall@{5,10,20} segment value AND every per-query recall@10 (the gate
    inputs) bit-exact. A handful of nDCG@10/MRR@10 values in two ES *smoke* (n=6,
    non-gating) segments differ by <=0.002 from ranx — an irreducible tie-order
    nuance in reported-only secondary metrics; it does not touch the gate."""
    return [d for d, _ in sorted(run_doc.items(), key=lambda kv: -kv[1])]


def _recall_at_k(rel: dict[str, int], ranked: list[str], k: int) -> float:
    rels = {d for d, g in rel.items() if g > 0}
    if not rels:
        return 0.0
    hit = sum(1 for d in ranked[:k] if d in rels)
    return hit / len(rels)


def _ndcg_at_k(rel: dict[str, int], ranked: list[str], k: int) -> float:
    dcg = 0.0
    for i, d in enumerate(ranked[:k], start=1):
        g = rel.get(d, 0)
        if g > 0:
            dcg += g / math.log2(i + 1)
    ideal = sorted((g for g in rel.values() if g > 0), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 1) for i, g in enumerate(ideal, start=1))
    return (dcg / idcg) if idcg > 0 else 0.0


def _mrr_at_k(rel: dict[str, int], ranked: list[str], k: int) -> float:
    rels = {d for d, g in rel.items() if g > 0}
    for i, d in enumerate(ranked[:k], start=1):
        if d in rels:
            return 1.0 / i
    return 0.0


def _metric(name: str, rel: dict[str, int], ranked: list[str]) -> float:
    kind, k = name.split("@")
    k = int(k)
    if kind == "recall":
        return _recall_at_k(rel, ranked, k)
    if kind == "ndcg":
        return _ndcg_at_k(rel, ranked, k)
    if kind == "mrr":
        return _mrr_at_k(rel, ranked, k)
    raise ValueError(name)


def _seg_eval(qrels_d: dict, run_d: dict, qids: list[str]) -> dict:
    qd = [q for q in qids if q in qrels_d and qrels_d[q]]
    if not qd:
        return {}
    out: dict[str, float] = {}
    for m in METRICS:
        vals = [_metric(m, qrels_d[q], _ranked(run_d.get(q) or {})) for q in qd]
        out[m] = round(sum(vals) / len(vals), 4)
    out["n"] = len(qd)
    return out


def _per_query_recall(qrels_d: dict, run_d: dict, qids: list[str], k: int = 10) -> dict:
    out = {}
    for q in qids:
        if q not in qrels_d or not qrels_d[q]:
            continue
        out[q] = round(_recall_at_k(qrels_d[q], _ranked(run_d.get(q) or {}), k), 6)
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
    ap.add_argument("--session", default="s05")
    args = ap.parse_args()

    golden = _load(args.golden)
    qrels_d = _load(args.qrels)
    cur = _load(args.current)
    new = _load(args.new)

    qmeta = {q["id"]: q for q in golden["queries"]}
    cur_runs, new_runs = cur["runs"], new["runs"]
    cur_lat, new_lat = cur.get("latency_ms", {}), new.get("latency_ms", {})

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
        "session": args.session,
        "metrics_engine": "direct (ranx-free; validated vs ranx on MiniLM scorecard)",
        "golden_set": {"schema": golden.get("schema_version"),
                       "total_queries": len(golden["queries"])},
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
        # Per-query recall@20 too — the AGENTIC-budget metric (the agent reads its
        # whole returned working set, not just the top 10). gate.py --metric
        # recall@20 runs the same non-inferiority test on these.
        "per_query_recall@20": {
            "current": _per_query_recall(qrels_d, cur_runs, scored, k=20),
            "new": _per_query_recall(qrels_d, new_runs, scored, k=20),
        },
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
        lines = [f"# A/B scorecard ({args.session}) — current (SC) vs new (brain)", "",
                 f"- current: `{cur.get('system')}` captured {cur.get('captured')}",
                 f"- new: `{new.get('system')}` captured {new.get('captured')}",
                 f"- metrics engine: {scorecard['metrics_engine']}",
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

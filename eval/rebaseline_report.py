#!/usr/bin/env python3
"""S02 (BL-02) — per-stratum recall@10/recall@20/mrr@10/hit@1 + latency p50/p95
for each captured arm, plus per-query rank of every qrel hit.

Deliberately standalone (not a change to eval/harness_direct.py or eval/gate.py):
those two are shared A/B-gate infrastructure other sessions (s04/s08/s09/s10)
depend on unchanged, and hit@1 is not one of their metrics. This script only
*reads* eval/golden_set.json + eval/qrels/qrels.json + the arm run files
already produced by the committed eval/capture_run.py — it computes nothing
capture_run.py didn't already retrieve.

Latency: the FIRST query of each arm is dropped before computing p50/p95 (a
one-time embedder/reranker model load dominates it in an 11-query arm, and
both s04 and s09 decide from these numbers) — reported separately as
``first_query_ms``.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STRATA = [
    "cross_lingual_pt_en", "cross_lingual_es_en", "cross_lingual_en_es", "cross_lingual_en_pt",
    "lexical_identifier", "multi_hop", "temporal",
]


def _pctl(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 2)


def _ranked(run_doc: dict[str, float]) -> list[str]:
    return [d for d, _ in sorted(run_doc.items(), key=lambda kv: -kv[1])]


def _rank_of(doc: str, ranked: list[str]) -> int | None:
    try:
        return ranked.index(doc) + 1
    except ValueError:
        return None


def score_arm(golden: dict, qrels: dict, run: dict, arm_label: str) -> dict:
    qmeta = {q["id"]: q for q in golden["queries"]}
    runs = run["runs"]
    latency = run["latency_ms"]

    per_query = {}
    for qid, q in qmeta.items():
        if qid not in runs or qid not in qrels:
            continue
        rel = {p for p, g in qrels[qid].items() if g > 0}
        ranked = _ranked(runs[qid])
        ranks = {doc: _rank_of(doc, ranked) for doc in rel}
        hit10 = sum(1 for r in ranks.values() if r is not None and r <= 10)
        hit20 = sum(1 for r in ranks.values() if r is not None and r <= 20)
        recall10 = hit10 / len(rel) if rel else 0.0
        recall20 = hit20 / len(rel) if rel else 0.0
        best_rank = min((r for r in ranks.values() if r is not None), default=None)
        mrr10 = (1.0 / best_rank) if best_rank is not None and best_rank <= 10 else 0.0
        hit_at_1 = 1.0 if best_rank == 1 else 0.0
        per_query[qid] = {
            "stratum": q["stratum"],
            "qrel_ranks": ranks,
            "recall@10": round(recall10, 4),
            "recall@20": round(recall20, 4),
            "mrr@10": round(mrr10, 4),
            "hit@1": hit_at_1,
            "latency_ms": latency.get(qid),
        }

    def _agg(qids):
        qids = [q for q in qids if q in per_query]
        if not qids:
            return None
        out = {"n": len(qids)}
        for m in ("recall@10", "recall@20", "mrr@10", "hit@1"):
            out[m] = round(sum(per_query[q][m] for q in qids) / len(qids), 4)
        return out

    by_stratum = {st: _agg([q for q in per_query if per_query[q]["stratum"] == st]) for st in STRATA}
    overall = _agg(list(per_query))

    qids_in_order = [q["id"] for q in golden["queries"] if q["id"] in latency]
    lat_values = [latency[q] for q in qids_in_order]
    first_query_ms = lat_values[0] if lat_values else None
    warm = lat_values[1:] if len(lat_values) > 1 else []
    latency_summary = {
        "first_query_ms": first_query_ms,
        "p50_ms_warm": _pctl(warm, 0.50),
        "p95_ms_warm": _pctl(warm, 0.95),
        "n_warm": len(warm),
        "note": "first query dropped (one-time model load); p50/p95 computed over the remaining warm queries",
    }

    return {
        "arm": arm_label,
        "captured": run.get("captured"),
        "index_state_fingerprint": run.get("index_state", {}).get("fingerprint"),
        "params": run.get("index_state", {}).get("params"),
        "by_stratum": by_stratum,
        "overall": overall,
        "latency_ms": latency_summary,
        "per_query": per_query,
    }


def main() -> int:
    golden = json.loads((REPO / "eval" / "golden_set.json").read_text(encoding="utf-8"))
    qrels = json.loads((REPO / "eval" / "qrels" / "qrels.json").read_text(encoding="utf-8"))

    # rerank50 landed 2026-08-04 via the orchestrator's instrumented,
    # checkpointed capture (eval/rebaseline_rerank_capture.py --resume),
    # rerank_applied_verified 66/66 -- so it is scored like any other arm now.
    # Its LATENCY is not comparable: it ran alongside the full test suite and
    # its numbers carry that contention. Its RANKING metrics are unaffected.
    arms = {
        "bare": "rebaseline-0.19.24-2026-08-04-bare.json",
        "rerank15": "rebaseline-0.19.24-2026-08-04-rerank15.json",
        "rerank20": "rebaseline-0.19.24-2026-08-04-rerank20.json",
        "rerank50": "rebaseline-0.19.24-2026-08-04-rerank50.json",
    }
    out = {}
    for label, fname in arms.items():
        run = json.loads((REPO / "eval" / "runs" / fname).read_text(encoding="utf-8"))
        out[label] = score_arm(golden, qrels, run, label)
        applied = run.get("index_state", {}).get("rerank_applied_verified")
        out[label]["rerank_status"] = (
            "n/a (rerank not requested)" if label == "bare"
            else f"MEASURED: rerank_applied_verified={applied} "
                 "(rank-preserving capture: eval/rebaseline_rerank_capture.py)"
        )
    out["rerank50"]["latency_caveat"] = (
        "captured under CPU contention with the full test suite; ranking metrics "
        "are valid, p50/p95 are an upper bound and not comparable to the other arms"
    )

    out_path = REPO / "eval" / "runs" / "rebaseline-0.19.24-2026-08-04-scored.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out_path}")
    for label, doc in out.items():
        if "overall" in doc:
            print(f"[{label}] overall={doc['overall']} latency={doc['latency_ms']}")
        else:
            print(f"[{label}] {doc.get('status')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

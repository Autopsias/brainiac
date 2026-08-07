#!/usr/bin/env python3
"""S11 (RK-02) — offline calibration of the adaptive rerank gate.

The gate decides, PER QUERY, whether the cross-encoder runs at all. Reranking
is worth ~5-6s of latency on this vault (BR-03, owner ruling 2026-08-04), but
the S04 measurement showed the gain is wildly uneven across strata:
lexical_identifier is already at ceiling bare, while temporal and multi_hop
roughly double. Spending the cross-encoder on a query it cannot improve is
pure latency.

This script answers "which gating rule, at what threshold" WITHOUT any new
rerank corpus run:

  stage 1 (--capture-signals)  run the 66 golden queries on the BARE path
      (no cross-encoder, ~280ms each) with a trace, and record the signals a
      production gate could actually see BEFORE reranking: the pre-rerank
      top-1 evidence label, its create_safety, whether ADR-0008 pinned a
      unique full identity, and the pre-rerank RRF score margin between
      rank 1 and rank 2.

  stage 2 (--calibrate)  for each candidate rule, synthesise the run a gated
      engine WOULD have produced: SKIP queries take their rows from the
      already-captured bare arm, RERANK queries take theirs from the
      already-captured rerank50 arm. Score that synthetic run with the same
      scorer the arms were scored with (eval/rebaseline_report.score_arm) and
      report skip fraction + recall@10 / mrr@10 / hit@1 vs always-on.

Both arm artifacts are RANK-PRESERVING captures
(eval/rebaseline_rerank_capture.py): the older eval/capture_run.py recorded
Hit.score, and every scorer rebuilds rank by re-sorting on it, which makes a
rerank-only reordering invisible. Two SELF-CHECK rows are always emitted for
exactly that reason -- `_control_always_on` must reproduce the chosen rerank
arm and `_control_always_off` must reproduce the bare arm. If those two rows are
equal, the calibration is blind and every other number in the file is noise.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

from rebaseline_report import score_arm  # noqa: E402

BARE_ARM = REPO / "eval" / "runs" / "rebaseline-0.19.24-2026-08-04-bare.json"
ARMS = {
    n: REPO / "eval" / "runs" / f"rebaseline-0.19.24-2026-08-04-rerank{n}.json"
    for n in (15, 20, 50)
}

# Evidence labels that mean "the top hit is a literal identity match". A
# cross-encoder cannot improve a hit that matched the query string exactly.
IDENTITY_EVIDENCE = {"alias_hit", "exact_title_match"}


# --------------------------------------------------------------------------
# stage 1 — signal capture (bare path only, no cross-encoder)
# --------------------------------------------------------------------------
def capture_signals(args) -> dict:
    sys.path.insert(0, args.source_root)
    from brain.index import BrainIndex
    from brain.vectors import get_backend
    from brain.embed import get_embedder

    idx = BrainIndex(
        db_path=Path(args.index_db).resolve(),
        backend=get_backend(args.vector_backend),
        embedder=get_embedder(args.embedder),
        read_only=True,
    )
    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))

    signals: dict[str, dict] = {}
    for n, q in enumerate(golden["queries"], start=1):
        t0 = time.perf_counter()
        hits, trace = idx.hybrid_search_with_trace(
            q["text"], k=args.k, rerank=False, rrf_k=args.rrf_k,
        )
        elapsed = round((time.perf_counter() - t0) * 1000.0, 2)
        order = trace.pre_rerank_order
        scores = [trace._records[r]["pre_rerank_score"] for r in order[:2]]
        s1 = float(scores[0]) if scores else 0.0
        s2 = float(scores[1]) if len(scores) > 1 else 0.0
        signals[q["id"]] = {
            "stratum": q["stratum"],
            "evidence_top1": hits[0].evidence if hits else None,
            "create_safety_top1": hits[0].create_safety if hits else None,
            "pinned_unique_identity": any(
                rec["pin"]["applied"] for rec in trace._records.values()
            ),
            "pre_rerank_top1_score": round(s1, 8),
            "pre_rerank_top2_score": round(s2, 8),
            "margin_rel": round((s1 - s2) / s1, 6) if s1 > 0 else 0.0,
            "bare_latency_ms": elapsed,
        }
        print(f"[{n}/{len(golden['queries'])}] {q['id']} {elapsed:.0f}ms "
              f"evidence={signals[q['id']]['evidence_top1']} "
              f"pinned={signals[q['id']]['pinned_unique_identity']} "
              f"margin={signals[q['id']]['margin_rel']:.3f}",
              file=sys.stderr, flush=True)

    return {
        "captured": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "params": {"k": args.k, "rrf_k": args.rrf_k, "rerank": False},
        "index_db": str(args.index_db),
        "signals": signals,
    }


# --------------------------------------------------------------------------
# stage 2 — offline calibration
# --------------------------------------------------------------------------
def _rules() -> dict:
    """name -> (description, predicate(sig) -> True means SKIP the reranker)."""
    rules: dict[str, tuple[str, object]] = {
        "_control_always_on": ("control: never skip — must reproduce the rerank arm",
                               lambda s: False),
        "_control_always_off": ("control: always skip — must reproduce the bare arm",
                                lambda s: True),
        "pinned_identity": (
            "skip when ADR-0008 pinned a unique full alias/title owner at rank 1",
            lambda s: s["pinned_unique_identity"]),
        "identity_evidence": (
            "skip when the pre-rerank top hit is a literal identity match "
            "(alias_hit | exact_title_match)",
            lambda s: s["evidence_top1"] in IDENTITY_EVIDENCE),
        "identity_or_phrase": (
            "identity_evidence, widened to include title_phrase_match",
            lambda s: s["evidence_top1"] in IDENTITY_EVIDENCE | {"title_phrase_match"}),
        "create_safety_exists": (
            "skip when the top hit's create_safety is `exists`",
            lambda s: s["create_safety_top1"] == "exists"),
        "keyword_exact_too": (
            "identity_evidence, widened to include keyword_exact",
            lambda s: s["evidence_top1"] in IDENTITY_EVIDENCE | {"keyword_exact"}),
    }
    for t in (0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50):
        rules[f"margin_rel>={t:.2f}"] = (
            f"skip when the pre-rerank rank1/rank2 RRF margin is >= {t:.0%}",
            (lambda t: lambda s: s["margin_rel"] >= t)(t),
        )
        rules[f"identity_or_margin>={t:.2f}"] = (
            f"identity_evidence OR a rank1/rank2 margin >= {t:.0%}",
            (lambda t: lambda s: s["evidence_top1"] in IDENTITY_EVIDENCE
             or s["margin_rel"] >= t)(t),
        )
    return rules


def calibrate(args) -> dict:
    golden = json.loads((REPO / "eval" / "golden_set.json").read_text(encoding="utf-8"))
    qrels = json.loads((REPO / "eval" / "qrels" / "qrels.json").read_text(encoding="utf-8"))
    bare = json.loads(BARE_ARM.read_text(encoding="utf-8"))
    rerank_arm = ARMS[args.rerank_window]
    rerank = json.loads(rerank_arm.read_text(encoding="utf-8"))
    sig_doc = json.loads(Path(args.signals).read_text(encoding="utf-8"))
    signals = sig_doc["signals"]

    def synth(skip_pred) -> tuple[dict, list[str]]:
        """Build the run a gated engine would have produced, + the skipped ids."""
        runs, latency, skipped = {}, {}, []
        for qid in rerank["runs"]:
            sig = signals.get(qid)
            if sig is not None and skip_pred(sig):
                skipped.append(qid)
                runs[qid] = bare["runs"][qid]
                latency[qid] = bare["latency_ms"][qid]
            else:
                runs[qid] = rerank["runs"][qid]
                latency[qid] = rerank["latency_ms"][qid]
        doc = dict(rerank)
        doc["runs"], doc["latency_ms"] = runs, latency
        return doc, skipped

    metrics = ("recall@10", "recall@20", "mrr@10", "hit@1")
    always_on = score_arm(golden, qrels, rerank, "always_on")["overall"]

    rows = []
    for name, (desc, pred) in _rules().items():
        doc, skipped = synth(pred)
        scored = score_arm(golden, qrels, doc, name)
        rows.append({
            "rule": name,
            "description": desc,
            "queries_skipped": len(skipped),
            "skip_fraction": round(len(skipped) / len(rerank["runs"]), 4),
            "skipped_query_ids": sorted(skipped),
            "overall": scored["overall"],
            "delta_vs_always_on": {
                m: round(scored["overall"][m] - always_on[m], 4) for m in metrics
            },
            "by_stratum": scored["by_stratum"],
        })

    skipped_latency = {}
    for label, doc in (("bare_arm", bare), ("rerank_arm", rerank)):
        vals = sorted(doc["latency_ms"][q] for q in
                      next(r for r in rows if r["rule"] == "pinned_identity")["skipped_query_ids"])
        skipped_latency[f"{label}_median_ms"] = round(vals[len(vals) // 2], 2)

    on_row = next(r for r in rows if r["rule"] == "_control_always_on")
    off_row = next(r for r in rows if r["rule"] == "_control_always_off")
    bare_overall = score_arm(golden, qrels, bare, "bare")["overall"]
    self_check = {
        "always_on_reproduces_the_rerank_arm": on_row["overall"] == always_on,
        "always_off_reproduces_bare_arm": off_row["overall"] == bare_overall,
        "controls_differ": on_row["overall"] != off_row["overall"],
        "why": "if controls_differ is false the calibration cannot see a "
               "rerank-only reordering and every row here is meaningless "
               "(the rank-preserving-capture trap, see module docstring)",
    }

    return {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        # Recorded here rather than hand-patched onto the artifact, so re-running
        # --calibrate reproduces the committed file instead of silently dropping it.
        "shipped_rule": {
            "rule": "pinned_identity",
            "why": ("zero measured delta on all four metrics at the largest skip "
                    "fraction that is free; the RRF-margin family selects the SAME "
                    "7 queries for any cutoff in 0.20-0.40, so the pin is that rule "
                    "without a tuned constant, and every wider rule measured — "
                    "including adding a COLLIDING alias hit — costs recall@10"),
            "overrides": ["--no-rerank-gate (per call)",
                          "BRAIN_RERANK_GATE_DISABLED=1 (global)"],
            "observability": "search --explain --json -> ranking.rerank_gate",
            "measured_latency_of_the_skipped_queries": dict(
                skipped_latency,
                note=("captured latencies of the skipped queries in this run's own "
                      "two input arms — computed, not hand-entered, so it always "
                      "matches the --rerank-window actually scored"),
            ),
        },
        "inputs": {
            "bare_arm": BARE_ARM.name, "rerank_arm": rerank_arm.name,
            "signals": Path(args.signals).name,
            "n_queries": len(rerank["runs"]),
        },
        "self_check": self_check,
        "always_on_overall": always_on,
        "bare_overall": bare_overall,
        "rules": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capture-signals", action="store_true")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--golden", default=str(REPO / "eval" / "golden_set.json"))
    ap.add_argument("--index-db")
    ap.add_argument("--signals", default=str(REPO / "eval" / "runs"
                                             / "rerank-gate-signals-2026-08-04.json"))
    ap.add_argument("--out", default=str(REPO / "eval" / "runs"
                                        / "rerank-gate-calibration-2026-08-04.json"))
    ap.add_argument("-k", type=int, default=20)
    ap.add_argument("--rrf-k", type=int, default=60)
    ap.add_argument("--rerank-window", type=int, default=20, choices=sorted(ARMS),
                    help="which captured rerank arm is the always-on comparator "
                         "(default 20 — the shipped default window since the "
                         "2026-08-04 latency ruling)")
    ap.add_argument("--embedder", default="auto")
    ap.add_argument("--vector-backend", default="sqlite-vec")
    ap.add_argument("--source-root", default=str(REPO / "src"))
    args = ap.parse_args()

    if args.capture_signals:
        doc = capture_signals(args)
        Path(args.signals).write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.signals}")
    if args.calibrate:
        doc = calibrate(args)
        Path(args.out).write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
        print(json.dumps(doc["self_check"], indent=2))
        for r in doc["rules"]:
            d = r["delta_vs_always_on"]
            print(f"{r['rule']:28s} skip={r['skip_fraction']:.2%} "
                  f"recall@10{d['recall@10']:+.4f} mrr@10{d['mrr@10']:+.4f} "
                  f"hit@1{d['hit@1']:+.4f}")
    if not (args.capture_signals or args.calibrate):
        ap.error("pass --capture-signals and/or --calibrate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""s01 dual-model adjudication — merge Claude + Codex judgments into locked qrels.

Agreement rule (Ricardo directive, no human labeling):
  both say ``rel``    -> LOCKED rel   (enters the trusted core)
  both say ``notrel`` -> LOCKED notrel
  any other combo (incl. either ``unsure`` / ``codex_unavailable``)
                      -> EXCLUDED from the trusted core (state ``unsure``)

Cohen's κ is computed over pairs where BOTH models gave a DEFINITE label
(rel/notrel) — the honest agreement base. The unsure/exclusion rate is reported
alongside, never hidden.

Outputs
-------
1. ``qrels_adjudicated.json`` — in the exact shape ``emit_pt_manifest.py
   --adjudicated`` consumes:  {"adjudicator", "generated_at", "method",
   "qrels": {qid: {note_id: 1}}}  where ``qrels`` holds ONLY locked-rel notes
   (grade 1). This is the ingest contract (rel = state 'rel').
2. ``disagreements.json`` — every excluded pair with both labels + snippet, for
   optional later human review.
3. stdout — κ overall + per class, lock/exclude rates, zero-locked-rel query count.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def kappa(a: list[int], b: list[int]) -> float | None:
    n = len(a)
    if n == 0:
        return None
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pa1 = sum(a) / n
    pb1 = sum(b) / n
    pe = pa1 * pb1 + (1 - pa1) * (1 - pb1)
    if pe == 1.0:
        return 1.0
    return round((po - pe) / (1 - pe), 3)


def load_judgments(path: str) -> dict[tuple[str, str], str]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    return {(r["qid"], r["note_id"]): r["label"] for r in rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude", required=True)
    ap.add_argument("--codex", required=True)
    ap.add_argument("--snippets", required=True, help="all_candidates_snippets.json (for disagreement context + qclass)")
    ap.add_argument("--golden", required=True, help="pt-golden-set.json (for stratum per qid)")
    ap.add_argument("--out-qrels", required=True)
    ap.add_argument("--out-disagreements", required=True)
    ap.add_argument("--out-stats", default=None,
                    help="egress-safe agreement stats JSON (counts + kappa only, no text) "
                         "consumed by emit_pt_manifest.py --dual-stats")
    args = ap.parse_args()

    claude = load_judgments(args.claude)
    codex = load_judgments(args.codex)

    snips = json.loads(Path(args.snippets).read_text(encoding="utf-8"))
    snip_of: dict[tuple[str, str], str] = {}
    qclass_of: dict[str, str] = {}
    order: list[tuple[str, str]] = []
    query_of: dict[str, str] = {}
    for q in snips:
        qid = q["qid"]
        qclass_of[qid] = q.get("qclass", "?")
        query_of[qid] = q["query"]
        for c in q["candidates"]:
            key = (qid, c["note_id"])
            snip_of[key] = c["snippet"]
            order.append(key)

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    stratum_of = {q["id"]: q["stratum"] for q in golden["queries"]}

    locked_rel: dict[str, dict[str, int]] = defaultdict(dict)
    disagreements: list[dict] = []

    n_total = 0
    n_lock_rel = n_lock_notrel = n_excluded = 0
    n_codex_unavail = 0
    # κ base: both definite
    per_class_km: dict[str, list[tuple[int, int]]] = defaultdict(list)
    all_a: list[int] = []
    all_b: list[int] = []

    for key in order:
        qid, note_id = key
        ca = claude.get(key, "missing")
        co = codex.get(key, "missing")
        n_total += 1
        if co == "codex_unavailable" or co == "missing":
            n_codex_unavail += 1
        # κ contribution only when both are definite
        if ca in ("rel", "notrel") and co in ("rel", "notrel"):
            va, vb = (1 if ca == "rel" else 0), (1 if co == "rel" else 0)
            per_class_km[stratum_of.get(qid, "?")].append((va, vb))
            all_a.append(va)
            all_b.append(vb)
        # merge rule
        if ca == "rel" and co == "rel":
            locked_rel[qid][note_id] = 1
            n_lock_rel += 1
        elif ca == "notrel" and co == "notrel":
            n_lock_notrel += 1
        else:
            n_excluded += 1
            disagreements.append({
                "qid": qid,
                "qclass": qclass_of.get(qid, "?"),
                "stratum": stratum_of.get(qid, "?"),
                "query": query_of.get(qid, ""),
                "note_id": note_id,
                "claude": ca,
                "codex": co,
                "snippet": snip_of.get(key, ""),
            })

    # zero-locked-rel queries (unusable for Recall@k until resolved)
    all_qids = [q["qid"] for q in snips]
    zero_rel_qids = [qid for qid in all_qids if not locked_rel.get(qid)]

    # κ table
    per_class_kappa = {}
    for cls in sorted(per_class_km):
        pairs = per_class_km[cls]
        a = [p[0] for p in pairs]
        b = [p[1] for p in pairs]
        per_class_kappa[cls] = {"n": len(pairs), "kappa": kappa(a, b)}
    overall_kappa = kappa(all_a, all_b)

    adjudicated = {
        "adjudicator": "dual-model (Claude Opus label + Codex/GPT independent verify)",
        "method": "dual-model-agreement",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agreement_rule": "both rel -> locked rel; both notrel -> locked notrel; else excluded (unsure)",
        "families": {"labeler": "Claude Opus 4.8", "verifier": "Codex / GPT (codex-cli 0.141.0)"},
        "qrels": {qid: notes for qid, notes in sorted(locked_rel.items())},
    }
    Path(args.out_qrels).write_text(
        json.dumps(adjudicated, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    Path(args.out_disagreements).write_text(
        json.dumps({"count": len(disagreements), "pairs": disagreements}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )

    kappa_base = len(all_a)

    if args.out_stats:
        stats = {
            "method": "dual-model-agreement (Claude label x Codex/GPT blind verify)",
            "generated_at": adjudicated["generated_at"],
            "n_pairs": n_total,
            "n_locked_rel": n_lock_rel,
            "n_locked_notrel": n_lock_notrel,
            "n_excluded": n_excluded,
            "n_codex_unavailable": n_codex_unavail,
            "kappa_base_pairs": kappa_base,
            "kappa_overall": overall_kappa,
            "kappa_per_class": per_class_kappa,
            "n_queries": len(all_qids),
            "n_queries_with_locked_rel": len(all_qids) - len(zero_rel_qids),
            "zero_locked_rel_qids": zero_rel_qids,
        }
        Path(args.out_stats).write_text(
            json.dumps(stats, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )

    print("=== dual-model merge ===")
    print(f"total pairs           : {n_total}")
    print(f"locked rel            : {n_lock_rel} ({100*n_lock_rel/n_total:.1f}%)")
    print(f"locked notrel         : {n_lock_notrel} ({100*n_lock_notrel/n_total:.1f}%)")
    print(f"excluded (unsure/disagree): {n_excluded} ({100*n_excluded/n_total:.1f}%)")
    print(f"codex unavailable pairs: {n_codex_unavail}")
    print(f"kappa base (both definite): {kappa_base} ({100*kappa_base/n_total:.1f}% of pairs)")
    print(f"overall Cohen's kappa : {overall_kappa}")
    for cls, v in per_class_kappa.items():
        print(f"  {cls:20s} n={v['n']:4d}  kappa={v['kappa']}")
    print(f"queries total         : {len(all_qids)}")
    print(f"queries w/ >=1 locked-rel: {len(all_qids) - len(zero_rel_qids)}")
    print(f"queries w/ ZERO locked-rel: {len(zero_rel_qids)}  {zero_rel_qids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

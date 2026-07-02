"""Agentic multi-query fan-out eval (S10 follow-up).

Models the agentic recovery the research identifies for cross-lingual misses: an
LLM agent issues the original PT/ES query AND an EN reformulation, then RRF-merges
the two result lists. Measures recall@10 over the MERGED top-10 vs the canonical
qrels — the agentic-budget metric, not single-query recall.

Compares, per PT/ES query:
  - single  : hybrid(original) top-10              (what S10 measured)
  - fanout  : RRF(hybrid(original), hybrid(EN))    (agentic multi-query)

Runs over the already-built e5-small index with the best base config
(BRAIN_ZONE_SCOPE=semantic_only, zone weights set by caller). No re-index.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))
import path_normalize as pn  # noqa: E402
from brain.core import BrainCore  # noqa: E402

VAULT = "/Users/user/Downloads/Acme-Vault"

# Accurate EN reformulations of the PT/ES golden queries (translated by a fluent
# speaker; this is the "agent reformulates cross-lingually" step).
EN = {
    "mh_09": "What is the relationship between RetainedCo's IT strategy and the decision to exclude residential B2C from the perimeter?",
    "tmp_08": "What is the latest version of the integration presentation?",
    "pt_01": "What does RetainedCo's IT strategy say?",
    "pt_02": "What was the decision about Carlos Mendes's succession?",
    "pt_03": "What is the agentic operating model?",
    "pt_04": "Where is the analysis of the Tier 2 greenfield choice between Acme and Northwind?",
    "pt_05": "What was decided about the separation and technology separation?",
    "pt_06": "What is Gamma and what does the audit say about it?",
    "pt_07": "What are the Day 1 requirements for RetailCo integration?",
    "pt_08": "What is Acme's relationship with Northwind in Project Meridian?",
    "pt_09": "What does the meeting with Northwind say about the separation?",
    "pt_10": "What is the decision about the third entity's model?",
    "pt_11": "What is the three-tier architecture?",
    "pt_12": "What is the scope of the PMO defined by Falcon?",
    "es_01": "What was discussed in the bilateral IT meeting at the Madrid headquarters?",
    "es_02": "What is the audit of the physical data centers in Spain?",
    "es_03": "What does Northwind's company profile say?",
    "es_04": "Where is the feasibility analysis of cloning and wiping Northwind's ERP?",
    "es_05": "What was covered in the trading IT meeting with Northwind?",
    "es_06": "What is the red flags report on retail IT separation?",
}


def norm_hits(hh, vault_root):
    out = []
    for h in hh:
        raw = h.path
        rel = os.path.relpath(raw, vault_root) if os.path.isabs(raw) else raw
        out.append(pn.normalize(rel, None))
    return out


def rrf_merge(list_a, list_b, k=60):
    score = {}
    for lst in (list_a, list_b):
        for rank, p in enumerate(lst, start=1):
            score[p] = score.get(p, 0.0) + 1.0 / (k + rank)
    return [p for p, _ in sorted(score.items(), key=lambda t: -t[1])]


def recall_at(top, gold, k=10):
    if not gold:
        return None
    hit = sum(1 for g in gold if g in top[:k])
    return hit / len(gold)


def main():
    golden = {q["id"]: q for q in json.loads((HERE / "golden_set.json").read_text())["queries"]}
    qrels = json.loads((HERE / "qrels" / "qrels.json").read_text())
    core = BrainCore(vault=VAULT)
    vroot = str(Path(VAULT).resolve())

    rows = []
    for qid, en in EN.items():
        q = golden[qid]
        gold = list(qrels.get(qid, {}).keys())
        pt_hits = norm_hits(core.hybrid_search(q["text"], k=20), vroot)
        en_hits = norm_hits(core.hybrid_search(en, k=20), vroot)
        merged = rrf_merge(pt_hits, en_hits)
        rows.append({
            "id": qid, "stratum": q["stratum"],
            "single@10": recall_at(pt_hits, gold, 10),
            "fanout@10": recall_at(merged, gold, 10),
        })

    # aggregate by stratum + overall
    def agg(pred):
        sel = [r for r in rows if pred(r)]
        s = [r["single@10"] for r in sel if r["single@10"] is not None]
        f = [r["fanout@10"] for r in sel if r["fanout@10"] is not None]
        return (sum(s) / len(s) if s else 0, sum(f) / len(f) if f else 0, len(sel))

    print(f"{'segment':18s} {'n':>3s} {'single@10':>11s} {'fanout@10':>11s} {'Δ':>8s}")
    print("-" * 56)
    for name, pred in [
        ("ALL PT+ES", lambda r: True),
        ("monolingual_pt", lambda r: r["stratum"] == "monolingual_pt"),
        ("monolingual_es", lambda r: r["stratum"] == "monolingual_es"),
    ]:
        s, f, n = agg(pred)
        print(f"{name:18s} {n:3d} {s:11.3f} {f:11.3f} {f-s:+8.3f}")
    print("\nper-query:")
    for r in rows:
        d = (r["fanout@10"] or 0) - (r["single@10"] or 0)
        flag = "  <== recovered" if d > 0 else ""
        print(f"  {r['id']:10s} {r['stratum']:16s} single={r['single@10']} fanout={r['fanout@10']}{flag}")


if __name__ == "__main__":
    main()

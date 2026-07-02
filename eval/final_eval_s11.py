#!/usr/bin/env python3
"""S11 UPG-05 — final eval: run the full gate suite on the winning config and
emit the before/after metric table + cutover decision.

Reads the S10 baseline (e5-small + jina-v2) and the S11 candidate runs,
computes the full metric table (recall@5/10/20, nDCG@10, success@5, per-stratum),
and writes the cutover-vs-stay decision with deltas.
"""
from __future__ import annotations
import json, math, sys
from pathlib import Path

def _load(p): return json.loads(Path(p).read_text(encoding="utf-8"))

def _ranked(run): return [d for d,_ in sorted(run.items(), key=lambda kv: -kv[1])]

def _recall(rel, ranked, k):
    rels = {d for d,g in rel.items() if g>0}
    return sum(1 for d in ranked[:k] if d in rels)/len(rels) if rels else 0.0

def _ndcg(rel, ranked, k=10):
    dcg=sum((rel.get(d,0))/math.log2(i+2) for i,d in enumerate(ranked[:k]) if rel.get(d,0)>0)
    ideal=sorted((g for g in rel.values() if g>0),reverse=True)[:k]
    idcg=sum(g/math.log2(i+2) for i,g in enumerate(ideal,1) if g>0)
    return dcg/idcg if idcg>0 else 0.0

def _success_at_5(rel, ranked):
    """A definitive (grade-3) answer in the top-5."""
    return 1.0 if any(rel.get(d,0)>=3 for d in ranked[:5]) else 0.0

def main():
    import argparse
    ap=argparse.ArgumentParser()
    ap.add_argument("--baseline",required=True)
    ap.add_argument("--candidate",required=True)
    ap.add_argument("--golden",required=True)
    ap.add_argument("--qrels",required=True)
    ap.add_argument("--label",default="S11 final")
    ap.add_argument("--out",required=True)
    a=ap.parse_args()
    g=_load(a.golden); qrels=_load(a.qrels); base=_load(a.baseline); cand=_load(a.candidate)
    qmeta={q["id"]:q for q in g["queries"]}
    scored=sorted(set(base["runs"])&set(cand["runs"])&set(qrels))
    def metrics(run,qids):
        r5=r10=r20=ndcg=succ=0.0; n=0
        for q in qids:
            if q not in qrels or not qrels[q]: continue
            n+=1; ranked=_ranked(run.get(q) or {})
            r5+=_recall(qrels[q],ranked,5); r10+=_recall(qrels[q],ranked,10)
            r20+=_recall(qrels[q],ranked,20); ndcg+=_ndcg(qrels[q],ranked)
            succ+=_success_at_5(qrels[q],ranked)
        return {m:round(v/n,4) for m,v in [("recall@5",r5),("recall@10",r10),("recall@20",r20),("ndcg@10",ndcg),("success@5",succ)]} if n else {}
    out={"label":a.label,"baseline_system":base.get("system"),"candidate_system":cand.get("system"),
         "n_paired":len(scored),"metrics":{}}
    out["metrics"]["overall_base"]=metrics(base["runs"],scored)
    out["metrics"]["overall_cand"]=metrics(cand["runs"],scored)
    # per-stratum
    strata={}
    for st in sorted({qmeta[q]["stratum"] for q in scored}):
        st_q=[q for q in scored if qmeta[q]["stratum"]==st]
        strata[st]={"n":len(st_q),"base":metrics(base["runs"],st_q),"cand":metrics(cand["runs"],st_q)}
    out["strata"]=strata
    # decision
    bo=out["metrics"]["overall_base"]; co=out["metrics"]["overall_cand"]
    delta_r10=co.get("recall@10",0)-bo.get("recall@10",0)
    delta_succ=co.get("success@5",0)-bo.get("success@5",0)
    out["decision"]={
        "delta_recall@10":round(delta_r10,4),
        "delta_success@5":round(delta_succ,4),
        "candidate_is_better_or_equal": delta_r10>=0 and delta_succ>=0,
    }
    Path(a.out).parent.mkdir(parents=True,exist_ok=True)
    Path(a.out).write_text(json.dumps(out,indent=2)+"\n",encoding="utf-8")
    print(f"=== {a.label} ===")
    print(f"  baseline: {base.get('system')}")
    print(f"  candidate: {cand.get('system')}")
    print(f"  recall@10: {bo.get('recall@10')} -> {co.get('recall@10')} (Δ{delta_r10:+.4f})")
    print(f"  success@5: {bo.get('success@5')} -> {co.get('success@5')} (Δ{delta_succ:+.4f})")
    print(f"  candidate better-or-equal: {out['decision']['candidate_is_better_or_equal']}")
    return 0

if __name__=="__main__": raise SystemExit(main())

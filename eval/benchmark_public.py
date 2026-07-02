"""Run brain's EXACT retrieval stack on a public BEIR benchmark → leaderboard-comparable nDCG@10.

Why: our 135-query result is on a PRIVATE corpus with our own qrels, so its absolute numbers
can't be placed on a public leaderboard. This script runs the same pipeline (multilingual-e5-small
dense + FTS5 BM25, RRF fusion, jina cross-encoder rerank) over a standard BEIR dataset with the
benchmark's own queries+qrels, scoring the canonical metric (nDCG@10). The dense-only number is
directly comparable to intfloat/multilingual-e5-small's published BEIR figure (validates our
integration is on the public scale); hybrid and hybrid+rerank quantify our pipeline's lift the
same way the literature reports it.

Flat corpus ⇒ no typed zones ⇒ the zone-authority prior is a no-op here: this measures the CORE
stack, not our vault-specific tuning. Fan-out is omitted (needs an LLM to make query variants;
not part of the core retriever and not standard for BEIR).

Usage: BRAIN_EMBED_MODEL=intfloat/multilingual-e5-small BRAIN_EMBED_DIM=384 \
       BRAIN_FASTEMBED_CACHE=.fastembed_cache python eval/benchmark_public.py beir/scifact/test
"""
from __future__ import annotations
import sys, os, math, json, tempfile, shutil
from pathlib import Path

DATASET = sys.argv[1] if len(sys.argv) > 1 else "beir/scifact/test"
WORK = Path(os.environ.get("BENCH_WORK", "_evidence/s10/bench"))
WORK.mkdir(parents=True, exist_ok=True)
SAFE = DATASET.replace("/", "_")


def log(m): print(m, flush=True)


def materialize_corpus(ds, vault: Path) -> int:
    """Write each benchmark doc as one markdown note with id-frontmatter."""
    corpus = vault / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    n = 0
    for d in ds.docs_iter():
        did = d.doc_id
        title = (getattr(d, "title", "") or "").replace("\n", " ").strip()
        text = (getattr(d, "text", "") or "").strip()
        # filename safe; TRUE id kept in frontmatter (Hit.id == frontmatter id)
        fn = corpus / (f"{n:06d}.md")
        body = (title + "\n\n" + text) if title else text
        # escape frontmatter-breaking chars in title
        ttl = title.replace('"', "'") or did
        fn.write_text(
            f'---\nid: "{did}"\ntitle: "{ttl[:200]}"\ntype: source\nclassification: public\n---\n\n{body}\n',
            encoding="utf-8")
        n += 1
    return n


def ndcg_at_k(ranked_ids, rel: dict, k=10) -> float:
    dcg = 0.0
    for i, did in enumerate(ranked_ids[:k]):
        g = rel.get(did, 0)
        if g > 0:
            dcg += (2 ** g - 1) / math.log2(i + 2)
    ideal = sorted(rel.values(), reverse=True)[:k]
    idcg = sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(ideal) if g > 0)
    return dcg / idcg if idcg > 0 else 0.0


def recall_at_k(ranked_ids, rel: dict, k) -> float:
    relset = {d for d, g in rel.items() if g > 0}
    if not relset:
        return float("nan")
    return len(set(ranked_ids[:k]) & relset) / len(relset)


def main() -> int:
    import ir_datasets
    ds = ir_datasets.load(DATASET)
    # qrels (test split)
    qrels: dict[str, dict] = {}
    for qr in ds.qrels_iter():
        qrels.setdefault(qr.query_id, {})[qr.doc_id] = int(qr.relevance)
    queries = {q.query_id: q.text for q in ds.queries_iter()}
    queries = {qid: t for qid, t in queries.items() if qid in qrels}
    log(f"{DATASET}: {len(queries)} queries with qrels")

    vault = Path(tempfile.mkdtemp(prefix=f"bench_{SAFE}_", dir=str(WORK)))
    try:
        ndocs = materialize_corpus(ds, vault)
        log(f"materialized {ndocs} docs -> {vault}")

        os.environ["BRAIN_INDEX_DIR"] = str(vault / ".brain-index")
        from brain.core import BrainCore
        core = BrainCore(vault=str(vault))
        info = core.rebuild()
        log(f"indexed: {info.get('indexed')} notes, backend={info.get('backend')}, model={info.get('embed_model')}")

        idx = core.index
        # rowid -> docid (frontmatter id) for dense-only mapping
        rid2did = {int(r): str(i) for r, i in idx.conn.execute("SELECT rowid, id FROM notes")}

        K = 100
        runs = {"dense": {}, "hybrid": {}, "hybrid_rerank": {}}
        for n, (qid, qtext) in enumerate(queries.items(), 1):
            # dense-only (internal): rowids -> docids, dedup preserving order
            dense_rids, _, _ = idx._dense_ranked(qtext, K)
            seen = set(); dlist = []
            for rid in dense_rids:
                did = rid2did.get(int(rid))
                if did and did not in seen:
                    seen.add(did); dlist.append(did)
            runs["dense"][qid] = dlist
            # hybrid (RRF) and hybrid+rerank via the public API
            runs["hybrid"][qid] = [h.id for h in core.hybrid_search(qtext, k=K)]
            runs["hybrid_rerank"][qid] = [h.id for h in core.hybrid_search(qtext, k=K, rerank=True, rerank_top=20)]
            if n % 50 == 0:
                log(f"  {n}/{len(queries)} queries")

        out = {"dataset": DATASET, "n_queries": len(queries), "n_docs": ndocs,
               "model": os.environ.get("BRAIN_EMBED_MODEL"), "metrics": {}}
        for stage, run in runs.items():
            nd = sum(ndcg_at_k(run[q], qrels[q], 10) for q in queries) / len(queries)
            r10 = sum(recall_at_k(run[q], qrels[q], 10) for q in queries) / len(queries)
            r100 = sum(recall_at_k(run[q], qrels[q], 100) for q in queries) / len(queries)
            out["metrics"][stage] = {"ndcg@10": round(nd, 4), "recall@10": round(r10, 4), "recall@100": round(r100, 4)}
            log(f"  {stage:14} nDCG@10={nd:.4f}  recall@10={r10:.4f}  recall@100={r100:.4f}")
        dest = WORK / f"result_{SAFE}.json"
        dest.write_text(json.dumps(out, indent=1))
        log(f"wrote {dest}")
        return 0
    finally:
        shutil.rmtree(vault, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""S05 GATE 0 runner — score the PT capacity fixture on the CURRENT embedder.

Two independent probes per arm, because they fail for different reasons:

* ``dense``  — cosine similarity between the query vector and each whole-note
  vector, computed directly from ``get_embedder("auto")``.  No index, no
  chunker, no BM25, no RRF.  This is the embedder's raw capacity.
* ``hybrid`` — the production ``BrainIndex.hybrid_search`` over a disposable
  index built in ``$BRAIN_INDEX_DIR``.  Same embedder, plus chunking, the
  lexical leg and RRF fusion.

Scored on hit@1 and MRR (see the fixture module for why not recall@10).

Usage::

    python3 eval/pt_capacity_probe.py --fixture <dir> --out eval/runs/.../gate0.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

from pt_capacity_fixture import QUESTIONS  # noqa: E402


def _rank_of(answer_id: str, ordered_ids: list[str]) -> int | None:
    for i, nid in enumerate(ordered_ids, start=1):
        if nid == answer_id:
            return i
    return None


def _score(ranks: list[int | None]) -> dict[str, float]:
    n = len(ranks)
    return {
        "n": n,
        "hit@1": round(sum(1 for r in ranks if r == 1) / n, 4),
        "hit@3": round(sum(1 for r in ranks if r and r <= 3) / n, 4),
        "mrr": round(sum((1.0 / r) if r else 0.0 for r in ranks) / n, 4),
        "recall@10": round(sum(1 for r in ranks if r and r <= 10) / n, 4),
    }


def _note_bodies(vault: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted((vault / "brain" / "resources").glob("*.md")):
        text = p.read_text(encoding="utf-8")
        body = text.split("---", 2)[2].strip() if text.startswith("---") else text
        out[p.stem] = body
    return out


def dense_probe(vault: Path, prefer: str) -> dict:
    from brain.embed import get_embedder

    emb = get_embedder(prefer)
    bodies = _note_bodies(vault)
    ids = list(bodies)
    doc_vecs = emb.embed_batch([bodies[i] for i in ids], is_query=False)
    per_query = []
    ranks: list[int | None] = []
    for qid, text, answer in QUESTIONS:
        qv = emb.embed(text, is_query=True)
        sims = sorted(
            ((sum(a * b for a, b in zip(qv, dv)), nid) for nid, dv in zip(ids, doc_vecs)),
            reverse=True,
        )
        ordered = [nid for _, nid in sims]
        r = _rank_of(answer, ordered)
        ranks.append(r)
        per_query.append({
            "query": qid, "answer_id": answer, "rank": r,
            "top1": ordered[0], "top1_sim": round(float(sims[0][0]), 4),
            "answer_sim": round(float(next(s for s, nid in sims if nid == answer)), 4),
        })
    return {"model": getattr(emb, "model_id", "?"), "dim": getattr(emb, "dim", None),
            "metrics": _score(ranks), "per_query": per_query}


def hybrid_probe(vault: Path, index_dir: Path, prefer: str) -> dict:
    os.environ["BRAIN_INDEX_DIR"] = str(index_dir)
    from brain import config
    from brain.embed import get_embedder
    from brain.index import BrainIndex
    from brain.vectors import get_backend

    index_dir.mkdir(parents=True, exist_ok=True)
    db = config.index_path(str(vault))
    if Path(db).exists():
        Path(db).unlink()
    index = BrainIndex(db_path=db, backend=get_backend("brute-force"),
                       embedder=get_embedder(prefer))
    t0 = time.perf_counter()
    info = index.rebuild(vault)
    build_ms = round((time.perf_counter() - t0) * 1000, 1)
    per_query = []
    ranks: list[int | None] = []
    for qid, text, answer in QUESTIONS:
        hits = index.hybrid_search(text, k=22, rerank=False)
        ordered, seen = [], set()
        for h in hits:
            nid = Path(h.path).stem
            if nid not in seen:
                seen.add(nid)
                ordered.append(nid)
        r = _rank_of(answer, ordered)
        ranks.append(r)
        per_query.append({"query": qid, "answer_id": answer, "rank": r,
                          "top1": ordered[0] if ordered else None,
                          "returned": len(ordered)})
    stats = index.stats()
    return {"index": {"notes": stats.get("notes"), "chunks": stats.get("chunks"),
                      "embed_model": stats.get("embed_model"),
                      "embed_dim": stats.get("embed_dim"),
                      "backend": info.get("backend"), "build_ms": build_ms},
            "metrics": _score(ranks), "per_query": per_query}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixture", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--embedder", default="auto",
                    help="passed straight to get_embedder(); use 'hash' for the "
                         "negative control that proves this fixture can fail")
    args = ap.parse_args()
    fx = Path(args.fixture).resolve()
    result = {"probe": "s05-gate0-pt-capacity.v1",
              "embedder_preference": args.embedder,
              "fixture": json.loads((fx / "fixture.json").read_text(encoding="utf-8")),
              "arms": {}}
    for arm in ("pt", "en"):
        result["arms"][arm] = {
            "dense": dense_probe(fx / arm, args.embedder),
            "hybrid": hybrid_probe(fx / arm, fx / f"index-{arm}", args.embedder),
        }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for arm, data in result["arms"].items():
        print(f"[{arm}] dense  {data['dense']['metrics']}")
        print(f"[{arm}] hybrid {data['hybrid']['metrics']}")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

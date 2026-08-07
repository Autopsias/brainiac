#!/usr/bin/env python3
"""S05 GATE 0, part 3 — WHICH retrieval leg loses the Portuguese queries?

Parts 1-2 established that the shipped embedder handles Portuguese (rank 1,
5/5 monolingual) and that every ``monolingual_pt`` gold document is retrievable
by its own title at rank 1, yet the PT questions land around rank ~52 on the
real corpus while their English paraphrases land around rank ~13.

This probe splits the fused ranking into its two legs on the SAME read-only
index and reports where each gold document sits in each:

* ``dense``  — ``BrainIndex._dense_ranked`` alone (the embedder's own order).
* ``lexical``— the FTS/BM25 leg alone.
* ``fused``  — production ``hybrid_search`` (RRF over both).

If dense ranks the gold document far higher than fused does, the loss is in
FUSION — an empty cross-language lexical leg diluting a good dense order —
and a bigger embedder cannot recover it.

Read-only throughout; emits ranks and canonical qrel paths, never note bodies.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

from pt_stratum_diagnosis import EN_PARAPHRASE, _slug, _vault_notes  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--index-db", required=True)
    ap.add_argument("--golden", default=str(HERE / "golden_set.json"))
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    from brain.embed import get_embedder
    from brain.index import BrainIndex
    from brain.vectors import get_backend

    vault = Path(args.vault).resolve()
    index = BrainIndex(db_path=Path(args.index_db).resolve(),
                       backend=get_backend("sqlite-vec"),
                       embedder=get_embedder("auto"), read_only=True)
    notes = _vault_notes(vault)
    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    queries = [q for q in golden["queries"] if q["stratum"] == "monolingual_pt"]

    conn = index.conn

    def _rel_of_rowid(rowid: int) -> str | None:
        row = conn.execute("SELECT path FROM notes WHERE rowid=?", (rowid,)).fetchone()
        if not row:
            return None
        p = row[0]
        return str(Path(p).relative_to(vault)) if Path(p).is_absolute() else p

    def dense_order(text: str) -> list[str]:
        order, _, _, _ = index._dense_ranked(text, args.n)
        out, seen = [], set()
        for rid in order:
            rel = _rel_of_rowid(rid)
            if rel and rel not in seen:
                seen.add(rel)
                out.append(rel)
        return out

    def lexical_order(text: str) -> list[str]:
        rows = index._lexical_ranked(text, args.n)
        rowids = rows[0] if isinstance(rows, tuple) else rows
        out, seen = [], set()
        for rid in rowids:
            rel = _rel_of_rowid(int(rid))
            if rel and rel not in seen:
                seen.add(rel)
                out.append(rel)
        return out

    def fused_order(text: str) -> list[str]:
        hits = index.hybrid_search(text, k=args.n, rerank=False)
        out, seen = [], set()
        for h in hits:
            rel = (str(Path(h.path).relative_to(vault))
                   if Path(h.path).is_absolute() else h.path)
            if rel not in seen:
                seen.add(rel)
                out.append(rel)
        return out

    def rank(order: list[str], rel: str) -> int | None:
        return order.index(rel) + 1 if rel in order else None

    rows = []
    for q in queries:
        gold = {}
        for r in q["qrels"]:
            s = _slug(Path(r["path"]).stem)
            if s in notes:
                gold[notes[s][0]] = r["path"]
        pt_d, pt_l, pt_f = dense_order(q["text"]), lexical_order(q["text"]), fused_order(q["text"])
        en = EN_PARAPHRASE[q["id"]]
        en_d, en_l, en_f = dense_order(en), lexical_order(en), fused_order(en)
        rows.append({
            "query": q["id"],
            "gold": [{
                "canonical_qrel_path": gold[rel],
                "pt": {"dense": rank(pt_d, rel), "lexical": rank(pt_l, rel), "fused": rank(pt_f, rel)},
                "en": {"dense": rank(en_d, rel), "lexical": rank(en_l, rel), "fused": rank(en_f, rel)},
            } for rel in gold],
        })

    def agg(lang: str, leg: str) -> dict:
        best = []
        for row in rows:
            rs = [g[lang][leg] for g in row["gold"] if g[lang][leg]]
            best.append(min(rs) if rs else None)
        n = len(best)
        found = sorted(r for r in best if r)
        return {
            "n": n,
            "hit@1": round(sum(1 for r in best if r == 1) / n, 4),
            "recall@10": round(sum(1 for r in best if r and r <= 10) / n, 4),
            "recall@20": round(sum(1 for r in best if r and r <= 20) / n, 4),
            f"recall@{args.n}": round(len(found) / n, 4),
            "mrr": round(sum((1.0 / r) if r else 0.0 for r in best) / n, 4),
            "median_rank_when_found": found[len(found) // 2] if found else None,
        }

    result = {
        "probe": "s05-gate0-pt-leg-attribution.v1",
        "index": index.stats(),
        "n": args.n,
        "stratum": "monolingual_pt",
        "aggregate": {
            f"{lang}_{leg}": agg(lang, leg)
            for lang in ("pt", "en") for leg in ("dense", "lexical", "fused")
        },
        "per_query": rows,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, m in result["aggregate"].items():
        print(f"{name:12s} {m}")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

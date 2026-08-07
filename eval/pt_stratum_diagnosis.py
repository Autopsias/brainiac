#!/usr/bin/env python3
"""S05 GATE 0, part 2 — WHY is ``monolingual_pt`` exactly 0.000 on the real
reference corpus, if the shipped embedder demonstrably does Portuguese?

Gate 0's fixture (``eval/pt_capacity_probe.py``) shows ``multilingual-e5-small``
answering monolingual-PT questions at rank 1 (5/5) and PT->EN cross-lingual
questions at hit@1 0.8 / MRR 0.90 at the embedder level.  So the exact zero on
the golden set is not an embedder-capacity signature.  This probe reads the
REAL, already-published index strictly read-only and asks three questions:

1.  **Where do the gold documents actually land?**  Deep rank (k=200), not the
    top-10 window, so "absent from the corpus" is distinguishable from
    "retrieved but ranked 40th".
2.  **Is it the QUERY LANGUAGE?**  The same 12 questions are re-asked as
    English paraphrases.  If English scores the same near-zero, language is not
    the fault.
3.  **Is it the QRELS?**  A control probe asks each gold document's own TITLE
    verbatim.  If a document cannot be retrieved by its exact title, it is not
    reachable by any query and the qrel is the problem, not the embedder.

Never writes: opened ``read_only=True`` (``mode=ro`` + ``PRAGMA query_only``).
Emits ranks and canonical qrel paths only — never note bodies.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))

# English paraphrases of the 12 monolingual_pt questions, written to preserve
# the question's intent without borrowing the gold document's title wording.
EN_PARAPHRASE = {
    "pt_01": "What does the RetainedCo IT strategy say?",
    "pt_02": "What was decided about the Carlos Mendes succession?",
    "pt_03": "What is the agentic operating model?",
    "pt_04": "Where is the Tier 2 greenfield choice analysis between Acme and Northwind?",
    "pt_05": "What was decided about the carve-out and the technology separation?",
    "pt_06": "What is shERPa and what does the audit say about it?",
    "pt_07": "What are the Day 1 requirements for the RetailCo integration?",
    "pt_08": "What is Acme's relationship with Northwind on the Meridian project?",
    "pt_09": "What does the meeting with Northwind say about the carve-out?",
    "pt_10": "What is the decision on the third entity model?",
    "pt_11": "What is the three-tier architecture?",
    "pt_12": "What is the scope of the PMO defined by McKinsey?",
}


def _slug(name: str) -> str:
    name = re.sub(r"\.md$", "", name)
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c)).lower()
    n = re.sub(r"['’`]", "", n)
    n = re.sub(r"[^a-z0-9]+", "-", n)
    return re.sub(r"-+", "-", n).strip("-")


def _vault_notes(vault: Path) -> dict[str, tuple[str, str]]:
    """{note_id: (relative path, title)} over the indexed zones."""
    out: dict[str, tuple[str, str]] = {}
    for zone in ("brain", "raw"):
        for p in sorted((vault / zone).rglob("*.md")):
            rel = p.relative_to(vault).as_posix()
            if rel.startswith("raw/originals/"):
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            if not text.startswith("---"):
                continue
            fm = text.split("---", 2)[1]
            mid = re.search(r"^id:\s*(.+)$", fm, re.M)
            mti = re.search(r"^title:\s*(.+)$", fm, re.M)
            if mid:
                out[mid.group(1).strip().strip("\"'")] = (
                    rel, (mti.group(1).strip().strip("\"'") if mti else ""),
                )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--index-db", required=True)
    ap.add_argument("--golden", default=str(HERE / "golden_set.json"))
    ap.add_argument("--deep-k", type=int, default=200)
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

    def ranks_for(text: str, gold_rels: list[str]) -> dict[str, int | None]:
        hits = index.hybrid_search(text, k=args.deep_k, rerank=False)
        order: list[str] = []
        seen: set[str] = set()
        for h in hits:
            rel = h.path if not Path(h.path).is_absolute() else str(Path(h.path).relative_to(vault))
            if rel not in seen:
                seen.add(rel)
                order.append(rel)
        return {g: (order.index(g) + 1 if g in order else None) for g in gold_rels}

    rows = []
    for q in queries:
        gold: dict[str, str] = {}
        for r in q["qrels"]:
            s = _slug(Path(r["path"]).stem)
            if s in notes:
                gold[notes[s][0]] = r["path"]
        gold_rels = list(gold)
        pt = ranks_for(q["text"], gold_rels)
        en = ranks_for(EN_PARAPHRASE[q["id"]], gold_rels)
        title_ctl = {}
        for rel in gold_rels:
            nid = next(k for k, v in notes.items() if v[0] == rel)
            title_ctl[rel] = ranks_for(notes[nid][1] or nid, [rel])[rel]
        rows.append({
            "query": q["id"],
            "gold": [{
                "canonical_qrel_path": gold[rel],
                "vault_path": rel,
                "rank_pt_query": pt[rel],
                "rank_en_paraphrase": en[rel],
                "rank_own_title_control": title_ctl[rel],
            } for rel in gold_rels],
        })

    def agg(key: str) -> dict[str, float]:
        best = []
        for row in rows:
            rs = [g[key] for g in row["gold"] if g[key]]
            best.append(min(rs) if rs else None)
        n = len(best)
        return {
            "n": n,
            "hit@1": round(sum(1 for r in best if r == 1) / n, 4),
            "recall@10": round(sum(1 for r in best if r and r <= 10) / n, 4),
            "recall@20": round(sum(1 for r in best if r and r <= 20) / n, 4),
            f"recall@{args.deep_k}": round(sum(1 for r in best if r) / n, 4),
            "mrr": round(sum((1.0 / r) if r else 0.0 for r in best) / n, 4),
            "median_rank_when_found": (
                sorted(r for r in best if r)[len([r for r in best if r]) // 2]
                if any(best) else None
            ),
        }

    result = {
        "probe": "s05-gate0-pt-stratum-diagnosis.v1",
        "index": index.stats(),
        "deep_k": args.deep_k,
        "stratum": "monolingual_pt",
        "gold_document_language": "English (measured separately; see the readout)",
        "aggregate": {
            "pt_query": agg("rank_pt_query"),
            "en_paraphrase": agg("rank_en_paraphrase"),
            "own_title_control": agg("rank_own_title_control"),
        },
        "per_query": rows,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, m in result["aggregate"].items():
        print(f"{name:20s} {m}")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

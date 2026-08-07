#!/usr/bin/env python3
"""S05 GATE 0, part 4 — is the shipped anti-burial prior actually ARMED?

``BrainIndex._hybrid_search_impl`` carries the RET-01/RET-01b zone-authority
prior: a multiplicative boost for curated notes reached only through the dense
leg — written specifically to rescue cross-lingually buried canonical notes.
Two facts make it a no-op on the live reference corpus:

* ``BrainIndex._DEFAULT_ZONE_WEIGHTS`` is ``{}`` — every zone weighs 1.0
  unless ``$BRAIN_ZONE_WEIGHTS`` is set.
* ``_resolve_zone`` keys on each note's ``source_zone:`` frontmatter, and
  0 of the live vault's 2,570 INDEXED notes carry that field, so it falls back
  to the flattened ``notes.zone`` column, whose only values are ``brain`` /
  ``raw``. (An earlier "3,589" here named no population and was wrong; the
  count is the indexed zones ``brain/`` + ``raw/`` minus ``raw/originals/``.)

This probe sweeps ``$BRAIN_ZONE_WEIGHTS`` over the flattened zones and
re-measures the ``monolingual_pt`` stratum through the PRODUCTION fused path
on the read-only reference index.  It ships no default change — it measures
whether the dormant mechanism, when armed, moves the number that BR-02 was
about to buy a bigger embedder to move.

Read-only; emits ranks and canonical qrel paths, never note bodies.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

from pt_stratum_diagnosis import _slug, _vault_notes  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--index-db", required=True)
    ap.add_argument("--golden", default=str(HERE / "golden_set.json"))
    ap.add_argument("--weights", nargs="*", type=float,
                    default=[1.0, 1.25, 1.5, 2.0, 3.0],
                    help="brain-zone multipliers to sweep (raw stays 1.0)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    from brain.embed import get_embedder
    from brain.index import BrainIndex
    from brain.vectors import get_backend

    vault = Path(args.vault).resolve()
    notes = _vault_notes(vault)
    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))

    def gold_for(q) -> dict[str, str]:
        out = {}
        for r in q["qrels"]:
            s = _slug(Path(r["path"]).stem)
            if s in notes:
                out[notes[s][0]] = r["path"]
        return out

    strata = ["monolingual_pt", "monolingual_es", "cross_lingual_en_pt",
              "cross_lingual_en_es", "lexical_identifier", "multi_hop", "temporal"]

    def measure(weight: float) -> dict:
        os.environ["BRAIN_ZONE_WEIGHTS"] = json.dumps({"brain": weight, "raw": 1.0})
        index = BrainIndex(db_path=Path(args.index_db).resolve(),
                           backend=get_backend("sqlite-vec"),
                           embedder=get_embedder("auto"), read_only=True)
        per_stratum: dict[str, list[int | None]] = {s: [] for s in strata}
        for q in golden["queries"]:
            gold = gold_for(q)
            if not gold:
                per_stratum[q["stratum"]].append(None)
                continue
            hits = index.hybrid_search(q["text"], k=20, rerank=False)
            order, seen = [], set()
            for h in hits:
                rel = (str(Path(h.path).relative_to(vault))
                       if Path(h.path).is_absolute() else h.path)
                if rel not in seen:
                    seen.add(rel)
                    order.append(rel)
            rs = [order.index(g) + 1 for g in gold if g in order]
            per_stratum[q["stratum"]].append(min(rs) if rs else None)
        index.close() if hasattr(index, "close") else None

        def score(ranks: list[int | None]) -> dict:
            n = len(ranks)
            return {
                "n": n,
                "hit@1": round(sum(1 for r in ranks if r == 1) / n, 4),
                "recall@10": round(sum(1 for r in ranks if r and r <= 10) / n, 4),
                "recall@20": round(sum(1 for r in ranks if r and r <= 20) / n, 4),
                "mrr@10": round(sum((1.0 / r) if r and r <= 10 else 0.0 for r in ranks) / n, 4),
                "queries_found_at_10": sorted(
                    i for i, r in enumerate(ranks) if r and r <= 10
                ),
            }

        allr = [r for s in strata for r in per_stratum[s]]
        out = {s: score(per_stratum[s]) for s in strata}
        out["overall"] = score(allr)
        return out

    result = {
        "probe": "s05-gate0-pt-zone-prior.v1",
        "note": "BRAIN_ZONE_WEIGHTS sweep only; no shipped default is changed",
        "sweep": {},
    }
    for w in args.weights:
        result["sweep"][f"brain={w}"] = measure(w)
        m = result["sweep"][f"brain={w}"]
        print(f"brain={w:<5} pt {m['monolingual_pt']['recall@10']:.4f} "
              f"es {m['monolingual_es']['recall@10']:.4f} "
              f"ident {m['lexical_identifier']['recall@10']:.4f} "
              f"overall {m['overall']['recall@10']:.4f} "
              f"overall_mrr {m['overall']['mrr@10']:.4f}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

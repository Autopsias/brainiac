#!/usr/bin/env python3
"""S03 (PWR-01/PWR-02) — pool judging candidates from EACH RETRIEVAL LEG,
UNFUSED, so new qrels are not biased toward whatever the shipped fusion
already surfaces.

Why this file exists at all
---------------------------
Judging a new query only against the fused pipeline's output builds a qrel set
that is systematically biased against any system that did not contribute to the
pool (Buettcher et al. 2007; Yilmaz et al., SIGIR 2020) — which for this plan
would UNDERSTATE exactly the fusion improvement it is trying to measure.

The nearest existing surface, ``TraceRecord.compact_digest``
(``src/brain/index.py``), cannot be used for this: it documents verbatim that
"IDs are filtered to already-surfaced results before any rank is materialised",
so every per-leg list it emits is a SUBSET of the fused pool (bounded further by
``per_leg_limit = 20``). Using it would relabel the fused pool as per-leg
provenance — paperwork saying the bias was removed while the bias remained.

So this tool calls the three ranking legs DIRECTLY and UNFUSED, at a depth the
fusion never bounds:

  * BM25   -> ``BrainIndex._lexical_ranked``  (FTS5 ``ORDER BY rank``)
  * dense  -> ``BrainIndex._dense_ranked``    (vector index, lazily embedded)
  * exact  -> ``BrainIndex._exact_leg``       (alias / title / title-phrase)
  * fused  -> ``BrainIndex.hybrid_search``    (the shipped ranking, for contrast)

and emits the UNION plus, per leg, which candidates that leg found and no other
leg did. A leg contributing zero unique candidates across a whole query set is
the signature of a fused pool wearing per-leg labels — that is the known-positive
check for the pooling itself, and it is computed here rather than asserted.

Read-only: opens the published snapshot with ``mode=ro``; never takes the vault
writer lock, never writes to the vault.

Usage
-----
    python3 eval/leg_capture.py \\
        --db "<vault>/.brain/snapshot/index.snapshot.sqlite" \\
        --queries /path/to/queries.json \\
        --source-vault "$EVAL_SOURCE_VAULT" \\
        --depth 50 --out /path/to/pool.json

``--queries`` is either a golden-set file (``{"queries":[{"id","text",...}]}``)
or a bare list of ``{"id","text"}``.
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

LEGS = ("bm25", "dense", "exact", "fused")


def _slugify(name: str) -> str:
    """The corpus migration's deterministic filename resolver (copied from
    eval/s02_established_path_map.py so both agree on the join key)."""
    name = re.sub(r"\.md$", "", name)
    normalized = unicodedata.normalize("NFKD", name)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.lower()
    normalized = re.sub(r"['’`]", "", normalized)
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
    return re.sub(r"-+", "-", normalized).strip("-")


def source_path_index(source_vault: Path) -> dict[str, str]:
    """slug(stem) -> canonical source-vault-relative path.

    The canonical qrel key is the SOURCE vault path; the index stores the
    migrated brain path whose note id is that slug.

    51 of 1836 slugs are ambiguous, and the ambiguity has one shape: the
    canonical document sits at the top of its zone (``50 Sources/<Title>.md``)
    and export copies of it sit one level down (``cowork_outputs/``,
    ``final_deliverables/``, ``presentations/``, ``pdfs/``). The existing
    golden-set qrels all key on the top-level form, so SHALLOWEST-PATH-WINS
    reproduces the convention already in use rather than guessing; ties break
    lexicographically so the map is deterministic.
    """
    seen: dict[str, list[str]] = {}
    for p in source_vault.rglob("*.md"):
        rel = p.relative_to(source_vault).as_posix()
        if rel.startswith(("_archive/", "tmp/", "99 Workspace/")):
            continue
        seen.setdefault(_slugify(p.stem), []).append(rel)
    return {k: sorted(v, key=lambda r: (r.count("/"), r))[0] for k, v in seen.items()}


def open_index(db: Path, embedder: str, backend: str):
    from brain.embed import get_embedder
    from brain.index import BrainIndex
    from brain.vectors import get_backend

    return BrainIndex(
        db_path=db,
        backend=get_backend(backend),
        embedder=get_embedder(embedder),
        read_only=True,
    )


def capture(index, queries: list[dict], slug2src: dict[str, str], depth: int) -> dict:
    out: dict[str, dict] = {}
    for q in queries:
        legs: dict[str, list[int]] = {}
        legs["bm25"] = index._lexical_ranked(q["text"], depth)
        legs["dense"] = index._dense_ranked(q["text"], depth)[0]
        legs["exact"] = list(index._exact_leg(q["text"], 60).ranked)
        fused_hits = index.hybrid_search(q["text"], k=depth)
        fused_rowids: list[int] = []
        for h in fused_hits:
            row = index.conn.execute(
                "SELECT rowid FROM notes WHERE id = ?", (h.id,)
            ).fetchone()
            if row:
                fused_rowids.append(int(row[0]))
        legs["fused"] = fused_rowids

        meta: dict[int, dict] = {}
        for rid in {r for v in legs.values() for r in v}:
            row = index.conn.execute(
                "SELECT id, title, path FROM notes WHERE rowid = ?", (rid,)
            ).fetchone()
            if not row:
                continue
            note_id = str(row[0])
            meta[rid] = {
                "note_id": note_id,
                "title": row[1],
                "brain_path": row[2],
                # note id IS the migration slug, so it joins straight back to
                # the canonical source path.
                "source_path": slug2src.get(note_id),
            }

        unique = {
            leg: sorted(
                set(legs[leg]) - {r for other in LEGS if other != leg for r in legs[other]}
            )
            for leg in LEGS
        }
        out[q["id"]] = {
            "text": q["text"],
            "legs": {leg: legs[leg] for leg in LEGS},
            "unique_by_leg": {leg: unique[leg] for leg in LEGS},
            "unique_counts": {leg: len(unique[leg]) for leg in LEGS},
            "pool_size": len({r for v in legs.values() for r in v}),
            "meta": {str(k): v for k, v in meta.items()},
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--queries", required=True)
    ap.add_argument("--source-vault", required=True)
    ap.add_argument("--depth", type=int, default=50)
    ap.add_argument("--embedder", default="auto")
    ap.add_argument("--vector-backend", default="auto")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    doc = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    queries = doc["queries"] if isinstance(doc, dict) else doc

    index = open_index(Path(args.db).resolve(), args.embedder, args.vector_backend)
    if not index.model_matches():
        print("ABORT: live embedder does not match the indexed vectors — the "
              "dense leg would be noise, so per-leg pooling is meaningless.",
              file=sys.stderr)
        return 2

    slug2src = source_path_index(Path(args.source_vault).expanduser().resolve())
    per_query = capture(index, queries, slug2src, args.depth)

    totals = {leg: sum(v["unique_counts"][leg] for v in per_query.values()) for leg in LEGS}
    result = {
        "schema_version": "s03-leg-pool.v1",
        "db": str(Path(args.db).resolve()),
        "depth": args.depth,
        "embed_model": index.conn.execute(
            "SELECT v FROM meta WHERE k='embed_model'").fetchone()[0],
        "n_queries": len(per_query),
        "unique_totals_by_leg": totals,
        "source_slug_entries": len(slug2src),
        "queries": per_query,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"out": args.out, "n": len(per_query),
                      "unique_totals_by_leg": totals}, indent=1))
    # KNOWN-POSITIVE for the pooling itself: if a leg never contributes a
    # unique candidate, the "pool" is the fused list wearing per-leg labels.
    dead = [leg for leg in ("bm25", "dense") if totals[leg] == 0]
    if dead:
        print(f"WARNING: leg(s) {dead} contributed ZERO unique candidates — "
              "the per-leg pooling did not happen.", file=sys.stderr)
        return 3
    return 0


def _selfcheck() -> int:
    """`python3 eval/leg_capture.py --selfcheck` — the shallowest-path tie-break is
    the one non-obvious rule here (it decides which of several same-stem files
    becomes the canonical qrel key), so it gets a check that fails if it breaks."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for rel in ("50 Sources/Northwind IT Workplan v2.md",
                    "50 Sources/presentations/northwind_it_workplan_v2.md",
                    "50 Sources/final_deliverables/northwind_it_workplan_v2.md",
                    "_archive/Ignored.md"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("x", encoding="utf-8")
        m = source_path_index(root)
        assert m["northwind-it-workplan-v2"] == "50 Sources/Northwind IT Workplan v2.md", m
        assert "ignored" not in m, m
        assert _slugify("Audit 2024-011 SAMPLE") == "audit-2024-011-sample"
    print("selfcheck ok")
    return 0


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        raise SystemExit(_selfcheck())
    raise SystemExit(main())

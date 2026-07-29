#!/usr/bin/env python3
"""AUT-04 — capture ONE brain hybrid-search run over the golden set, with the
four knobs the autoresearch skill (.claude/skills/autoresearch/) tunes exposed
as first-class flags: ``--rrf-k``, ``--rerank``/``--no-rerank``,
``--rerank-top``, ``-k``.

Trimmed, parameterised sibling of ``_archive/eval/capture_brain_run.py``
(session s05's frozen-SC-vs-brain A/B) — same run-file schema (so
``eval/harness_direct.py`` + ``eval/gate.py`` consume the output unchanged),
same ``BrainCore.hybrid_search`` call, but only the hybrid/hybrid-rerank path
survives (grep/agentic modes belonged to the retired SC comparison, not
parameter self-tuning) and rrf_k/rerank_top are runtime flags instead of
whatever ``BrainCore.hybrid_search``'s own defaults happen to be.

Usage (one capture per parameter set — call it once for "current", once for
"candidate" each autoresearch iteration):

    python3 eval/capture_run.py --golden eval/golden_set.json \\
        --vault "${BRAIN_VAULT:-vault}" --rrf-k 60 --rerank-top 15 -k 20 \\
        --system baseline --out eval/runs/autoresearch-current.json

Pass --rebuild once per vault session if the index hasn't been built with
vector support yet (``no such table: vec_index`` means this).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

import path_normalize as pn  # noqa: E402


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--vault", default=os.environ.get("BRAIN_VAULT", "vault"),
                    help="vault to retrieve over (default: $BRAIN_VAULT or ./vault)")
    ap.add_argument("--system", required=True, help="run label, e.g. 'current' or 'rrf40'")
    ap.add_argument("--out", required=True)
    ap.add_argument("-k", type=int, default=20,
                    help="agentic-budget default (matches gate.py --metric recall@20)")
    ap.add_argument("--rrf-k", type=int, default=60)
    ap.add_argument("--rerank", dest="rerank", action="store_true", default=False)
    ap.add_argument("--no-rerank", dest="rerank", action="store_false")
    ap.add_argument("--rerank-top", type=int, default=15)
    ap.add_argument("--warmup", type=int, default=0,
                    help="unmeasured searches per query before timing (default: 0)")
    ap.add_argument("--samples", type=int, default=1,
                    help="measured searches per query; latency stores their median (default: 1)")
    ap.add_argument("--map", default=None,
                    help="optional JSON {brain_rel_path: canonical_source_path}")
    ap.add_argument("--source-root", default=None,
                    help="optional source tree to import brain from (for clean A/B baselines)")
    ap.add_argument("--derived-index-only", action="store_true",
                    help="build/query only the disposable BRAIN_INDEX_DIR index; never "
                         "take the vault's host writer lock (for read-only eval corpora)")
    ap.add_argument("--index-db", default=None,
                    help="optional prebuilt SQLite index to query directly; requires "
                         "--derived-index-only and is never rebuilt")
    ap.add_argument("--read-only-index", action="store_true",
                    help="open --index-db mode=ro so a captured snapshot cannot be mutated")
    ap.add_argument("--embedder", default="hash",
                    help="embedder passed to BrainIndex in derived-index mode "
                         "(default: hash; use auto only for a compatible prebuilt snapshot)")
    ap.add_argument("--vector-backend", default="brute-force",
                    choices=["auto", "brute-force", "sqlite-vec"],
                    help="vector backend passed to BrainIndex in derived-index mode")
    ap.add_argument("--rebuild", action="store_true",
                    help="rebuild the index before capture (needed once if the "
                         "vault's index predates vector support)")
    args = ap.parse_args()
    if args.warmup < 0:
        ap.error("--warmup must be >= 0")
    if args.samples < 1:
        ap.error("--samples must be >= 1")
    if args.index_db and not args.derived_index_only:
        ap.error("--index-db requires --derived-index-only")
    if args.read_only_index and not args.index_db:
        ap.error("--read-only-index requires --index-db")
    if args.index_db and args.rebuild:
        ap.error("--index-db cannot be combined with --rebuild")

    source_root = Path(args.source_root).resolve() if args.source_root else REPO / "src"
    sys.path.insert(0, str(source_root))
    if args.derived_index_only:
        if args.index_db and not Path(args.index_db).is_file():
            ap.error(f"--index-db does not exist: {args.index_db}")
        # Evaluation corpora can be intentionally mounted read-only.  BrainCore
        # correctly takes a host writer lock before a production rebuild, but
        # that lock belongs under the canonical vault and is neither needed nor
        # permitted for a disposable benchmark index.  Build the derived cache
        # directly instead: input notes remain read-only and BRAIN_INDEX_DIR
        # remains the sole write target.
        from brain import config
        from brain.embed import get_embedder
        from brain.index import BrainIndex
        from brain.vectors import get_backend
    else:
        from brain.core import BrainCore

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    qmeta = {q["id"]: q for q in golden["queries"]}
    mapping: dict[str, str] | None = None
    if args.map:
        mapping_document = json.loads(Path(args.map).read_text(encoding="utf-8"))
        # S02's owner-vault mapping is deliberately evidence-bearing: alongside
        # the actual {migrated path -> canonical qrel path} projection it
        # records its resolution/coverage proof.  Continue accepting the
        # original bare map form so ordinary captures stay backward-compatible.
        candidate = mapping_document.get("mapping") if isinstance(mapping_document, dict) else None
        if candidate is None:
            candidate = mapping_document
        if not isinstance(candidate, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in candidate.items()
        ):
            ap.error("--map must be a JSON string-to-string map or contain a string-to-string 'mapping' object")
        mapping = candidate

    vault_root = str(Path(args.vault).resolve())
    index = None
    if args.derived_index_only:
        db_path = (Path(args.index_db).resolve()
                   if args.index_db else config.index_path(vault_root))
        index = BrainIndex(
            db_path=db_path,
            backend=get_backend(args.vector_backend),
            embedder=get_embedder(args.embedder),
            read_only=args.read_only_index,
        )
        if args.rebuild:
            info = index.rebuild(Path(vault_root))
            print(f"rebuilt: {info.get('indexed')} notes, backend={info.get('backend')}, "
                  f"model={info.get('embed_model')}")
        search = index.hybrid_search
        index_state = {
            "index": index.stats(),
            "prebuilt_index": str(db_path) if args.index_db else None,
            "read_only_index": args.read_only_index,
        }
    else:
        core = BrainCore(vault=vault_root)
        if args.rebuild:
            info = core.rebuild()
            print(f"rebuilt: {info.get('indexed')} notes, backend={info.get('backend')}, "
                  f"model={info.get('embed_model')}")
        search = core.hybrid_search
        try:
            status = core.status()
            index_state = {"index": status.get("index")}
        except Exception:
            index_state = {}

    params = {"rrf_k": args.rrf_k, "rerank": args.rerank, "rerank_top": args.rerank_top}
    index_state.update({"params": params, "vault": vault_root, "source_root": str(source_root)})

    runs: dict[str, dict[str, float]] = {}
    latency: dict[str, float] = {}

    for qid, q in qmeta.items():
        for _ in range(args.warmup):
            search(q["text"], k=args.k, rerank=args.rerank,
                   rerank_top=args.rerank_top, rrf_k=args.rrf_k)
        samples: list[float] = []
        hits = []
        for _ in range(args.samples):
            t0 = time.perf_counter()
            hits = search(q["text"], k=args.k, rerank=args.rerank,
                          rerank_top=args.rerank_top, rrf_k=args.rrf_k)
            samples.append((time.perf_counter() - t0) * 1000.0)
        latency[qid] = round(statistics.median(samples), 2)

        doc_scores: dict[str, float] = {}
        for h in hits:
            rel = os.path.relpath(h.path, vault_root) if os.path.isabs(h.path) else h.path
            # ``src`` is the frozen canonical key, often a pre-migration path
            # that no longer exists on disk.  Temporal state belongs to the
            # *physical migrated note* (``rel``), then the result is emitted
            # under its canonical qrel key.  Resolving after mapping made every
            # migrated temporal note fall through to a path-date guess.
            src = pn.normalize(rel, mapping)
            if q.get("stratum") == "temporal":
                vstate, _ = pn.resolve_version(rel, vault_root)
                src = f"{src}#{vstate}"
            if src not in doc_scores or h.score > doc_scores[src]:
                doc_scores[src] = h.score
        runs[qid] = doc_scores

    out = {
        "system": args.system,
        "captured": _iso(),
        "index_state": index_state,
        "k": args.k,
        "runs": runs,
        "latency_ms": latency,
        "timing": {
            "warmup_per_query": args.warmup,
            "samples_per_query": args.samples,
            "statistic": "median",
        },
        "scope": {"queries_captured": sorted(runs), "n": len(runs),
                  "egress": "retrieval-primitive (no egress filter)",
                  "mapped": bool(mapping)},
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    print(f"captured {len(runs)} queries [{args.system}] rrf_k={args.rrf_k} "
          f"rerank={args.rerank} rerank_top={args.rerank_top} k={args.k} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

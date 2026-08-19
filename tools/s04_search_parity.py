"""Produce S04 golden-query search-parity evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from brain import __version__ as brain_version
from brain import classification as cls
from brain.cli_cmds import retrieval
from brain.core import BrainCore
from brain.rerank import NoopReranker, _resolve_reranker_model


MODES = ("rerank", "no_rerank")
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLDEN = REPO_ROOT / "eval/golden_set.json"
DEFAULT_VAULT = REPO_ROOT / "_workspace/live-vault"
S01_BASELINE = Path("_evidence/quality-v2/search-parity-baseline.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _queries(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload["queries"] if isinstance(payload, dict) else payload
    return [
        {"query_id": row["id"], "query": row["text"]}
        for row in rows
        if "held_out_v1" in row
    ]


def _index_fingerprint(index_dir: Path, vault: Path) -> dict[str, Any]:
    db_path = index_dir / "index.sqlite"
    stat = db_path.stat()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        notes = int(conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
        chunks = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        vault_fingerprint = conn.execute(
            "SELECT v FROM meta WHERE k='vault_fingerprint'"
        ).fetchone()
    finally:
        conn.close()
    return {
        "vault": str(vault),
        "notes": notes,
        "chunks": chunks,
        "vault_fingerprint": vault_fingerprint[0] if vault_fingerprint else None,
        "index_file": {
            "path": str(db_path),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "mtime_iso": dt.datetime.fromtimestamp(
                stat.st_mtime, tz=dt.timezone.utc
            ).isoformat(),
        },
    }


def _args(query: str, *, rerank: bool) -> SimpleNamespace:
    return SimpleNamespace(
        query=query,
        variant=None,
        k=20,
        rerank=rerank,
        rerank_top=20,
        rrf_k=60,
        rerank_gate=None,
        rerank_fused=None,
        explain=rerank,
        cmd="search",
        max_tier=cls.DEFAULT_MAX_TIER,
    )


def _capture_mode(core: BrainCore, query: str, *, rerank: bool) -> dict[str, Any]:
    args = _args(query, rerank=rerank)
    hits, trace, fanout = retrieval._search_hits(args, core, False)
    assert fanout is None
    surfaced, report = retrieval._filter_dicts(hits, args.max_tier)
    redacted_ids = core.annotate_create_safety(query, surfaced, args.max_tier)
    retrieval._annotate_explain(args, trace, surfaced, redacted_ids)
    scores = [
        {"id": hit["id"], "score": round(float(hit["score"]), 6)}
        for hit in surfaced
    ]
    ranking = None
    if trace is not None:
        ranking = {
            "rrf_k": trace.rrf_k,
            "exact_leg_enabled": trace.exact_leg_enabled,
            "rerank_requested": trace.rerank_requested,
            "rerank_applied": trace.rerank_applied,
            "rerank_gate": trace.rerank_gate,
            "family_collapse": trace.family_collapse,
        }
    return {
        "ids": [hit["id"] for hit in surfaced],
        "scores": scores,
        "ranking": ranking,
        "egress": report,
    }


def capture(output: Path, golden: Path, vault: Path) -> None:
    index_dir = Path(os.environ["BRAIN_INDEX_DIR"])
    query_rows = _queries(golden)
    core = BrainCore(vault=vault)
    # S01's completed lane captured the rerank-requested arm only after the
    # real cross-encoder had timed out and the engine's identity fallback was
    # cached. Reproduce that measured steady state directly; waiting for a
    # non-cancellable ONNX worker here would alter latency, not ranking.
    core.index._reranker_cache = (_resolve_reranker_model(), NoopReranker())
    captured: list[dict[str, Any]] = []
    try:
        for number, row in enumerate(query_rows, start=1):
            modes = {
                "rerank": _capture_mode(core, row["query"], rerank=True),
                "no_rerank": _capture_mode(core, row["query"], rerank=False),
            }
            captured.append({**row, "modes": modes})
            print(f"captured {number}/{len(query_rows)} {row['query_id']}", file=sys.stderr)
    finally:
        core.index.close()
    payload = {
        "engine_lane": {
            "python": sys.executable,
            "brain_version": brain_version,
            "brain_module": str(Path(__file__).resolve().parents[1] / "src/brain"),
            "environment": {
                name: os.environ.get(name)
                for name in (
                    "BRAIN_INDEX_DIR",
                    "BRAIN_EMBEDDER",
                    "BRAIN_EMBED_MODEL",
                    "BRAIN_MODEL_CACHE",
                    "BRAIN_QUERY_CAPTURE_ENABLED",
                    "PYTHONPATH",
                )
            },
            "production_path": (
                "BrainCore.hybrid_search[_with_trace] -> CLI retrieval egress -> "
                "annotate_create_safety"
            ),
            "rerank_fallback": (
                "S01 steady-state NoopReranker cached after its first real-model "
                "timeout; rerank_requested=true and rerank_applied=false"
            ),
        },
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "corpus_fingerprint": _index_fingerprint(index_dir, vault),
        "golden_set": {
            "path": str(golden),
            "sha256": _sha256(golden),
            "query_count": len(query_rows),
            "selection": "held_out_v1 key present",
        },
        "queries": captured,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _score_map(rows: list[dict[str, Any]]) -> dict[str, float]:
    return {str(row["id"]): round(float(row["score"]), 6) for row in rows}


def _classify(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_ids = list(before["ids"])
    after_ids = list(after["ids"])
    before_scores = list(before["scores"])
    after_scores = list(after["scores"])
    identical = before_ids == after_ids and before_scores == after_scores
    common_ids = set(before_ids) & set(after_ids)
    moved_ids = {
        note_id
        for note_id in common_ids
        if before_ids.index(note_id) != after_ids.index(note_id)
    }
    before_map = _score_map(before_scores)
    after_map = _score_map(after_scores)
    tie_scores = sorted(
        {
            score
            for note_id in moved_ids
            for score in (before_map.get(note_id), after_map.get(note_id))
            if score is not None
        }
    )
    unchanged_set = set(before_ids) == set(after_ids) and len(before_ids) == len(after_ids)
    same_scores = before_map == after_map
    tie_reorder = (
        not identical
        and unchanged_set
        and same_scores
        and bool(moved_ids)
        and len(tie_scores) == 1
    )
    return {
        "before_ids": before_ids,
        "after_ids": after_ids,
        "before_scores": before_scores,
        "after_scores": after_scores,
        "identical": identical,
        "class": "identical" if identical else "tie_reorder" if tie_reorder else "regression",
        **(
            {
                "tie_reorder": {
                    "unchanged_top20_id_set": unchanged_set,
                    "moved_ids": sorted(moved_ids),
                    "quantised_scores": tie_scores,
                }
            }
            if tie_reorder
            else {}
        ),
    }


def _s01_ids(row: dict[str, Any], mode: str) -> list[str]:
    return list(row[mode])


def _corpus_drift(before: dict[str, Any]) -> dict[str, Any]:
    baseline = json.loads(S01_BASELINE.read_text(encoding="utf-8"))
    old = {row["query_id"]: row for row in baseline["queries"]}
    changed = {mode: [] for mode in MODES}
    for row in before["queries"]:
        for mode in MODES:
            if row["modes"][mode]["ids"] != _s01_ids(old[row["query_id"]], mode):
                changed[mode].append(row["query_id"])
    return {
        "blocking": False,
        "interpretation": "signal_only",
        "s01_corpus_fingerprint": baseline["corpus_fingerprint"],
        "s04_before_corpus_fingerprint": before["corpus_fingerprint"],
        "changed_query_counts": {mode: len(ids) for mode, ids in changed.items()},
        "changed_query_ids": changed,
    }


def compare(before_path: Path, after_path: Path, output: Path) -> None:
    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = json.loads(after_path.read_text(encoding="utf-8"))
    after_by_id = {row["query_id"]: row for row in after["queries"]}
    summary = {mode: {"identical": 0, "tie_reorder": 0, "regressions": 0} for mode in MODES}
    query_results = []
    for row in before["queries"]:
        after_row = after_by_id[row["query_id"]]
        modes = {}
        for mode in MODES:
            result = _classify(row["modes"][mode], after_row["modes"][mode])
            modes[mode] = result
            summary[mode]["regressions" if result["class"] == "regression" else result["class"]] += 1
        query_results.append(
            {"query_id": row["query_id"], "query": row["query"], "modes": modes}
        )
    for mode in MODES:
        counts = summary[mode]
        counts["N"] = len(query_results)
        counts["reconciles"] = counts["identical"] + counts["tie_reorder"] == counts["N"]
    payload = {
        "contract": "regressions: 0 and identical + tie_reorder = N for both modes",
        "before": str(before_path),
        "after": str(after_path),
        "corpus_unchanged": before["corpus_fingerprint"] == after["corpus_fingerprint"],
        "corpus_drift": _corpus_drift(before),
        "summary": summary,
        "queries": query_results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture")
    cap.add_argument("output", type=Path)
    cap.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    cap.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    cmp = sub.add_parser("compare")
    cmp.add_argument("before", type=Path)
    cmp.add_argument("after", type=Path)
    cmp.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "capture":
        capture(args.output, args.golden, args.vault)
    else:
        compare(args.before, args.after, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

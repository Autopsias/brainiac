#!/usr/bin/env python3
"""Capture S02's 82-query evidence against the full qrel-bearing corpus.

The established golden set retains canonical pre-migration qrel paths while the
owner corpus uses Brainiac paths.  This runner reads the owner's published
snapshot, makes disposable baseline/candidate copies, and queries every indexed
note (including distractors).  It never rebuilds or changes the owner vault.

The published snapshot predates ADR-0008's schema bump.  The candidate copy is
therefore upgraded *only* with the new metadata projections (normalized titles
and aliases); chunks and vectors are copied byte-for-byte.  This lets the clean
S01 source query the v3 baseline and the current source exercise its v4 exact
leg over the same full corpus without an hours-long re-embedding run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
CAPTURE = REPO / "eval" / "capture_run.py"
CURRENT_SOURCE = REPO / "src"
sys.path.insert(0, str(CURRENT_SOURCE))
from brain.frontmatter import normalize_identity, parse_text  # noqa: E402


CYCLE_ORDER = [
    "baseline_then_candidate",
    "candidate_then_baseline",
    "baseline_then_candidate",
]
SNAPSHOT_BASELINE_SCHEMA = "3"
SNAPSHOT_CANDIDATE_SCHEMA = "4"


@dataclass(frozen=True)
class Component:
    name: str
    golden: Path
    qrels: Path
    vault: Path
    mapping: Path | None = None


@dataclass(frozen=True)
class CaptureTarget:
    """One disposable index surface used by a capture arm/component."""

    index_dir: Path | None = None
    index_db: Path | None = None
    embedder: str = "hash"
    vector_backend: str = "brute-force"


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _query_ids(golden: dict[str, Any]) -> list[str]:
    ids = [query["id"] for query in golden["queries"]]
    if len(ids) != len(set(ids)):
        raise ValueError("golden set contains duplicate query ids")
    return sorted(ids)


def _run_capture(
    *,
    component: Component,
    source_root: Path,
    target: CaptureTarget,
    label: str,
    output: Path,
    exact_leg_enabled: bool,
    k: int,
    warmup: int,
    samples: int,
    rebuild: bool,
) -> dict[str, Any]:
    """Run one isolated capture without ever opening the owner index for write."""
    if target.index_db is not None and rebuild:
        raise ValueError("a prebuilt snapshot must not be rebuilt")
    if target.index_db is None and target.index_dir is None:
        raise ValueError("capture target needs an index directory or prebuilt DB")

    command = [
        sys.executable,
        str(CAPTURE),
        "--golden", str(component.golden),
        "--vault", str(component.vault),
        "--system", label,
        "--rrf-k", "60",
        "--no-rerank",
        "--rerank-top", "15",
        "-k", str(k),
        "--warmup", str(warmup),
        "--samples", str(samples),
        "--source-root", str(source_root),
        "--derived-index-only",
        "--embedder", target.embedder,
        "--vector-backend", target.vector_backend,
        "--out", str(output),
    ]
    if component.mapping is not None:
        command.extend(["--map", str(component.mapping)])
    if target.index_db is not None:
        command.extend(["--index-db", str(target.index_db), "--read-only-index"])
    if rebuild:
        command.append("--rebuild")

    env = os.environ.copy()
    # Named-entity fixtures use deterministic hash/brute-force indexes.  The
    # full established arm explicitly uses the copied production snapshot's
    # cached e5/sqlite-vec representation.  Both retain their own source arm.
    env.update({
        "BRAIN_EMBEDDER": target.embedder,
        "BRAIN_VECTOR_BACKEND": target.vector_backend,
        "BRAIN_ROLE": "host",
        "BRAIN_EXACT_LEG_ENABLED": "1" if exact_leg_enabled else "0",
    })
    if target.index_dir is not None:
        env["BRAIN_INDEX_DIR"] = str(target.index_dir)
    subprocess.run(command, cwd=REPO, env=env, check=True)
    return _load(output)


def _relative_index_path(stored_path: object, vault: Path) -> str:
    """Normalize a snapshot's absolute path to the audited vault-relative key.

    Production snapshots store absolute paths, while S02's audited mapping is
    intentionally vault-relative.  Comparing those raw strings caused every
    old qrel to look absent despite being indexed.  Paths outside this component
    vault are retained as non-matches rather than silently treated as covered.
    """
    path = Path(str(stored_path))
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(vault.resolve()).as_posix()
    except ValueError:
        return str(path)


def _indexed_paths_from_db(db: Path, vault: Path) -> set[str]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        return {
            _relative_index_path(stored_path, vault)
            for (stored_path,) in conn.execute("SELECT path FROM notes")
        }


def _snapshot_meta(db: Path) -> dict[str, Any]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        meta = dict(conn.execute("SELECT k, v FROM meta"))
        return {
            "schema_version": meta.get("schema_version"),
            "vector_backend": meta.get("vector_backend"),
            "embed_model": meta.get("embed_model"),
            "embed_dim": meta.get("embed_dim"),
            "note_count": int(conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]),
            "chunk_count": int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]),
        }


def _copy_snapshot(source: Path, destination: Path) -> dict[str, Any]:
    """Copy a published snapshot and prove the baseline copy is byte-identical."""
    source_hash = _sha256(source)
    shutil.copy2(source, destination)
    copied_hash = _sha256(destination)
    if copied_hash != source_hash:
        raise RuntimeError("snapshot copy hash mismatch")
    return {
        "source_sha256": source_hash,
        "copy_sha256_before_projection": copied_hash,
        "metadata": _snapshot_meta(destination),
    }


def _source_path_for_indexed_note(stored_path: object, vault: Path) -> Path | None:
    path = Path(str(stored_path))
    candidate = path.resolve() if path.is_absolute() else (vault / path).resolve()
    try:
        candidate.relative_to(vault.resolve())
    except ValueError:
        return None
    return candidate


def _prepare_candidate_snapshot(source: Path, destination: Path, vault: Path) -> dict[str, Any]:
    """Derive ADR-0008's v4 metadata projections on a disposable snapshot copy.

    The operation intentionally does not re-embed or re-chunk.  It applies the
    same title normalization and brain-zone alias extraction as the candidate
    indexer, then records a v4 schema marker so the current source exercises the
    exact leg.  The owner snapshot and owner vault remain read-only inputs.
    """
    result = _copy_snapshot(source, destination)
    before = result["metadata"]
    if before["schema_version"] != SNAPSHOT_BASELINE_SCHEMA:
        raise RuntimeError(
            f"expected snapshot schema {SNAPSHOT_BASELINE_SCHEMA}, got {before['schema_version']}"
        )

    source_paths_checked = 0
    missing_brain_paths: list[str] = []
    aliases_projected = 0
    with sqlite3.connect(str(destination)) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(notes)")}
        if "title_norm" not in columns:
            conn.execute("ALTER TABLE notes ADD COLUMN title_norm TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS aliases ("
            "alias_norm TEXT NOT NULL, note_rowid INTEGER NOT NULL, "
            "PRIMARY KEY (alias_norm, note_rowid), "
            "FOREIGN KEY (note_rowid) REFERENCES notes(rowid))"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_aliases_lookup ON aliases(alias_norm)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_title_norm ON notes(title_norm)")
        conn.execute("DELETE FROM aliases")

        rows = list(conn.execute("SELECT rowid, title, zone, path FROM notes"))
        for rowid, title, zone, stored_path in rows:
            conn.execute(
                "UPDATE notes SET title_norm=? WHERE rowid=?",
                (normalize_identity(title), int(rowid)),
            )
            if str(zone) != "brain":
                continue
            source_path = _source_path_for_indexed_note(stored_path, vault)
            source_paths_checked += 1
            if source_path is None or not source_path.is_file():
                missing_brain_paths.append(str(stored_path))
                continue
            meta, _body = parse_text(source_path.read_text(encoding="utf-8", errors="replace"))
            raw_aliases = meta.get("aliases")
            if not isinstance(raw_aliases, list):
                continue
            for alias in raw_aliases:
                alias_norm = normalize_identity(alias)
                if isinstance(alias, str) and alias.strip() and alias_norm:
                    conn.execute(
                        "INSERT OR IGNORE INTO aliases(alias_norm, note_rowid) VALUES (?, ?)",
                        (alias_norm, int(rowid)),
                    )
                    aliases_projected += 1
        if missing_brain_paths:
            raise RuntimeError(
                "candidate snapshot cannot safely project aliases; missing indexed brain paths: "
                f"{len(missing_brain_paths)}"
            )
        conn.execute(
            "INSERT OR REPLACE INTO meta(k, v) VALUES ('schema_version', ?)",
            (SNAPSHOT_CANDIDATE_SCHEMA,),
        )
        conn.commit()

    result.update({
        "metadata_after_projection": _snapshot_meta(destination),
        "copy_sha256_after_projection": _sha256(destination),
        "title_projection": "normalized_identity(title) for every indexed note",
        "aliases_projected": aliases_projected,
        "brain_source_paths_checked": source_paths_checked,
        "missing_brain_paths": missing_brain_paths,
    })
    if result["metadata_after_projection"]["schema_version"] != SNAPSHOT_CANDIDATE_SCHEMA:
        raise RuntimeError("candidate snapshot did not receive the v4 schema marker")
    return result


def _established_evaluability(
    *,
    golden: dict[str, Any],
    qrels: dict[str, dict[str, int]],
    path_map: dict[str, Any],
    indexed_paths: set[str],
) -> dict[str, Any]:
    """Prove every established query has a real, indexed relevant document."""
    expected_ids = _query_ids(golden)
    if set(expected_ids) != set(qrels):
        raise ValueError("established qrels and golden query IDs differ")
    coverage = path_map.get("coverage", {})
    mapping = path_map.get("mapping")
    if not isinstance(mapping, dict) or not all(
        isinstance(path, str) and isinstance(canonical, str)
        for path, canonical in mapping.items()
    ):
        raise ValueError("path map has no valid mapping object")
    if not coverage.get("complete"):
        raise ValueError("path map itself is incomplete")

    docs_by_query = {
        qid: {document.split("#", 1)[0] for document in qrels[qid]}
        for qid in expected_ids
    }
    all_canonical = set().union(*docs_by_query.values()) if docs_by_query else set()
    reverse: dict[str, set[str]] = {}
    for migrated, canonical in mapping.items():
        reverse.setdefault(canonical, set()).add(migrated)
    missing_canonical = sorted(all_canonical - set(reverse))
    unindexed_paths = sorted(path for path in mapping if path not in indexed_paths)
    evaluable: list[str] = []
    unevaluable: list[str] = []
    for qid in expected_ids:
        docs = docs_by_query[qid]
        is_evaluable = bool(docs) and all(
            any(path in indexed_paths for path in reverse.get(canonical, set()))
            for canonical in docs
        )
        (evaluable if is_evaluable else unevaluable).append(qid)

    complete = (
        coverage.get("complete") is True
        and not missing_canonical
        and not unindexed_paths
        and not unevaluable
        and sorted(evaluable) == expected_ids
    )
    return {
        "expected_query_ids": expected_ids,
        "evaluable_query_ids": evaluable,
        "unevaluable_query_ids": unevaluable,
        "qrel_document_count": len(all_canonical),
        "mapped_document_count": len(mapping),
        "unmapped_canonical_documents": missing_canonical,
        "unindexed_mapping_paths": unindexed_paths,
        "indexed_note_count": len(indexed_paths),
        "mapping_sha256": path_map.get("inputs", {}).get("qrels_sha256", ""),
        "path_map_sha256": _sha256(Path(path_map["_path"])),
        "complete": complete,
    }


def _assert_stable_runs(captures: list[dict[str, Any]], ids: list[str], label: str) -> None:
    for document in captures:
        if sorted(document["runs"]) != ids:
            raise ValueError(f"{label} did not capture the expected query IDs")
    reference = captures[0]["runs"]
    for position, document in enumerate(captures[1:], start=2):
        if document["runs"] != reference:
            raise ValueError(f"{label} ranking changed between paired cycle 1 and {position}")


def _aggregate_component(
    captures: list[dict[str, Any]], ids: list[str], label: str,
) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    _assert_stable_runs(captures, ids, label)
    runs = captures[0]["runs"]
    latency = {
        qid: round(statistics.median(float(document["latency_ms"][qid]) for document in captures), 2)
        for qid in ids
    }
    return runs, latency


def _merged_coverage(established: dict[str, Any], named: dict[str, Any]) -> dict[str, Any]:
    """Merge coverage without promoting a smoke stratum to gate power."""
    power_rank = {"smoke": 0, "marginal": 1, "gate": 2}
    out: dict[str, Any] = {"strata": {}, "languages": {}}
    for group in ("strata", "languages"):
        for source in (established, named):
            for name, value in source.get("coverage", {}).get(group, {}).items():
                current = out[group].get(name)
                if current is None:
                    out[group][name] = dict(value)
                    continue
                current["n"] = int(current.get("n", 0)) + int(value.get("n", 0))
                current["min_n"] = max(int(current.get("min_n", 0)), int(value.get("min_n", 0)))
                current_power = str(current.get("power", "smoke"))
                value_power = str(value.get("power", "smoke"))
                current["power"] = max(
                    (current_power, value_power), key=lambda power: power_rank.get(power, -1)
                )
    return out


def _write_augmented_inputs(
    *,
    established_golden: dict[str, Any],
    established_qrels: dict[str, dict[str, int]],
    named_golden: dict[str, Any],
    named_qrels: dict[str, dict[str, int]],
    golden_out: Path,
    qrels_out: Path,
) -> list[str]:
    established_ids = _query_ids(established_golden)
    named_ids = _query_ids(named_golden)
    if set(established_ids) & set(named_ids):
        raise ValueError("established and named-entity golden sets overlap")
    if set(established_ids) != set(established_qrels) or set(named_ids) != set(named_qrels):
        raise ValueError("one component's qrels and golden query IDs differ")

    merged_ids = sorted(established_ids + named_ids)
    golden_out.parent.mkdir(parents=True, exist_ok=True)
    qrels_out.parent.mkdir(parents=True, exist_ok=True)
    augmented = {
        "schema_version": "s02-augmented-golden.v3",
        "created": _iso(),
        "session": "s02",
        "canonical_key": "component-local canonical path; established paths are mapped in capture scope",
        "components": {
            "established": {"query_count": len(established_ids), "corpus": "full-snapshot"},
            "named_entity": {"query_count": len(named_ids), "corpus": "synthetic-fixture"},
        },
        "coverage": _merged_coverage(established_golden, named_golden),
        "queries": established_golden["queries"] + named_golden["queries"],
    }
    merged_qrels = {**established_qrels, **named_qrels}
    golden_out.write_text(json.dumps(augmented, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    qrels_out.write_text(json.dumps(merged_qrels, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return merged_ids


def _aggregate_arm(
    *,
    arm: str,
    captures: dict[str, list[dict[str, Any]]],
    components: list[Component],
    source_root: Path,
    source_revision: str,
    exact_leg_enabled: bool,
    established_evaluability: dict[str, Any],
    full_corpus_inspection: dict[str, Any],
) -> dict[str, Any]:
    runs: dict[str, dict[str, float]] = {}
    latency: dict[str, float] = {}
    component_info: dict[str, Any] = {}
    scope_components: dict[str, Any] = {}
    for component in components:
        golden = _load(component.golden)
        ids = _query_ids(golden)
        component_runs, component_latency = _aggregate_component(
            captures[component.name], ids, f"{arm}/{component.name}"
        )
        if set(runs) & set(component_runs):
            raise ValueError("component captures have overlapping query IDs")
        runs.update(component_runs)
        latency.update(component_latency)
        first = captures[component.name][0]
        component_info[component.name] = {
            "index": first["index_state"].get("index"),
            "prebuilt_index": first["index_state"].get("prebuilt_index"),
            "read_only_index": first["index_state"].get("read_only_index"),
            "params": first["index_state"].get("params"),
            "vault": str(component.vault.resolve()),
            "source_root": str(source_root.resolve()),
        }
        scope_components[component.name] = {"query_count": len(ids)}
        if component.name == "established":
            component_info[component.name]["qrel_evaluability"] = established_evaluability
            component_info[component.name]["full_corpus"] = full_corpus_inspection
            scope_components[component.name]["qrel_evaluability"] = established_evaluability

    return {
        "system": f"s02-established-paired-{arm}-aggregate",
        "captured": _iso(),
        "source_revision": source_revision,
        "index_state": {
            "params": {
                "rrf_k": 60,
                "rerank": False,
                "rerank_top": 15,
                "exact_leg_enabled": exact_leg_enabled,
            },
            "components": component_info,
        },
        "k": 10,
        "runs": dict(sorted(runs.items())),
        "latency_ms": dict(sorted(latency.items())),
        "timing": {
            "warmup_per_query": 1,
            "samples_per_query": 7,
            "within_query_statistic": "median",
            "paired_capture_cycles": len(CYCLE_ORDER),
            "cross_cycle_statistic": "median",
            "cycle_order": CYCLE_ORDER,
            "protocol": "paired clean-HEAD/current alternating capture over the full established snapshot and synthetic named-entity fixture",
        },
        "scope": {
            "queries_captured": sorted(runs),
            "n": len(runs),
            "components": scope_components,
            "egress": "retrieval-primitive (no egress filter)",
            "mapped": True,
        },
    }


def _capture_negative(
    *,
    components: list[Component],
    source_root: Path,
    targets: dict[str, CaptureTarget],
    directory: Path,
    established_evaluability: dict[str, Any],
    full_corpus_inspection: dict[str, Any],
) -> dict[str, Any]:
    captures: dict[str, list[dict[str, Any]]] = {}
    for component in components:
        output = directory / f"negative-{component.name}.json"
        captures[component.name] = [
            _run_capture(
                component=component,
                source_root=source_root,
                target=targets[component.name],
                label=f"s02-established-negative-{component.name}",
                output=output,
                exact_leg_enabled=False,
                k=1,
                warmup=1,
                samples=7,
                rebuild=False,
            )
        ]
    result = _aggregate_arm(
        arm="negative-k1-exact-disabled",
        captures=captures,
        components=components,
        source_root=source_root,
        source_revision="current working tree, deliberately regressed k=1 exact-disabled control",
        exact_leg_enabled=False,
        established_evaluability=established_evaluability,
        full_corpus_inspection=full_corpus_inspection,
    )
    result["k"] = 1
    result["timing"] = {
        "warmup_per_query": 1,
        "samples_per_query": 7,
        "within_query_statistic": "median",
        "protocol": "deliberately regressed k=1 exact-disabled control",
    }
    return result


def _write_full_corpus_inspection(
    *,
    out: Path,
    source_snapshot: Path,
    source_snapshot_info: dict[str, Any],
    baseline_copy: dict[str, Any],
    candidate_copy: dict[str, Any],
    baseline_evaluability: dict[str, Any],
    candidate_evaluability: dict[str, Any],
) -> dict[str, Any]:
    """Persist proof that this run retained full-corpus distractors."""
    source_notes = int(source_snapshot_info["note_count"])
    qrel_docs = int(baseline_evaluability["qrel_document_count"])
    document = {
        "schema_version": "s02-full-corpus-inspection.v1",
        "captured": _iso(),
        "source_snapshot_sha256": _sha256(source_snapshot),
        "source_snapshot": {"path": str(source_snapshot), **source_snapshot_info},
        "baseline_copy": baseline_copy,
        "candidate_metadata_projection": candidate_copy,
        "qrel_evaluability": {
            "baseline": baseline_evaluability,
            "candidate": candidate_evaluability,
        },
        "full_corpus": {
            "indexed_notes": source_notes,
            "qrel_document_count": qrel_docs,
            "non_qrel_distractor_notes": source_notes - qrel_docs,
            "distractors_retained": source_notes > qrel_docs,
            "query_backend": "sqlite-vec",
            "query_embedder": "intfloat/multilingual-e5-small",
            "owner_vault_mutated": False,
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not document["full_corpus"]["distractors_retained"]:
        raise RuntimeError("full-corpus evidence has no distractors")
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--established-golden", type=Path, required=True)
    parser.add_argument("--established-qrels", type=Path, required=True)
    parser.add_argument("--established-vault", type=Path, required=True)
    parser.add_argument("--established-map", type=Path, required=True)
    parser.add_argument("--established-snapshot", type=Path, required=True,
                        help="published full-corpus index.snapshot.sqlite; read-only input")
    parser.add_argument("--named-golden", type=Path, required=True)
    parser.add_argument("--named-qrels", type=Path, required=True)
    parser.add_argument("--named-vault", type=Path, required=True)
    parser.add_argument("--baseline-source-root", type=Path, required=True)
    parser.add_argument("--baseline-revision", required=True)
    parser.add_argument("--candidate-source-root", type=Path, default=REPO / "src")
    parser.add_argument("--baseline-out", type=Path, required=True)
    parser.add_argument("--candidate-out", type=Path, required=True)
    parser.add_argument("--negative-out", type=Path, required=True)
    parser.add_argument("--augmented-golden-out", type=Path, required=True)
    parser.add_argument("--augmented-qrels-out", type=Path, required=True)
    parser.add_argument("--full-corpus-inspection-out", type=Path, required=True)
    parser.add_argument("--temp-dir", type=Path, default=Path("/private/tmp"))
    args = parser.parse_args()

    established_snapshot = args.established_snapshot.resolve()
    if not established_snapshot.is_file():
        parser.error(f"missing established snapshot: {established_snapshot}")
    source_snapshot_info = _snapshot_meta(established_snapshot)
    if source_snapshot_info["schema_version"] != SNAPSHOT_BASELINE_SCHEMA:
        parser.error(
            f"established snapshot must be schema {SNAPSHOT_BASELINE_SCHEMA}; "
            f"got {source_snapshot_info['schema_version']}"
        )
    if source_snapshot_info["vector_backend"] != "sqlite-vec":
        parser.error("established snapshot is not sqlite-vec; full-corpus replay would change backend")
    if source_snapshot_info["embed_model"] != "intfloat/multilingual-e5-small":
        parser.error("established snapshot does not carry the expected e5 model")

    components = [
        Component("established", args.established_golden, args.established_qrels,
                  args.established_vault.resolve(), args.established_map),
        Component("named_entity", args.named_golden, args.named_qrels,
                  args.named_vault.resolve(), None),
    ]
    for component in components:
        if not component.golden.is_file() or not component.qrels.is_file():
            parser.error(f"missing golden/qrels input for {component.name}")
        if not component.vault.is_dir():
            parser.error(f"missing vault for {component.name}: {component.vault}")
    if not args.baseline_source_root.is_dir() or not args.candidate_source_root.is_dir():
        parser.error("both source roots must exist")

    established_golden = _load(args.established_golden)
    established_qrels = _load(args.established_qrels)
    named_golden = _load(args.named_golden)
    named_qrels = _load(args.named_qrels)
    path_map = _load(args.established_map)
    path_map["_path"] = str(args.established_map.resolve())
    all_ids = _write_augmented_inputs(
        established_golden=established_golden,
        established_qrels=established_qrels,
        named_golden=named_golden,
        named_qrels=named_qrels,
        golden_out=args.augmented_golden_out,
        qrels_out=args.augmented_qrels_out,
    )

    args.temp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="s02-full-established-paired-", dir=args.temp_dir) as raw_temp:
        temp = Path(raw_temp)
        baseline_snapshot = temp / "baseline-established-snapshot.sqlite"
        candidate_snapshot = temp / "candidate-established-snapshot.sqlite"
        baseline_copy = _copy_snapshot(established_snapshot, baseline_snapshot)
        candidate_copy = _prepare_candidate_snapshot(
            established_snapshot, candidate_snapshot, components[0].vault
        )

        # This is the audited absolute-to-relative comparison that prevents the
        # prior all-zero failure.  It runs before any timing capture and fails
        # closed rather than letting harness_direct intersect away the signal.
        baseline_evaluable = _established_evaluability(
            golden=established_golden,
            qrels=established_qrels,
            path_map=path_map,
            indexed_paths=_indexed_paths_from_db(baseline_snapshot, components[0].vault),
        )
        candidate_evaluable = _established_evaluability(
            golden=established_golden,
            qrels=established_qrels,
            path_map=path_map,
            indexed_paths=_indexed_paths_from_db(candidate_snapshot, components[0].vault),
        )
        if not baseline_evaluable["complete"] or not candidate_evaluable["complete"]:
            raise RuntimeError("established full corpus is not completely evaluable")

        full_corpus_inspection = _write_full_corpus_inspection(
            out=args.full_corpus_inspection_out,
            source_snapshot=established_snapshot,
            source_snapshot_info=source_snapshot_info,
            baseline_copy=baseline_copy,
            candidate_copy=candidate_copy,
            baseline_evaluability=baseline_evaluable,
            candidate_evaluability=candidate_evaluable,
        )

        baseline_targets = {
            "established": CaptureTarget(
                index_db=baseline_snapshot, embedder="auto", vector_backend="sqlite-vec"
            ),
            "named_entity": CaptureTarget(index_dir=temp / "baseline-named-entity-index"),
        }
        candidate_targets = {
            "established": CaptureTarget(
                index_db=candidate_snapshot, embedder="auto", vector_backend="sqlite-vec"
            ),
            "named_entity": CaptureTarget(index_dir=temp / "candidate-named-entity-index"),
        }

        # Rebuild only the small synthetic indexes.  The full established copies
        # are already a verified, immutable snapshot surface.
        for arm, source_root, targets, enabled in (
            ("baseline", args.baseline_source_root, baseline_targets, False),
            ("candidate", args.candidate_source_root, candidate_targets, True),
        ):
            component = components[1]
            _run_capture(
                component=component,
                source_root=source_root,
                target=targets[component.name],
                label=f"s02-full-established-setup-{arm}-{component.name}",
                output=temp / f"setup-{arm}-{component.name}.json",
                exact_leg_enabled=enabled,
                k=10,
                warmup=0,
                samples=1,
                rebuild=True,
            )

        captured: dict[str, dict[str, list[dict[str, Any]]]] = {
            "baseline": {component.name: [] for component in components},
            "candidate": {component.name: [] for component in components},
        }
        for cycle, order in enumerate(CYCLE_ORDER, start=1):
            arms = ["baseline", "candidate"] if order.startswith("baseline") else ["candidate", "baseline"]
            for arm in arms:
                source_root = args.baseline_source_root if arm == "baseline" else args.candidate_source_root
                targets = baseline_targets if arm == "baseline" else candidate_targets
                enabled = arm == "candidate"
                for component in components:
                    output = temp / f"{arm}-{component.name}-cycle-{cycle}.json"
                    captured[arm][component.name].append(_run_capture(
                        component=component,
                        source_root=source_root,
                        target=targets[component.name],
                        label=f"s02-full-established-{arm}-{component.name}-cycle-{cycle}",
                        output=output,
                        exact_leg_enabled=enabled,
                        k=10,
                        warmup=1,
                        samples=7,
                        rebuild=False,
                    ))

        baseline = _aggregate_arm(
            arm="baseline", captures=captured["baseline"], components=components,
            source_root=args.baseline_source_root, source_revision=args.baseline_revision,
            exact_leg_enabled=False, established_evaluability=baseline_evaluable,
            full_corpus_inspection=full_corpus_inspection,
        )
        candidate = _aggregate_arm(
            arm="candidate", captures=captured["candidate"], components=components,
            source_root=args.candidate_source_root, source_revision="current working tree",
            exact_leg_enabled=True, established_evaluability=candidate_evaluable,
            full_corpus_inspection=full_corpus_inspection,
        )
        if sorted(baseline["runs"]) != all_ids or sorted(candidate["runs"]) != all_ids:
            raise RuntimeError("aggregate capture is not the expected paired 82-query set")
        negative = _capture_negative(
            components=components,
            source_root=args.candidate_source_root,
            targets=candidate_targets,
            directory=temp,
            established_evaluability=candidate_evaluable,
            full_corpus_inspection=full_corpus_inspection,
        )

    for path, document in (
        (args.baseline_out, baseline),
        (args.candidate_out, candidate),
        (args.negative_out, negative),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"captured {len(all_ids)} paired queries over {source_snapshot_info['note_count']} full-corpus notes: "
        f"established={len(_query_ids(established_golden))}, named_entity={len(_query_ids(named_golden))}; "
        f"evaluable established={len(baseline_evaluable['evaluable_query_ids'])}/"
        f"{len(baseline_evaluable['expected_query_ids'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

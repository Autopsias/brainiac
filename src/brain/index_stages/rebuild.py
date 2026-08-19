"""Build a complete derived index."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..notes import scan_vault
from ..progress import ProgressReporter


@dataclass
class RebuildPlan:
    """Describe one resumable rebuild execution."""

    notes: list[Any]
    scan_stats: dict[str, int]
    fingerprint: str
    resume_from_batch: int
    start_chunk_rowid: int
    notes_per_batch: int
    batch_starts: list[int]


def _plan_rebuild(index: Any, vault: Path, resume: Any | None) -> RebuildPlan:
    if resume is None:
        index._create_schema()
    index._seen_ids = set()
    scan_stats: dict[str, int] = {}
    notes = [
        index._plan_note(note, number)
        for number, note in enumerate(scan_vault(vault, stats=scan_stats), start=1)
    ]
    fingerprint = index._vault_fingerprint(notes)
    resume_from_batch = 0
    start_chunk_rowid = 1
    if resume is not None:
        if resume.vault_fingerprint != fingerprint:
            index._create_schema()
            index._seen_ids = set()
        else:
            resume_from_batch = resume.committed_batches
            start_chunk_rowid = resume.start_chunk_rowid
    if resume is not None and resume_from_batch > 0:
        notes_per_batch = resume.notes_per_batch
    else:
        notes_per_batch = max(
            1, int(os.environ.get("BRAIN_REBUILD_NOTES_PER_BATCH", "200"))
        )
    return RebuildPlan(
        notes=notes,
        scan_stats=scan_stats,
        fingerprint=fingerprint,
        resume_from_batch=resume_from_batch,
        start_chunk_rowid=start_chunk_rowid,
        notes_per_batch=notes_per_batch,
        batch_starts=list(range(0, len(notes), notes_per_batch)),
    )


def _write_manifest(index: Any, schema_version: int, format_version: int) -> None:
    if index.db_path == Path(":memory:"):
        return
    manifest_path = index.db_path.with_name(index.db_path.name + ".manifest.json")
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": str(schema_version),
                "index_format_version": str(format_version),
                "vector_backend": index.backend.name,
                "embed_model": index.embedder.model_id,
                "embed_dim": str(index.embedder.dim),
            }
        ),
        encoding="utf-8",
    )


def _commit_rebuild_batch(
    index: Any,
    batch: list[Any],
    vectors: list[list[float]],
    start_rowid: int,
    batch_number: int,
    is_last_batch: bool,
    plan: RebuildPlan,
    format_version: int,
) -> int:
    conn = index.conn
    conn.execute("BEGIN IMMEDIATE")
    chunk_rowid = start_rowid
    vector_offset = 0
    for note_plan in batch:
        chunk_count = len(note_plan.inputs)
        chunk_rowid = index._write_planned(
            note_plan,
            vectors[vector_offset : vector_offset + chunk_count],
            chunk_rowid,
        )
        vector_offset += chunk_count
    index._set_meta("committed_batches", str(batch_number + 1))
    index._set_meta("vault_fingerprint", plan.fingerprint)
    index._set_meta("index_format_version", str(format_version))
    index._set_meta("notes_per_batch", str(plan.notes_per_batch))
    index._set_meta("finished", "true" if is_last_batch else "false")
    conn.commit()
    return chunk_rowid


def _run_rebuild_batches(
    index: Any,
    plan: RebuildPlan,
    reporter: ProgressReporter,
    format_version: int,
    retry: Callable[..., Any],
) -> int:
    chunk_rowid = plan.start_chunk_rowid
    for batch_number, batch_start in enumerate(plan.batch_starts):
        if batch_number < plan.resume_from_batch:
            continue
        batch = plan.notes[batch_start : batch_start + plan.notes_per_batch]
        inputs = [text for note_plan in batch for text in note_plan.inputs]
        vectors = index.embedder.embed_batch(inputs, is_query=False) if inputs else []
        is_last = batch_number == len(plan.batch_starts) - 1

        def commit() -> int:
            return _commit_rebuild_batch(
                index,
                batch,
                vectors,
                chunk_rowid,
                batch_number,
                is_last,
                plan,
                format_version,
            )

        chunk_rowid = retry(commit, conn=index.conn)
        reporter.update(chunk_rowid - 1)
    return chunk_rowid


def _commit_empty_rebuild(index: Any, plan: RebuildPlan, format_version: int) -> None:
    if plan.batch_starts:
        return
    index.conn.execute("BEGIN IMMEDIATE")
    index._set_meta("committed_batches", "0")
    index._set_meta("vault_fingerprint", plan.fingerprint)
    index._set_meta("index_format_version", str(format_version))
    index._set_meta("finished", "true")
    index.conn.commit()


def rebuild_index(
    index: Any,
    vault: Path,
    *,
    resume: Any | None,
    json_mode: bool,
    schema_version: int,
    format_version: int,
    retry: Callable[..., Any],
) -> dict[str, Any]:
    """Execute the resumable three-pass rebuild."""
    plan = _plan_rebuild(index, vault, resume)
    _write_manifest(index, schema_version, format_version)
    total_chunks = sum(len(note_plan.inputs) for note_plan in plan.notes)
    reporter = ProgressReporter("rebuild", total_chunks, json_mode=json_mode)
    reporter.update(plan.start_chunk_rowid - 1)
    chunk_rowid = _run_rebuild_batches(index, plan, reporter, format_version, retry)
    _commit_empty_rebuild(index, plan, format_version)
    index._seen_ids = None
    return {
        "indexed": len(plan.notes),
        "excluded_machine_output": plan.scan_stats.get("excluded_machine_output", 0),
        "chunks": chunk_rowid - 1,
        "backend": index.backend.name,
        "embed_model": index.embedder.model_id,
        "embed_dim": index.embedder.dim,
        "vault_fingerprint": index.get_meta("vault_fingerprint"),
        "db": str(index.db_path),
    }

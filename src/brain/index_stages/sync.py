"""Reconcile indexed notes with vault state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..notes import Note, scan_vault
from ..progress import ProgressReporter


@dataclass
class SyncCounts:
    """Track one atomic incremental reconciliation."""

    added: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    rebased: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "deleted": self.deleted,
            "rebased": self.rebased,
        }


def _clear_search_caches(index: Any) -> None:
    index._title_phrase_records_cache = None
    index._identity_owner_cache.clear()
    index._title_phrase_match_cache.clear()
    index._literal_text_cache.clear()


def _fallback_rebuild(
    index: Any, vault: Path, json_mode: bool
) -> dict[str, Any] | None:
    if not index._schema_ready():
        result = index.rebuild(vault, json_mode=json_mode)
        result["mode"] = "rebuild(no-schema)"
        return result
    if not index.model_matches():
        result = index.rebuild(vault, json_mode=json_mode)
        result["mode"] = "rebuild(model-change)"
        return result
    return None


def _scan_sync_inputs(
    index: Any, vault: Path
) -> tuple[dict[str, Note], dict[str, tuple[int, str]], dict[str, int]]:
    scan_stats: dict[str, int] = {}
    on_disk = {
        note.path.as_posix(): note for note in scan_vault(vault, stats=scan_stats)
    }
    indexed = {
        row[0]: (int(row[1]), row[2] or "")
        for row in index.conn.execute(
            "SELECT path, rowid, content_hash FROM notes"
        ).fetchall()
    }
    return on_disk, indexed, scan_stats


def _rebase_moved_notes(
    index: Any,
    on_disk: dict[str, Note],
    local_indexed: dict[str, tuple[int, str]],
) -> int:
    """Rebase content-identical moves before path-keyed reconciliation."""
    gone: dict[str, list[tuple[str, int]]] = {}
    for path, (note_rowid, content_hash) in local_indexed.items():
        if path not in on_disk and content_hash:
            gone.setdefault(content_hash, []).append((path, note_rowid))
    rebased = 0
    for path, note in on_disk.items():
        if path in local_indexed:
            continue
        candidates = gone.get(note.content_hash)
        if not candidates:
            continue
        old_path, note_rowid = candidates.pop()
        index.conn.execute("UPDATE notes SET path=? WHERE rowid=?", (path, note_rowid))
        del local_indexed[old_path]
        local_indexed[path] = (note_rowid, note.content_hash)
        rebased += 1
    return rebased


def _delete_stale_notes(
    index: Any,
    on_disk: dict[str, Note],
    local_indexed: dict[str, tuple[int, str]],
) -> int:
    """Propagate deletes before inserts can encounter same-id renames."""
    deleted = 0
    for path, (note_rowid, _content_hash) in local_indexed.items():
        if path not in on_disk:
            index._delete_note(note_rowid)
            deleted += 1
    return deleted


def _upsert_notes(
    index: Any,
    on_disk: dict[str, Note],
    local_indexed: dict[str, tuple[int, str]],
    *,
    json_mode: bool,
) -> tuple[int, int, int]:
    indexed_ids = {
        row[0]: int(row[1])
        for row in index.conn.execute("SELECT id, rowid FROM notes").fetchall()
    }
    reporter = ProgressReporter("sync", len(on_disk), json_mode=json_mode)
    chunk_rowid = index._next_rowid("chunks")
    added = updated = unchanged = 0
    for number, (path, note) in enumerate(on_disk.items(), start=1):
        if path not in local_indexed:
            if note.id in indexed_ids:
                index._delete_note(indexed_ids.pop(note.id))
            note_rowid = index._next_rowid("notes")
            chunk_rowid = index._insert_note(note, note_rowid, chunk_rowid)
            added += 1
        elif local_indexed[path][1] != note.content_hash:
            old_rowid = local_indexed[path][0]
            index._delete_note(old_rowid)
            chunk_rowid = index._insert_note(note, old_rowid, chunk_rowid)
            updated += 1
        else:
            unchanged += 1
        reporter.update(number)
    return added, updated, unchanged


def _commit_vault_fingerprint(index: Any) -> None:
    fingerprint = index._vault_fingerprint_projection(
        (str(path), str(content_hash or ""))
        for path, content_hash in index.conn.execute(
            "SELECT path, content_hash FROM notes"
        ).fetchall()
    )
    index._set_meta("vault_fingerprint", fingerprint)


def _do_sync(
    index: Any,
    on_disk: dict[str, Note],
    indexed: dict[str, tuple[int, str]],
    *,
    json_mode: bool,
) -> dict[str, int]:
    """Run every mutation stage inside one CC-01 transaction."""
    index.conn.execute("BEGIN IMMEDIATE")
    local_indexed = dict(indexed)
    counts = SyncCounts()
    counts.rebased = _rebase_moved_notes(index, on_disk, local_indexed)
    counts.deleted = _delete_stale_notes(index, on_disk, local_indexed)
    counts.added, counts.updated, counts.unchanged = _upsert_notes(
        index, on_disk, local_indexed, json_mode=json_mode
    )
    _commit_vault_fingerprint(index)
    index.conn.commit()
    return counts.as_dict()


def sync_index(
    index: Any,
    vault: Path,
    *,
    json_mode: bool,
    retry: Callable[..., Any],
) -> dict[str, Any]:
    """Execute schema checks then one incremental reconciliation."""
    _clear_search_caches(index)
    fallback = _fallback_rebuild(index, vault, json_mode)
    if fallback is not None:
        return fallback
    on_disk, indexed, scan_stats = _scan_sync_inputs(index, vault)

    def reconcile() -> dict[str, int]:
        return _do_sync(index, on_disk, indexed, json_mode=json_mode)

    counts = retry(reconcile, conn=index.conn)
    total_chunks = int(index.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
    return {
        "mode": "incremental",
        **counts,
        "indexed": counts["added"] + counts["updated"] + counts["unchanged"],
        "excluded_machine_output": scan_stats.get("excluded_machine_output", 0),
        "chunks": total_chunks,
        "backend": index.backend.name,
        "embed_model": index.embedder.model_id,
        "embed_dim": index.embedder.dim,
        "vault_fingerprint": index.get_meta("vault_fingerprint"),
        "languages": index._refresh_language_census(),
        "db": str(index.db_path),
    }

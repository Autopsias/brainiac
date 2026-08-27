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
) -> tuple[dict[str, Note], dict[str, tuple[int, str, str]], dict[str, int]]:
    scan_stats: dict[str, int] = {}
    on_disk = {
        note.path.as_posix(): note for note in scan_vault(vault, stats=scan_stats)
    }
    indexed = {
        row[0]: (int(row[1]), row[2] or "", row[3] or "")
        for row in index.conn.execute(
            "SELECT path, rowid, content_hash, classification FROM notes"
        ).fetchall()
    }
    return on_disk, indexed, scan_stats


def _rebase_moved_notes(
    index: Any,
    on_disk: dict[str, Note],
    local_indexed: dict[str, tuple[int, str, str]],
) -> int:
    """Rebase content-identical moves before path-keyed reconciliation."""
    gone: dict[str, list[tuple[str, int, str]]] = {}
    for path, (note_rowid, content_hash, tier) in local_indexed.items():
        if path not in on_disk and content_hash:
            gone.setdefault(content_hash, []).append((path, note_rowid, tier))
    rebased = 0
    for path, note in on_disk.items():
        if path in local_indexed:
            continue
        candidates = gone.get(note.content_hash)
        if not candidates:
            continue
        old_path, note_rowid, tier = candidates.pop()
        index.conn.execute("UPDATE notes SET path=? WHERE rowid=?", (path, note_rowid))
        del local_indexed[old_path]
        local_indexed[path] = (note_rowid, note.content_hash, tier)
        rebased += 1
    return rebased


def _delete_stale_notes(
    index: Any,
    on_disk: dict[str, Note],
    local_indexed: dict[str, tuple[int, str, str]],
) -> int:
    """Propagate deletes before inserts can encounter same-id renames."""
    deleted = 0
    for path, (note_rowid, _content_hash, _tier) in local_indexed.items():
        if path not in on_disk:
            index._delete_note(note_rowid)
            deleted += 1
    return deleted


def _chain_key(path: str, vault: Path) -> str:
    """The audit chain's vault-RELATIVE spelling of an on-disk path key.

    The chain and the drift dispositions record paths relative to the vault;
    sync's on_disk keys and the index's ``notes.path`` are scanner-resolved
    absolute paths — and "resolved" can differ between the two legs on a
    symlinked prefix (macOS ``/var`` vs ``/private/var``). Both sides resolved,
    one subtraction: the identity the triage flow (verify-audit, dispositions)
    already uses."""
    try:
        return Path(path).resolve().relative_to(Path(vault).resolve()).as_posix()
    except (ValueError, OSError):
        return path


def _refused_downgrade(
    path: str,
    note: Note,
    old_tier: str,
    *,
    vault: Path,
    signed_hashes: dict[str, str] | None,
    dispositions: dict[str, dict] | None,
) -> bool:
    """VULN-3387 (external pentest 2026-08): an out-of-band edit LOWERING a
    note's classification must not be honored by the index until the audit
    chain or a triaged disposition explains these exact bytes. The egress gate
    trusts the index, so an unexplained relabel (Confidential -> Internal)
    would otherwise hand higher-tier content to capped readers — the Cowork
    VM leg — within one sync. A RAISE is always honored (fail-closed
    direction), and these exact bytes being signed means the change came
    through the audited write path. Unrecognised/missing labels are never
    refused here: read-time default-deny normalization already fail-closes
    them to MNPI, which is the restrictive direction."""
    from ..classification import RANK

    new_tier = note.classification if isinstance(note.classification, str) else ""
    old_rank = RANK.get(old_tier)
    new_rank = RANK.get(new_tier)
    if old_rank is None or new_rank is None:
        return False
    if new_rank >= old_rank:
        return False
    rel = _chain_key(path, vault)
    if signed_hashes is not None and signed_hashes.get(rel) == note.content_hash:
        return False  # these exact bytes were signed — audited change
    if dispositions:
        from ..audit_drift import match_disposition

        if match_disposition(
            {"path": rel, "issue": "content_drift",
             "actual_sha256": note.content_hash},
            dispositions,
        ):
            return False  # the owner triaged exactly these bytes
    return True


def _upsert_notes(
    index: Any,
    on_disk: dict[str, Note],
    local_indexed: dict[str, tuple[int, str, str]],
    *,
    json_mode: bool,
    vault: Path,
    signed_hashes: dict[str, str] | None = None,
    dispositions: dict[str, dict] | None = None,
) -> tuple[int, int, int, list[dict]]:
    indexed_ids = {
        row[0]: int(row[1])
        for row in index.conn.execute("SELECT id, rowid FROM notes").fetchall()
    }
    reporter = ProgressReporter("sync", len(on_disk), json_mode=json_mode)
    chunk_rowid = index._next_rowid("chunks")
    added = updated = unchanged = 0
    refused: list[dict] = []
    for number, (path, note) in enumerate(on_disk.items(), start=1):
        if path not in local_indexed:
            if note.id in indexed_ids:
                index._delete_note(indexed_ids.pop(note.id))
            note_rowid = index._next_rowid("notes")
            chunk_rowid = index._insert_note(note, note_rowid, chunk_rowid)
            added += 1
        elif local_indexed[path][1] != note.content_hash:
            old_rowid, _old_hash, old_tier = local_indexed[path]
            if _refused_downgrade(
                path, note, old_tier, vault=vault,
                signed_hashes=signed_hashes, dispositions=dispositions,
            ):
                # The index keeps the last committed state (the signed one);
                # the refused note still counts as UNEXPLAINED drift for
                # verify-audit/doctor, which is where the owner triages it.
                # The path is reported in the chain's relative spelling —
                # the one verify-audit and the disposition file use.
                refused.append({
                    "path": _chain_key(path, vault),
                    "from": old_tier or "(unlabelled)",
                    "to": note.classification or "(unlabelled)",
                    "reason": "unexplained-downgrade",
                })
                reporter.update(number)
                continue
            index._delete_note(old_rowid)
            chunk_rowid = index._insert_note(note, old_rowid, chunk_rowid)
            updated += 1
        else:
            unchanged += 1
        reporter.update(number)
    return added, updated, unchanged, refused


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
    indexed: dict[str, tuple[int, str, str]],
    *,
    json_mode: bool,
    vault: Path,
    signed_hashes: dict[str, str] | None = None,
    dispositions: dict[str, dict] | None = None,
) -> tuple[dict[str, int], list[dict]]:
    """Run every mutation stage inside one CC-01 transaction."""
    index.conn.execute("BEGIN IMMEDIATE")
    local_indexed = dict(indexed)
    counts = SyncCounts()
    counts.rebased = _rebase_moved_notes(index, on_disk, local_indexed)
    counts.deleted = _delete_stale_notes(index, on_disk, local_indexed)
    counts.added, counts.updated, counts.unchanged, refused = _upsert_notes(
        index, on_disk, local_indexed, json_mode=json_mode, vault=vault,
        signed_hashes=signed_hashes, dispositions=dispositions,
    )
    _commit_vault_fingerprint(index)
    index.conn.commit()
    return counts.as_dict(), refused


def sync_index(
    index: Any,
    vault: Path,
    *,
    json_mode: bool,
    retry: Callable[..., Any],
    signed_hashes: dict[str, str] | None = None,
    dispositions: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """Execute schema checks then one incremental reconciliation."""
    _clear_search_caches(index)
    fallback = _fallback_rebuild(index, vault, json_mode)
    if fallback is not None:
        return fallback
    on_disk, indexed, scan_stats = _scan_sync_inputs(index, vault)

    def reconcile() -> tuple[dict[str, int], list[dict]]:
        return _do_sync(
            index, on_disk, indexed, json_mode=json_mode, vault=vault,
            signed_hashes=signed_hashes, dispositions=dispositions,
        )

    counts, refused = retry(reconcile, conn=index.conn)
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
        # VULN-3387: out-of-band classification downgrades the audit chain
        # (and no disposition) does not explain — refused, index unchanged.
        "refused_downgrades": refused,
    }

"""COS proposal-maintenance operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._attachment_store import _attachment_meta_path, attachment_expired_dir, attachment_lifecycle_dir, attachment_metas
from ._batches import _batches_path
from ._claims_state import _pending_metas
from ._guards import _move_dirent, _unique_dest
from ._io import _read_jsonl, _write_atomic
from ._layout import _env_days, _parse_ts, _utcnow, hold_dir, proposals_dir
from ._version_links import _expire_version_links, _version_link_expired

def expire_proposals(vault, now: _dt.datetime | None = None) -> list[str]:
    """Move TTL-expired pending proposals to ``expired/`` (never signed).

    EXPIRY IS NOT A VERDICT (HARDENED:claude-2): an unanswered candidate
    writes NO outcome record — it is excluded from the graduation numerator
    AND the denominator, and is emphatically not a silent `rejected`. Only an
    explicit owner decision moves the evidence."""
    now = now or _utcnow()
    with vault_writer_lock(vault, verb="cos-expire"):
        pending = proposals_dir(vault) / "pending"
        expired_dir = proposals_dir(vault) / "expired"
        expired: list[str] = []
        for m in _pending_metas(vault):
            exp = _parse_ts(m.get("ttl_expires", ""))
            if exp and exp <= now:
                expired_dir.mkdir(parents=True, exist_ok=True)
                for suffix in (".md", ".json"):
                    src = pending / f"{m['id']}{suffix}"
                    if src.exists():
                        shutil.move(str(src), expired_dir / src.name)
                expired.append(m["id"])
        # Same for quarantined attachments — moved aside (recoverable until
        # the GC window closes), never auto-accepted, never a verdict.
        adir = attachment_expired_dir(vault)
        for m in attachment_metas(vault):
            exp = _parse_ts(m.get("ttl_expires", ""))
            if not (exp and exp <= now):
                continue
            adir.mkdir(parents=True, exist_ok=True)
            src = Path(m["path"])
            if src.is_symlink() or src.exists():
                _move_dirent(src, _unique_dest(adir, src.name))
            meta_path = _attachment_meta_path(vault, m["id"])
            if meta_path.exists():
                _move_dirent(meta_path, _unique_dest(adir, meta_path.name))
            expired.append(m["id"])
        expired.extend(_expire_version_links(vault, now))
        return expired

def gc_compact(vault, now: _dt.datetime | None = None) -> dict[str, int]:
    """Delete rejected/expired artifacts older than the GC window.

    Under the writer lock (B4): it REWRITES ``batches.jsonl`` wholesale from a
    snapshot it read, so an unlocked GC racing a consume can resurrect a batch
    the consumer just closed — after the consumer already moved the files."""
    now = now or _utcnow()
    with vault_writer_lock(vault, verb="cos-gc"):
        return _gc_compact_locked(vault, now)

def _prune_expired_files(directories: tuple[Path, ...], cutoff: float) -> int:
    """Delete expired filesystem artifacts."""
    removed = 0
    for directory in directories:
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
    return removed


def _prune_released_markers(vault, cutoff: float) -> int:
    """Delete expired released-hold markers."""
    removed = 0
    hdir = hold_dir(vault)
    if hdir.is_dir():
        for f in hdir.glob("*.released.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
    return removed


def _compact_old_batches(vault, cutoff: float) -> int:
    """Remove expired closed batch records."""
    bpath = _batches_path(vault)
    batches = _read_jsonl(bpath)
    keep = []
    dropped = 0
    for b in batches:
        closed = b.get("state") in ("consumed", "expired", "invalid")
        ts = _parse_ts(b.get("consumed_at") or b.get("expired_at") or b.get("created", ""))
        if closed and ts and ts.timestamp() < cutoff:
            dropped += 1
            continue
        keep.append(b)
    if dropped:
        public("_write_atomic")(bpath, "".join(json.dumps(b, sort_keys=True) + "\n"
                                     for b in keep).encode("utf-8"))
    return dropped


def _gc_compact_locked(vault, now: _dt.datetime) -> dict[str, int]:
    """Prune expired proposal artifacts under the vault writer lock."""
    cutoff = now.timestamp() - _env_days(GC_DAYS_ENV, DEFAULT_GC_DAYS) * 86400
    artifact_dirs = (
        proposals_dir(vault) / "rejected", proposals_dir(vault) / "expired",
        attachment_expired_dir(vault), _version_link_expired(vault), attachment_lifecycle_dir(vault))
    removed = _prune_expired_files(artifact_dirs, cutoff)
    removed += _prune_released_markers(vault, cutoff)
    return {"files_removed": removed, "batches_compacted": _compact_old_batches(vault, cutoff)}

__all__ = ['expire_proposals', 'gc_compact', '_gc_compact_locked']

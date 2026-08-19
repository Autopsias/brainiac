"""COS attachment-store operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._attachment_anchors import clear_attachment_hold_authz
from ._guards import _leaf_in, _move_dirent, _safe_meta_id, _unique_dest
from ._io import _write_atomic
from ._layout import drop_dir, host_dir

def ingest_manifest_dir(vault=None) -> Path:
    return drop_dir(vault) / "ingest-manifest"

def attachments_dir(vault=None) -> Path:
    return host_dir(vault) / "attachments"

def attachment_quarantine_dir(vault=None) -> Path:
    return attachments_dir(vault) / "quarantine"

def attachment_expired_dir(vault=None) -> Path:
    return attachments_dir(vault) / "expired"

def attachment_lifecycle_dir(vault=None) -> Path:
    """Where an attachment's identity SURVIVES its release (B3).

    The quarantine sidecar is consumed the moment the file moves into
    ``vault/inbox/``, and the ingest drain then renames it to a date+filename
    ``raw/`` id — so without this record ``undo_state(<att-id>)`` returned
    ``absent`` and an owner undo silently did nothing: no deletion, no audited
    retirement, no category demotion. One small JSON per released attachment
    carries id -> inbox destination -> content sha, and the sha is what the
    ingest drain's own manifest maps to the final note id.
    """
    return attachments_dir(vault) / "lifecycle"

def _attachment_lifecycle(vault, aid: str) -> dict[str, Any]:
    try:
        m = json.loads((attachment_lifecycle_dir(vault) / f"{aid}.json")
                       .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return m if isinstance(m, dict) else {}

def _lifecycle_payload(life: dict[str, Any] | None, vault=None) -> Path | None:
    """Where the released payload actually IS, per its lifecycle record (R3).

    ``dest`` once the move completed; ``src`` while the record is still
    ``releasing`` and the process died before ``shutil.move`` ran. ``None``
    means neither exists — the drain has consumed it (or it was withdrawn).

    Both fields are read off the mount and both become a move/unlink target
    (`hold_undo`'s ``inbox-pending`` branch withdraws this file), so neither is
    used AS a path (INT-05): only its last component survives, and that name is
    joined onto the one root this lane can legitimately have put it in — the
    inbox for ``dest``, the attachment quarantine for ``src``. A record naming
    ``/etc/hosts`` therefore points at ``<vault>/inbox/hosts``, which does not
    exist, instead of at ``/etc/hosts``."""
    if not vault:
        return None
    for key, root in (("dest", config.vault_root(vault) / "inbox"),
                      ("src", attachment_quarantine_dir(vault))):
        p = _leaf_in(root, (life or {}).get(key))
        if p is not None:
            return p
    return None

def _ingested_raw_id(vault, sha: str) -> str | None:
    """The ``raw/`` note id the ingest drain minted for exactly these bytes.

    The drain already keeps an authoritative original-sha -> note-id map at
    ``.brain/ingest-manifest.json``; reading it is what lets an undo reach an
    attachment after ingestion renamed it out of all recognition."""
    if not sha:
        return None
    try:
        m = json.loads((config.brain_runtime_dir(vault) / "ingest-manifest.json")
                       .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    val = m.get(sha) if isinstance(m, dict) else None
    if not val:
        return None
    try:
        return safe_slug(str(val))     # names a note an undo will RETIRE
    except ValueError:
        return None

def attachment_metas(vault, *, state: str | None = None) -> list[dict[str, Any]]:
    """Every quarantined attachment candidate's sidecar (optionally filtered to
    one ``state``), skipping any whose payload file has gone."""
    qdir = attachment_quarantine_dir(vault)
    out: list[dict[str, Any]] = []
    if not qdir.is_dir():
        return out
    for meta_path in sorted(qdir.glob("*.json")):
        try:
            m = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        nid = _safe_meta_id(m)
        if not nid:
            continue
        # INT-05: `path` is a MOVE SOURCE (expire -> expired/, accept ->
        # vault/inbox/) and it used to be validated and then used — a window a
        # rename + symlink wins. It is not read at all now: the payload is
        # DERIVED from the guarded id and the real directory entry beside the
        # sidecar, which is where the sweep always put it. The field is
        # overwritten below so no consumer can pick up the mount's version.
        payload = _quarantine_payload(qdir, nid)
        if payload is None:
            continue
        if state is not None and m.get("state", "pending") != state:
            continue
        out.append({**m, "id": nid, "path": str(payload)})
    return out

def _quarantine_payload(qdir: Path, nid: str) -> Path | None:
    """The quarantined payload for ``nid``, from the DIRECTORY ENTRY.

    ``ingest_sweep`` writes it as ``<qdir>/<id><suffix>`` beside the ``.json``
    sidecar, so the id (already proven a bare slug) plus a real dirent is the
    whole address — no attacker-written string participates."""
    try:
        entries = sorted(qdir.iterdir())
    except OSError:
        return None
    for p in entries:
        if p.name == f"{nid}.json" or p.stem != nid:
            continue
        try:
            if p.is_symlink() or not p.is_file():
                continue
        except OSError:
            continue
        return p
    return None

def _attachment_meta_path(vault, aid: str) -> Path:
    return attachment_quarantine_dir(vault) / f"{aid}.json"

def _write_attachment_meta(vault, meta: dict[str, Any]) -> None:
    p = _attachment_meta_path(vault, meta["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    public("_write_atomic")(p, (json.dumps(meta, sort_keys=True) + "\n").encode("utf-8"))

def _discard_attachment(vault, meta: dict[str, Any]) -> dict[str, str]:
    """Remove a quarantined attachment from the funnel — RECOVERABLY (B7).

    Zero residue in the VAULT is the guarantee, and it still holds exactly:
    the file never reached ``vault/inbox/``, so there is no ``raw/`` note, no
    archived original, no index row and no audit entry. But "zero residue"
    must not mean "no copy anywhere" — the sweep MOVED this file out of the
    owner's download location, so an immediate ``unlink`` on a reject (whose
    stated default is `reject all`, behind an opaque ``att-…`` id) destroys
    what may be his only copy. AGENTS.md §9 names deleting a possibly-sole-copy
    as a genuinely owner-only decision, so instead the payload and its sidecar
    move to the SAME GC-windowed ``expired/`` holding area a TTL expiry uses
    (``gc_compact`` clears it after ``$BRAIN_COS_GC_DAYS``, default 30).
    """
    adir = attachment_expired_dir(vault)
    adir.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    src = Path(meta["path"])
    if src.is_symlink() or src.exists():
        dest = _unique_dest(adir, src.name)
        if _move_dirent(src, dest):
            out["expired_payload"] = str(dest)
    meta_path = _attachment_meta_path(vault, meta["id"])
    if meta_path.exists():
        dest = _unique_dest(adir, meta_path.name)
        if _move_dirent(meta_path, dest):
            out["expired_meta"] = str(dest)
    # R3: the payload is out of the funnel, so its lifecycle record must go
    # with it — including a `releasing` record left by a crash before the move,
    # which would otherwise keep claiming an identity nothing backs.
    (attachment_lifecycle_dir(vault) / f"{meta['id']}.json").unlink(missing_ok=True)
    clear_attachment_hold_authz(vault, str(meta["id"]))
    return out

def _write_attachment_lifecycle(vault, record: dict[str, Any]) -> None:
    """Persist ONE lifecycle record DURABLY (fsync, not just write).

    R3: this record IS the identity of a released attachment. A record that
    only reached the page cache is one the recovery path cannot read back, and
    by then the payload has already moved."""
    ldir = attachment_lifecycle_dir(vault)
    ldir.mkdir(parents=True, exist_ok=True)
    public("_write_atomic")(ldir / f"{record['id']}.json",
                  (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))

def _sweep_claims_path(vault) -> Path:
    return ingest_manifest_dir(vault) / "claims.jsonl"

def _manifest_line_key(entry: dict[str, Any]) -> str:
    """Stable identity of ONE manifest line (idempotency key for claims)."""
    return sha256_text(json.dumps(entry, sort_keys=True, separators=(",", ":")))

def _sweep_max_bytes() -> int:
    try:
        return int(os.environ.get(INGEST_SWEEP_MAX_BYTES_ENV,
                                  DEFAULT_INGEST_SWEEP_MAX_BYTES))
    except ValueError:
        return DEFAULT_INGEST_SWEEP_MAX_BYTES

def _sweep_recency_seconds() -> int:
    try:
        return int(os.environ.get(INGEST_SWEEP_RECENCY_ENV,
                                  DEFAULT_INGEST_SWEEP_RECENCY_SECONDS))
    except ValueError:
        return DEFAULT_INGEST_SWEEP_RECENCY_SECONDS

__all__ = ['ingest_manifest_dir', 'attachments_dir', 'attachment_quarantine_dir', 'attachment_expired_dir', 'attachment_lifecycle_dir', '_attachment_lifecycle', '_lifecycle_payload', '_ingested_raw_id', 'attachment_metas', '_quarantine_payload', '_attachment_meta_path', '_write_attachment_meta', '_discard_attachment', '_write_attachment_lifecycle', '_sweep_claims_path', '_manifest_line_key', '_sweep_max_bytes', '_sweep_recency_seconds']

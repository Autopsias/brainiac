"""COS attachment-store operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._attachment_anchors import clear_attachment_hold_authz
from ._guards import _leaf_in, _move_dirent, _safe_meta_id, _unique_dest
from ._io import _append_jsonl, _read_jsonl, _write_atomic
from ._layout import _ts, drop_dir, host_dir

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

def attachment_joins_path(vault=None) -> Path:
    """The BYTES-JOIN claim ledger (ATT-03).

    It sits beside the lifecycle records in the 0700 ``host/`` subtree, which
    the host broker writes and the VM leg has no reason to touch, rather than
    in ``cos-ops/`` where run reports and the review-gate workspace live: this
    is the file lane's own bookkeeping, keyed to the very lifecycle records
    next to it. STATED PLAINLY, because "host-private" is easy to over-read:
    that subtree is still ON the mount (unlike the attachment ANCHORS, which
    INT-04 moved off it), so this is a convention and a mode bit, not a
    boundary.

    It does not have to be one. NOTHING reads this file for authorization —
    :func:`attachment_lane_context` recomputes the whole chain from the
    manifest, the sweep's claims and the payload's content hash on every ask,
    so a forged, truncated or deleted row changes no decision. Its value is
    that a later reader can see WHICH FILE, WHICH CONTENT HASH and WHICH NOTE
    without re-walking the vault.
    """
    return attachments_dir(vault) / "joins.jsonl"

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

#: The HOST's own record that the sweep settled a manifest line WITHOUT
#: keeping bytes. One row per declined line: ``{"key": ..., "disposition":
#: ..., "ts": ...}``.
#: ONE HOST RECORD FOR EVERY SETTLEMENT ANYONE WRITES DOWN.
LINE_SETTLEMENT_SCHEMA = "cos_line_settlement/v1"


def line_settlements_path(vault) -> Path:
    """Where the HOST records that it has STOPPED WAITING for one line.

    WHY THERE IS ONE FILE AND WHY IT IS KEYED BY THE LINE (redesign
    2026-09-05, after four patches to two files failed to close the class).

    Something has to end the vault's wait for an attachment, and two of the
    three things that can are RECORDED rather than computed: the sweep
    declining a line, and a claimed payload leaving the funnel. Both used to
    live in their own ledger with its own shape, and the two shapes disagreed
    about the one thing that matters. The decline ledger was keyed by the
    MANIFEST LINE; the discard ledger was keyed by the ATTACHMENT ID.

    That difference was the bug, four times over. The attachment id reaches
    the reader off the MOUNT — the sweep's claims row names a ``dest``, the
    host takes its basename stem, and the untrusted leg writes that ledger. So
    the host supplied the AUTHORITY ("this id was discarded") while the
    attacker supplied the DESIGNATION ("this line's file is that id"), and one
    real discard anywhere in the vault's history settled a forged line on any
    thread, in any run. Hardy named this in 1988 and Miller gave it its rule:
    *don't separate designation from authority* — a capability must itself
    name the object it authorizes (erights.org/elib/capability/deputy.html).
    Every patch that instead counted payments — one payload settles one line,
    then one payload settles one line per state — was arithmetic on a deputy
    that was still taking the attacker's designation.

    So there is now ONE ledger and its key is the MANIFEST-LINE KEY: the
    sha256 of the bridge's own manifest entry. A settlement designates the
    line it settles, and cannot be re-pointed at a different one. There is
    nothing left to double-spend.

    BE PRECISE ABOUT WHAT THAT BUYS, because an earlier draft of this
    paragraph was WRONG (adversarial review pass 2, 2026-09-05). It said the
    untrusted leg "cannot choose" this key. It can: the key is a hash of a
    manifest entry, the manifest lives under ``drop_dir``, and choosing the
    entry chooses the hash. What the keying buys is that a settlement names
    ONE line and only that line — not that the line itself is honest. The
    untrusted leg still picks a line's ``conversation_id`` and ``filename``,
    so it can attach a real, honestly-declined download to any thread it
    likes. What stops that is a SECOND rule, in
    :func:`~brain.cos._attachment_join.attachment_lane_pending`: a settled
    line pays only for an attachment the thread's own ledger row NAMES. The
    ledger row is host-side, read from the mailbox, and is the only thing here
    the untrusted leg does not write.

    FAIL CLOSED. An unsafe or unreadable receipts root yields no settlements,
    so every line stays owed and its thread stays out of the chip. A row whose
    ``state`` is not one of :data:`RECORDABLE_SETTLEMENTS` is ignored, and so
    is one with no key.

    MEASURED BEFORE SHIPPING (2026-09-05, reference vault): both predecessor
    ledgers — ``sweep-declines.jsonl`` and ``attachment-discards.jsonl`` — are
    ABSENT, zero rows between them, and the lane resolves 431 lines ``joined``,
    756 ``unclaimed``, 3 ``in-funnel`` and ZERO ``withdrawn`` with ``expired/``
    empty. Nothing that ever happened on this host has a settlement to migrate,
    which is why neither old shape is read here: the discard shape CANNOT be
    read safely (it carries no designation, that is the defect) and the decline
    shape has nothing in it to carry over.
    """
    from ._proposal_state import bridge_receipts_root         # noqa: PLC0415

    return bridge_receipts_root(vault) / "line-settlements.jsonl"


def record_line_settlement(vault, *, key: str, state: str, msg_key: str = "",
                           detail: str = "", now: _dt.datetime | None = None
                           ) -> bool:
    """HOST-record that ONE manifest line is settled. False when unwritable.

    Reported, never raised: the sweep's job is to move files, and a host tree
    it cannot write is a degradation for the CALLER to report — refusing the
    whole sweep over it would strand the files it CAN claim. The cost of a
    miss is a line that stays owed, which is the safe direction.
    """
    if not key or state not in RECORDABLE_SETTLEMENTS:
        return False
    try:
        path = line_settlements_path(vault)
    except config.HostPathUnsafe:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        config.secure_file_permissions(path.parent, 0o700)
        _append_jsonl(path, {"schema": LINE_SETTLEMENT_SCHEMA, "key": str(key),
                             "state": str(state),
                             "msg_key": str(msg_key or ""),
                             "detail": public("scrub")(str(detail))[:200],
                             "ts": _ts(now)}, vault=vault)
    except OSError:
        return False
    return True


def line_settlements(vault) -> dict[str, str]:
    """Manifest-line key -> the settled state the HOST recorded for that line.

    FIRST ROW WINS. A settlement is a fact about a line, not a running total,
    so a later row never overturns an earlier one — and an appended second row
    therefore buys nothing even to a writer who reaches this file.
    """
    try:
        path = line_settlements_path(vault)
    except config.HostPathUnsafe:
        return {}
    out: dict[str, str] = {}
    for row in _read_jsonl(path):
        if row.get("schema") != LINE_SETTLEMENT_SCHEMA:
            continue
        key, state = str(row.get("key") or ""), str(row.get("state") or "")
        if key and state in RECORDABLE_SETTLEMENTS:
            out.setdefault(key, state)
    return out


def record_sweep_decline(vault, *, key: str, disposition: str,
                         msg_key: str = "", now: _dt.datetime | None = None
                         ) -> bool:
    """HOST-record one DECLINED manifest line.

    "The sweep refused this line" is one of the things that ends the vault's
    wait for a file. Read off the MOUNT-resident claims ledger it was a bypass
    with a very short exploit — append ``{"key": k, "disposition":
    "refused: ..."}`` to ``cos-ops/drop/ingest-manifest/claims.jsonl`` and an
    attachment-carrying thread settles on its TEXT alone. So it is recorded
    where the untrusted leg cannot reach.
    """
    return record_line_settlement(vault, key=key, state=LINE_DECLINED,
                                  msg_key=msg_key, detail=disposition, now=now)


def record_attachment_withdrawal(vault, meta: dict[str, Any]) -> bool:
    """HOST-record that ONE claimed payload left the funnel with no bytes kept.

    The key comes off the quarantine SIDECAR, which the host wrote when it
    claimed the line, so the withdrawal designates the line it settles. A
    sidecar with no ``manifest_line_key`` settles NOTHING and says so by
    returning False: that is a payload claimed by an older build, and leaving
    its line owed is the safe direction.
    """
    meta = meta or {}
    return record_line_settlement(
        vault, key=str(meta.get("manifest_line_key") or ""),
        state=LINE_WITHDRAWN, msg_key=str(meta.get("msg_key") or ""),
        detail=str(meta.get("id") or ""))


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
    # THE WITNESS THIS LANE'S GATE READS: without it `_line_state` cannot tell
    # a real withdrawal from a forged claims row, so it must not be inferred.
    # It is keyed by the MANIFEST LINE, not by this id — see
    # `line_settlements_path` for why that difference was four separate bugs.
    record_attachment_withdrawal(vault, meta)
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

__all__ = ['ingest_manifest_dir', 'attachments_dir', 'attachment_joins_path', 'attachment_quarantine_dir', 'attachment_expired_dir', 'attachment_lifecycle_dir', '_attachment_lifecycle', '_lifecycle_payload', '_ingested_raw_id', 'attachment_metas', '_quarantine_payload', '_attachment_meta_path', '_write_attachment_meta', '_discard_attachment', 'LINE_SETTLEMENT_SCHEMA', 'line_settlements_path', 'record_line_settlement', 'line_settlements', 'record_sweep_decline', 'record_attachment_withdrawal', '_write_attachment_lifecycle', '_sweep_claims_path', '_manifest_line_key', '_sweep_max_bytes', '_sweep_recency_seconds']

"""COS attachment-acceptance operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._attachment_anchors import clear_attachment_hold_authz, stage_attachment_anchor
from ._attachment_store import _attachment_meta_path, _write_attachment_lifecycle, attachment_lifecycle_dir
from ._guards import _safe_basename, _unique_dest
from ._io import _read_nofollow, _write_atomic
from ._layout import _ts, _utcnow

def _accepted_attachment_bytes(vault, meta: dict[str, Any], *, expected_sha: str | None,
                               expected_name: str | None) -> tuple[Path, Path, bytes, str, str]:
    """Read one host-authorized attachment payload."""
    source = Path(meta["path"])
    inbox = config.vault_root(vault) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    name = _safe_basename(str(expected_name or ""))
    if not name:
        raise ApprovedRefused(
            f"{meta.get('id')}: no host-protected destination name for this attachment — refusing "
            "to release (the sidecar's own `filename` is on the mount beside the payload, and its "
            "SUFFIX chooses which ingest handler parses these bytes)")
    destination = _unique_dest(inbox, name)
    data = _read_nofollow(source)
    sha = hashlib.sha256(data).hexdigest()
    wanted = str(expected_sha or "")
    if not re.fullmatch(r"[0-9a-f]{64}", wanted):
        raise ApprovedRefused(
            f"{meta.get('id')}: no host-protected content hash for this attachment — refusing "
            "to release (the sidecar's own `sha256` is on the mount beside the payload and authorizes nothing)")
    if sha != wanted:
        raise ApprovedRefused(
            f"{meta.get('id')}: quarantined attachment drifted since the owner accepted it "
            f"(accepted {wanted[:12]}…, on disk {sha[:12]}…)")
    return source, destination, data, sha, name


def _attachment_lifecycle(meta: dict[str, Any], *, source: Path, destination: Path,
                          sha: str, name: str, now: _dt.datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build attachment release records."""
    claim = dict(meta.get("provenance") or {})
    for key in ("category", "classification", "msg_key"):
        value = meta.get("tier") if key == "classification" else meta.get(key)
        if value:
            claim[key] = provenance.sanitize_value(value)
    lifecycle = {
        **{key: meta.get(key) for key in (
            "id", "sha256", "filename", "category", "lane", "tier", "rules_version", "pattern",
            "bundle_version", "evidence_unit", "evidence_lineage")},
        "src": str(source), "dest": str(destination), "released": _ts(now),
        "filename": name, "sha256": sha,
    }
    return claim, lifecycle


def _accept_attachment(vault, meta: dict[str, Any], *,
                       expected_sha: str | None = None,
                       expected_name: str | None = None, batch_id: str = "",
                       now: _dt.datetime | None = None) -> str:
    """Release one accepted attachment through its signed anchor."""
    now = now or _utcnow()
    src, dest, data, sha, want_name = _accepted_attachment_bytes(
        vault, meta, expected_sha=expected_sha, expected_name=expected_name)
    record, life = _attachment_lifecycle(
        meta, source=src, destination=dest, sha=sha, name=want_name, now=now)
    public("_write_attachment_lifecycle")(vault, {**life, "state": "releasing"})
    public("stage_attachment_anchor")(vault, dest, sha256_hex=sha, aid=str(meta["id"]),
                            batch_id=batch_id, claim=record, now=now)
    public("_write_atomic")(dest, data)
    src.unlink(missing_ok=True)
    # B3: keep the identity alive past the sidecar so a later undo can find
    # the unsigned inbox copy — or, once ingested, the raw/ note it became.
    public("_write_attachment_lifecycle")(vault, {**life, "state": "released"})
    _attachment_meta_path(vault, meta["id"]).unlink(missing_ok=True)
    clear_attachment_hold_authz(vault, str(meta["id"]))
    return str(dest)

def attachment_release_records(vault) -> list[dict[str, str]]:
    """Every attachment this host RELEASED into ``vault/inbox/``: id and the
    verified content sha.

    The fail-CLOSED half of INT-04. ``verify_attachment_bytes`` answers ``None``
    = "ordinary inbox drop" when no anchor covers a file, which is right for a
    file the owner simply dropped in — and wrong for one the attachment lane
    released, because "no anchor" then means the anchor was LOST (the GC window,
    a repointed ``$BRAIN_INDEX_DIR``, a deleted index dir) and the file would
    ingest at ``Internal`` instead of its email-derived MNPI floor. Every other
    arm of this design fails closed; this was the one that failed open.

    The caller (``ingest.run_ingest``) drops records whose sha is already in the
    ingest manifest — those are spent, and a later unrelated drop reusing the
    name must not inherit their refusal.

    These records live on the mount, so this is a SAFETY NET, not an
    authorization: planting one can only cause a refusal, never a release. What
    a VM CAN do is delete or corrupt them — so an unreadable record (or an
    unlistable directory) raises :class:`ReleaseRecordsUnreadable` instead of
    being skipped. The caller halts the drain on it: the cost of the fail-open
    is signing released material back down to ``Internal``, the cost of the
    fail-closed is a loud, recoverable, self-inflicted ingest stall.

    Only the SHA is returned. Matching on the bare destination name refused an
    owner's unrelated same-named drop for the whole GC window, under a defect
    that claimed the host had released it.
    """
    d = attachment_lifecycle_dir(vault)
    out: list[dict[str, str]] = []
    if not d.is_dir():
        return out
    try:
        entries = sorted(d.glob("*.json"))
    except OSError as exc:
        raise ReleaseRecordsUnreadable(
            f"{d}: {type(exc).__name__}: {exc}") from None
    for p in entries:
        try:
            rec = json.loads(_read_nofollow(p).decode("utf-8"))
        except (ApprovedRefused, OSError, ValueError, UnicodeDecodeError) as exc:
            raise ReleaseRecordsUnreadable(
                f"{p.name}: {type(exc).__name__}: {exc}") from None
        if not isinstance(rec, dict) or rec.get("state") not in (
                "releasing", "released"):
            continue
        out.append({"id": str(rec.get("id") or "?"),
                    "sha256": str(rec.get("sha256") or "")})
    return out

__all__ = ['_accept_attachment', 'attachment_release_records']

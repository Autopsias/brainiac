"""COS approval-queue operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._io import _read_nofollow, _write_atomic
from ._layout import _ts

def _host_private_base() -> Path:
    """The host-controlled root for state the VM must not reach at all.

    ONE definition, in ``config`` — the single-writer lock moved here too
    (INT-05), so a second copy of this rule is exactly what must not exist."""
    return config.host_private_base()

def approved_queue_dir(vault=None) -> Path:
    """Where owner-accepted candidates wait for the audited signature.

    HOST-ONLY, deliberately NOT under ``<vault>/.brain/`` — and deliberately NOT
    resolved through ``config.index_dir`` either. That function is keyed on
    ``<vault>/.brain/vault-id`` when one exists (move-stability for the INDEX,
    which is correct and unchanged), but that file is ON THE MOUNT: rewriting it
    re-pointed this directory at another vault's app-data, after which the drain
    and ``brain status`` looked in an empty directory while the ONLY copy of
    owner-approved content sat, stranded, in the old one.

    So both components are host-controlled: an explicit ``$BRAIN_INDEX_DIR``
    (host configuration, not vault content) else the per-user app-data base,
    then the path-derived identity the signature also binds. Nothing a VM can
    write participates."""
    return _host_private_base() / _APPROVED_DIRNAME / approved_vault_identity(vault)

def _proven_off_mount(d: Path, vault, *, what: str) -> Path:
    """``d`` resolved and PROVEN outside every VM-visible root, or refused.

    Raises :class:`ApprovedQueueUnsafe` (a ``config.HostPathUnsafe``) otherwise.
    Thin adapter over ``config.proven_off_mount`` so the approved queue, the
    attachment-anchor store (INT-04) and the writer lock (INT-05) share ONE
    implementation — a second copy of a verification rule is how the first one
    ends up subtly weaker."""
    try:
        return config.proven_off_mount(d, vault, what=what)
    except config.HostPathUnsafe as exc:
        raise ApprovedQueueUnsafe(str(exc)) from None

def approved_queue_root(vault=None) -> Path:
    """``approved_queue_dir`` resolved and PROVEN off every VM-visible root."""
    return _proven_off_mount(approved_queue_dir(vault), vault,
                             what="approved queue")

def _approved_ensure(vault) -> Path:
    """Create the queue. ONLY the staging path calls this — a read-side helper
    must not materialise directories as a side effect."""
    d = approved_queue_root(vault)
    d.mkdir(parents=True, exist_ok=True)
    config.secure_file_permissions(d, 0o700)
    return d

def approved_payload_path(vault, nid: str) -> Path:
    return approved_queue_root(vault) / f"{safe_slug(nid)}.md"

def approved_anchor_path(vault, nid: str) -> Path:
    return approved_queue_root(vault) / f"{safe_slug(nid)}.anchor.json"

def approved_vault_identity(vault=None) -> str:
    """The ONE identity of the vault an anchor belongs to: ``vault_slug8``.

    Without a vault bound INTO the signed body, one account's key signs an
    anchor that verifies in ANY vault it is copied to — the pair replays across
    vaults.

    It is the hash of the RESOLVED VAULT PATH, and nothing else, because that is
    the only identity the VM cannot rewrite. ``.brain/vault-id`` is move-stable
    but it is a plain file on the shared mount: an earlier revision accepted
    EITHER, which meant overwriting that one file with another vault's id made
    that vault's anchors verify here. An OR between an immutable identity and a
    mutable one is only as strong as the mutable half.

    The cost is deliberate and bounded: MOVING a vault changes this identity, so
    anything still queued at the moment of the move stops verifying. Drain first
    (``brain sync`` — the queue is normally empty; the hourly job empties it),
    or re-decide the affected candidates. A host-authorised rebinding migration
    is the upgrade path if that ever becomes a real workflow."""
    return config.vault_slug8(vault)

def _identity_binds(vault, body: dict[str, Any]) -> bool:
    """True when a signed body names THIS vault. One identity, no OR."""
    return body.get("vault") == approved_vault_identity(vault)

def _anchor_body(nid: str, sha: str, batch_id: str, accepted_at: str, *,
                 vault_identity: str, kind: str) -> str:
    return json.dumps({"schema": APPROVED_ANCHOR_SCHEMA, "id": nid,
                       "sha256": sha, "batch_id": batch_id,
                       "accepted_at": accepted_at, "vault": vault_identity,
                       "kind": kind},
                      sort_keys=True, separators=(",", ":"))

def approved_verify_key(vault=None):
    """Resolve the material anchor verification needs, ONCE per drain.

    Raises :class:`ApprovedKeyUnavailable` for every "this host cannot do
    crypto right now" cause — no key, locked keychain, wrong scheduler user,
    ``cryptography`` missing. Callers resolve it up front and hand it to every
    per-item check, so a key outage can never be mistaken for tampering by an
    item-level verification that fails for the wrong reason."""
    from .. import audit

    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        return load_pem_public_key(audit.public_key_pem())
    except Exception as exc:  # noqa: BLE001 — every cause is the same answer
        raise ApprovedKeyUnavailable(f"{type(exc).__name__}: {exc}") from None

def stage_approved(vault, nid: str, text: str, *, sha256_hex: str,
                   batch_id: str, kind: str = "proposal",
                   now: _dt.datetime | None = None) -> Path:
    """Park APPROVED bytes in the host-only queue under a signed anchor.

    Fails closed on every arm: a hash that does not match what was approved, a
    queue the VM could reach, or no signing key -> nothing is staged, and the
    caller leaves the candidate where it was."""
    from .. import audit

    if sha256_text(text) != sha256_hex:
        raise ApprovedRefused(
            f"{nid}: refusing to stage bytes that do not match the approved sha")
    nid = safe_slug(nid)
    _approved_ensure(vault)                          # raises if the queue is unsafe
    payload = approved_payload_path(vault, nid)
    body = _anchor_body(nid, sha256_hex, batch_id, _ts(now),
                        vault_identity=approved_vault_identity(vault), kind=kind)
    key_obj, _src = audit.resolve_signing_key()      # KeyUnavailable -> fail closed
    sig = key_obj.sign(body.encode("utf-8")).hex()
    # Payload first, anchor second: a crash between the two leaves an
    # unanchored payload, which the drain REFUSES. The reverse order would
    # leave an anchor whose payload the next replay could supply.
    public("_write_atomic")(payload, text.encode("utf-8"))
    public("_write_atomic")(approved_anchor_path(vault, nid),
                  (json.dumps({"body": body, "sig": sig}, sort_keys=True) + "\n")
                  .encode("utf-8"))
    return payload

def approved_anchor(vault, nid: str, *, pubkey=None) -> dict[str, Any] | None:
    """The anchor for ``nid`` — parsed ONLY if its host signature verifies.

    Returns ``None`` for missing, malformed, unsigned, wrong-schema,
    id-mismatched or FOREIGN-VAULT records: every one means "no host approval
    exists here for these bytes". ``pubkey`` is the once-resolved verification
    key; without it this resolves the key itself and therefore propagates
    :class:`ApprovedKeyUnavailable` rather than reporting a key outage as a
    missing approval."""
    try:
        nid = safe_slug(nid)
    except ValueError:
        return None
    if pubkey is None:
        pubkey = approved_verify_key(vault)          # may raise KeyUnavailable
    try:
        rec = json.loads(_read_nofollow(approved_anchor_path(vault, nid))
                         .decode("utf-8"))
    except (ApprovedQueueUnsafe, ApprovedRefused, OSError, ValueError,
            UnicodeDecodeError):
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("body"), str):
        return None
    try:
        pubkey.verify(bytes.fromhex(str(rec.get("sig", ""))),
                      rec["body"].encode("utf-8"))
        body = json.loads(rec["body"])
    except Exception:  # noqa: BLE001 — a bad signature is "not host-approved"
        return None
    if (not isinstance(body, dict)
            or body.get("schema") != APPROVED_ANCHOR_SCHEMA
            or body.get("id") != safe_slug(nid)
            or not isinstance(body.get("sha256"), str)
            # one account key signs for every vault it owns, so the anchor must
            # name ITS vault or a copied pair replays in the next one
            or not _identity_binds(vault, body)):
        return None
    return body

def approved_payload_path_or_none(vault, nid: str) -> Path | None:
    """The payload path, or ``None`` when no queue is reachable (reporting)."""
    try:
        return approved_payload_path(vault, nid)
    except (ApprovedQueueUnsafe, ValueError):
        return None

def approved_anchor_path_or_none(vault, nid: str) -> Path | None:
    try:
        return approved_anchor_path(vault, nid)
    except (ApprovedQueueUnsafe, ValueError):
        return None

def approved_queued(vault, nid: str) -> bool:
    """True when ANY trace of ``nid`` is in the queue — no crypto involved.

    This is the right question for "is it still waiting?" (undo, status): a key
    outage must not make a parked item look absent, which is what asking
    :func:`approved_staged` would do."""
    try:
        return (approved_payload_path(vault, nid).exists()
                or approved_anchor_path(vault, nid).exists())
    except (ApprovedQueueUnsafe, ValueError):
        return False

def approved_staged(vault, nid: str) -> bool:
    """True when ``nid`` is already parked with a valid anchor (replay-safe)."""
    try:
        return (approved_payload_path(vault, nid).exists()
                and approved_anchor(vault, nid) is not None)
    except (ApprovedQueueUnsafe, ApprovedKeyUnavailable, ValueError):
        return False

def read_approved(vault, path: Path, *, pubkey=None) -> tuple[str, str]:
    """Return ``(text, sha256)`` for one queued payload, or REFUSE.

    The file is opened ONCE (no-follow) and the hash is taken over the bytes
    actually read, so the caller can sign that exact buffer without ever
    re-opening the path.

    Every failure arm raises :class:`ApprovedRefused` — including a filename
    that is not a safe slug. A bare ``ValueError`` escaping here would abort the
    whole drain (and the surrounding ``sync``) on one bad file. A key outage is
    :class:`ApprovedKeyUnavailable` instead, and must NOT be quarantined."""
    try:
        nid = safe_slug(path.stem)
    except ValueError as exc:
        raise ApprovedRefused(f"{path.name}: unsafe queued filename ({exc})") from None
    anchor = approved_anchor(vault, nid, pubkey=pubkey)
    if anchor is None:
        raise ApprovedRefused(
            f"{nid}: no valid host-signed approval anchor for these bytes")
    data = _read_nofollow(path)
    sha = hashlib.sha256(data).hexdigest()
    if sha != anchor["sha256"]:
        raise ApprovedRefused(
            f"{nid}: content drifted since the owner approved it "
            f"(anchor {anchor['sha256'][:12]}…, on disk {sha[:12]}…)")
    try:
        return data.decode("utf-8"), sha
    except UnicodeDecodeError:
        raise ApprovedRefused(f"{nid}: approved payload is not valid UTF-8") from None

def anchor_still_binds(vault, nid: str, sha: str, *, pubkey=None) -> bool:
    """Re-verify the anchor against a buffer already in hand — the check the
    drain repeats INSIDE the signing critical section (a consume-time check
    alone is TOCTOU)."""
    anchor = approved_anchor(vault, nid, pubkey=pubkey)
    return bool(anchor and anchor.get("sha256") == sha)

__all__ = ['_host_private_base', 'approved_queue_dir', '_proven_off_mount', 'approved_queue_root', '_approved_ensure', 'approved_payload_path', 'approved_anchor_path', 'approved_vault_identity', '_identity_binds', '_anchor_body', 'approved_verify_key', 'stage_approved', 'approved_anchor', 'approved_payload_path_or_none', 'approved_anchor_path_or_none', 'approved_queued', 'approved_staged', 'read_approved', 'anchor_still_binds']

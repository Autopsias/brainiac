"""COS attachment-anchor operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from ._facade import public
from ._approval import _host_private_base, _identity_binds, _proven_off_mount, approved_vault_identity, approved_verify_key
from ._guards import _safe_basename
from ._io import _read_nofollow, _write_atomic
from ._layout import _env_days, _parse_ts, _ts, _utcnow

def attachment_anchor_dir(vault=None) -> Path:
    """Host-private store of accepted-attachment anchors (never on the mount)."""
    return (_host_private_base() / _ATTACHMENT_ANCHOR_DIRNAME
            / approved_vault_identity(vault))

def attachment_anchor_root(vault=None) -> Path:
    return _proven_off_mount(attachment_anchor_dir(vault), vault,
                             what="attachment anchor store")

def _attachment_dest_ref(dest: Path | str) -> str:
    """The identity of an anchored destination: ``inbox/<bare name>``.

    Only the BASENAME participates. This lane's destination is always
    ``<vault>/inbox/``, and reducing the reference to a bare name means no
    absolute path — from a sidecar, a lifecycle record or an anchor — is ever
    joined, compared or resolved as one."""
    name = _safe_basename(Path(str(dest)).name)
    if name is None:
        raise ValueError(f"unsafe anchored destination {str(dest)[:60]!r}")
    return f"inbox/{name}"

def attachment_anchor_path(vault, dest: Path | str) -> Path:
    ref = _attachment_dest_ref(dest)
    key = hashlib.sha256(ref.encode("utf-8")).hexdigest()[:32]
    return attachment_anchor_root(vault) / f"{key}.json"

def attachment_anchor_sha_path(vault, sha256_hex: str) -> Path:
    """The same anchor, filed by the CONTENT it covers (see the note above)."""
    sha = str(sha256_hex or "")
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError(f"not a sha256 hex digest: {sha[:16]!r}")
    return attachment_anchor_root(vault) / f"sha-{sha}.json"

def _attachment_anchor_body(ref: str, sha: str, *, aid: str, batch_id: str,
                            accepted_at: str, vault_identity: str,
                            claim: dict[str, Any]) -> str:
    return json.dumps({"schema": ATTACHMENT_ANCHOR_SCHEMA, "dest": ref,
                       "sha256": sha, "id": aid, "batch_id": batch_id,
                       "accepted_at": accepted_at, "vault": vault_identity,
                       "claim": claim},
                      sort_keys=True, separators=(",", ":"))

def stage_attachment_anchor(vault, dest: Path | str, *, sha256_hex: str,
                            aid: str, batch_id: str = "",
                            claim: dict[str, Any] | None = None,
                            now: _dt.datetime | None = None) -> Path:
    """Sign the owner's acceptance of exactly ``sha256_hex`` at ``dest``.

    Fails closed on every arm (unsafe store location, no signing key) — the
    caller then leaves the attachment in quarantine rather than releasing it
    unanchored."""
    from .. import audit

    ref = _attachment_dest_ref(dest)
    d = attachment_anchor_root(vault)
    d.mkdir(parents=True, exist_ok=True)
    config.secure_file_permissions(d, 0o700)
    _prune_attachment_anchors(d, vault=vault, now=now)
    # `verified` is a HOST assertion earned only by parsing the archived
    # original; nothing that rode in on a VM manifest line may carry it into
    # bytes the drain will sign (`provenance.HOST_ONLY_KEYS`, one lane over).
    record = {k: v for k, v in provenance.scrub(dict(claim or {})).items()
              if k != "verified"}
    # HOST-derived, never copied from the mount: this function IS the
    # attachment lane, and the lane stamp is what makes the ingest drain treat
    # the file as email-derived (floor: MNPI). Left to the sidecar it was a
    # DOWNGRADE path — strip `provenance` from the sidecar before the accept
    # and the drain saw no email fields, so the material landed at `Internal`
    # and became visible at the VM egress cap.
    record["lane"] = LANE_ATTACHMENT
    body = _attachment_anchor_body(
        ref, sha256_hex, aid=safe_slug(aid), batch_id=str(batch_id or ""),
        accepted_at=_ts(now), vault_identity=approved_vault_identity(vault),
        claim=record)
    key_obj, _src = audit.resolve_signing_key()   # KeyUnavailable -> fail closed
    sig = key_obj.sign(body.encode("utf-8")).hex()
    blob = (json.dumps({"body": body, "sig": sig}, sort_keys=True)
            + "\n").encode("utf-8")
    path = attachment_anchor_path(vault, dest)
    public("_write_atomic")(path, blob)
    # ...and the same record by CONTENT, so a rename cannot strip the claim.
    # One file per digest, so two messages carrying IDENTICAL bytes would
    # otherwise overwrite each other's signed claim and content-only recovery
    # would attach whichever record won to either file — another email's
    # provenance and category. An ambiguous digest keeps the FIRST record and
    # raises a marker; recovery by content then refuses (the destination-keyed
    # anchor still covers both files, so nothing legitimate is lost except the
    # rename convenience).
    sha_path = attachment_anchor_sha_path(vault, sha256_hex)
    prior = _anchor_body_unverified(sha_path)
    if prior is not None and prior.get("dest") != ref:
        public("_write_atomic")(sha_path.with_suffix(".ambiguous"), b"{}\n")
    else:
        public("_write_atomic")(sha_path, blob)
    return path

def _anchor_body_unverified(path: Path) -> dict[str, Any] | None:
    """The ``body`` of an anchor file WITHOUT checking its signature.

    Only ever used to decide what to DELETE or whether a digest already has an
    owner — never to authorize anything. Every authorization path goes through
    ``_verified_attachment_anchor``."""
    try:
        body = json.loads(json.loads(path.read_text(encoding="utf-8"))["body"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return body if isinstance(body, dict) else None

def _prune_attachment_anchors(d: Path, *, vault=None,
                              now: _dt.datetime | None = None) -> None:
    """Drop anchors whose destination never arrived, past the GC window.

    An accepted attachment is normally ingested within the hour; one whose
    file never landed would otherwise keep a refusal armed forever.

    An anchor whose destination IS still sitting in ``vault/inbox/`` is NEVER
    pruned, however long it has waited. Pruning it would turn owner-accepted,
    email-derived (floor: MNPI) material into an unanchored ordinary drop that
    ingests at ``Internal`` — the exact classification downgrade the content key
    exists to prevent, performed by our own garbage collector."""
    cutoff = ((now or _utcnow()).timestamp()
              - _env_days(GC_DAYS_ENV, DEFAULT_GC_DAYS) * 86400)
    try:
        inbox = config.vault_root(vault) / "inbox"
    except Exception:  # noqa: BLE001 — no vault resolvable: prune by age only
        inbox = None
    for p in list(d.glob("*.json")) + list(d.glob("*.ambiguous")):
        try:
            if p.stat().st_mtime >= cutoff:
                continue
            if inbox is not None:
                body = _anchor_body_unverified(p) or {}
                name = _safe_basename(Path(str(body.get("dest") or "")).name)
                if name and (inbox / name).exists():
                    continue
            p.unlink(missing_ok=True)
        except OSError:
            pass

def attachment_anchors_awaiting_drain(vault=None) -> int:
    """How many ACCEPTED ATTACHMENTS are still waiting for the ingest drain.

    Counts destination-keyed acceptance anchors only — the content key
    (``sha-…``) and the auto-hold authorization (``hold-…``) are the same
    acceptance filed twice, so counting files would report 2 for one waiting
    attachment.

    Reported by ``brain status`` and ``brain rebuild`` for the same reason the
    approved queue is: this store is NOT rebuildable from ``vault/``, and losing
    it (deleting the index dir, repointing ``$BRAIN_INDEX_DIR``) makes the drain
    refuse the material it covers. Never raises."""
    try:
        d = attachment_anchor_root(vault)
    except ApprovedQueueUnsafe:
        return 0
    if not d.is_dir():
        return 0
    return sum(1 for p in d.glob("*.json")
               if not p.name.startswith(("hold-", "sha-")))

def _verified_attachment_anchor(vault, path: Path, *, pubkey,
                                expect_dest: str | None = None,
                                expect_sha: str | None = None,
                                expect_id: str | None = None,
                                schema: str = ATTACHMENT_ANCHOR_SCHEMA
                                ) -> dict[str, Any] | None:
    """Parse ONE signed host record, ONLY if its signature verifies and it names
    this vault (plus whichever of destination/content/id the caller looked it up
    by). ``None`` for every "no host approval exists here" answer.

    ONE routine behind every lookup — the acceptance anchor's two keys AND the
    auto-hold authorization (``schema=ATTACHMENT_HOLD_SCHEMA``). A second copy
    of a verification routine is how the first one ends up subtly weaker."""
    try:
        rec = json.loads(_read_nofollow(path).decode("utf-8"))
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
            or body.get("schema") != schema
            or not isinstance(body.get("sha256"), str)
            or not _identity_binds(vault, body)
            or (expect_dest is not None and body.get("dest") != expect_dest)
            or (expect_sha is not None and body.get("sha256") != expect_sha)
            or (expect_id is not None and body.get("id") != expect_id)):
        return None
    return body

def attachment_hold_authz_path(vault, aid: str) -> Path:
    return attachment_anchor_root(vault) / f"hold-{safe_slug(aid)}.json"

def stage_attachment_hold_authz(vault, aid: str, *, sha256_hex: str,
                                not_before: str, filename: str,
                                category: str = "",
                                now: _dt.datetime | None = None) -> Path:
    """Sign "this host placed <aid> on hold for exactly these bytes, under this
    destination name and this category, until <not_before>".

    Fails closed (no key, unsafe store, unsafe name) — the caller then leaves
    the attachment in the owner's pending queue rather than holding it.

    ``filename`` and ``category`` are signed alongside the hash (round 3,
    CRITICAL). Binding the BYTES bound nothing about how they would be READ:
    release took the destination name from the mount-resident sidecar, so
    keeping the authorized bytes and rewriting ``filename`` to another suffix
    picked a different ingest handler for them (a polyglot is the sharp case),
    and dropping ``category`` made ``_still_eligible_at_release`` read an auto
    hold as operator-placed and skip the demotion re-check. An authorization
    has to cover the parser and the policy context, not just the content."""
    from .. import audit

    sha = str(sha256_hex or "")
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError(f"not a sha256 hex digest: {sha[:16]!r}")
    name = _safe_basename(filename)
    if not name:
        raise ValueError(f"unsafe destination filename {str(filename)[:60]!r}")
    d = attachment_anchor_root(vault)
    d.mkdir(parents=True, exist_ok=True)
    config.secure_file_permissions(d, 0o700)
    body = json.dumps({"schema": ATTACHMENT_HOLD_SCHEMA, "id": safe_slug(aid),
                       "sha256": sha, "not_before": str(not_before),
                       "filename": name, "category": str(category or ""),
                       "created": _ts(now),
                       "vault": approved_vault_identity(vault)},
                      sort_keys=True, separators=(",", ":"))
    key_obj, _src = audit.resolve_signing_key()   # KeyUnavailable -> fail closed
    sig = key_obj.sign(body.encode("utf-8")).hex()
    path = attachment_hold_authz_path(vault, aid)
    public("_write_atomic")(path, (json.dumps({"body": body, "sig": sig}, sort_keys=True)
                         + "\n").encode("utf-8"))
    return path

def attachment_hold_authz(vault, aid: str, *, pubkey=None
                          ) -> dict[str, Any] | None:
    """The signed hold authorization for ``aid`` — or ``None`` (refuse)."""
    if pubkey is None:
        pubkey = public("approved_verify_key")(vault)       # may raise KeyUnavailable
    try:
        path = attachment_hold_authz_path(vault, aid)
    except (ApprovedQueueUnsafe, ValueError):
        return None
    body = _verified_attachment_anchor(vault, path, pubkey=pubkey,
                                       expect_id=safe_slug(aid),
                                       schema=ATTACHMENT_HOLD_SCHEMA)
    if body is None or _parse_ts(str(body.get("not_before", ""))) is None:
        return None
    # An authorization with no signed destination name came from a pre-round-3
    # engine: it binds the bytes but not the handler that will parse them, so
    # it is not an authorization this release path can honour. Same answer as a
    # missing one — the item stays held and the defect names why.
    if not _safe_basename(str(body.get("filename") or "")):
        return None
    return body

def clear_attachment_hold_authz(vault, aid: str) -> None:
    """Retire a hold authorization once it is spent (released or discarded)."""
    try:
        attachment_hold_authz_path(vault, aid).unlink(missing_ok=True)
    except (ApprovedQueueUnsafe, ValueError, OSError):
        pass

def attachment_anchor(vault, dest: Path | str, *, pubkey=None
                      ) -> dict[str, Any] | None:
    """The anchor covering ``dest`` — verified, and naming this destination."""
    if pubkey is None:
        pubkey = public("approved_verify_key")(vault)       # may raise KeyUnavailable
    try:
        ref = _attachment_dest_ref(dest)
        path = attachment_anchor_path(vault, dest)
    except (ApprovedQueueUnsafe, ValueError):
        return None
    return _verified_attachment_anchor(vault, path, pubkey=pubkey,
                                       expect_dest=ref)

def attachment_anchor_for_bytes(vault, sha256_hex: str, *, pubkey=None,
                                suffix: str | None = None
                                ) -> dict[str, Any] | None:
    """The anchor covering exactly these BYTES, whatever they are called now.

    Two limits on this recovery, both deliberate:

    * an AMBIGUOUS digest (identical bytes accepted from two different
      messages) has no single owner, so there is no honest answer to "whose
      provenance does this carry" — refuse rather than pick one;
    * ``suffix`` binds the ingest HANDLER. The bytes may be renamed; renaming
      ``x.txt`` to ``x.html`` is not a rename, it is a different parser over
      owner-accepted content, so the extension must survive the rename.
    """
    if pubkey is None:
        pubkey = public("approved_verify_key")(vault)       # may raise KeyUnavailable
    try:
        path = attachment_anchor_sha_path(vault, sha256_hex)
    except (ApprovedQueueUnsafe, ValueError):
        return None
    if path.with_suffix(".ambiguous").exists():
        return None
    body = _verified_attachment_anchor(vault, path, pubkey=pubkey,
                                       expect_sha=sha256_hex)
    if body is None:
        return None
    if suffix is not None and Path(str(body.get("dest") or "")).suffix.lower() \
            != suffix.lower():
        return None
    return body

def attachment_anchor_exists(vault, dest: Path | str,
                             sha256_hex: str | None = None) -> bool:
    """True when SOME anchor file covers ``dest`` (or these bytes) — no key,
    no crypto.

    The question a key outage must still be able to answer: "were these bytes
    owner-accepted?" is unanswerable without the key, and answering it "no"
    would sign them unverified."""
    for factory in (lambda: attachment_anchor_path(vault, dest),
                    lambda: attachment_anchor_sha_path(vault, sha256_hex or "")):
        try:
            if factory().exists():
                return True
        except (ApprovedQueueUnsafe, ValueError):
            return False
    return False

def verify_attachment_bytes(vault, dest: Path | str, data: bytes, *,
                            pubkey=None) -> dict[str, Any] | None:
    """The check the ingest drain makes on the buffer it is about to sign.

    ``None``  -> no anchor covers this destination: an ordinary inbox drop,
                 handled exactly as before.
    a mapping -> the host-signed acceptance record for these EXACT bytes.
    raises :class:`ApprovedRefused` -> an anchor exists and the bytes are not
                 the accepted ones (or the anchor is unreadable/foreign).
    raises :class:`ApprovedKeyUnavailable` -> anchored, but this host cannot
                 verify right now. Never signed on a guess."""
    sha = hashlib.sha256(data).hexdigest()
    if not attachment_anchor_exists(vault, dest, sha):
        return None
    anchor = attachment_anchor(vault, dest, pubkey=pubkey)
    if anchor is not None:
        if sha != anchor["sha256"]:
            raise ApprovedRefused(
                f"{Path(str(dest)).name}: content drifted since the owner "
                f"accepted it (anchor {anchor['sha256'][:12]}…, on disk "
                f"{sha[:12]}…)")
        return anchor
    # No verified anchor for the NAME. Either these bytes are the accepted
    # ones under a different name (the rename case — the acceptance follows
    # the content), or the name is anchored and the record does not verify,
    # which is a refusal, not a downgrade to "ordinary drop".
    by_bytes = attachment_anchor_for_bytes(
        vault, sha, pubkey=pubkey, suffix=Path(str(dest)).suffix)
    if by_bytes is not None:
        return by_bytes
    raise ApprovedRefused(
        f"{Path(str(dest)).name}: an owner-acceptance anchor covers this "
        f"destination but does not verify against these bytes (unsigned, "
        f"wrong schema, or another vault's) — refusing to sign them")

def clear_attachment_anchor(vault, dest: Path | str,
                            sha256_hex: str | None = None) -> bool:
    """Retire the anchor once its bytes are signed (or withdrawn) — BOTH keys.

    The content key is read back off the destination record when the caller
    does not have it (an undo has no buffer to hash); a leftover content anchor
    is inert either way and the GC window clears it."""
    paths: list[Path] = []
    try:
        paths.append(attachment_anchor_path(vault, dest))
    except (ApprovedQueueUnsafe, ValueError):
        return True
    if not sha256_hex:
        try:    # best-effort, no signature needed to decide what to DELETE
            rec = json.loads(paths[0].read_text(encoding="utf-8"))
            sha256_hex = json.loads(rec["body"])["sha256"]
        except (OSError, ValueError, KeyError, TypeError):
            sha256_hex = None
    if sha256_hex:
        try:
            paths.append(attachment_anchor_sha_path(vault, sha256_hex))
        except (ApprovedQueueUnsafe, ValueError):
            pass
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    return not any(p.exists() for p in paths)

__all__ = ['attachment_anchor_dir', 'attachment_anchor_root', '_attachment_dest_ref', 'attachment_anchor_path', 'attachment_anchor_sha_path', '_attachment_anchor_body', 'stage_attachment_anchor', '_anchor_body_unverified', '_prune_attachment_anchors', 'attachment_anchors_awaiting_drain', '_verified_attachment_anchor', 'attachment_hold_authz_path', 'stage_attachment_hold_authz', 'attachment_hold_authz', 'clear_attachment_hold_authz', 'attachment_anchor', 'attachment_anchor_for_bytes', 'attachment_anchor_exists', 'verify_attachment_bytes', 'clear_attachment_anchor']

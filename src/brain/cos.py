"""COS host-engine capabilities (CUT-01E) — proposal broker, evidence signer,
correction transport, priority-map generator, auto-capture hold store.

Every verb here that mutates state of record is HOST-BROKER ONLY. The single
VM-allowed ingress is ``cos-propose``: an UNSIGNED drop into a proposal-drop
directory that ``brain sync`` NEVER reads — a proposal can only reach the
signed write path through the broker's validate → owner-inbox-batch →
on-accept → APPROVED-QUEUE flow (the queue is host-only and off the VM mount —
see the INT-01 section below).

Directory layout (``config.cos_ops_dir``, default ``<vault>/.brain/cos`` —
gitignored wholesale via ``vault/.brain/``, never indexed, never exported),
split by PERMISSION:

    cos/
    ├── host/                # (a) HOST-PRIVATE (0700) — the VM never reads or
    │   │                    #     writes below here.
    │   ├── evidence/        #     signed trust-gate evidence bundles
    │   ├── proposals/       #     broker queue OF RECORD
    │   │   ├── pending/     #       claimed, validated, awaiting a batch/answer
    │   │   ├── rejected/    #       failed validation / owner-rejected
    │   │   ├── expired/     #       TTL-expired (GC-compacted later)
    │   │   ├── corrections-pending/  # VM correction drops awaiting the owner
    │   │   ├── claims.jsonl #       content-hash claim ledger (anti-replay)
    │   │   └── batches.jsonl#       cos_ingestion_batch records (state machine)
    │   ├── hold/            #     auto-capture hold store (not_before gated)
    │   └── corrections.sqlite  #  correction_events of record (append-only)
    ├── shared/              # (b) VM-READABLE projection (0755) — host writes,
    │   └── priority-map.md  #     VM reads. Never host-private content.
    └── drop/                # (c) VM-WRITABLE input (0775) — VM appends, host
        ├── proposal-drop/   #     claims. `brain sync` IGNORES all of drop/.
        └── verdict-drop/    #     shadow-ledger + correction drops (s04)

Proposal lifecycle: dropped → pending (claimed+validated) → queued (member of
an open, signed ``cos_ingestion_batch`` owner-inbox question) → accepted
(staged into the host-only approved queue under an Ed25519-signed content
anchor, then signed by the ordinary host drain) | rejected |
expired (TTL) → GC. Backpressure: at most ONE open batch at a time (the owner
queue is ~5-capped); new proposals aggregate into the next batch. A batch
answer is verified against the batch's Ed25519-signed candidate-set digest,
must be a SUBSET of the batch, and is consumed exactly once — a replayed or
late (post-expiry) answer is rejected.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat as _stat_mod
import time
from pathlib import Path
from typing import Any

from . import config, frontmatter, provenance
from .lock import vault_writer_lock
from .notes import MAX_SLUG_BYTES, safe_slug, sha256_text

# -- tunables ---------------------------------------------------------------
PROPOSAL_TTL_DAYS_ENV = "BRAIN_COS_PROPOSAL_TTL_DAYS"
DEFAULT_PROPOSAL_TTL_DAYS = 14
BATCH_TTL_DAYS_ENV = "BRAIN_COS_BATCH_TTL_DAYS"
DEFAULT_BATCH_TTL_DAYS = 7
GC_DAYS_ENV = "BRAIN_COS_GC_DAYS"
DEFAULT_GC_DAYS = 30

BATCH_SCHEMA = "cos_ingestion_batch/v1"
EVIDENCE_SCHEMA = "cos-evidence-manifest/v1"
BROKER_KEY_PREFIX = "cosbroker:"
CORRECT_KEY_PREFIX = "coscorrect:"

# Answer grammar for a cos_ingestion_batch owner question.
_ACCEPT_ALL = "accept all"
_REJECT_ALL = "reject all"
_ACCEPT_PARTIAL_RE = re.compile(r"^accept\s*:\s*(?P<ids>[a-z0-9,\s-]+?)\s*(?:\(.*\))?$",
                                re.IGNORECASE)

# Secret scrub (trust boundary: VM drops are attacker-influenced; a proposal
# must never smuggle credentials into the signed vault, a report, or a ledger).
# ONE routine, every serialization surface — it lives in provenance.py because
# the ingest lane needs it too, and is re-exported here for the callers (and
# tests) that have always found it at `cos.secret_findings`.
SECRET_PATTERNS = provenance.SECRET_PATTERNS
secret_findings = provenance.secret_findings
scrub = provenance.scrub


# -- layout -----------------------------------------------------------------
def ops_dir(vault: Path | str | None = None) -> Path:
    return config.cos_ops_dir(vault)


def host_dir(vault=None) -> Path:
    return ops_dir(vault) / "host"


def shared_dir(vault=None) -> Path:
    return ops_dir(vault) / "shared"


def drop_dir(vault=None) -> Path:
    return ops_dir(vault) / "drop"


def proposal_drop_dir(vault=None) -> Path:
    return drop_dir(vault) / "proposal-drop"


def verdict_drop_dir(vault=None) -> Path:
    return drop_dir(vault) / "verdict-drop"


def evidence_dir(vault=None) -> Path:
    return host_dir(vault) / "evidence"


def proposals_dir(vault=None) -> Path:
    return host_dir(vault) / "proposals"


def hold_dir(vault=None) -> Path:
    return host_dir(vault) / "hold"


def corrections_db_path(vault=None) -> Path:
    return host_dir(vault) / "corrections.sqlite"


def priority_map_path(vault=None) -> Path:
    return shared_dir(vault) / "priority-map.md"


# Documented permission per sub-path (best-effort chmod; VirtioFS/Windows may
# only partially honour POSIX bits — the split is ALSO enforced behaviourally:
# no VM verb ever resolves a path under host/).
_PERMS = {"host": 0o700, "shared": 0o755, "drop": 0o775}


def ensure_layout(vault=None) -> dict[str, str]:
    """Create the three permission zones + their sub-dirs (idempotent)."""
    zones = {
        "host": host_dir(vault),
        "shared": shared_dir(vault),
        "drop": drop_dir(vault),
    }
    for name, d in zones.items():
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, _PERMS[name])
        except OSError:
            pass
    for d in (evidence_dir(vault), proposals_dir(vault), hold_dir(vault),
              proposals_dir(vault) / "pending", proposals_dir(vault) / "rejected",
              proposals_dir(vault) / "expired",
              proposals_dir(vault) / "corrections-pending",
              attachments_dir(vault), attachment_quarantine_dir(vault),
              attachment_expired_dir(vault), attachment_lifecycle_dir(vault)):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)  # nosemgrep: insecure-file-permissions -- intentionally OWNER-ONLY (host-private zone), not overly-permissive
        except OSError:
            pass
    for d in (proposal_drop_dir(vault), verdict_drop_dir(vault),
              ingest_manifest_dir(vault)):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o775)  # nosemgrep: insecure-file-permissions -- VM-writable drop zone needs group-write; owner+group only, no world access
        except OSError:
            pass
    # The run-record store is NOT one of these zones — it is off the mount
    # entirely (gap-05). Layout time is where its one-time carry-forward runs,
    # because every host write path already comes through here.
    migrate_run_records(vault)
    return {str(p): oct(_PERMS[n]) for n, p in zones.items()}


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _ts(dt: _dt.datetime | None = None) -> str:
    return (dt or _utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(s: str) -> _dt.datetime | None:
    try:
        out = _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return out if out.tzinfo else out.replace(tzinfo=_dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def _env_days(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env, default))
    except ValueError:
        return default


# -- INT-01: the APPROVED QUEUE (accept -> signature, with no writable gap) ----
#
# An owner-accepted candidate used to be moved into `capture-inbox/`, which is
# VM-WRITABLE by design: `brain --role vm draft-capture` can overwrite the same
# id, and the host drain then re-read those mutable bytes and SIGNED them. The
# accept verified one set of bytes; the signature covered another.
#
# Two properties close that, and both are needed:
#
#   1. LOCATION. The approved payload waits in the per-vault HOST APP-DATA dir
#      (`config.index_dir`, the ADR-0008 query-ledger precedent) — physically
#      outside `<vault>/`, so it is not on the Cowork VirtioFS mount at all.
#      `.brain/cos/host/` would NOT do: it is visible on that mount, and 0700
#      is only a boundary if the VM runs as a different uid AND VirtioFS
#      honours mode bits (neither is established).
#   2. AN UNFORGEABLE ANCHOR. The approved sha rides in an Ed25519-signed
#      anchor record (the same host key the batch digest is signed with, held
#      in the OS secret store — not on the filesystem). Location alone would
#      leave the "one write substitutes both" hole: an attacker who can write
#      the payload can write a plain sidecar sha beside it, after which the
#      signing-time check verifies the attacker's hash against the attacker's
#      bytes and passes. A signature cannot be rewritten without the key.
#
# The drain reads the payload ONCE (no-follow), hashes the bytes it actually
# read, re-verifies the anchor immediately before `write_note`, and signs THAT
# buffer — it never re-opens the path. Anything that does not verify is refused
# loudly (quarantined out of the queue + a defect row), never signed.
#
# INT-04 extends BOTH properties to the ATTACHMENT lane, whose accepted bytes
# cannot live in the approved queue: an attachment is a binary that must reach
# `vault/inbox/` for the signed ingest drain, and that destination is ON the
# mount. So the PAYLOAD stays where the drain needs it and only the ANCHOR
# moves off-mount (`ATTACHMENT_ANCHOR_SCHEMA` below): the accepted bytes are
# read once at accept time, hashed, signed into a host-private anchor keyed by
# the inbox destination, and the ingest drain re-verifies the buffer it is
# about to sign against that anchor. Same helpers, same failure posture — a
# mismatch is quarantined loudly, never signed.
APPROVED_ANCHOR_SCHEMA = "cos_approved_anchor/v1"
_APPROVED_DIRNAME = "cos-approved"
ATTACHMENT_ANCHOR_SCHEMA = "cos_attachment_anchor/v1"
_ATTACHMENT_ANCHOR_DIRNAME = "cos-attachment-anchors"


class ApprovedQueueUnsafe(config.HostPathUnsafe):
    """The configured approved queue resolves inside something the VM can see.

    Then the whole point is lost, so it is refused rather than used (fail
    closed) — the same posture ``querylog`` takes when ``$BRAIN_INDEX_DIR`` is
    pointed into the vault. Subclasses ``config.HostPathUnsafe`` because the
    RULE now lives in ``config`` (the single-writer lock needs it too and
    ``config`` cannot import ``cos``) — one rule, one exception type."""


class ApprovedRefused(RuntimeError):
    """These bytes are not the bytes the host approved. Never sign them."""


class ReleaseRecordsUnreadable(RuntimeError):
    """The attachment RELEASE records could not be read as a whole.

    Deliberately not an :class:`ApprovedRefused` (nothing here is a verdict
    about bytes) and deliberately not swallowed: these records arm the drain's
    fail-closed refusal of a released-but-unanchored file, they live on the
    mount, and treating "unreadable" as "there are none" is the fail-open the
    refusal exists to close."""


class ApprovedTooLarge(ApprovedRefused):
    """The entry is bigger than the caller's cap — the read stopped at it.

    Distinct from a plain refusal so a caller with its own size policy (the
    ingest drain's ``MAX_INGEST_BYTES``) can report "file_too_large" rather
    than "unreadable", without a second reading routine that enforces the cap
    on a path it re-opens."""


class ApprovedKeyUnavailable(RuntimeError):
    """The host key could not be resolved, so no anchor can be VERIFIED.

    Deliberately NOT an :class:`ApprovedRefused`: a locked keychain, a
    scheduler running as the wrong user, a missing ``cryptography`` or a key
    rotation must read as "this host cannot sign right now" — the same
    fail-closed, leave-it-in-place answer the ordinary draft path gives — never
    as "someone tampered with the bytes"."""


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


#: ONE definition, in ``config`` — the single-writer lock (INT-05) has to prove
#: the same property and ``config`` cannot import ``cos``. Re-exported here
#: because every existing caller (and test) finds them at ``cos.``.
vm_visible_roots = config.vm_visible_roots


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


#: File modes per ops zone, mirroring `_PERMS` for DIRECTORIES. A file created
#: through `_write_atomic` defaults to owner-only, which is right for
#: `host/` — and WRONG for the two zones the VM must read or write. The
#: conversion to atomic writes silently took `shared/current-run.json` and the
#: VM's own proposal drops from umask-default to 0600, which breaks a split-UID
#: host/VM install: no functional test can see it, so the modes are named here
#: and asserted per zone in `tests/test_cos_pathguard.py`.
MODE_HOST_PRIVATE = 0o600
MODE_VM_READABLE = 0o644     # shared/: host writes, VM reads
MODE_VM_WRITABLE = 0o664     # drop/: VM writes, host reads (dir is 0775)

#: Bound on waiting for the per-ledger append lock. Every holder writes one
#: short record and releases, so this is contention relief, not a queue.
_APPEND_LOCK_SECONDS = 10.0


def _reserve_exclusive(path: Path, *, mode: int = MODE_HOST_PRIVATE) -> None:
    """Claim ``path`` for this process, or raise ``FileExistsError``. NO content.

    THE THIRD SANCTIONED RAW WRITER, and it is deliberately the smallest one
    that can exist: create-if-absent, nothing written, nothing truncated.
    `_write_atomic` cannot do this job — it publishes through a rename, which
    REPLACES whatever is there, so two racing callers both "succeed" and the
    loser silently overwrites the winner. Reserving a name needs the one
    operation the filesystem serialises, and that is `O_CREAT|O_EXCL`.

    `O_NOFOLLOW` for the same reason every other opener in this module carries
    it (INT-05): these paths are mount-resident, and a pre-created symlink at
    the name would otherwise be followed. `O_EXCL|O_CREAT` already refuses a
    symlink — even a dangling one — so this is belt and braces on a path where
    the cost of being wrong is a host process writing through an attacker's
    link. It is `getattr`-ed (like every OTHER opener here — `_write_atomic`
    below) because Windows is a supported host and has no `O_NOFOLLOW`; a bare
    `os.O_NOFOLLOW` raised `AttributeError` there before a manifest was ever
    written, and `O_EXCL|O_CREAT` still refuses an existing symlink at the name
    so the fallback loses nothing.

    Sole caller: `write_run_manifest`, reserving the run id — allocated OR
    explicit, since round 6's H-resv — before it writes the manifest into the
    same name (C-lock, 2026-08-13). Listed in
    `tests/test_cos_pathguard.py::_SANCTIONED_RAW_WRITERS` — that guard exists
    to force this paragraph to be written, not to be edited around.
    """
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                     | getattr(os, "O_NOFOLLOW", 0), mode))


def _write_atomic(path: Path, data: bytes, *, mode: int = MODE_HOST_PRIVATE) -> None:
    """Durable, atomic, and never through a symlink — including the TEMP name.

    The temp name used to be the predictable ``<target>.tmp`` opened with a
    plain ``open(..., "wb")``, which FOLLOWS a symlink. Round 2 pointed this
    helper at ``batches.jsonl`` under ``host/proposals/`` — on the shared mount
    — so pre-creating ``batches.jsonl.tmp`` as a link to any host file made the
    host truncate and overwrite that file. Unpredictable name +
    ``O_CREAT|O_EXCL|O_NOFOLLOW`` + a regular-file check closes it: EXCL means
    an existing entry (symlink or not) fails outright, and the random suffix
    means an attacker cannot pre-create the name to begin with.

    The PARENT is fsynced after the rename too: the caller unlinks the only
    other copy (the pending source) straight after, and an unsynced directory
    entry can lose the rename in a crash — durable file contents under a lost
    name is still a lost file."""
    import secrets
    import stat as _stat

    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    fd = os.open(tmp, flags, 0o600)   # narrow while it is being written
    closed = False
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"refusing to write {tmp.name}: not a regular file")
        # os.write is NOT guaranteed to write everything it is given. A short
        # write used to be treated as success and then atomically PUBLISHED —
        # and for approved staging the caller deletes the pending copy straight
        # after, so the accepted content would be gone and the truncated queue
        # copy correctly refused by its own anchor. Loop until it is all out.
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            if n <= 0:
                # A zero-progress write leaves `view` unchanged: the loop would
                # spin forever with the temp fd open and the drain hung. Empty
                # input never enters the loop (the memoryview is falsy).
                raise OSError(f"write made no progress on {tmp.name} "
                              f"({len(view)} bytes left)")
            view = view[n:]
        os.fsync(fd)
        if mode != 0o600:
            os.fchmod(fd, mode)     # on the FD, so no name is ever re-resolved
        os.close(fd)
        closed = True
        os.replace(tmp, path)
    except BaseException:
        # EVERY post-open step, close and replace included: a failure in either
        # used to skip cleanup and strand the temp file.
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _fsync_dir(d: Path) -> None:
    """Make a directory ENTRY durable (a rename/unlink, not file contents)."""
    try:
        dfd = os.open(d, getattr(os, "O_DIRECTORY", os.O_RDONLY))
    except OSError:
        return          # Windows has no directory fd; nothing to sync
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def _read_nofollow(path: Path, *, max_bytes: int | None = None) -> bytes:
    """Read a regular file WITHOUT following a symlink at the final component.

    A swapped-in symlink is the classic way to make a verified path serve
    someone else's bytes; ``O_NOFOLLOW`` refuses it at the syscall. Windows has
    no such flag, so the explicit ``is_symlink`` check carries that platform.

    ``max_bytes`` caps the read on the OPEN DESCRIPTOR (``ApprovedTooLarge``
    past the cap) rather than on a ``stat()`` of the name — INT-04 round 3:
    a caller that stats the path, then opens it again, has re-resolved the
    name twice and can be given two different objects. One open, one buffer,
    every decision made about THAT descriptor."""
    import stat as _stat

    if path.is_symlink():
        raise ApprovedRefused(f"{path.name} is a symlink — refused")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ApprovedRefused(f"{path.name} unreadable: {type(exc).__name__}") from None
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise ApprovedRefused(f"{path.name} is not a regular file — refused")
        chunks: list[bytes] = []
        total = 0
        while True:
            b = os.read(fd, 1 << 16)
            if not b:
                return b"".join(chunks)
            total += len(b)
            if max_bytes is not None and total > max_bytes:
                raise ApprovedTooLarge(
                    f"{path.name} exceeds the {max_bytes}-byte cap")
            chunks.append(b)
    finally:
        os.close(fd)


#: The one no-follow read, under a public name for callers outside this module
#: (``brain.ingest.pipeline``). A SECOND implementation is how the first one
#: ends up subtly weaker — the reason this alias exists rather than a copy.
read_nofollow = _read_nofollow


def approved_verify_key(vault=None):
    """Resolve the material anchor verification needs, ONCE per drain.

    Raises :class:`ApprovedKeyUnavailable` for every "this host cannot do
    crypto right now" cause — no key, locked keychain, wrong scheduler user,
    ``cryptography`` missing. Callers resolve it up front and hand it to every
    per-item check, so a key outage can never be mistaken for tampering by an
    item-level verification that fails for the wrong reason."""
    from . import audit

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
    from . import audit

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
    _write_atomic(payload, text.encode("utf-8"))
    _write_atomic(approved_anchor_path(vault, nid),
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


# -- INT-04: the same anchor, one lane over (owner-accepted ATTACHMENTS) -------
# The note lane can hold its accepted bytes off-mount until they are signed.
# An attachment cannot: it is a binary, and the thing that signs it is the
# ingest drain, which reads `vault/inbox/` — on the mount. So the payload goes
# where the drain needs it and the ANCHOR is what stays out of reach:
#
#   accept  -> read the quarantined bytes ONCE (no-follow, regular file only),
#              hash THAT buffer, verify it against the sha the owner's SIGNED
#              batch digest covered, sign an anchor naming the inbox
#              destination, then write the verified buffer to that destination;
#   ingest  -> hash the bytes it actually read, and if an anchor exists for
#              that destination the buffer must match it, or the file is
#              quarantined loudly and NEVER signed.
#
# The anchor is filed under TWO keys, and both are needed:
#
#   by DESTINATION — catches substitution IN PLACE (the accepted name now holds
#              different bytes), which is the attack;
#   by CONTENT sha — keeps the acceptance attached to the BYTES when the name
#              changes. Without it, renaming the file in the inbox silently
#              strips the owner's claim, and an email-derived attachment that
#              resolves MNPI *because* it carries provenance would ingest as a
#              plain unlabelled drop at `Internal` — a classification DOWNGRADE
#              performed with nothing but a rename. It also survives the ingest
#              pipeline's own collision/retry renames.
#
# Anchor BEFORE payload here, the reverse of `stage_approved`. An anchor with
# no payload is inert (nothing is at that path; a later unrelated drop under
# the same name is refused, which is the conservative direction). A payload
# with no anchor is precisely the gap being closed.
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
    from . import audit

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
    _write_atomic(path, blob)
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
        _write_atomic(sha_path.with_suffix(".ambiguous"), b"{}\n")
    else:
        _write_atomic(sha_path, blob)
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


# -- the AUTO lane's own authorization (INT-04, round 2 finding 2) -------------
# An attachment the auto lane holds is released an undo-window later WITHOUT an
# owner batch — so there is no signed batch digest to CAS against. The release
# read `meta["sha256"]` out of the quarantine sidecar, which sits on the mount
# beside the payload: one write replaced both, and the host then signed a fresh
# acceptance anchor over the substituted bytes. The same coupling the anchor
# exists to break, on the lane that never asks a human.
#
# So the auto lane signs its authorization AT HOLD TIME — the one moment the
# host itself decided this content may auto-capture — over (id, content sha,
# not_before, vault), off the mount beside the acceptance anchors. Release
# verifies it, takes `not_before` from the SIGNED body (never the plain field
# beside it), and passes the SIGNED sha as the CAS target. No authorization, no
# release: a missing expected hash must never mean "no check".
ATTACHMENT_HOLD_SCHEMA = "cos_attachment_hold/v1"


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
    from . import audit

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
    _write_atomic(path, (json.dumps({"body": body, "sig": sig}, sort_keys=True)
                         + "\n").encode("utf-8"))
    return path


def attachment_hold_authz(vault, aid: str, *, pubkey=None
                          ) -> dict[str, Any] | None:
    """The signed hold authorization for ``aid`` — or ``None`` (refuse)."""
    if pubkey is None:
        pubkey = approved_verify_key(vault)       # may raise KeyUnavailable
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
        pubkey = approved_verify_key(vault)       # may raise KeyUnavailable
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
        pubkey = approved_verify_key(vault)       # may raise KeyUnavailable
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


def approved_pending(vault) -> list[Path]:
    """Queued payloads awaiting the signature (host-only; never raises)."""
    try:
        d = approved_queue_root(vault)
    except ApprovedQueueUnsafe:
        return []
    return sorted(d.glob("*.md")) if d.is_dir() else []


def approved_refused(vault) -> list[Path]:
    try:
        d = approved_queue_root(vault)
    except ApprovedQueueUnsafe:
        return []
    # payloads only — one entry per refused ITEM, not one per quarantined file
    return sorted(d.glob("*.md.refused")) if d.is_dir() else []


def clear_approved(vault, nid: str) -> bool:
    """Drop a queued item — after a successful signature, or on an undo.

    Returns True only when NEITHER file is on disk afterwards. It used to
    swallow every OSError and return nothing, so an undo reported
    ``draft-deleted`` while the item was still queued and the next drain signed
    the thing the owner had just revoked. A caller acting on the return value
    is the point; a silent best-effort delete is not."""
    try:
        payload = approved_payload_path(vault, nid)
        anchor = approved_anchor_path(vault, nid)
    except (ApprovedQueueUnsafe, ValueError):
        return True                       # no reachable queue: nothing is staged
    for p in (payload, anchor):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    return not (payload.exists() or anchor.exists())


discard_approved = clear_approved


def refuse_approved(vault, path: Path, reason: str,
                    now: _dt.datetime | None = None) -> Path | None:
    """Quarantine a payload that failed verification, and say so LOUDLY.

    Moved OUT of the queue (never signed, never retried silently) and recorded
    as a defect — leaving it would re-refuse it on every drain, which reads as
    noise instead of the security event it is."""
    stamp = (now or _utcnow()).strftime("%Y%m%dT%H%M%S")
    dest = None
    try:
        nid = safe_slug(path.stem)
    except ValueError:
        nid = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:12]
    try:
        d = approved_queue_root(vault)
        dest = d / f"{stamp}-{nid}.md.refused"
        if path.is_symlink() or path.exists():
            os.replace(path, dest)
        anchor = approved_anchor_path(vault, nid)
        if anchor.exists():
            os.replace(anchor, d / f"{stamp}-{nid}.anchor.json.refused")
    except (ApprovedQueueUnsafe, OSError, ValueError):
        pass
    log_defect(vault, "approved-queue-refusal", f"{nid}: {reason}",
               ts=_ts(now))
    return dest


# -- VM ingress: cos-propose --------------------------------------------------
def propose(vault, content: str, *, ident: str | None = None) -> dict[str, Any]:
    """Write ONE unsigned proposal candidate into ``drop/proposal-drop/``.

    VM-ALLOWED. Never signs, never indexes, never touches capture-inbox — the
    ordinary ``brain sync`` drain does not read this directory, so nothing
    dropped here can reach the signed write path without the broker.

    PRV-01: email provenance travels in the candidate's own frontmatter as the
    flat dotted keys ``provenance.sender``/``.sent``/``.conversation_id``/
    ``.subject``. ``capture.enforce`` sanitizes + secret-scrubs them and strips
    any ``provenance.verified`` a VM tried to assert; from there they are
    preserved untouched through claim, batch, accept and the signed drain.
    """
    from . import capture as cap_mod

    meta, _body = frontmatter.parse_text(content)
    note_id = ident or (str(meta.get("id")) if meta and meta.get("id") else None)
    if not note_id:
        note_id = "cosprop-" + sha256_text(content)[:12]
    note_id = safe_slug(note_id)  # C-1 fail-closed on traversal ids
    # ING-03 fix: capture.enforce()'s generic default (Internal, UX-01) is wrong
    # here — Phase 1.6 requires ingestion candidates to default to MNPI
    # (most-restrictive) unless the candidate content itself states a tier.
    # Malformed/double-frontmatter candidate content (observed 3/10 in the
    # 2026-07-14/15 window) silently fell through to Internal without this.
    cls_override = meta.get("classification") or "MNPI"
    staged = cap_mod.enforce(
        content, override={"id": note_id, "classification": cls_override})
    # STA-01: the producer-version stamps are the HOST's to derive (from the
    # run manifest), so they come off here too — the same keys the claim strips.
    # Doing it at the ingress as well means the sha this call REPORTS is the sha
    # the host will compute, so an honest producer's ledger row joins by
    # construction; a raw drop-dir writer that still asserts them gets a digest
    # mismatch, which is the loud outcome that shape deserves.
    try:
        staged = provenance.without_host_only_text(staged, keys=_STRIPPED_CLAIM_KEYS)
    except provenance.HostOnlyKeyResidue as exc:
        raise ValueError(f"candidate smuggles a host-derived key: {exc}") from exc
    ddir = proposal_drop_dir(vault)
    ddir.mkdir(parents=True, exist_ok=True)
    target = ddir / f"{note_id}.md"
    if target.resolve().parent != ddir.resolve():
        raise ValueError(f"proposal target escapes drop dir: {note_id!r}")
    _write_atomic(target, staged.encode("utf-8"), mode=MODE_VM_WRITABLE)
    return {"proposal": str(target), "id": note_id, "signed": False,
            "state": "dropped",
            # The run copies BOTH into its ingestion-ledger row: the host joins
            # the category back by (id + full content digest), and a row
            # carrying only the id proves nothing about these bytes.
            "sha256": sha256_text(staged),
            "note": "unsigned proposal drop; the host broker validates, asks the "
                    "owner, and only an ACCEPTED candidate is ever signed. Record "
                    "`id` + `sha256` in this run's ingestion-ledger row with the "
                    "category — the host joins them there, and an unjoinable "
                    "candidate is quarantined, never silently unclassified"}


def propose_correction(vault, payload: dict[str, Any]) -> dict[str, Any]:
    """VM-ALLOWED: drop ONE correction request into ``drop/verdict-drop/``.

    This is the defined transport for the owner's one-line Cowork correction
    (see docs/cos-ops.md): VM drop → host broker validates against the shadow
    ledger → owner-inbox question → the ANSWER (the human act on the host) is
    what inserts the ``correction_events`` row. A VM write alone never mutates
    the corrections store of record."""
    errs = _validate_correction_payload(payload)
    if errs:
        raise ValueError("invalid correction payload: " + "; ".join(errs))
    ddir = verdict_drop_dir(vault)
    ddir.mkdir(parents=True, exist_ok=True)
    name = f"correction-{payload['round']}-{safe_slug(payload['msg_key'])}.json"
    target = ddir / name
    _write_atomic(target, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"),
                  mode=MODE_VM_WRITABLE)
    return {"drop": str(target), "state": "dropped",
            "note": "correction drop staged; the host broker will surface it as "
                    "an owner-inbox question — a VM write never mutates the "
                    "corrections store of record"}


def _validate_correction_payload(p: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(p, dict):
        return ["payload must be a JSON object"]
    if not isinstance(p.get("round"), int):
        errs.append("round must be an integer")
    for k in ("msg_key", "corrected_bucket", "corrected_tier"):
        v = p.get(k)
        if not isinstance(v, str) or not v.strip():
            errs.append(f"{k} must be a non-empty string")
    return errs


# -- claims ledger ------------------------------------------------------------
def _claims_path(vault) -> Path:
    return proposals_dir(vault) / "claims.jsonl"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _open_append_nofollow(path: Path) -> int:
    """Open ``path`` for append, PROVABLY not through a symlink — or raise.

    Two shapes, and neither has a check-then-open window:

    1. **The ledger does not exist yet: create it EXCLUSIVELY.**
       ``O_CREAT | O_EXCL`` fails outright if anything is already at the name,
       symlink included — the kernel decides, atomically. The previous version
       lstat'd first and, on ``FileNotFoundError``, opened with ``O_CREAT`` and
       skipped the identity comparison entirely, so a symlink inserted between
       the two calls WAS followed: ``fstat`` then saw a perfectly ordinary
       victim file and ledger data was appended outside the ledger. Removing
       the window beats narrowing it.
    2. **It already exists: open it WITHOUT ``O_CREAT``, and verify — always.**
       ``lstat`` the name, refuse a symlink, then confirm on the fd that the
       object is a REGULAR FILE and the very same inode. ``O_NOFOLLOW`` is used
       where it exists, but it is belt, not the check: it refuses a symlink and
       tells you nothing about which file you got, so the branch that had it
       used to return straight from ``os.open`` and accepted a swapped-in
       regular file — the redirection the other branch refused. One path now,
       and no ``0`` fallback: a guard that degrades to nothing while still
       reporting success is the failure mode this work exists to remove.

    A file that is created and then deleted under us bounces between the two
    branches, so the pair is retried a few times before giving up."""
    base = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    # A non-regular object must not be able to BLOCK the open: a planted FIFO
    # made `os.open` wait for a reader, which sails straight past the append
    # lock's timeout and wedges the unattended nightly drain. With O_NONBLOCK a
    # writer-side FIFO open fails fast (ENXIO) instead, and the flag is ignored
    # for the regular files this is actually meant to open.
    nonblock = getattr(os, "O_NONBLOCK", 0)
    for _attempt in range(5):
        try:
            # (1) atomic create — never follows a link, no window to race
            return os.open(path, base | os.O_CREAT | os.O_EXCL | nofollow,
                           MODE_HOST_PRIVATE)
        except FileExistsError:
            pass
        # (2) it exists: ONE verification path, whether or not the kernel
        #     helped. `O_NOFOLLOW` refuses a symlink; it says nothing about
        #     WHICH file you got or whether it is a regular file at all, so the
        #     branch that had it used to return straight from `os.open` and
        #     accepted a swapped-in regular file — the very redirection the
        #     other branch refused.
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue                    # deleted under us — retry the create
        if _stat_mod.S_ISLNK(st.st_mode):
            raise OSError(f"refusing to append to {path.name}: it is a symlink")
        before = (st.st_dev, st.st_ino)
        try:
            fd = os.open(path, base | nofollow | nonblock, MODE_HOST_PRIVATE)
        except FileNotFoundError:
            continue
        try:
            after = os.fstat(fd)
            if not _stat_mod.S_ISREG(after.st_mode):
                raise OSError(
                    f"refusing to append to {path.name}: not a regular file")
            if (after.st_dev, after.st_ino) != before:
                raise OSError(
                    f"refusing to append to {path.name}: it was replaced "
                    f"between the check and the open")
        except BaseException:
            os.close(fd)
            raise
        return fd
    raise OSError(f"refusing to append to {path.name}: it kept appearing and "
                  f"disappearing under us")


def _append_lock_path(ledger: Path) -> Path:
    """The off-mount lock file serializing appends to ``ledger``.

    HOST-PRIVATE, in the same app-data location the approved queue proved out —
    the one thing in this whole hardening arc that never came back, because it
    was MOVED rather than guarded. Keyed by a hash of the ledger's absolute
    name (``abspath``, not ``resolve``: a symlinked ledger must not silently
    share a lock with its target), so no vault argument is needed and two
    ledgers never collide."""
    key = hashlib.sha256(str(os.path.abspath(ledger)).encode("utf-8")).hexdigest()[:16]
    # `create=True`: this IS the acquisition path (its caller opens the fd on
    # the next line), which is where directory creation belongs now that
    # `host_lock_dir` is pure name resolution.
    return config.host_lock_dir(create=True) / f"{key}.lock"


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    """Append ONE record to a mount-resident ledger, without following a link.

    ``path.open("a")`` follows a symlink at the final name, so a link planted at
    a ledger path redirected host-written ledger data. These files are appended
    on nearly every fold and can grow large, so rewriting them atomically would
    be the wrong trade — an append that provably does not follow a link is
    (`_open_append_nofollow`).

    RECORD ATOMICITY IS THE LOCK'S JOB, NOT ``O_APPEND``'S. ``O_APPEND`` puts
    each individual ``os.write`` at EOF; it says nothing about a whole logical
    JSON line. A short write makes the loop below issue a second write, and a
    concurrent fold can land its own bytes in between — a corrupt or lost
    ledger row. So the ENTIRE record, retries included, is serialized on a
    per-ledger lock (the same portable primitive ``lock.py`` uses, which is why
    it is imported rather than re-implemented).

    THE LOCK LIVES OFF THE MOUNT (`_append_lock_path`). A lock file beside its
    ledger was itself attacker-reachable: unlink the inode while writer A holds
    it, drop a new file at the same name, and writer B locks the NEW inode and
    proceeds concurrently — which puts the interleaving back. No amount of
    checking at open time fixes that; not being reachable does."""
    from .lock import _open_lock_fd, _try_lock, _unlock

    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
    lock_fd = _open_lock_fd(_append_lock_path(path))
    try:
        deadline = time.monotonic() + _APPEND_LOCK_SECONDS
        while not _try_lock(lock_fd):
            if time.monotonic() >= deadline:
                raise OSError(f"could not serialize an append to {path.name} "
                              f"within {_APPEND_LOCK_SECONDS}s")
            time.sleep(0.01)
        fd = _open_append_nofollow(path)
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError(f"append made no progress on {path.name}")
                view = view[n:]
        finally:
            os.close(fd)
        _unlock(lock_fd)
    finally:
        os.close(lock_fd)


#: The one locked, no-follow JSONL append, under a public name for callers
#: outside this module (``brain.cos_corpus``). Same reason ``read_nofollow``
#: exists rather than a copy: a second implementation of a write rule is how
#: the first one ends up subtly weaker.
append_jsonl = _append_jsonl


# -- STA-01: the RUN MANIFEST — the host's own record of what produced a run ---
# Run 59 (2026-07-31) staged 8 candidates and EVERY one arrived with no
# `category`, no `extraction_rules_version` and no `bundle_version`, so all 8
# host-defaulted to the never-graduable `unclassified` — while the run's own
# `_cos_ingestion_ledger_2026-07-31-run59.jsonl` carried the right category
# beside each proposal id. The defect was never the missing string: it was that
# facts the HOST already knows were being copied by the untrusted side, and
# therefore could be lost (or forged) in transit.
#
# So the host writes them itself. At run LAUNCH (`brain cos-run-begin`) it
# freezes a manifest: the run id, the resolved SKILL.md path + content digest,
# both versions read out of that file, and the artifacts the run owes. Every
# later claim reads THAT — never "whatever skill is deployed right now".
# `claim_drops` fires hourly and the deployed bundle changes between runs, so
# claim-time readback would stamp a proposal with a version that did not
# produce it. The manifest is immutable: a second write with different content
# is refused, not merged.
RUN_MANIFEST_SCHEMA = "cos_run_manifest/v1"
RUN_ID_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-run[0-9]+$")
_RUN_NUMBER_RE = re.compile(r"run([0-9]+)")

# The four states of INS-01's host-side run validator (s03). They live here
# because the CLAIM GATE below is what makes them bite; the validator that
# fills them lands next session and needs no change here.
RUN_VALID = "VALID"
RUN_VALID_DEGRADED = "VALID_DEGRADED"
RUN_INVALID = "INVALID"
RUN_INCONCLUSIVE = "INCONCLUSIVE"
RUN_VERDICTS = (RUN_VALID, RUN_VALID_DEGRADED, RUN_INVALID, RUN_INCONCLUSIVE)
#: the ONLY verdicts that permit binding a candidate. An absent verdict is
#: INCONCLUSIVE and quarantines like any other — there is deliberately no
#: "not scored yet, let it through" interim, because that carve-out is the
#: cosmetic-FAILED defect wearing a schedule.
CLAIMABLE_VERDICTS = (RUN_VALID, RUN_VALID_DEGRADED)


def runs_dir(vault=None) -> Path:
    """Run manifests, validity verdicts and plan bindings — OFF THE MOUNT.

    Host-private by PLACEMENT since gap-05 (2026-08-16), not by contract: this
    returned ``<vault>/.brain/cos/host/runs`` until then, inside the VirtioFS
    workspace, while this docstring and two others said "never VM-writable".
    ``config.cos_run_records_dir`` carries the reasoning and the fallback;
    :func:`migrate_run_records` carries the historical records forward.

    RESOLUTION ONLY — it does not create the directory, for the same reason
    ``config.host_lock_dir`` does not: a name lookup that materialises host
    state runs on every read path that merely wanted the name."""
    return config.cos_run_records_dir(vault)


def legacy_runs_dir(vault=None) -> Path:
    """Where run records lived until 2026-08-16: ON the Cowork mount.

    Read by :func:`migrate_run_records` (once) and by
    :func:`run_record_intruders` (forever, as EVIDENCE). Nothing else may
    consult it again — a reader here is a VM-writable authority, which is the
    whole defect gap-05 closed."""
    return host_dir(vault) / "runs"


#: Written into the host-private store the moment the carry-forward completes.
#: It is what makes the migration ONE-TIME: after it exists, a file appearing
#: in the legacy directory is not a record to import, it is either an engine
#: too old to have moved or someone writing into the run-validity store from
#: the mount — and importing it would launder exactly the forgery this
#: relocation exists to stop.
RUNS_MIGRATION_MARKER = ".carried-forward-from-mount.json"


def migrate_run_records(vault=None) -> dict[str, Any]:
    """Carry the pre-2026-08-16 on-mount run records forward, ONCE.

    Returns ``{"carried": [...], "conflicts": [...], "already": bool}``.
    Idempotent and cheap: after the marker exists it scans the legacy directory
    and returns without reading a byte of it.

    MOVE, not copy (unlike ``audit.migrate_drift_dispositions``, which carries
    one small file the operator may still want to read): these 100+ records are
    now read ONLY from the destination, and leaving a second set of manifests
    and verdicts lying in a VM-writable directory is the confusion this change
    exists to remove. Written to the destination and fsynced BEFORE the source
    is unlinked, so a crash mid-migration strands nothing.

    SAME RUN ID IN BOTH PLACES — FAIL CLOSED. Identical bytes are the ordinary
    resumed migration and the legacy copy is simply dropped. DIFFERING bytes
    are never resolved by preferring either side: a manifest or verdict that
    disagrees with its host-private counterpart is the tampering signal this
    directory was moved to make impossible, so the legacy file is left exactly
    where it is, a defect is logged, and :func:`run_record_intruders` keeps
    reporting it — which is what takes the affected run to INCONCLUSIVE in
    ``cos_runverify.verify_run``. It is deliberately scoped to the RUN, not to
    the whole store: one planted file must not be able to stop every other
    night being verified.

    THE ONE WINDOW, STATED. The migration is gated on the marker, and the
    marker is stamped the first time this runs on a host whose host-private
    store exists. A host that has NEVER written a run record and whose legacy
    directory is created from the mount would import that plant once. It is
    bounded (a deployment with no run history has no candidates a forged
    verdict could claim) and it closes the moment the host writes its first
    record, which every `cos-run-begin` does before anything is judged."""
    legacy = legacy_runs_dir(vault)
    try:
        dest_dir = runs_dir(vault)
    except Exception:  # noqa: BLE001 — unsafe destination: stay fail-closed
        return {"carried": [], "conflicts": [], "already": False}
    marker = dest_dir / RUNS_MIGRATION_MARKER
    if marker.exists():
        return {"carried": [], "conflicts": [], "already": True}
    carried: list[str] = []
    conflicts: list[str] = []
    try:
        names = sorted(p.name for p in legacy.iterdir() if p.is_file())
    except OSError:
        names = []
    # NOTHING ON EITHER SIDE — leave no trace. A vault that never ran the old
    # layout has no legacy directory (nothing creates one any more), and a name
    # resolution that mkdirs an app-data directory per throwaway vault is the
    # side effect `config.host_lock_dir` refuses for the same reason. The
    # marker gets stamped by the next call after the store exists, which is the
    # first thing any write path creates.
    if not names and not dest_dir.is_dir():
        return {"carried": [], "conflicts": [], "already": False}
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dest_dir, 0o700)  # nosemgrep: insecure-file-permissions -- host-private run-record store, OWNER-ONLY by design
    except OSError:
        pass
    for name in names:
        src, dst = legacy / name, dest_dir / name
        try:
            data = src.read_bytes()
        except OSError:
            conflicts.append(name)
            continue
        if dst.exists():
            try:
                same = dst.read_bytes() == data
            except OSError:
                same = False
            if not same:
                conflicts.append(name)
                log_defect(
                    vault, "run-record-mount-conflict",
                    f"{name} exists both in the host-private run store and in "
                    f"the legacy on-mount {legacy} with DIFFERENT bytes. "
                    "Neither copy is preferred: the on-mount one is left in "
                    "place as evidence and its run scores INCONCLUSIVE until a "
                    "human decides which is real.")
                continue
        else:
            try:
                _write_atomic(dst, data, mode=0o600)
            except OSError:
                conflicts.append(name)
                continue
            carried.append(name)
        try:
            src.unlink()
        except OSError:
            pass
    if not conflicts:
        try:
            _write_atomic(marker, json.dumps({
                "carried_from": str(legacy), "files": len(carried),
                "at": _ts()}, indent=2).encode("utf-8"), mode=0o600)
        except OSError:
            pass
    return {"carried": carried, "conflicts": conflicts, "already": False}


def run_record_intruders(vault=None, run_id: str | None = None) -> list[str]:
    """Files still sitting in the legacy on-mount run directory.

    After :func:`migrate_run_records` has run, this is empty on a healthy host.
    Anything it names is either a refused conflict or a file written into a
    VM-writable directory that the run validator once trusted — never an input,
    always a reason to refuse a verdict for the run it names."""
    try:
        names = [p.name for p in legacy_runs_dir(vault).iterdir() if p.is_file()]
    except OSError:
        return []
    if run_id is None:
        return sorted(names)
    rid = str(run_id)
    return sorted(n for n in names if rid in n)


def run_ops_dir(vault=None) -> Path:
    """Where the RUN writes its own artifacts — ``<vault>/cos-ops``.

    VM-writable by nature (the run authors its report, its ledgers and its
    metrics row there), which is exactly why nothing read from it is treated as
    authority: the ledger join below makes the category TAMPER-EVIDENT and
    single-sourced, not host-authoritative."""
    return config.vault_root(vault) / "cos-ops"


def run_manifest_path(vault, run_id: str) -> Path:
    return runs_dir(vault) / f"{_checked_run_id(run_id)}.json"


def run_validity_path(vault, run_id: str) -> Path:
    return runs_dir(vault) / f"{_checked_run_id(run_id)}.validity.json"


def run_plan_binding_path(vault, run_id: str) -> Path:
    """Where the APPLY records WHICH plan it dispatched — ONE name, host-private.

    The writer (`tools/cos_mutate.plan_binding_path`) and the reader
    (`cos_runverify.check_plan_binding`) each spelled this literal for
    themselves until round 7. A name held in two files is not one fact: the
    round-6 move out of the VM-writable `cos-ops` zone had to be made twice,
    and a third caller would have had to guess. It sits beside the manifest and
    the validity verdict because those are what the validator already trusts.

    NOT `_checked_run_id`, unlike the two siblings above, and deliberately: this
    file is the PER-RUN ARTIFACT pair of `_cos_undo_ledger_<run>.jsonl`, which
    `run_ops_dir` composes with the same unchecked id. Rejecting a run id here
    that the ledger path accepts would make one artifact of a run reachable and
    the other not — and the guard would be theatre anyway while its sibling is
    open. The id is checked where it is MINTED (`write_run_manifest`).
    """
    return runs_dir(vault) / f"_cos_plan_binding_{run_id}.json"


#: A run number wider than this is not a counter, it is a length attack: the id
#: becomes `runs/<id>.validity.json`, and an over-long component raises
#: ENAMETOOLONG at the open — BEFORE `_write_atomic`'s cleanup try — aborting
#: run creation. 9 digits is a billion runs; the real deployment is at ~60.
MAX_RUN_DIGITS = 9


def _checked_run_id(run_id: Any) -> str:
    rid = str(run_id or "").strip()
    if not RUN_ID_RE.match(rid):
        raise ValueError(
            f"run id must look like <YYYY-MM-DD>-run<N>, got {rid!r}")
    # The regex accepts an UNBOUNDED digit suffix, and `next_run_id` derives
    # that suffix from VM-writable `cos-ops` directory names.
    if len(rid.encode("utf-8")) > MAX_SLUG_BYTES:
        raise ValueError(
            f"run id {rid[:40]!r}… is {len(rid.encode('utf-8'))} encoded bytes, "
            f"over the {MAX_SLUG_BYTES}-byte path-component limit")
    return rid


#: Public names for what ``brain.cos_corpus`` shares with this module: the
#: run-id vocabulary and the timestamp format. Same reason ``append_jsonl`` is
#: exported — a second copy of either is how they drift apart.
checked_run_id = _checked_run_id
utcnow = _utcnow
timestamp = _ts


def next_run_id(vault, now: _dt.datetime | None = None) -> str:
    """``<today>-run<N+1>``, N being the highest run number on disk.

    The run number is a monotonic counter across the whole deployment (run 59
    followed run 58 on the same day), so it is derived from every artifact that
    names one — the run's own ops dir AND the manifests already written."""
    now = now or _utcnow()
    highest = 0
    for d in (run_ops_dir(vault), runs_dir(vault)):
        try:
            names = [p.name for p in d.iterdir()]
        except OSError:
            continue
        for name in names:
            m = _RUN_NUMBER_RE.search(name)
            # An absurd run number in a VM-writable dir name would make the
            # host CHOOSE an id it then cannot write. Ignore it rather than
            # inherit it — the counter is ours, the directory names are not.
            if m and len(m.group(1)) <= MAX_RUN_DIGITS:
                highest = max(highest, int(m.group(1)))
    return f"{now.strftime('%Y-%m-%d')}-run{highest + 1}"


def run_manifest(vault, run_id: Any) -> dict[str, Any] | None:
    """The frozen manifest for ``run_id``, or ``None`` if the host wrote none.

    A READ entry point, so it carries the one-time mount carry-forward too
    (same shape as ``audit.load_drift_dispositions``): verification re-executes
    over historical runs on hosts that may not have written a manifest since
    the relocation, and a migration that only ran on the write path would make
    every one of those nights read as "the host recorded nothing"."""
    migrate_run_records(vault)
    try:
        p = run_manifest_path(vault, run_id)
    except ValueError:
        return None
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return m if isinstance(m, dict) and m.get("run_id") == str(run_id) else None


def write_run_manifest(vault, *, run_id: str | None = None,
                       lane: str | None = None,
                       skill_path: Path | str | None = None,
                       attended: bool = False,
                       now: _dt.datetime | None = None) -> dict[str, Any]:
    """HOST-ONLY, at run LAUNCH: freeze WHICH bundle is about to run.

    ``skill_path`` asserts the file outright; otherwise the executing lane is
    resolved by ``brain.cos_deploy`` (the ONE definition of the lane rules —
    an ambiguous or unresolvable deployment raises rather than guesses, because
    a manifest stamped from a guess reads as authority).

    A missing ``kernel_version`` is refused: that file is not a COS bundle and
    a manifest naming it would stamp every candidate with ``None``. A missing
    ``extraction_rules_version`` is NOT refused — it rides as ``None``, which
    keeps every candidate out of the auto lane and shows up in
    ``unstamped_batched``, exactly as an unstamped producer should.
    """
    from . import cos_deploy

    now = now or _utcnow()
    ensure_layout(vault)
    # WAS THE ID ALLOCATED HERE, OR HANDED TO US? (review 2026-08-13, round 5,
    # C-lock.) `next_run_id` reads the highest number on disk and returns the
    # next one, reserving nothing — so two nightlies starting inside the same
    # second both got `<today>-runN`, both wrote an IDENTICAL manifest (only
    # `written` differed, and that key is excluded from the equality check
    # below), and both proceeded under one run id and one evidence directory.
    # Their applies serialise on the lane lock; their enumeration, judgment,
    # plan and rehearsal artifacts overwrite each other.
    #
    # An EXPLICIT id stays idempotent — fixtures, `cos_run_verify`'s
    # reconstruction and a retried `cos-run-begin` all legitimately re-assert
    # one, and re-asserting the id you were given is not a race. An ALLOCATED
    # id that already has a manifest IS one, and it fails rather than joins.
    allocated = not run_id
    rid = _checked_run_id(run_id or next_run_id(vault, now))
    skill = (cos_deploy.read_skill(skill_path) if skill_path
             else cos_deploy.deployed_skill(lane=lane))
    if not skill.get("bundle_version"):
        raise ValueError(
            f"{skill['path']} states no `kernel_version` — refusing to write a "
            "run manifest that would stamp every candidate with nothing")
    from . import cos_echecks                                # noqa: PLC0415
    _capability_digest = cos_echecks.capability_digest
    _git = cos_echecks.git_state()
    # AN ATTENDED RUN REFUSES A DIRTY TREE. Attended means a human is about to
    # approve a plan and watch it apply, and the record of WHICH CODE he
    # approved is `git_commit` — which says nothing at all if uncommitted edits
    # were in the tree beside it. The scheduled lane is unaffected: it never
    # passes `attended`, and its default behaviour is byte-identical.
    if attended and _git["clean"] is not True:
        raise ValueError(
            "refusing to begin an ATTENDED run from a "
            + ("dirty" if _git["clean"] is False else "non-git")
            + " working tree: the manifest would record commit "
            f"{_git['commit']} while the code that actually runs is something "
            "else, and assertion (6) of the attended validation — 'the "
            "capability set is unchanged AT THE COMMIT THAT RAN' — would be "
            "unprovable. Commit or stash first.")
    manifest = {
        "schema": RUN_MANIFEST_SCHEMA,
        "run_id": rid,
        "lane": skill.get("lane") or lane,
        "lane_reason": skill.get("lane_reason") or "operator-asserted skill path",
        "skill_path": skill["path"],
        "skill_sha256": skill["sha256"],
        "bundle_version": skill["bundle_version"],
        "extraction_rules_version": skill["extraction_rules_version"],
        # How many E-checks the bundle THAT RAN defines. Frozen here because it
        # cannot be re-derived later: the file changes, and then the count the
        # run owed is gone with the bytes (`cos_deploy.read_skill`).
        "expected_echecks": skill.get("echecks"),
        # WHICH CODE RAN, recorded by the HOST at launch (DOCTRINE v7 §8.2
        # E1/E10). Until this existed the manifest froze the SKILL bundle and
        # nothing else, so "MODEL_TOOLS and the mutation allowlist were
        # unchanged at the commit that actually ran" could only ever be
        # answered by inspecting today's constants — which proves what exists
        # while you verify, never what mutated the mailbox. `capability_digest`
        # hashes the tool grant + the two zero-send denylist blocks;
        # `git_commit`/`git_clean` name the tree they were hashed from. All
        # three are `None` when the executing tree is not on disk beside the
        # engine, and E1/E10 then FAIL rather than pass on an absence.
        "capability_digest": _capability_digest(),
        "git_commit": _git["commit"],
        "git_clean": _git["clean"],
        "attended": bool(attended),
        "expected_artifacts": [
            f"_cos_nightly_{rid}.md",
            f"_cos_ingestion_ledger_{rid}.jsonl",
            f"cos_contract_pre_{rid}.json",
            "_cos_metrics.jsonl",
        ],
    }
    # `written` is the only key that legitimately differs between two honest
    # assertions of one id (it is the wall-clock of the write), so it is the
    # one key excluded from the immutability comparison.
    def _same_manifest(doc: dict[str, Any]) -> bool:
        return {k: v for k, v in doc.items() if k != "written"} == manifest

    existing = run_manifest(vault, rid)
    if existing is not None:
        if allocated:
            raise ValueError(
                f"refusing to begin {rid}: this run ALLOCATED that id (no "
                "--run-id was given) and a manifest for it already exists, so "
                "another run took it first. Two runs under one id share one "
                "evidence directory and overwrite each other's enumeration, "
                "judgment, plan and rehearsal. Begin again — the allocator "
                "will hand out the next number.")
        if not _same_manifest(existing):
            raise ValueError(
                f"a run manifest for {rid} already exists and differs — a run "
                "manifest is IMMUTABLE (it is the record of what produced that "
                "run's candidates). Start a new run id instead.")
        return existing
    p = run_manifest_path(vault, rid)
    p.parent.mkdir(parents=True, exist_ok=True)
    # RESERVE THE NAME — FOR EXPLICIT IDS TOO (review 2026-08-13, round 6,
    # H-resv). The `existing` check above is a READ, so two callers racing the
    # SAME id both saw `None`; when the id was EXPLICIT the reservation used to
    # be skipped, so both reached `_write_atomic` and the second rename replaced
    # the first — the original C-lock split, reachable by any two explicit
    # `--run-id` callers (fixtures, `cos_run_verify` reconstruction, a retried
    # begin). `O_CREAT|O_EXCL` is the one filesystem-serialised operation:
    # exactly one caller creates the name, the loser gets FileExistsError.
    # A begin that raises before the real manifest replaces the placeholder
    # burns the id (it reads back as "no manifest" AND still bumps
    # `next_run_id`), rather than silently reissuing it.
    try:
        _reserve_exclusive(p)
    except FileExistsError as exc:
        if allocated:
            raise ValueError(
                f"refusing to begin {rid}: another run reserved that id "
                "between this one allocating it and writing its manifest. "
                "Begin again — the allocator will hand out the next number."
            ) from exc
        # EXPLICIT id. Re-asserting an id is legitimate, so idempotency is
        # preserved — but ONLY against a COMPLETE manifest already published:
        # re-read, and a matching manifest is returned, a differing one is the
        # immutability refusal. A retry that arrives WHILE the reservation is
        # in flight (the name exists, no manifest yet) does NOT wait and does
        # NOT succeed — it fails and the caller retries after the manifest is
        # published (answered open question, round 6). A placeholder from a
        # crashed begin reads back as `None` here for the same reason and burns
        # the id, exactly as the allocated path burns it.
        reread = run_manifest(vault, rid)
        if reread is None:
            raise ValueError(
                f"refusing to begin {rid}: its name is reserved but no manifest "
                "is published under it yet. Either another writer is mid-begin "
                "— retry in a moment, after the manifest is published, and do "
                "NOT wait on the reservation — or a prior begin CRASHED between "
                "reserving the id and writing the manifest, which BURNS the id "
                "permanently: no retry can ever clear it, so begin under a "
                f"different --run-id (the reservation at {rid} is a zero-byte "
                "placeholder a human may remove once no writer holds it)."
            ) from exc
        if not _same_manifest(reread):
            raise ValueError(
                f"a run manifest for {rid} already exists and differs — a run "
                "manifest is IMMUTABLE (it is the record of what produced that "
                "run's candidates). Start a new run id instead.")
        return reread
    record = {**manifest, "written": _ts(now)}
    _write_atomic(p, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
    try:
        os.chmod(p, 0o400)  # nosemgrep: insecure-file-permissions -- read-only by design
    except OSError:
        pass
    # THE RUN'S INSTRUCTION SHEET (MAN-01, 2026-08-09) — the VM-readable
    # projection of the manifest, and the ONLY place the run may take its own
    # identity from. It used to carry the id and the start stamp alone, so the
    # run still DERIVED everything else — and derivation is what fails: on
    # 2026-08-09 the host stamped run107 while the run named itself run106
    # (an abandoned manifest whose frozen digest no longer matched the bundle
    # it was executing), COMPOSED `_cos_brief_…md` where the manifest declared
    # `_cos_nightly_…md`, and so never completed — not one host check executed
    # on that night. Four host-owned facts, read literally, never composed:
    #
    #   run_id             the id every artifact is named after
    #   expected_artifacts the names it must write, verbatim
    #   skill_path/_sha256 the bundle it must be executing, so the run can
    #                      prove it is not running under a stale manifest
    #   lane               which deployment surface the host resolved
    #
    # The two VERSIONS stay host-side, deliberately and unchanged (STA-03): a
    # VM-claimed version stamp is stripped at the trust boundary, so projecting
    # it could only invite a claim that buys nothing. These four are
    # INSTRUCTIONS the run obeys, not claims it makes back.
    cur = current_run_path(vault)
    cur.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(cur, (json.dumps(
        {"run_id": rid, "started": _ts(now),
         "lane": manifest["lane"],
         "skill_path": manifest["skill_path"],
         "skill_sha256": manifest["skill_sha256"],
         "expected_artifacts": manifest["expected_artifacts"]},
        sort_keys=True) + "\n").encode("utf-8"), mode=MODE_VM_READABLE)
    return record


def current_run_path(vault=None) -> Path:
    """VM-READABLE pointer at the run id the host assigned (id + start only)."""
    return shared_dir(vault) / "current-run.json"


def record_run_validity(vault, run_id: str, verdict: str, *, reason: str = "",
                        detail: Any = None,
                        ts: str | None = None) -> dict[str, Any]:
    """Record the host validator's verdict for ONE run (INS-01 / s03 writes it).

    Re-validation is legitimate (a run's artifact set can complete after a
    first look), so this OVERWRITES — the verdict of record is the newest one,
    and the claim gate re-reads it on every hourly pass."""
    if verdict not in RUN_VERDICTS:
        raise ValueError(f"verdict must be one of {RUN_VERDICTS}, got {verdict!r}")
    rid = _checked_run_id(run_id)
    rec: dict[str, Any] = {"run_id": rid, "verdict": verdict,
                           "reason": reason, "recorded": ts or _ts()}
    if detail is not None:
        rec["detail"] = detail
    p = run_validity_path(vault, rid)
    p.parent.mkdir(parents=True, exist_ok=True)
    # ATOMIC: the claim gate re-reads this file on every hourly pass, and a torn
    # read is indistinguishable from "no verdict recorded" — which would silently
    # quarantine a valid run's candidates.
    _write_atomic(p, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
    return rec


def run_validity(vault, run_id: Any) -> dict[str, Any]:
    """The host validator's verdict for ``run_id`` — ABSENT MEANS INCONCLUSIVE."""
    try:
        p = run_validity_path(vault, run_id)
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rec = None
    if isinstance(rec, dict) and rec.get("verdict") in RUN_VERDICTS:
        return rec
    return {"run_id": str(run_id), "verdict": RUN_INCONCLUSIVE, "recorded": None,
            "reason": "no host validation verdict recorded for this run — an "
                      "unvalidated run is INCONCLUSIVE, and INCONCLUSIVE never "
                      "permits claiming (there is no unvalidated interim)"}


# -- STA-01: the ledger join — one run, one proposal id, one digest ------------
# The run writes one `_cos_ingestion_ledger_<run>.jsonl` row per in-scope
# thread, and a `candidate` row names the proposal it staged. That row is where
# the category lives, so the host joins it back by proposal id — but NOT from
# "the newest ledger": `claim_drops` fires hourly and one day can hold several
# runs (2026-07-31 held run 58, degraded signed-out, and run 59, live). The
# join is by id AND full content digest, and it must resolve to exactly ONE
# run, ONE digest and ONE category or it is refused.
#
# BE HONEST ABOUT WHAT THIS BUYS. The ledger is still VM-written, so this makes
# the category TAMPER-EVIDENT and SINGLE-SOURCED — not host-authoritative. The
# joined value stays a VM CLAIM: it is still validated against the owner's
# overlay taxonomy by `resolve_category`, and it still can never select the
# auto lane by itself. What the join removes is the "prefer the newest row"
# rule, which a hostile or buggy producer satisfies at will by publishing a
# later row; a collision here is a quarantine and a defect, never a silent pick.
_LEDGER_GLOB = "_cos_ingestion_ledger_*.jsonl"
_LEDGER_RUN_RE = re.compile(r"^_cos_ingestion_ledger_(.+)\.jsonl$")
#: the row key naming the staged proposal (`brain cos-propose --json` returns
#: both `id` and `sha256`, so an honest producer copies them straight out)
_LEDGER_ID_KEYS = ("proposal_id", "id")
#: the row key carrying the proposal's FULL content digest
_LEDGER_DIGEST_KEYS = ("content_sha256", "proposal_sha256", "sha256")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _first(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def ledger_index(vault) -> dict[str, list[dict[str, str]]]:
    """``{proposal_id: [{"run_id", "digest", "category"}]}`` over EVERY run
    ledger in the ops dir. Built once per claim pass, not once per candidate."""
    idx: dict[str, list[dict[str, str]]] = {}
    d = run_ops_dir(vault)
    if not d.is_dir():
        return idx
    for path in sorted(d.glob(_LEDGER_GLOB)):
        m = _LEDGER_RUN_RE.match(path.name)
        if not m:
            continue
        run_id = m.group(1)
        for row in _read_jsonl(path):
            pid = _first(row, _LEDGER_ID_KEYS)
            if not pid:
                continue
            idx.setdefault(pid, []).append({
                "run_id": run_id,
                "digest": _first(row, _LEDGER_DIGEST_KEYS).lower(),
                "category": str(row.get("category") or "").strip(),
            })
    return idx


def join_ledger_category(idx: dict[str, list[dict[str, str]]],
                         proposal_id: str, sha: str) -> dict[str, Any]:
    """Which run produced this exact content, and what category did it assign?

    ``{"status": "joined"|"no-ledger-row"|"collision"|"no-digest"|
    "digest-mismatch", "run_id", "category", "reason"}``. Every non-``joined``
    status is a quarantine — never a silent default."""
    rows = idx.get(proposal_id) or []
    if not rows:
        return {"status": "no-ledger-row", "reason":
                f"no run's ingestion ledger carries a row for {proposal_id!r} — "
                "the host cannot tell which run produced it, or what category "
                "that run assigned"}
    runs = sorted({r["run_id"] for r in rows})
    if len(runs) > 1:
        return {"status": "collision", "reason":
                f"{proposal_id!r} is claimed by {len(runs)} runs ({', '.join(runs)}) "
                "— one run / one proposal id is required; refusing to pick one"}
    run_id = runs[0]
    digests = sorted({r["digest"] for r in rows})
    if len(digests) > 1:
        return {"status": "collision", "run_id": run_id, "reason":
                f"run {run_id} published {len(digests)} different content digests "
                f"for {proposal_id!r} — refusing to prefer one row over another"}
    digest = digests[0]
    if not digest:
        return {"status": "no-digest", "run_id": run_id, "reason":
                f"run {run_id}'s ledger row for {proposal_id!r} carries no content "
                "digest, so it proves nothing about THESE bytes"}
    if not _SHA256_RE.match(digest) or digest != sha:
        return {"status": "digest-mismatch", "run_id": run_id, "reason":
                f"run {run_id}'s ledger row for {proposal_id!r} names content "
                f"{digest[:12]}… but the claimed drop hashes to {sha[:12]}…"}
    cats = sorted({r["category"] for r in rows})
    if len(cats) > 1:
        return {"status": "collision", "run_id": run_id, "reason":
                f"run {run_id} assigned {proposal_id!r} {len(cats)} different "
                f"categories ({', '.join(c or '(empty)' for c in cats)})"}
    return {"status": "joined", "run_id": run_id, "category": cats[0],
            "reason": f"joined to run {run_id} by id + full content digest"}


# -- STA-01/STA-02: the claim quarantine --------------------------------------
# A candidate the host cannot ATTRIBUTE to a valid run does not get bound, and
# does not get thrown away either: it waits here with its reason recorded, and
# `claim_drops` re-tries it every pass. That is what makes "run validity gates
# claiming" implementable BEFORE the validator exists (s03) — until a run is
# scored, its candidates queue here; the moment a verdict lands, the valid
# ones are released on the next hourly pass. Nothing is stranded and nothing
# from an unproven run reaches the owner batch, the evidence keys, or the
# signed drain.
QUARANTINE_NO_LEDGER = "no-ledger-row"
QUARANTINE_NO_MANIFEST = "no-run-manifest"


def claim_quarantine_dir(vault=None) -> Path:
    return proposals_dir(vault) / "claim-quarantine"


# ===========================================================================
# INT-01 / R3 — MOUNT-RESIDENT DATA THAT BECOMES A FILESYSTEM PATH
# ===========================================================================
# One defect CLASS produced a finding in FOUR consecutive review rounds, at a
# different site each time: a value the VM can write is used as (or to build) a
# path, and the host then opens / moves / renames / unlinks it. Guarding the
# sites a reviewer happened to report is what failed twice. This is the
# ENUMERATION the guards are derived from.
#
# WHAT THE TEST BINDS: ``tests/test_cos_pathguard.py`` fails if a cos.py
# function parses JSON off disk (``json.loads``/``json.load``/a ``JSONDecoder``,
# ``_read_jsonl`` or any other ``_read_*`` helper, at module level too) and is
# not classified here; a GUARDED classification is checked by TAINT, not by a
# source grep — a parsed field may not reach a path expression except through a
# guard. Both checks are themselves probed with known-positive fixtures, so a
# detector that stopped detecting fails instead of reporting clean.
#
# WHAT IT STILL DOES NOT BIND (stated so nobody reads the census as stronger
# than it is): reader-hood does not propagate transitively past a ``_read_*``
# helper. Measured on this module, the full call-graph closure would classify
# 81 functions instead of 40 — the extra 41 are orchestration (`hold_cancel`,
# `status_block`, `calibration_report`, …) whose "classification" would be
# "guarded by its callee", which is noise, not a property. The bound is the
# helper hop, and it is enforced: a helper that parses must be named
# ``_read_*`` (test), and every caller of one is a reader.
#
# CLOSED IN INT-04/INT-05 (was: five tracked gaps here):
#   - the ATTACHMENT LANE has its signed anchor (`stage_attachment_anchor`,
#     verified in the ingest drain against the buffer it is about to sign);
#   - ``_safe_meta_path`` is GONE. It was resolve-then-use and therefore
#     raceable; rather than reopen-with-an-inode-check around it, the surface
#     was removed — a mount-written path field is reduced to its last component
#     (`_safe_basename`) and joined onto a HOST-derived root, and the
#     attachment payload is derived from the guarded id + the real directory
#     entry (`_quarantine_payload`) instead of being read at all;
#   - the SINGLE-WRITER lock moved off the mount with the append locks
#     (``config.writer_lock_path`` -> ``config.host_lock_dir()``);
#   - the no-raw-write gate now inspects ``os.open`` FLAGS (and refuses a
#     non-literal mode), instead of exempting every ``os.open``;
#   - the shared-zone permission test drives ``write_run_manifest`` and asserts
#     the fchmod-before-replace ORDERING, so deleting the production ``mode=``
#     argument fails it.
#
# Everything under ``<vault>/.brain/`` is on the Cowork VirtioFS mount, so
# every sidecar/marker/manifest/ledger below is attacker-writable. Two field
# classes matter, and each has exactly ONE guard:
#
#   ``id``-like   -> `_safe_meta_id`  : a bare slug, length-capped in ENCODED
#                                       BYTES (it becomes ``<dir>/<id>.<ext>``;
#                                       an over-long one raises ENAMETOOLONG at
#                                       the write and wedges apply/recovery)
#   ``path``-like -> NOT USED AS A PATH: reduced to `_safe_basename` and joined
#                                       onto a host-derived root (`_leaf_in`),
#                                       or ignored entirely in favour of the
#                                       real directory entry
#                                       (`_quarantine_payload`)
#   ``filename``  -> `_unique_dest`   : a bare filename (it is a move
#                                       DESTINATION joined onto an inbox root).
#                                       On the ATTACHMENT release path it is not
#                                       taken from the sidecar at all any more
#                                       (INT-04 round 3): the destination name
#                                       comes from the SIGNED batch row or hold
#                                       authorization, because the suffix picks
#                                       the ingest handler and an authorization
#                                       over bytes alone left that choice to the
#                                       mount.
#
# And no raw writes: EVERY write in this module goes through `_write_atomic`
# (unpredictable temp, O_CREAT|O_EXCL|O_NOFOLLOW, write-until-complete, cleanup
# on any failure). A predictable ``.tmp`` on the mount was found twice — first
# at ``batches.jsonl``, then at ``<run-id>.validity.json`` — so the rule is now
# the whole module, and `test_no_raw_write_remains_on_a_mount_path` enforces it.
#
# THE CENSUS (reader -> fields that reach the filesystem -> guard):
#
#   _read_receipt_pairs         id                      _safe_meta_id
#                                                       (the ONE scanner behind
#                                                       the three below; it also
#                                                       COUNTS the pairs it
#                                                       could not read, because
#                                                       a skipped receipt used
#                                                       to read as an absence —
#                                                       H6, 2026-08-13)
#   quarantined_claims          id                      via _read_receipt_pairs
#   _pending_metas              id                      via _read_receipt_pairs
#   run_proposal_drop_record    run_id (never a path)   via _read_receipt_pairs
#   attachment_metas            id, filename            _safe_meta_id +
#                                                       _quarantine_payload
#                                                       (the sidecar's `path` is
#                                                       DISCARDED, not checked;
#                                                       its `filename` no longer
#                                                       reaches the release
#                                                       destination at all — the
#                                                       SIGNED name does)
#   version_link_metas          id                      _safe_meta_id
#   hold_list                   id                      _safe_meta_id
#   _hold_release_due_locked    id (marker + dirent)    _safe_meta_id, else the
#                                                       raw directory entry
#   _attachment_lifecycle       dest, src               _leaf_in(inbox |
#                                                       quarantine) — basename
#                                                       only, host-derived root
#   attachment_release_records  (none)                  content sha only; the
#                                                       dest NAME is no longer
#                                                       read or compared
#   _ingested_raw_id            <note id>               safe_slug
#   verify_evidence_bundle      files{} keys            basename-only
#   ingest_sweep manifests      filename                already basename-only
#                                                       (documented at the verb)
#
#   NO path-bearing field (read for metadata only, listed so the next session
#   does not have to re-derive it): approved_anchor + attachment_anchor +
#   verified_hold (all signature-verified, and their stores are OFF the mount),
#   _bound_meta, _hold_evidence, _read_jsonl over claims/outcomes/batches/
#   ledgers, corrections-pending payloads, route_stats.
#
#   The consume journal's ``batch_id`` is a lookup key, but the BATCH
#   CANDIDATE IDS it leads to become ``pending/<id>.md`` — so
#   `_apply_batch_decision` guards every one of them. A batch signature proves
#   the host wrote the row, NOT that the id is path-safe: a batch signed by a
#   pre-guard version replays through there after an upgrade.
#
#   ATTACHMENT LANE (INT-04, now closed): `_accept_attachment` reads the
#   quarantined bytes ONCE, checks them against the sha the owner's SIGNED
#   batch covered, signs an off-mount anchor naming the `vault/inbox/`
#   destination, and WRITES that verified buffer there (it no longer moves a
#   file it did not read). `brain.ingest.pipeline` re-verifies the buffer it is
#   about to sign against that anchor and quarantines a mismatch loudly.
# ===========================================================================
def _safe_meta_id(m: Any) -> str | None:
    """The sidecar's ``id``, ONLY if it is a bare slug — else ``None``.

    Every one of these sidecars lives under ``.brain/`` on the shared mount and
    every reader turns its ``id`` into a PATH (``<dir>/<id>.md``,
    ``<id>.refused.json``, …). A marker carrying
    ``id: "../../../../brain/resources/pwned"`` wrote a real attacker-named file
    inside the vault. Guarding the readers — not one call site — is what makes
    that true for the callers too, present and future."""
    if not isinstance(m, dict):
        return None
    try:
        return safe_slug(str(m.get("id") or ""))
    except ValueError:
        return None


def _safe_basename(value: Any) -> str | None:
    """The BARE FILENAME in ``value``, or ``None`` if there isn't one.

    The replacement for the old ``_safe_meta_path`` (INT-05). That guard took a
    mount-written path, resolved it, and checked the RESULT was inside an
    allowed root — a resolve-then-use check with a real window between them:
    rename the checked directory and substitute a symlink, and the move/unlink
    that followed acted on something else entirely. Narrowing that window is
    not a fix, so the surface is gone instead: a mount-written path field is
    now reduced to its last component and joined onto a root the HOST derives,
    which cannot name anything outside that root at any point in time. No
    resolve, no comparison, nothing to race.

    Separators are refused rather than stripped on both platforms' spellings,
    so ``a\\b`` cannot become a filename on POSIX either."""
    name = str(value or "")
    if not name or name in (".", ".."):
        return None
    if (os.sep in name or (os.altsep and os.altsep in name)
            or "/" in name or "\\" in name):
        return None
    if Path(name).name != name or Path(name).is_absolute():
        return None
    return name


def _move_dirent(src: Path, dest: Path) -> bool:
    """Move a DIRECTORY ENTRY, never following a symlink at the leaf.

    ``os.replace`` acts on the entry itself, so a link planted at a derived
    name travels as a link and is never dereferenced. Only the cross-device
    fallback (``shutil.move``, which copies through a link) could exfiltrate
    what such a link points at, so that fallback is refused for one — the
    quarantine, expired and inbox trees are on one filesystem in every
    supported layout, and a link there is not the payload anyway."""
    try:
        os.replace(src, dest)
        return True
    except OSError:
        if src.is_symlink():
            return False
        shutil.move(str(src), str(dest))
        return True


def _leaf_in(root: Path, value: Any) -> Path | None:
    """``root/<basename of value>``, but only if it is a regular file today.

    ``is_symlink`` is checked explicitly: ``is_file()`` FOLLOWS links, so a
    link planted at the derived name would otherwise pass as a file and hand a
    move/unlink someone else's inode."""
    name = _safe_basename(Path(str(value or "")).name)
    if name is None:
        return None
    p = Path(root) / name
    try:
        if p.is_symlink() or not p.is_file():
            return None
    except OSError:
        return None
    return p


def _read_receipt_pairs(d: Path) -> tuple[list[dict[str, Any]], int]:
    """Every READABLE ``<id>.json`` + ``<id>.md`` pair in ``d``, and how many
    pairs were UNREADABLE or INCOMPLETE.

    ONE SCANNER, TWO DIRECTORIES, AND IT COUNTS WHAT IT COULD NOT READ (review
    2026-08-13, round 5, H6). Both readers used to `continue` past a meta that
    would not parse, so a corrupt or half-written receipt was indistinguishable
    from a directory with nothing in it. That absence is what
    ``run_proposal_drops`` reports as a count, and what K2's
    ``check_candidate_stamps`` reads as "the HOST's own record agrees: zero
    drops" — so a run denying a drop it made passed the control by damaging the
    receipt. Unreadable evidence is not absence; it is unreadable evidence, and
    the number of it has to come back with the answer.

    Four ways a pair fails, all counted the same because the caller's decision
    is the same: the meta will not parse, it carries no usable id, its ``.md``
    half is missing, or its ``.json`` half is missing — BOTH directions of a
    partially-written or partially-deleted pair.

    SCAN THE UNION OF STEMS, not just ``*.json`` (review 2026-08-13, round 6,
    H-md). The producer writes the ``.md`` before the ``.json``, so a crash
    between the two atomic writes leaves a ``.md`` with NO ``.json`` — and
    iterating ``*.json`` alone never sees it, returning a clean ``0`` that reads
    as "the HOST's own record agrees: zero drops" and reopens H6's fail-open in
    that partial-write window. A missing half in either direction is an
    incomplete pair.
    """
    out: list[dict[str, Any]] = []
    malformed = 0
    if not d.is_dir():
        return out, 0
    stems = sorted({p.stem for p in d.glob("*.json")}
                   | {p.stem for p in d.glob("*.md")})
    for stem in stems:
        meta_path = d / f"{stem}.json"
        try:
            m = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A `.md`-only orphan (no `.json` to open → FileNotFoundError) or a
            # `.json` that will not parse — both are an unreadable/incomplete
            # pair, counted the same.
            malformed += 1
            continue
        nid = _safe_meta_id(m)
        # THE PAIR IS ONE STEM, not two files that happen to both exist (review
        # 2026-08-13, round 7). `a.json` carrying `{"id": "b"}` beside a real
        # `b.md` used to be ACCEPTED — the `.md` existence test asked about the
        # EMBEDDED id, so a receipt could claim another receipt's body and be
        # returned as usable to `_pending_metas`/`quarantined_claims` while its
        # own body was missing. A receipt names itself or it is malformed.
        if not nid or nid != stem or not (d / f"{nid}.md").exists():
            malformed += 1
            continue
        out.append({**m, "id": nid})
    return out, malformed


def quarantined_claims(vault) -> list[dict[str, Any]]:
    """Every candidate waiting on run attribution/validity, newest reason first."""
    return _read_receipt_pairs(claim_quarantine_dir(vault))[0]


def _quarantine_claim(vault, *, nid: str, text: str, sha: str, code: str,
                      reason: str, run_id: str | None, now: _dt.datetime,
                      source: str) -> dict[str, Any]:
    """Park ONE candidate with its reason. Idempotent per (id, reason code):
    the release path re-runs the gate hourly, and a defect row per retry would
    bury the real ones under thousands of copies of the same sentence."""
    qdir = claim_quarantine_dir(vault)
    qdir.mkdir(parents=True, exist_ok=True)
    meta_path = qdir / f"{nid}.json"
    try:
        prior = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prior = {}
    _write_atomic(qdir / f"{nid}.md", text.encode("utf-8"))
    first = not (isinstance(prior, dict) and prior.get("code") == code
                 and prior.get("sha256") == sha)
    rec = {"id": nid, "sha256": sha, "code": code, "reason": scrub(reason),
           "run_id": run_id, "source": source,
           "quarantined": prior.get("quarantined") if not first else _ts(now),
           "last_checked": _ts(now), "first": first}
    _write_atomic(meta_path, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
    if first:
        log_defect(vault, f"claim-quarantined:{code}", f"{nid}: {reason}",
                   ts=_ts(now))
    return rec


# -- broker: claim drops -------------------------------------------------------
def claim_drops(vault, now: _dt.datetime | None = None) -> dict[str, Any]:
    """Validate + claim every proposal drop into ``host/proposals/pending/``.

    HOST side of the trust boundary. Each drop is: schema-validated
    (``capture.validate``), classification-checked, secret-scrubbed, and
    replay-checked against the content-hash claims ledger. A drop that fails
    any check is moved to ``rejected/`` (never signed, never silently lost);
    a replayed drop (hash already claimed) is deleted and logged.

    Runs under the writer lock (R2, 2026-07-30 review, HIGH). A claim can fire
    `demote_category` on a security defect, and `hold_release_due` — which DOES
    hold the lock — re-checks eligibility from those same statistics before it
    moves a held item. Unlocked, a demotion could land between that check and
    the move, and the held item of a just-demoted category would still be
    released. The two must be mutually exclusive."""
    from . import capture as cap_mod

    with vault_writer_lock(vault, verb="cos-claim"):
        return _claim_drops_locked(vault, cap_mod, now or _utcnow())


def _claim_drops_locked(vault, cap_mod, now: _dt.datetime) -> dict[str, Any]:
    claimed: list[str] = []
    rejected: list[dict[str, str]] = []
    replayed: list[str] = []
    quarantined: list[dict[str, Any]] = []
    unjoined = 0
    ledger = _read_jsonl(_claims_path(vault))
    seen_hashes = {e.get("sha256") for e in ledger}
    pending = proposals_dir(vault) / "pending"
    rej_dir = proposals_dir(vault) / "rejected"
    ddir = proposal_drop_dir(vault)
    pending.mkdir(parents=True, exist_ok=True)
    rej_dir.mkdir(parents=True, exist_ok=True)

    ttl_days = _env_days(PROPOSAL_TTL_DAYS_ENV, DEFAULT_PROPOSAL_TTL_DAYS)
    # Read the owner's ingest taxonomy ONCE per claim pass (and log the
    # fail-closed defect at most once), never per candidate. Same for the
    # ledger index: one scan of every run ledger, not one per candidate.
    taxonomy = ingest_taxonomy(vault, log=True)
    ledger_idx = ledger_index(vault)

    # RUN VALIDITY GATES CLAIMING, in both directions and on every pass:
    #   (a) candidates bound BEFORE this gate existed, or whose run has since
    #       been scored INVALID/INCONCLUSIVE, leave `pending/` for quarantine;
    #   (b) anything a verdict has since cleared is released back into
    #       `pending/` — so a queue built while s03's validator did not yet
    #       exist drains by itself the hour after it lands.
    # Release BEFORE sweep, deliberately: a candidate swept out of `pending/`
    # in this pass waits for the next one rather than being re-examined
    # milliseconds later by the same fold. One decision per pass is easier to
    # read in the defect log than a sweep and a re-bind at the same timestamp.
    released = _release_quarantined_claims(
        vault, cap_mod, now, taxonomy=taxonomy, ledger_idx=ledger_idx,
        ttl_days=ttl_days)
    swept = _sweep_pending_without_valid_run(vault, now)
    quarantined.extend(swept)

    for f in sorted(ddir.glob("*.md")) if ddir.is_dir() else []:
        # Trust boundary: the drop dir is VM-writable. A symlink here could
        # smuggle host-side content past validation (read-through) or be
        # target-swapped after validation (TOCTOU) — only regular files are
        # ever claimed; a symlink is deleted and logged, never followed.
        if f.is_symlink() or not f.is_file():
            f.unlink(missing_ok=True)
            rejected.append({"drop": f.name, "reason": "not a regular file (symlink refused)"})
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as exc:
            rejected.append({"drop": f.name, "reason": f"unreadable: {exc}"})
            continue
        # R5 (2026-07-30 review, HIGH): host-only keys come off the untrusted
        # text BEFORE it is hashed, validated or moved — this is the one strip,
        # and everything after it (the sha, the sidecar, the copy promoted into
        # the signed drain) sees only sanitized content. STA-01 widens the key
        # set: the producer-version stamps are HOST-derived from the run
        # manifest now, so a VM claim of them is stripped exactly like a forged
        # `provenance.verified` — including out of the bytes that get signed.
        try:
            text = provenance.without_host_only_text(text, keys=_STRIPPED_CLAIM_KEYS)
        except provenance.HostOnlyKeyResidue as exc:
            # Fails closed: a construct engineered so no LINE spells the key
            # while the document still resolves it. That is a deliberate
            # forgery, not a malformed note — reject the drop outright.
            reason = f"host-only provenance forgery: {exc}"
            shutil.move(str(f), rej_dir / f"{now.strftime('%Y%m%dT%H%M%S')}-{f.name}")
            rejected.append({"drop": f.name, "reason": reason})
            _append_jsonl(_claims_path(vault),
                          {"sha256": sha256_text(text), "drop": f.name,
                           "ts": _ts(now), "disposition": "rejected: " + reason})
            continue
        sha = sha256_text(text)
        if sha in seen_hashes:
            f.unlink(missing_ok=True)
            replayed.append(f.name)
            _append_jsonl(_claims_path(vault),
                          {"sha256": sha, "drop": f.name, "ts": _ts(now),
                           "disposition": "replay-rejected"})
            continue
        out = _bind_claim(vault, cap_mod, text=text, sha=sha, source=f.name,
                          now=now, taxonomy=taxonomy, ledger_idx=ledger_idx,
                          ttl_days=ttl_days)
        if out["state"] == "rejected":
            shutil.move(str(f), rej_dir / f"{now.strftime('%Y%m%dT%H%M%S')}-{f.name}")
            rejected.append({"drop": f.name, "reason": out["reason"]})
            _append_jsonl(_claims_path(vault),
                          {"sha256": sha, "drop": f.name, "ts": _ts(now),
                           "disposition": "rejected: " + out["reason"]})
            continue
        f.unlink(missing_ok=True)
        seen_hashes.add(sha)
        if out["state"] == "quarantined":
            quarantined.append(out["quarantine"])
            if out["quarantine"]["code"] == QUARANTINE_NO_LEDGER:
                unjoined += 1
            _append_jsonl(_claims_path(vault),
                          {"sha256": sha, "id": out["id"], "ts": _ts(now),
                           "disposition": "quarantined: " + out["quarantine"]["code"]})
            continue
        claimed.append(out["id"])
        _append_jsonl(_claims_path(vault),
                      {"sha256": sha, "id": out["id"], "ts": _ts(now),
                       "disposition": "claimed"})

    # Correction drops (verdict-drop/correction-*.json) → corrections-pending/.
    corrections_claimed: list[str] = []
    vdir = verdict_drop_dir(vault)
    cor_pending = proposals_dir(vault) / "corrections-pending"
    if vdir.is_dir():
        cor_pending.mkdir(parents=True, exist_ok=True)
        for f in sorted(vdir.glob("correction-*.json")):
            if f.is_symlink() or not f.is_file():  # same symlink refusal as above
                f.unlink(missing_ok=True)
                rejected.append({"drop": f.name,
                                 "reason": "not a regular file (symlink refused)"})
                continue
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = None
            errs = _validate_correction_payload(payload)
            if errs:
                dest = rej_dir / f"{now.strftime('%Y%m%dT%H%M%S')}-{f.name}"
                shutil.move(str(f), dest)
                rejected.append({"drop": f.name, "reason": "; ".join(errs)})
                continue
            shutil.move(str(f), cor_pending / f.name)
            corrections_claimed.append(f.name)

    if quarantined or unjoined:
        _bump_route_stats(vault, now=now, unjoined_claims=unjoined,
                          quarantined_claims=len(quarantined))
    return {"claimed": claimed, "rejected": rejected, "replayed": replayed,
            "corrections_claimed": corrections_claimed,
            # Loud, never silent: an unjoinable or unvalidated candidate is
            # reported per pass here, counted cumulatively in `route_stats`,
            # and surfaced in `brain status` + the morning brief exactly like
            # `unstamped_batched`.
            "quarantined": quarantined,
            "unjoined_claims": unjoined,
            "released_from_quarantine": released,
            "quarantine_open": len(quarantined_claims(vault))}


#: producer-version stamps the HOST now derives from the run manifest. A VM
#: claim of either is stripped at the trust boundary like any host-only key —
#: out of the routing mapping AND out of the bytes that later get signed.
#: `category` is deliberately NOT stripped from the text: it is never read for
#: routing (the ledger join is the single source), and leaving the producer's
#: own label in the note keeps its claim visible beside the host's decision.
PRODUCER_STAMP_KEYS: tuple[str, ...] = ("bundle_version", "extraction_rules_version")
_STRIPPED_CLAIM_KEYS: tuple[str, ...] = provenance.HOST_ONLY_KEYS + PRODUCER_STAMP_KEYS


def _bind_claim(vault, cap_mod, *, text: str, sha: str, source: str,
                now: _dt.datetime, taxonomy: dict[str, Any],
                ledger_idx: dict[str, list[dict[str, str]]],
                ttl_days: int) -> dict[str, Any]:
    """Validate ONE sanitized candidate and bind it — or refuse it.

    Returns ``{"state": "claimed"|"rejected"|"quarantined", "id", "reason"}``.
    The caller owns the SOURCE bytes (a drop file to move/unlink, or a
    quarantine entry to clear); this owns the host-side records.

    Order matters and is deliberate: content rejections (schema, secrets, a
    `never` category, a duplicate id) are decided FIRST and still win, because
    they are statements about the candidate itself. Only a candidate that would
    otherwise be claimed reaches the RUN GATE, which asks a different question
    — can the host prove which run produced these exact bytes, and was that run
    valid? — and parks it if it cannot.
    """
    pending = proposals_dir(vault) / "pending"
    reasons: list[str] = list(cap_mod.validate(text))
    secrets = secret_findings(text)
    if secrets:
        reasons.append("secret-scrub: " + ", ".join(secrets))
    meta, body = frontmatter.parse_text(text)
    # R5: this text was read RAW off the VM-writable drop dir — `cos_propose`'s
    # own strip never ran on it. Drop every host-only + host-derived key HERE,
    # at the parse, so nothing downstream (lineage, tier, sidecar) has to
    # remember to distrust it.
    meta = provenance.without_host_only(meta, keys=_STRIPPED_CLAIM_KEYS)
    nid: str | None = None
    try:
        nid = safe_slug(meta.get("id") or Path(source).stem)
    except ValueError as exc:
        reasons.append(f"unsafe id: {exc}")

    # STA-01: WHICH RUN produced these bytes, and what did it call them? Joined
    # by proposal id + full content digest, from that run's own ledger — never
    # from "the newest ledger", and never from the candidate's frontmatter.
    join = (join_ledger_category(ledger_idx, nid, sha) if nid else
            {"status": QUARANTINE_NO_LEDGER,
             "reason": "candidate carries no usable id to join a ledger row by"})
    run_id = join.get("run_id")
    manifest = run_manifest(vault, run_id) if join["status"] == "joined" else None
    # The joined category remains a VM CLAIM — validated against the owner's
    # taxonomy here, exactly as a frontmatter claim used to be, and still never
    # able to select the auto lane by itself.
    category, disposition = resolve_category(
        vault, join.get("category"), lane=LANE_TEXT, taxonomy=taxonomy)
    # B1 (2026-07-30 review, CRITICAL): the tier is HOST-DERIVED from the
    # material, never copied off the VM's `classification:` label.
    # R1: every text on THIS lane — subject, sender, body — is VM-authored, so
    # none of it may drive the LOWERING keyword match. No `verified_texts` is
    # passed: the tier stays at the email-derived MNPI default unless
    # `proposed` / the category floor RAISES it.
    tier, _tier_why = provenance.email_classification(
        vault, proposed=meta.get("classification"), category=category)
    # HOST-DERIVED, from the manifest frozen at run LAUNCH — not from whatever
    # bundle is deployed at claim time, which is a different (later) bundle
    # whenever the skill has been updated since the run.
    bundle_version = (manifest or {}).get("bundle_version")
    rules_version = (manifest or {}).get("extraction_rules_version")

    if disposition == DISPOSITION_NEVER:
        # Belt-and-braces: the SKILL is supposed to stage zero candidates
        # for a `never` category, but doctrine alone is not a gate.
        reasons.append(f"category {category!r} is a never-ingest category "
                       "(overlay cos/ingest.md)")
        log_defect(vault, "never-category-proposed",
                   f"{source}: category={category}", ts=_ts(now))
    if secrets:
        # ING-04 defect signal: a claim-time secret/classification finding
        # disqualifies the candidate's PATTERN from auto-capture eligibility
        # outright (zero-tolerance), regardless of this candidate's own fate
        # — and (LRN-02) DEMOTES its category immediately, evidence reset.
        record_outcome(vault, pattern=meta.get("pattern"), ident=nid or Path(source).stem,
                       outcome="claim-rejected-security",
                       bundle_version=bundle_version, ts=_ts(now),
                       category=category, lane=LANE_TEXT, tier=tier,
                       rules_version=rules_version)
        demote_category(vault, category,
                        reason=f"claim-rejected-security: {source}", ts=_ts(now))
    if nid and not reasons and (pending / f"{nid}.md").exists():
        reasons.append(f"duplicate pending id: {nid!r}")
    if reasons:
        return {"state": "rejected", "id": nid, "reason": "; ".join(reasons)}

    # -- the RUN GATE ---------------------------------------------------------
    if join["status"] != "joined":
        code = (QUARANTINE_NO_LEDGER if join["status"] == QUARANTINE_NO_LEDGER
                else f"ledger-{join['status']}")
        return _quarantined(vault, nid=nid, text=text, sha=sha, code=code,
                            reason=join["reason"], run_id=run_id, now=now,
                            source=source)
    if manifest is None:
        return _quarantined(
            vault, nid=nid, text=text, sha=sha, code=QUARANTINE_NO_MANIFEST,
            reason=(f"run {run_id} has no host run manifest — the host never "
                    "recorded which bundle produced it, so its version stamps "
                    "cannot be derived (`brain cos-run-begin` writes one at "
                    "run launch)"),
            run_id=run_id, now=now, source=source)
    verdict = run_validity(vault, run_id)
    if verdict["verdict"] not in CLAIMABLE_VERDICTS:
        return _quarantined(
            vault, nid=nid, text=text, sha=sha,
            code=f"run-{verdict['verdict'].lower()}",
            reason=(f"run {run_id} is {verdict['verdict']}: "
                    f"{verdict.get('reason') or 'no reason recorded'}"),
            run_id=run_id, now=now, source=source)

    dest = pending / f"{nid}.md"
    # Write the SANITIZED text (R5) rather than moving the drop bytes, so
    # `pending/<id>.md` hashes to the recorded `sha` by construction — that
    # equality is the proposal-level CAS `consume_answers` re-checks.
    _write_atomic(dest, text.encode("utf-8"))
    # -- AUTHORITY OF EACH ROUTING LABEL (B1, 2026-07-30 review; STA-01) ------
    # Binding a VM-authored value to the content sha makes it tamper-EVIDENT,
    # not AUTHORITATIVE. So each label in this sidecar carries a DIFFERENT,
    # explicit authority:
    #
    #   tier            HOST-DERIVED above from the material itself. This is
    #                   the only routing value with real authority, and it is
    #                   an exact component of the graduation key — evidence
    #                   gathered on Internal material can never authorize an
    #                   MNPI-bound candidate.
    #   category        HOST-JOINED from the producing run's ledger by id +
    #                   content digest, then HOST-VALIDATED against the owner's
    #                   taxonomy. Single-sourced and tamper-evident; still a VM
    #                   CLAIM about MEMBERSHIP, so the name alone never selects
    #                   the auto lane — it only selects which (lane, tier,
    #                   ruleset) evidence bucket the candidate is matched
    #                   against, and that bucket is filled exclusively by
    #                   recorded OWNER verdicts.
    #   bundle_version  HOST-DERIVED from the run manifest frozen at LAUNCH.
    #   rules_version   Not producer-assertable at all any more.
    #   pattern         NOT host-derivable, and therefore NO authority of its
    #                   own: opaque producer identity used to SCOPE evidence,
    #                   and it can only NARROW. A value the host has no accrued
    #                   owner verdicts for has zero volume and forces the owner
    #                   batch; an absent value is `unclassified`, which vetoes
    #                   the auto lane outright (see `_UNPATTERNED`).
    meta_path = pending / f"{nid}.json"
    _write_atomic(meta_path, json.dumps({
        "id": nid, "sha256": sha, "claimed": _ts(now),
        "ttl_expires": _ts(now + _dt.timedelta(days=ttl_days)),
        "state": "pending",
        "category": category, "disposition": disposition, "lane": LANE_TEXT,
        "tier": tier, "rules_version": rules_version,
        "pattern": meta.get("pattern"),
        "bundle_version": bundle_version,
        "kind": meta.get("kind"),
        # STA-01: the host's own attribution of this candidate to a run, and
        # the manifest digest of the bundle that produced it. `expire`/`sweep`
        # re-read these, so a run later scored INVALID takes its candidates
        # with it.
        "run_id": run_id,
        "skill_sha256": manifest.get("skill_sha256"),
        "run_verdict": verdict["verdict"],
        "evidence_unit": evidence_unit_key(
            category=category, lane=LANE_TEXT, rules_version=rules_version,
            body=body),
        # Host-verified conversation lineage ONLY (B5). `capture.enforce`
        # strips any `provenance.verified` a VM asserts, and R5's
        # `without_host_only` above re-strips it for a drop written
        # DIRECTLY into the drop dir — so a VM drop never earns this,
        # whichever way it arrived. An unverified conversation id must not
        # be able to mark independent verdicts as one already-counted thread.
        "evidence_lineage": evidence_lineage_key(
            category=category, lane=LANE_TEXT, rules_version=rules_version,
            conversation_id=meta.get("provenance.conversation_id"),
            verified=bool(meta.get(provenance.VERIFIED_KEY))),
    }, sort_keys=True).encode("utf-8") + b"\n")
    return {"state": "claimed", "id": nid, "reason": join["reason"],
            "run_id": run_id, "category": category}


def _quarantined(vault, *, nid: str | None, text: str, sha: str, code: str,
                 reason: str, run_id: str | None, now: _dt.datetime,
                 source: str) -> dict[str, Any]:
    ident = nid or ("unnamed-" + sha[:12])
    rec = _quarantine_claim(vault, nid=ident, text=text, sha=sha, code=code,
                            reason=reason, run_id=run_id, now=now, source=source)
    return {"state": "quarantined", "id": ident, "reason": reason,
            "quarantine": rec}


def _sweep_pending_without_valid_run(vault, now: _dt.datetime) -> list[dict[str, Any]]:
    """STA-02: candidates ALREADY in ``pending/`` whose run is not proven valid.

    Run 59 staged 8 candidates and skipped its entire self-eval, so s03 must be
    able to score it INVALID — and a verdict that does not reach the candidates
    is cosmetic. Re-stamping them would be worse: it would launder the output
    of a provably-uncontrolled run into the trusted pipeline, where an owner
    accept would sign it AND teach the graduation gate. So they leave
    ``pending/`` with the reason recorded, and the content is recovered by
    re-extraction on a run that passes validation.

    A candidate already sitting in an OPEN OWNER BATCH is swept too, on
    purpose: the batch's own proposal-level CAS then refuses to promote it
    (`pending file missing or content drifted`), which is exactly the outcome
    wanted — the owner's answer cannot sign material from an unvalidated run.
    """
    pending = proposals_dir(vault) / "pending"
    out: list[dict[str, Any]] = []
    for m in _pending_metas(vault):
        nid = str(m.get("id"))
        run_id = m.get("run_id")
        if run_id:
            verdict = run_validity(vault, run_id)
            reason = (f"run {run_id} is {verdict['verdict']}: "
                      f"{verdict.get('reason') or 'no reason recorded'}")
            code = f"run-{verdict['verdict'].lower()}"
        else:
            verdict = {"verdict": RUN_INCONCLUSIVE}
            code = "no-run-attribution"
            reason = ("bound before the host derived its own stamps (STA-01): "
                      "no run attribution, so the producing run's validity "
                      "cannot be checked. Recover the content by re-extraction "
                      "on a validated run — never by re-stamping this copy")
        if verdict["verdict"] in CLAIMABLE_VERDICTS:
            continue
        md = pending / f"{nid}.md"
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        rec = _quarantine_claim(vault, nid=nid, text=text,
                                sha=str(m.get("sha256") or sha256_text(text)),
                                code=code, reason=reason, run_id=run_id,
                                now=now, source="pending")
        md.unlink(missing_ok=True)
        (pending / f"{nid}.json").unlink(missing_ok=True)
        out.append(rec)
    return out


def _release_quarantined_claims(vault, cap_mod, now: _dt.datetime, *,
                                taxonomy: dict[str, Any],
                                ledger_idx: dict[str, list[dict[str, str]]],
                                ttl_days: int) -> list[str]:
    """Re-run the gate over every quarantined candidate; bind the ones that now
    pass. This is the other half of gating on run validity: a candidate parked
    because no verdict existed yet is NOT stranded — s03's arrival releases it
    on the next hourly pass, with no operator ritual and no re-drop."""
    qdir = claim_quarantine_dir(vault)
    rej_dir = proposals_dir(vault) / "rejected"
    released: list[str] = []
    for m in quarantined_claims(vault):
        nid = str(m.get("id"))
        try:
            text = (qdir / f"{nid}.md").read_text(encoding="utf-8")
        except OSError:
            continue
        sha = sha256_text(text)
        out = _bind_claim(vault, cap_mod, text=text, sha=sha,
                          source=f"{nid}.md", now=now, taxonomy=taxonomy,
                          ledger_idx=ledger_idx, ttl_days=ttl_days)
        if out["state"] == "quarantined":
            continue                        # still unproven — reason refreshed
        for suffix in (".md", ".json"):
            (qdir / f"{nid}{suffix}").unlink(missing_ok=True)
        if out["state"] == "claimed":
            released.append(nid)
            _append_jsonl(_claims_path(vault),
                          {"sha256": sha, "id": nid, "ts": _ts(now),
                           "disposition": "claimed (released from quarantine)"})
        else:
            rej_dir.mkdir(parents=True, exist_ok=True)
            _write_atomic(rej_dir / f"{now.strftime('%Y%m%dT%H%M%S')}-{nid}.md",
                          text.encode("utf-8"))
            log_defect(vault, "claim-quarantine-rejected",
                       f"{nid}: {out['reason']}", ts=_ts(now))
    return released


# -- proposal state helpers ----------------------------------------------------
def undecided_proposal_ids(vault) -> set[str]:
    """Candidate ids the owner has NOT yet ruled on — staged in the VM's
    proposal drop, or claimed into host ``pending/``. Both states mean "the
    owner's answer is still outstanding".

    A capture draft carrying one of these ids is a GATE BYPASS: the same
    content is simultaneously travelling the gated route (cos-propose ->
    broker -> owner batch -> selective commit) and the UNGATED one
    (draft-capture -> capture-inbox -> signed on the next drain). The ungated
    one always wins the race, so the owner gets asked to approve a note that is
    already authoritative in the vault, and a "reject" has nothing to reject.

    Measured 2026-07-16 (run 14/15): the COS skill forbids SUBSTITUTING
    draft-capture for cos-propose in Phase 1.6, but Phase 5 separately requires
    draft-capture for anything the owner must see. A finding that is also an
    ingestion candidate satisfies both rules and bypasses the gate — no rule
    violated. Policy cannot fix a collision between two obeyed rules; the
    engine must.
    """
    ids: set[str] = set()
    # The claim quarantine counts too (STA-01): a candidate parked there has
    # not been decided either, and it may still be released into a batch once
    # its run is validated — so a capture draft carrying its id is the same
    # race, not a legitimate alternative route.
    for d in (proposal_drop_dir(vault), proposals_dir(vault) / "pending",
              claim_quarantine_dir(vault)):
        if d.is_dir():
            ids.update(p.stem for p in d.glob("*.md"))
    return ids


def quarantine_gate_bypass(vault, draft: Path, *, now: _dt.datetime | None = None) -> Path:
    """Move a bypassing capture draft out of the drain's path, reversibly.

    NOT deleted (recoverable, auditable) and NOT left in place: leaving it
    would re-offer it to every subsequent drain, and — the real hazard — a
    later owner REJECT clears the gated copy out of ``pending/``, after which
    the leftover draft no longer matches an undecided id and the next drain
    would sign the very content the owner just rejected.
    """
    dest_dir = host_dir(vault) / "gate-bypass"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or _utcnow()).strftime("%Y%m%dT%H%M%S")
    dest = dest_dir / f"{stamp}-{draft.name}"
    shutil.move(str(draft), dest)
    return dest


def _pending_metas(vault) -> list[dict[str, Any]]:
    return _read_receipt_pairs(proposals_dir(vault) / "pending")[0]


def run_proposal_drops(vault, run_id: str) -> int:
    """How many proposals THIS run dropped, as the HOST recorded them.

    ONE DEFINITION, TWO CALLERS (review 2026-08-13, round 2, K2). `load_night`
    used to hardcode `proposals_dropped: False` onto every candidate row, so
    nothing in production could ever set it True and the only "known positive"
    for the control that reads it was a hand-built dict. That is
    `vocabulary-needs-a-producer` one layer up: the flag short-circuits
    `check_candidate_stamps` BEFORE it inspects a single proposal id or digest,
    so the day a real drop lane exists, a producer that forgets the flag hides
    duplicate ids and digest mismatches behind "does not apply".

    So applicability is DERIVED, from the two host-written sidecars that carry
    a `run_id` — the pending proposal metas the host wrote when it took
    delivery, and the claims it quarantined for attribution or validity. Both
    are outside the run's control: a run writes its own ledger and its own
    markers, never these. The proposal DROP directory itself is deliberately
    not counted — `propose` records no run there, so a file in it cannot be
    attributed to a run, which is exactly why an unattributable claim
    quarantines and is counted here through that route instead.

    A count, not a boolean, because the caller that verifies wants the number
    and the caller that stamps wants `> 0`.
    """
    return run_proposal_drop_record(vault, run_id)["drops"]


def run_proposal_drop_record(vault, run_id: str) -> dict[str, Any]:
    """``{"drops": N, "malformed": M}`` — the host's record AND its damage.

    K2 FAILS CLOSED ON CORRUPTION (review 2026-08-13, round 5, H6). The count
    above answers "how many drops did the host record for this run", and
    ``check_candidate_stamps`` reads a zero as the host AGREEING that the run
    dropped nothing — the branch that makes the whole control inapplicable. A
    receipt the loaders could not parse produced exactly that zero, so damaging
    one receipt turned a contradiction into a "does not apply".

    ``malformed`` is deliberately NOT scoped to a run. An unreadable receipt has
    no readable ``run_id``, so it cannot be attributed to any run — including
    the one asking. Every run's answer is inconclusive until it is repaired,
    which is the honest reading and the fail-closed one.
    """
    want = str(run_id or "")
    pending, mal_pending = _read_receipt_pairs(proposals_dir(vault) / "pending")
    quarantined, mal_quarantined = _read_receipt_pairs(claim_quarantine_dir(vault))
    return {"drops": sum(1 for m in list(pending) + list(quarantined)
                         if str(m.get("run_id") or "") == want),
            "malformed": mal_pending + mal_quarantined}


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


def _gc_compact_locked(vault, now: _dt.datetime) -> dict[str, int]:
    cutoff = now.timestamp() - _env_days(GC_DAYS_ENV, DEFAULT_GC_DAYS) * 86400
    removed = 0
    for d in (proposals_dir(vault) / "rejected", proposals_dir(vault) / "expired",
              attachment_expired_dir(vault), _version_link_expired(vault),
              # B3: a lifecycle record only exists so a LATE undo can still
              # reach a released attachment — past the GC window it is residue,
              # exactly like the released-hold markers below.
              attachment_lifecycle_dir(vault)):
        if not d.is_dir():
            continue
        for f in d.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
    # Released-hold evidence markers exist only so a late undo can still name
    # the category to demote — past the GC window they are pure residue.
    hdir = hold_dir(vault)
    if hdir.is_dir():
        for f in hdir.glob("*.released.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
    # Compact consumed/expired batch records older than the GC window.
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
        _write_atomic(bpath, "".join(json.dumps(b, sort_keys=True) + "\n"
                                     for b in keep).encode("utf-8"))
    return {"files_removed": removed, "batches_compacted": dropped}


# -- VER-01/VER-02: deduced version links ------------------------------------
# A THIRD candidate kind in the SAME owner batch as ingestion candidates and
# quarantined attachments — one nightly question, three kinds of item. The
# signal rules live in `brain.versionlink` (pure); everything here is the
# broker half: persist a candidate, ride the batch, apply an ACCEPT through the
# audited `core.supersede`, and remember every pair so it is never re-asked.
#
# The two provably-unambiguous tiers stay AUTOMATIC and untouched:
# sha256-identical duplicates (DDP-01) and explicit `…-vN` id families
# (VER-01's `auto_version_chains`). This deduced tier is propose-only. It can
# graduate later under the SAME S05 gate with no special-case code, because its
# verdicts accrue against the ordinary evidence key
# (category="version-link", lane, classification tier, rules_version).
KIND_SUPERSEDE = "supersede"


def version_links_dir(vault=None) -> Path:
    return proposals_dir(vault) / "version-links"


def _version_link_pending(vault=None) -> Path:
    return version_links_dir(vault) / "pending"


def _version_link_expired(vault=None) -> Path:
    return version_links_dir(vault) / "expired"


def _version_ledger_path(vault=None) -> Path:
    return version_links_dir(vault) / "ledger.jsonl"


#: CUR-01: ONE line per fold run, in its own file so it never collides with the
#: per-pair ledger's schema. This is the ENGAGEMENT record — the cautionary
#: tale behind it is an inference job that silently never ran while every exit
#: code stayed green, so "did it run, and what did it see" has to be greppable
#: without reasoning about anything:
#:     grep -c '"event": "version-link-run"' .../version-links/runs.jsonl
VERSION_LINK_RUN_EVENT = "version-link-run"


def _version_runs_path(vault=None) -> Path:
    return version_links_dir(vault) / "runs.jsonl"


def version_link_runs(vault) -> list[dict[str, Any]]:
    return _read_jsonl(_version_runs_path(vault))


def version_link_digest(meta: dict[str, Any]) -> str:
    """The content identity of ONE version-link proposal — what the batch's
    signed digest actually binds. Covers both notes' hashes AND the signals the
    owner was shown, so a rewritten proposal can never ride an old approval."""
    return sha256_text(json.dumps(
        {k: meta.get(k) for k in
         ("old_id", "new_id", "old_sha256", "new_sha256", "signals")},
        sort_keys=True, separators=(",", ":"), default=str))


def version_link_metas(vault) -> list[dict[str, Any]]:
    """Version-link proposals awaiting an owner answer."""
    d = _version_link_pending(vault)
    out: list[dict[str, Any]] = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        nid = _safe_meta_id(m)
        if nid and isinstance(m, dict) and m.get("kind") == KIND_SUPERSEDE:
            out.append({**m, "id": nid})
    return out


def version_link_ledger(vault) -> list[dict[str, Any]]:
    return _read_jsonl(_version_ledger_path(vault))


def decided_pair_keys(vault) -> set[str]:
    """Every pair this vault has already ruled on, in EITHER direction —
    proposed-and-waiting, rejected, applied, declined-ambiguous, gone stale or
    expired unanswered. A pair in here is never generated again: re-asking a
    question the owner already answered (or pointedly did not) is nagging."""
    return {str(e.get("pair_key")) for e in version_link_ledger(vault)
            if e.get("pair_key")}


def _record_version_link(vault, pair_key: str, state: str, **extra: Any) -> dict[str, Any]:
    rec = {"pair_key": pair_key, "state": state, "ts": extra.pop("ts", None) or _ts()}
    rec.update(provenance.scrub(extra))
    _append_jsonl(_version_ledger_path(vault), rec)
    return rec


def _pair_tier(*classifications: str) -> str:
    """The MOST RESTRICTIVE of the pair's tiers — the evidence key a
    version-link verdict counts against. Evidence gathered on Internal material
    must never authorize the same move on Restricted material (LRN-01's
    exact-tier keying), so the pair takes its higher side."""
    from .classification import RANK

    best = ""
    for c in classifications:
        c = str(c or "").strip()
        if c in RANK and (not best or RANK[c] > RANK[best]):
            best = c
    return best or "unknown"


def version_link_fold(core, now: _dt.datetime | None = None) -> dict[str, Any]:
    """VER-01: deduce version links over recently committed sources and stage
    each as a propose-only candidate. HOST-broker only (the caller — the COS
    broker fold — already required host); writes under the writer lock."""
    core._require_host("generate version-link proposals")
    now = now or _utcnow()
    with vault_writer_lock(core.vault, verb="cos-version-links"):
        return _version_link_fold_locked(core, now)


def _version_link_fold_locked(core, now: _dt.datetime) -> dict[str, Any]:
    from . import versionlink as vl

    vault = core.vault
    cutoff = (now - _dt.timedelta(days=vl.window_days())).date().isoformat()
    res = vl.generate(core, cutoff=cutoff, exclude=decided_pair_keys(vault))
    report: dict[str, Any] = {
        "proposed": [], "declined": [], "by_class": {},
        "pairs_examined": res["pairs_examined"], "truncated": res["truncated"],
        "min_similarity": vl.min_similarity(),
    }
    for amb in res["ambiguous"]:
        # Declined, logged, never proposed — mirrors auto_version_chains'
        # skipped_ambiguous: an engine that cannot derive the order says so.
        _record_version_link(vault, amb["pair_key"], "declined", ts=_ts(now),
                             old_id=amb["old_id"], new_id=amb["new_id"],
                             reason=amb["reason"], signals=amb["signals"])
        report["declined"].append({"old_id": amb["old_id"], "new_id": amb["new_id"],
                                   "reason": amb["reason"]})

    pdir = _version_link_pending(vault)
    ttl_days = _env_days(PROPOSAL_TTL_DAYS_ENV, DEFAULT_PROPOSAL_TTL_DAYS)
    for cand in res["candidates"]:
        meta: dict[str, Any] = {
            "kind": KIND_SUPERSEDE,
            "lane": LANE_TEXT,
            "category": vl.CATEGORY,
            "rules_version": vl.RULES_VERSION,
            "tier": _pair_tier(cand["old_classification"], cand["new_classification"]),
            "old_id": cand["old_id"], "new_id": cand["new_id"],
            "old_title": cand["old_title"], "new_title": cand["new_title"],
            "old_sha256": cand["old_sha256"], "new_sha256": cand["new_sha256"],
            "pair_key": cand["pair_key"],
            # Item 7: the signals are owner-facing output — scrubbed like every
            # other serialization surface (a subject can carry a secret).
            "signals": provenance.scrub(cand["signals"]),
            "created": _ts(now),
            "ttl_expires": _ts(now + _dt.timedelta(days=ttl_days)),
        }
        # B5, same policy as the ingestion lane: ONE host-verified conversation
        # is ONE evidence unit. A thread that carries five successive versions
        # is five owner questions but a single counted verdict — otherwise a
        # chatty counterparty alone could walk this class toward graduation.
        lineage = evidence_lineage_key(
            category=vl.CATEGORY, lane=LANE_TEXT, rules_version=vl.RULES_VERSION,
            conversation_id=meta["signals"].get("conversation"), verified=True)
        if lineage:
            meta["evidence_lineage"] = lineage
        meta["sha256"] = version_link_digest(meta)
        meta["id"] = "vlink-" + meta["sha256"][:12]
        pdir.mkdir(parents=True, exist_ok=True)
        _write_atomic(pdir / f"{meta['id']}.json",
                      json.dumps(meta, indent=2, sort_keys=True).encode("utf-8"))
        _record_version_link(vault, cand["pair_key"], "proposed", ts=_ts(now),
                             id=meta["id"], old_id=cand["old_id"],
                             new_id=cand["new_id"], signals=meta["signals"])
        report["proposed"].append(meta["id"])
        klass = str(meta["signals"].get("family_class") or "unknown")
        report["by_class"][klass] = report["by_class"].get(klass, 0) + 1

    # CUR-01: the coverage metric + the engagement line, EVERY run — including
    # the runs that proposed nothing, which is exactly when a silently-dead
    # fold looks identical to a healthy one.
    pending = version_link_metas(vault)
    report["coverage"] = {
        **vl.coverage(core.index.conn),
        # Kept SEPARATE from `linked` on purpose (see versionlink.coverage):
        # a note sitting in an unanswered proposal is not a covered note.
        "family_members_unresolved": len(
            {m[k] for m in pending for k in ("old_id", "new_id") if m.get(k)}),
        "proposals_awaiting_owner": len(pending),
    }
    _append_jsonl(_version_runs_path(vault), {
        "event": VERSION_LINK_RUN_EVENT, "ts": _ts(now),
        "proposed": len(report["proposed"]), "declined": len(report["declined"]),
        "by_class": report["by_class"],
        "pairs_examined": report["pairs_examined"],
        "truncated": report["truncated"], **report["coverage"]})
    return report


def _expire_version_links(vault, now: _dt.datetime) -> list[str]:
    """TTL-expire unanswered version-link proposals (caller holds the lock).

    NOT a verdict — no outcome is recorded, exactly like an expired ingestion
    proposal. The pair stays in the ledger as decided, so an ignored supersede
    question is asked once and then dropped rather than re-offered every night
    (and never permanently occupying one of the four supersede slots)."""
    expired: list[str] = []
    for m in version_link_metas(vault):
        exp = _parse_ts(m.get("ttl_expires", ""))
        if not (exp and exp <= now):
            continue
        dest = _version_link_expired(vault)
        dest.mkdir(parents=True, exist_ok=True)
        src = _version_link_pending(vault) / f"{m['id']}.json"
        if src.exists():
            shutil.move(str(src), dest / src.name)
        _record_version_link(vault, str(m.get("pair_key") or m["id"]), "expired",
                             ts=_ts(now), id=m["id"])
        expired.append(m["id"])
    return expired


def _apply_version_link(core, meta: dict[str, Any], *, accepted: bool,
                        expected_sha: str | None, answer_mode: str,
                        batch_size: int, batch_id: str, now: _dt.datetime,
                        report: dict[str, Any]) -> None:
    """Apply ONE owner verdict on a version-link proposal. Idempotent."""
    vault = core.vault
    nid = meta["id"]
    pair = str(meta.get("pair_key") or nid)
    stamp = _ts(now)
    pending = _version_link_pending(vault) / f"{nid}.json"

    if not accepted:
        _record_verdict(vault, meta, outcome="rejected", answer_mode=answer_mode,
                        batch_size=batch_size, ts=stamp)
        _record_version_link(vault, pair, "rejected", ts=stamp, id=nid)
        pending.unlink(missing_ok=True)
        report["rejected"].append(nid)
        return

    # Proposal-level CAS: the proposal the owner approved must be the proposal
    # the batch digest covered.
    if expected_sha is not None and version_link_digest(meta) != expected_sha:
        report["invalid"].append(
            {"batch_id": batch_id, "id": nid,
             "reason": "version-link proposal drifted since the batch digest "
                       "— not applied"})
        return

    # The owner DID decide; that judgement is evidence whatever happens next.
    _record_verdict(vault, meta, outcome="accepted", answer_mode=answer_mode,
                    batch_size=batch_size, ts=stamp)

    # STALENESS: the nightly folds keep running while a proposal waits, so
    # re-verify BOTH sides against the vault as it is NOW. `core.supersede`'s
    # `expect` re-checks the same facts inside its own lock — that is the
    # authoritative gate; this check exists so a pair that moved on declines
    # cleanly and legibly instead of surfacing as an exception.
    # The two content hashes are the whole precondition — frontmatter lives
    # inside the file, so any chain mutation moves them. `old_superseded_by`
    # rides along only because "it was chained while you were deciding" is the
    # case worth naming out loud. `is_latest_version` is deliberately NOT
    # asserted: a live head legitimately carries `true` after an earlier
    # supersession, so pinning it would decline honest pairs.
    expect = {"old_sha256": meta["old_sha256"], "new_sha256": meta["new_sha256"],
              "old_superseded_by": ""}
    stale = _version_link_stale(core, meta)
    if stale:
        _record_version_link(vault, pair, "stale", ts=stamp, id=nid, reason=stale)
        pending.unlink(missing_ok=True)
        report.setdefault("supersedes_declined", []).append(
            {"id": nid, "reason": stale})
        return
    try:
        core.supersede(meta["old_id"], meta["new_id"], expect=expect,
                       reason=f"owner-accepted version-link proposal {nid} "
                              f"(COS deduced from email context)")
    except Exception as exc:  # noqa: BLE001 — one bad pair never aborts a batch
        detail = f"{type(exc).__name__}: {exc}"
        _record_version_link(vault, pair, "failed", ts=stamp, id=nid, reason=detail)
        pending.unlink(missing_ok=True)
        report.setdefault("supersedes_failed", []).append(
            {"id": nid, "reason": detail})
        return
    _record_version_link(vault, pair, "applied", ts=stamp, id=nid)
    pending.unlink(missing_ok=True)
    report["accepted"].append(nid)
    report.setdefault("supersedes_applied", []).append(
        {"id": nid, "old_id": meta["old_id"], "new_id": meta["new_id"]})


def _version_link_stale(core, meta: dict[str, Any]) -> str | None:
    """Why this pair can no longer be applied, or ``None``."""
    for side, want in (("old", meta["old_sha256"]), ("new", meta["new_sha256"])):
        nid = meta[f"{side}_id"]
        row = core.index.get(nid)
        if not row:
            return f"{side} note {nid!r} is no longer in the vault"
        path = Path(row["path"])
        if not path.is_absolute():
            path = Path(core.vault) / path
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return f"{side} note {nid!r} is unreadable"
        # Retirement first: it is the specific thing an operator needs told,
        # and it also moves the content hash (frontmatter lives in the file).
        fm_meta, _ = frontmatter.parse_text(text)
        retired = (str(fm_meta.get("superseded_by") or "").strip()
                   or str(fm_meta.get("is_latest_version", "")
                          ).strip().lower() == "false")
        if retired:
            return f"{side} note {nid!r} has already been superseded"
        if want and sha256_text(text) != want:
            return f"{side} note {nid!r} changed since the proposal was made"
    return None


# -- batches --------------------------------------------------------------------
#: One open batch at a time (cos.py backpressure) now carries THREE candidate
#: kinds, so the owner question needs a size bound as well as a count bound.
#: Ingestion keeps a RESERVED floor of its own slots — a night full of
#: supersede proposals must never starve the ingestion queue — and takes any
#: supersede slots that go unused, so the total is never idle while work waits.
BATCH_CAP_TOTAL = 12
BATCH_SUBCAP_INGESTION = 8
BATCH_SUBCAP_SUPERSEDE = 4


def _batches_path(vault) -> Path:
    return proposals_dir(vault) / "batches.jsonl"


def _write_batches(vault, batches: list[dict[str, Any]]) -> None:
    """Atomic + durable: this file IS the signed state crash recovery reads.

    It used to be a plain truncate-and-rewrite, so a power loss mid-CAS could
    leave it empty or half-written — after which recovery cannot re-derive the
    decision it needs while side effects are already in flight. Same temp ->
    fsync -> replace -> fsync-parent discipline the approved queue uses."""
    p = _batches_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(p, "".join(json.dumps(b, sort_keys=True) + "\n"
                             for b in batches).encode("utf-8"))


def batch_digest(batch_id: str, created: str, candidates: list[dict[str, str]]) -> str:
    """Canonical digest over the candidate SET (order-independent)."""
    canon = json.dumps(
        {"batch_id": batch_id, "created": created, "schema": BATCH_SCHEMA,
         "candidates": sorted(candidates, key=lambda c: c["id"])},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def open_batches(vault) -> list[dict[str, Any]]:
    return [b for b in _read_jsonl(_batches_path(vault)) if b.get("state") == "open"]


def enqueue_batch(core, now: _dt.datetime | None = None) -> dict[str, Any]:
    """Aggregate ALL pending proposals into ONE signed cos_ingestion_batch and
    enqueue exactly one owner-inbox question for it.

    Backpressure (ing-02): refuses while another batch is open — the owner
    queue holds at most one broker slot; new proposals wait in ``pending`` and
    join the next batch. Fails CLOSED if no signing key resolves (nothing is
    enqueued unsigned). Under the writer lock (B4): it appends to the same
    ``batches.jsonl`` the consumer and the GC rewrite."""
    now = now or _utcnow()
    with vault_writer_lock(core.vault, verb="cos-enqueue"):
        return _enqueue_batch_locked(core, now)


def _enqueue_batch_locked(core, now: _dt.datetime) -> dict[str, Any]:
    from . import audit

    vault = core.vault
    if open_batches(vault):
        # Report WHAT is waiting, not just that something is. Backpressure is
        # correct (one broker slot), but silent backpressure is not: measured
        # 2026-07-27, two proposals claimed 27 minutes after a batch opened sat
        # invisible for two days — the owner inbox showed the open batch and
        # nothing hinted that answering it would release more. The waiting ids
        # let `maintain` surface a count so the queue is never a surprise.
        # A proposal stays in pending/ until its batch is CONSUMED, so exclude
        # the ones already queued in the open batch — only genuinely
        # held-back proposals count as waiting.
        queued = {c["id"] for b in open_batches(vault) for c in b.get("candidates", [])}
        waiting = [m["id"] for m in
                   _pending_metas(vault) + attachment_metas(vault, state="pending")
                   + version_link_metas(vault)
                   if m["id"] not in queued]
        return {"enqueued": False, "reason": "batch-already-open (backpressure)",
                "waiting": waiting}
    # DOC-01: quarantined attachments are candidates in the SAME batch as
    # email-text proposals — one owner question, both lanes. VER-02 adds the
    # third kind: deduced supersede proposals.
    ingestion = _pending_metas(vault) + attachment_metas(vault, state="pending")
    supersedes = version_link_metas(vault)
    # Sub-caps: ingestion's floor is reserved, supersede is hard-capped, and
    # ingestion (never supersede) fills whatever is left up to the total.
    metas = ingestion[:BATCH_SUBCAP_INGESTION]
    metas += supersedes[:BATCH_SUBCAP_SUPERSEDE]
    spare = BATCH_CAP_TOTAL - len(metas)
    if spare > 0:
        metas += ingestion[BATCH_SUBCAP_INGESTION:BATCH_SUBCAP_INGESTION + spare]
    # Exclude proposals already queued in a (still-open) batch — defensive; an
    # open batch already blocks above.
    if not metas:
        return {"enqueued": False, "reason": "no-pending-proposals"}
    queued_now = {m["id"] for m in metas}
    deferred = [m["id"] for m in ingestion + supersedes if m["id"] not in queued_now]
    # An ATTACHMENT candidate also binds its destination NAME into the signed
    # digest (round 3, CRITICAL): the signature covered the bytes, while the
    # accept took the name — and therefore the suffix, and therefore the ingest
    # handler — from the sidecar on the mount. The owner is shown that name in
    # the question below; this is what makes the name he saw the name that is
    # released. Note/version-link candidates have no destination and carry none.
    candidates, kept = [], []
    for m in metas:
        c = {"id": m["id"], "sha256": m["sha256"]}
        if m.get("lane") == LANE_ATTACHMENT:
            name = _safe_basename(str(m.get("filename") or Path(m["path"]).name))
            if not name:
                # No safe destination to bind, so it cannot be authorized —
                # and a candidate silently dropped from every future batch is
                # a black hole, so it is named.
                log_defect(vault, "attachment-unbatchable",
                           f"{m['id']}: sidecar filename "
                           f"{str(m.get('filename'))[:60]!r} is not a bare "
                           f"filename — left in quarantine, not batched",
                           ts=_ts(now))
                continue
            c["name"] = name
        candidates.append(c)
        kept.append(m)
    metas = kept
    if not candidates:
        return {"enqueued": False, "reason": "no-pending-proposals"}
    batch_id = "cosb-" + hashlib.sha256(
        (_ts(now) + json.dumps(candidates, sort_keys=True)).encode()).hexdigest()[:12]
    created = _ts(now)
    digest = batch_digest(batch_id, created, candidates)
    key_obj, _src = audit.resolve_signing_key()  # KeyUnavailable → fail closed
    sig = key_obj.sign(digest.encode("utf-8")).hex()
    ttl_days = _env_days(BATCH_TTL_DAYS_ENV, DEFAULT_BATCH_TTL_DAYS)
    record = {
        "schema": BATCH_SCHEMA, "batch_id": batch_id, "created": created,
        "candidates": candidates, "digest": digest, "sig": sig,
        "state": "open", "expires": _ts(now + _dt.timedelta(days=ttl_days)),
        # HARDENED:codex-7 — the CAS token every state transition checks.
        "generation": 0,
    }
    batches = _read_jsonl(_batches_path(vault))
    batches.append(record)
    _write_batches(vault, batches)

    ids = [c["id"] for c in candidates]
    lines, n_files = _candidate_descriptions(vault, metas)
    gc_days = _env_days(GC_DAYS_ENV, DEFAULT_GC_DAYS)
    n_links = sum(1 for m in metas if m.get("kind") == KIND_SUPERSEDE)
    n_notes = len(ids) - n_files - n_links
    what = ", ".join(p for p in (
        f"{n_notes} note(s)" if n_notes else "",
        f"{n_files} FILE(s)" if n_files else "",
        f"{n_links} VERSION LINK(s)" if n_links else "") if p)
    question = {
        "key": BROKER_KEY_PREFIX + batch_id,
        "question": (f"COS ingestion batch {batch_id}: {what} await approval "
                     f"before signing:\n" + "\n".join(f"  - {ln}" for ln in lines)),
        "options": [_ACCEPT_ALL, _REJECT_ALL,
                    "accept: <id,id,...> (partial — list the ids to accept)"],
        "default": _REJECT_ALL,
        "context": f"schema={BATCH_SCHEMA} digest={digest[:16]}… "
                   f"expires={record['expires']}. Only accepted candidates are "
                   f"ever signed; unanswered batches expire and requeue. "
                   + (f"A rejected FILE is NOT deleted — it moves to the "
                      f"host-private expired/ holding area and is recoverable "
                      f"for {gc_days} days." if n_files else "")
                   + ("A VERSION LINK only retires the older note in favour of "
                      "the newer one — both stay readable, and rejecting it "
                      "changes nothing." if n_links else ""),
    }
    # Item 7: EVERY serialization surface routes through the one scrub — the
    # question text is owner-facing output like any report or ledger line.
    core.enqueue_question(provenance.scrub(question),
                          source=f"cos-broker:{batch_id}", today=now.date())
    out = {"enqueued": True, "batch_id": batch_id, "candidates": ids,
           "digest": digest}
    if deferred:
        # Over the caps: named, not silently dropped. They join the next batch.
        out["deferred"] = deferred
    return out


#: versionlink's WORD-marker ranks, back in plain language for the owner.
_MARKER_RANK_LABEL = {0: "draft", 100: "final"}


def _signal_phrases(signals: dict[str, Any]) -> list[str]:
    """The evidence behind a version-link proposal, in plain language. The
    owner is being asked to retire a note; "confidence 0.94" is not a reason."""
    out: list[str] = []
    if signals.get("conversation"):
        out.append("same verified email thread")
    if signals.get("sender"):
        out.append(f"same verified sender ({signals['sender']})")
    if signals.get("name_family"):
        out.append(f"same document name ({signals['name_family']!r})")
    adv = signals.get("version_advance")
    if isinstance(adv, dict):
        # A WORD marker's rank is an internal ordering number: "version marker
        # 0 -> 100" is not a reason to retire a document. Render the words.
        markers = signals.get("version_markers")
        scale = ""
        if isinstance(markers, dict) and isinstance(markers.get("new"), (list, tuple)):
            scale = str(markers["new"][0])
        label = ((lambda r: _MARKER_RANK_LABEL.get(r, r)) if scale == "word"
                 else (lambda r: r))
        out.append(f"version marker {label(adv.get('old'))} -> "
                   f"{label(adv.get('new'))}")
    dup = signals.get("near_duplicate")
    if isinstance(dup, dict):
        out.append(f"content {float(dup.get('score', 0)) * 100:.1f}% similar")
    dates = signals.get("newer_date")
    if isinstance(dates, dict):
        out.append(f"dated {dates.get('old')} -> {dates.get('new')}")
    return out


def _human_bytes(n: int | None) -> str:
    if not isinstance(n, int) or n < 0:
        return "size unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0  # type: ignore[assignment]
    return f"{n} B"


def _candidate_descriptions(vault, metas: list[dict[str, Any]]
                            ) -> tuple[list[str], int]:
    """One PLAIN-LANGUAGE line per batch candidate, plus the file count (B7).

    The owner is being asked to make an irreversible-ish call with `reject
    all` as the stated default, so the question has to say WHAT each item is.
    An attachment is a FILE (name, size, sender) — never a "candidate note":
    the sweep MOVED it out of the download location, so a reject decides the
    fate of what may be the owner's only copy (AGENTS.md §9).
    """
    lines: list[str] = []
    n_files = 0
    for m in metas:
        if m.get("kind") == KIND_SUPERSEDE:
            why = ", ".join(_signal_phrases(m.get("signals") or {}))
            lines.append(
                f"{m['id']} — VERSION LINK: retire {m.get('old_title') or m['old_id']!r} "
                f"({m['old_id']}) in favour of {m.get('new_title') or m['new_id']!r} "
                f"({m['new_id']}) — {why or 'no signals recorded'}")
            continue
        if m.get("lane") != LANE_ATTACHMENT:
            lines.append(f"{m['id']} — note ({m.get('category') or 'unclassified'})")
            continue
        n_files += 1
        try:
            size = Path(m["path"]).stat().st_size
        except OSError:
            size = None
        sender = str((m.get("provenance") or {}).get("sender") or "unknown sender")
        lines.append(
            f"{m['id']} — FILE {m.get('filename') or Path(m['path']).name!r} "
            f"({_human_bytes(size)}, from {sender}, "
            f"category {m.get('category') or 'unclassified'})")
    return lines, n_files


def expire_batches(vault, now: _dt.datetime | None = None) -> list[str]:
    """Expire open batches past their TTL. Their candidates REQUEUE (stay in
    ``pending/``) and join the next batch; a late answer to an expired batch
    is rejected by ``consume_answers``."""
    now = now or _utcnow()
    with vault_writer_lock(vault, verb="cos-expire-batches"):
        batches = _read_jsonl(_batches_path(vault))
        expired: list[str] = []
        changed = False
        for b in batches:
            if b.get("state") != "open":
                continue
            exp = _parse_ts(b.get("expires", ""))
            if exp and exp <= now:
                b["state"] = "expired"
                b["expired_at"] = _ts(now)
                b["generation"] = int(b.get("generation", 0)) + 1
                expired.append(b["batch_id"])
                changed = True
        if changed:
            _write_batches(vault, batches)
        return expired


def close_expired_batch_questions(core, expired_batch_ids: list[str]) -> int:
    """Mark the owner-inbox questions of expired batches ``expired`` so the
    ~5-cap queue never accumulates stale broker slots (ing-02) and a LATE
    answer is refused at the inbox level too (``record_answer`` only touches
    ``open`` entries)."""
    if not expired_batch_ids:
        return 0
    keys = {BROKER_KEY_PREFIX + b for b in expired_batch_ids}
    entries = core._read_inbox()
    closed = 0
    for e in entries:
        if (isinstance(e, dict) and e.get("key") in keys
                and e.get("status", "open") == "open"):
            e["status"] = "expired"
            closed += 1
    if closed:
        core._write_inbox(entries)
    return closed


def parse_batch_answer(answer: str, batch_ids: list[str]) -> tuple[list[str] | None, str]:
    """Parse an owner answer against the batch's candidate ids.

    Returns ``(accepted_ids, outcome)``; ``accepted_ids is None`` means the
    answer was invalid (not consumable — candidates requeue)."""
    a = (answer or "").strip().lower()
    if a == _ACCEPT_ALL:
        return list(batch_ids), "accept-all"
    if a == _REJECT_ALL:
        return [], "reject-all"
    m = _ACCEPT_PARTIAL_RE.match(a)
    if m:
        ids = [s.strip() for s in m.group("ids").split(",") if s.strip()]
        unknown = [i for i in ids if i not in batch_ids]
        if unknown:
            return None, f"invalid-answer: not in batch: {', '.join(unknown)}"
        return ids, "accept-partial"
    return None, f"invalid-answer: unparseable {answer!r}"


# -- HARDENED:codex-7 — consume atomicity -------------------------------------
# consume/expire/requeue mutate the broker queue of record, so they run under
# the SAME bounded single-writer lock every index-mutating verb uses, and the
# per-batch decision is journalled BEFORE any file moves (the same pattern as
# `core.supersede`'s pending journal). A crash mid-apply leaves a journal the
# next call re-applies — every step is idempotent, so replay is safe — and each
# state transition CASes on (batch generation, state) re-read from disk, with
# the per-candidate sha check as the proposal-level CAS.
_CONSUME_JOURNAL = "consume-pending.json"


def _consume_journal_path(vault) -> Path:
    return proposals_dir(vault) / _CONSUME_JOURNAL


def _clear_consume_journal(vault) -> None:
    """Durably forget the journal: an unlink that never reached the disk brings
    a finished decision back on the next boot."""
    path = _consume_journal_path(vault)
    path.unlink(missing_ok=True)
    _fsync_dir(path.parent)


def _write_consume_journal(vault, record: dict[str, Any]) -> None:
    """Journal the decision DURABLY before any side effect (B4). fsync, not
    just write: a decision that only reached the page cache is a decision the
    recovery path cannot resume, and by then files have already moved."""
    path = _consume_journal_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, json.dumps(record, sort_keys=True).encode("utf-8"))


def _cas_batch(vault, batch_id: str, *, expect_state: str, expect_gen: int,
               updates: dict[str, Any]) -> bool:
    """Compare-and-set ONE batch record's state, re-read from disk."""
    batches = _read_jsonl(_batches_path(vault))
    for b in batches:
        if b.get("batch_id") != batch_id:
            continue
        if b.get("state") != expect_state or int(b.get("generation", 0)) != expect_gen:
            return False
        b.update(updates)
        b["generation"] = expect_gen + 1
        _write_batches(vault, batches)
        return True
    return False


def _answer_beat_the_deadline(entry: dict[str, Any], batch: dict[str, Any]) -> bool:
    """Timeliness is judged on the DURABLE answer timestamp, never on when the
    consumer happens to run: an owner who answered inside the window must not
    lose the answer because the next maintain fire landed after expiry."""
    answered = _parse_ts(str(entry.get("answered_at") or entry.get("answered") or ""))
    expires = _parse_ts(str(batch.get("expires", "")))
    return bool(answered and expires and answered <= expires)


def _bound_meta(vault, nid: str, *, body: str = "") -> dict[str, Any]:
    """The HOST-bound sidecar for one candidate (proposal or attachment).

    Falls back to the candidate's own frontmatter ONLY for a legacy sidecar
    written before LRN-01 — a fresh claim always binds these host-side."""
    for path in (proposals_dir(vault) / "pending" / f"{nid}.json",
                 _attachment_meta_path(vault, nid),
                 _version_link_pending(vault) / f"{nid}.json"):
        try:
            m = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and m.get("id"):
            return m
    meta, _ = frontmatter.parse_text(body) if body else ({}, "")
    return {"id": nid, "lane": LANE_TEXT, "category": CATEGORY_UNCLASSIFIED,
            "pattern": meta.get("pattern"),
            "bundle_version": meta.get("bundle_version"),
            "kind": meta.get("kind")}


def _record_verdict(vault, bound: dict[str, Any], *, outcome: str,
                    answer_mode: str, batch_size: int, ts: str) -> None:
    record_outcome(
        vault, pattern=bound.get("pattern"), ident=bound["id"], outcome=outcome,
        bundle_version=bound.get("bundle_version"), ts=ts,
        category=bound.get("category"), lane=bound.get("lane"),
        tier=bound.get("tier"), rules_version=bound.get("rules_version"),
        kind=bound.get("kind"), answer_mode=answer_mode, batch_size=batch_size,
        evidence_unit=bound.get("evidence_unit"),
        evidence_lineage=bound.get("evidence_lineage"))


def _apply_batch_decision(core, b: dict[str, Any], accepted_ids: list[str],
                          answer_mode: str, now: _dt.datetime,
                          report: dict[str, Any]) -> None:
    """Move ONLY accepted candidates onward; reject the rest. Idempotent — a
    journal replay re-runs this and every step no-ops on already-done work."""
    vault = core.vault
    pending = proposals_dir(vault) / "pending"
    rej_dir = proposals_dir(vault) / "rejected"
    batch_id = b.get("batch_id", "")
    # R4: every candidate id here becomes `pending/<id>.md`. The batch's
    # signature proves the host WROTE this row, not that the id is path-safe —
    # a batch signed by a pre-guard version replays through here after an
    # upgrade — so the ids take the same guard as every other untrusted id.
    batch_ids = []
    for c in b.get("candidates", []):
        cid = _safe_meta_id(c)
        if cid is None:
            report["invalid"].append(
                {"batch_id": batch_id, "id": str(c.get("id"))[:60],
                 "reason": "candidate id is not a bare slug — not applied"})
            continue
        batch_ids.append(cid)
    sha_by_id = {c["id"]: c["sha256"] for c in b.get("candidates", [])}
    # INT-04 round 3: the destination NAME the owner's signature covers. Absent
    # on a batch signed before this field existed — the accept then refuses
    # (`invalid`), the attachment stays in quarantine, and it rejoins the next
    # batch with a name that IS bound. Fail closed, exactly like a missing sha.
    name_by_id = {c["id"]: c.get("name") for c in b.get("candidates", [])}
    size = len(batch_ids)
    stamp = _ts(now)
    attachments = {m["id"]: m for m in attachment_metas(vault)}
    vlinks = {m["id"]: m for m in version_link_metas(vault)}

    for nid in batch_ids:
        vl = vlinks.get(nid)
        if vl is not None:
            _apply_version_link(core, vl, accepted=nid in accepted_ids,
                                expected_sha=sha_by_id.get(nid),
                                answer_mode=answer_mode, batch_size=size,
                                batch_id=batch_id, now=now, report=report)
            continue

        att = attachments.get(nid)
        if att is not None:
            if nid in accepted_ids:
                # INT-04: the CAS against the owner's SIGNED batch sha now
                # happens inside `_accept_attachment`, over the one buffer it
                # reads and then writes — checking here and moving there left
                # the two operating on different objects. It also signs the
                # anchor the ingest drain re-verifies, so every failure arm
                # (drift, no key, unreachable anchor store) must leave the
                # attachment in quarantine WITHOUT an accepted verdict.
                try:
                    dest = _accept_attachment(
                        vault, att, expected_sha=sha_by_id.get(nid),
                        expected_name=name_by_id.get(nid),
                        batch_id=batch_id, now=now)
                except (ApprovedKeyUnavailable, config.HostPathUnsafe) as exc:
                    # A HOST-WIDE outage (locked keychain, wrong scheduler user,
                    # missing `cryptography`, a misconfigured $BRAIN_INDEX_DIR),
                    # not a verdict about this file. Filed under `invalid` it
                    # read as "these attachments are invalid" and sent the
                    # operator to the attachments instead of the keychain — with
                    # no defect row, while the hold-release path for the SAME
                    # failure writes one. Same vocabulary on both paths now.
                    report.setdefault("systemic_error", []).append(
                        {"batch_id": batch_id, "id": nid,
                         "reason": f"attachment NOT released — host-wide "
                                   f"failure ({type(exc).__name__}: {exc})"})
                    log_defect(vault, "attachment-release-refused",
                               f"{nid}: {type(exc).__name__}: {exc}", ts=stamp)
                    continue
                except Exception as exc:  # noqa: BLE001 — fail closed, keep the file
                    report["invalid"].append(
                        {"batch_id": batch_id, "id": nid,
                         "reason": f"attachment NOT released "
                                   f"({type(exc).__name__}: {exc})"})
                    continue
                _record_verdict(vault, att, outcome="accepted",
                                answer_mode=answer_mode, batch_size=size, ts=stamp)
                report["accepted"].append(nid)
                report.setdefault("attachments_accepted", []).append(
                    {"id": nid, "dest": dest})
            else:
                _record_verdict(vault, att, outcome="rejected",
                                answer_mode=answer_mode, batch_size=size, ts=stamp)
                _discard_attachment(vault, att)
                report["rejected"].append(nid)
                report.setdefault("attachments_rejected", []).append(nid)
            continue

        src_md = pending / f"{nid}.md"
        src_meta = pending / f"{nid}.json"
        if nid in accepted_ids:
            # Journal replay after a COMPLETED stage: the payload is already in
            # the host-only approved queue under a valid anchor, and the verdict
            # was recorded before it moved. That IS the accepted outcome — do
            # not re-record it, and do not report the (correctly) missing
            # pending file as drift.
            if approved_staged(vault, nid):
                report["accepted"].append(nid)
                # The pending copy is redundant once the queue holds the signed
                # anchor; leaving it would re-batch the same candidate and ask
                # the owner a second time for a decision already taken.
                src_md.unlink(missing_ok=True)
                src_meta.unlink(missing_ok=True)
                continue
            ok = src_md.exists()
            body = ""
            if ok:
                body = src_md.read_text(encoding="utf-8")
                ok = sha256_text(body) == sha_by_id.get(nid)
            if not ok:
                report["invalid"].append(
                    {"batch_id": batch_id, "id": nid,
                     "reason": "pending file missing or content drifted "
                               "since batch digest — not promoted"})
                continue
            bound = _bound_meta(vault, nid, body=body)
            meta, _ = frontmatter.parse_text(body)
            _record_verdict(vault, bound, outcome="accepted",
                            answer_mode=answer_mode, batch_size=size, ts=stamp)
            sign_as_note = True
            if meta.get("kind") == "commitment":
                try:
                    sign_as_note = _spine_ingest_commitment(
                        vault, meta, source_ref=nid, now=now)
                except Exception as exc:  # noqa: BLE001 — never block acceptance
                    report.setdefault("spine_errors", []).append(
                        {"id": nid, "reason": f"{type(exc).__name__}: {exc}"})
            if sign_as_note:
                # INT-01: into the HOST-ONLY approved queue under a signed
                # anchor — NOT capture-inbox/, which the VM can overwrite
                # between this accept and the drain's signature.
                try:
                    stage_approved(vault, nid, body,
                                   sha256_hex=sha_by_id.get(nid, ""),
                                   batch_id=batch_id, now=now)
                except Exception as exc:  # noqa: BLE001 — fail closed, keep the file
                    report["invalid"].append(
                        {"batch_id": batch_id, "id": nid,
                         "reason": f"approved queue unavailable, NOT promoted "
                                   f"({type(exc).__name__}: {exc}) — the candidate "
                                   f"stays pending for the next batch"})
                    continue
                src_md.unlink(missing_ok=True)
                report["accepted"].append(nid)
            else:
                # SP-01 hybrid: a non-keeper commitment is recorded into the
                # spine ledger only — it never becomes a signed brain note.
                evdir = host_dir(vault) / "spine-evidence"
                evdir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src_md), evdir / f"{nid}.md")
                report.setdefault("accepted_spine_only", []).append(nid)
            src_meta.unlink(missing_ok=True)
        else:
            if src_md.exists():
                body = src_md.read_text(encoding="utf-8")
                bound = _bound_meta(vault, nid, body=body)
                _record_verdict(vault, bound, outcome="rejected",
                                answer_mode=answer_mode, batch_size=size, ts=stamp)
                rej_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src_md),
                            rej_dir / f"{now.strftime('%Y%m%dT%H%M%S')}-{nid}.md")
            src_meta.unlink(missing_ok=True)
            report["rejected"].append(nid)


_SIG_FAILED = "digest/signature verification failed"
_NO_ANSWER = "no answered owner-inbox entry for this batch"


def _verified_decision(vault, b: dict[str, Any], entry: dict[str, Any] | None,
                       ) -> tuple[list[str] | None, str, str]:
    """THE verification routine: signed batch + owner answer -> accepted set.

    ONE implementation, used by the normal consumer AND by crash recovery.
    They used to differ: recovery replayed the journal's own ``accepted`` list
    with no signature check and no owner answer at all, so a forged
    ``consume-pending.json`` plus a forged ``batches.jsonl`` row — both on the
    shared mount — got their contents staged and signed. Any second
    implementation of this is the bug, not the fix.

    Returns ``(accepted_ids | None, answer_mode, reason)``.
    """
    batch_id = str(b.get("batch_id", ""))
    if not isinstance(entry, dict) or entry.get("status") != "answered":
        return None, "", _NO_ANSWER
    # The answer must be the answer to THIS question. The key is DERIVED from
    # the batch id, never read off the batch row: `batch_digest` covers only
    # (batch_id, created, schema, candidates), so a row's `answer_key` is
    # unsigned — appending one to a legitimately signed open batch used to let
    # a forged journal borrow any other answered "accept all" entry.
    if str(entry.get("key", "")) != f"{BROKER_KEY_PREFIX}{batch_id}":
        return None, "", "answer does not belong to this batch"
    if b.get("state") == "expired" and not _answer_beat_the_deadline(entry, b):
        return None, "", "late answer (post-expiry)"
    digest = batch_digest(batch_id, b.get("created", ""), b.get("candidates", []))
    if digest != b.get("digest"):
        return None, "", _SIG_FAILED
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        from . import audit

        pub = load_pem_public_key(audit.public_key_pem())
        pub.verify(bytes.fromhex(str(b.get("sig", ""))), digest.encode("utf-8"))
    except Exception:  # noqa: BLE001 — unverifiable is unusable
        return None, "", _SIG_FAILED
    batch_ids = [c["id"] for c in b.get("candidates", [])]
    accepted_ids, outcome = parse_batch_answer(str(entry.get("answer", "")), batch_ids)
    if accepted_ids is None:
        return None, "", outcome          # includes subset validation failures
    # `accept all` over a large batch is approval fatigue, not agreement — the
    # mode travels with every verdict so the evidence gate can exclude it.
    answer_mode = {"accept-all": "accept-all", "reject-all": "reject-all"}.get(
        outcome, "itemized")
    return accepted_ids, answer_mode, outcome


def _recover_consume_journal(core, report: dict[str, Any], now: _dt.datetime,
                             answered: dict[str, Any]) -> None:
    """RESUME an interrupted apply — never discard it (B4).

    The journal is written and fsynced before the batch is CASed into the
    ``applying`` generation, and the ``applying`` generation is entered before
    any file moves. So a journal on disk means one of exactly two things, and
    both are finished here rather than abandoned:

    - batch still ``open``/``expired``  -> the crash beat the applying CAS;
      take it now, then apply.
    - batch already ``applying``        -> the crash landed mid-apply; re-apply
      (every step is idempotent) and close it out.

    A journal whose batch is already ``consumed`` is the third window — the
    crash fell between the closing CAS and the journal unlink — and needs only
    the unlink.

    NOTHING IN THE JOURNAL IS TRUSTED except which batch was in flight. The
    journal and ``batches.jsonl`` both live under ``host/proposals/`` — on the
    shared mount — so the decision is RE-DERIVED here through the same
    ``_verified_decision`` the normal path uses: valid host signature over the
    batch, a real answered owner-inbox entry, subset-validated. A journal that
    does not survive that is discarded with a defect, never applied.
    """
    vault = core.vault
    path = _consume_journal_path(vault)
    if not path.exists():
        return
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _clear_consume_journal(vault)
        return
    batch_id = journal.get("batch_id")
    b = next((x for x in _read_jsonl(_batches_path(vault))
              if x.get("batch_id") == batch_id), None)
    if b is not None and b.get("state") in ("applying", "open", "expired"):
        state, gen = str(b.get("state")), int(b.get("generation", 0))
        key = f"{BROKER_KEY_PREFIX}{batch_id}"      # DERIVED, never read off the row
        accepted_ids, answer_mode, reason = _verified_decision(
            vault, b, answered.get(key))
        if accepted_ids is None:
            report["invalid"].append({
                "batch_id": batch_id,
                "reason": f"journal recovery REFUSED (nothing applied): {reason}"})
            log_defect(vault, "consume-journal-refused",
                       f"{batch_id}: {reason}", ts=_ts(now))
            # ...and do not leave the batch stuck in `applying` forever: an
            # unverifiable decision closes the batch as invalid, so the queue
            # is not wedged behind it.
            if state == "applying":
                _cas_batch(vault, batch_id, expect_state="applying",
                           expect_gen=gen,
                           updates={"state": "invalid", "consumed_at": _ts(now)})
            _clear_consume_journal(vault)
            return
        entered = True
        if state != "applying":
            entered = _cas_batch(vault, batch_id, expect_state=state,
                                 expect_gen=gen,
                                 updates={"state": "applying",
                                          "applying_at": _ts(now)})
            gen += 1
        if entered:
            _apply_batch_decision(core, b, accepted_ids, answer_mode, now, report)
            _cas_batch(vault, batch_id, expect_state="applying", expect_gen=gen,
                       updates={"state": "consumed", "outcome": reason,
                                "consumed_at": _ts(now)})
            report.setdefault("journal_recovered", []).append(batch_id)
    _clear_consume_journal(vault)


def consume_answers(core, now: _dt.datetime | None = None) -> dict[str, Any]:
    """The ANSWER-CONSUMER: apply owner answers to broker questions ONLY.

    - Ignores every inbox entry outside the ``cosbroker:``/``coscorrect:``
      namespaces (an unrelated answered question is never consumed here).
    - Verifies the batch record's Ed25519 signature over its recomputed
      candidate-set digest before acting (a tampered batches.jsonl fails).
    - Enforces subset validation, one-shot consumption (a replayed answer to a
      consumed batch is rejected), and late-answer rejection — judged on the
      DURABLE answer timestamp, never on when this consumer runs.
    - Stages ONLY accepted candidates into the HOST-ONLY approved queue under
      an Ed25519-signed content anchor (INT-01; whence the ordinary audited
      host drain signs them); rejected candidates go to rejected/, and
      a rejected ATTACHMENT is discarded with zero residue (it never entered
      the vault).
    - Runs under the single-writer lock with a crash-recoverable journal.
    """
    now = now or _utcnow()
    with vault_writer_lock(core.vault, verb="cos-consume"):
        return _consume_answers_locked(core, now)


def _consume_answers_locked(core, now: _dt.datetime) -> dict[str, Any]:
    vault = core.vault
    report: dict[str, Any] = {
        "accepted": [], "rejected": [], "requeued": [],
        "replay_rejected": [], "late_rejected": [], "invalid": [],
        "corrections_applied": [], "corrections_discarded": [],
        "corrections_failed": [],
    }
    entries = core._read_inbox()
    answered = {e["key"]: e for e in entries
                if isinstance(e, dict) and e.get("status") == "answered"
                and isinstance(e.get("key"), str)}
    # Recovery re-derives its decision from these same answers (never from the
    # journal's own word), so it runs AFTER they are read.
    _recover_consume_journal(core, report, now, answered)

    by_id = {b.get("batch_id"): b for b in _read_jsonl(_batches_path(vault))}

    for key, entry in answered.items():
        if not key.startswith(BROKER_KEY_PREFIX):
            continue
        batch_id = key[len(BROKER_KEY_PREFIX):]
        b = by_id.get(batch_id)
        if b is None:
            report["invalid"].append({"batch_id": batch_id, "reason": "unknown-batch"})
            continue
        state = b.get("state")
        gen = int(b.get("generation", 0))
        if state == "consumed":
            report["replay_rejected"].append(batch_id)
            continue
        if state == "expired" and not _answer_beat_the_deadline(entry, b):
            report["late_rejected"].append(batch_id)
            continue
        if state not in ("open", "expired"):
            report["invalid"].append({"batch_id": batch_id, "reason": f"state={state}"})
            continue
        # Anti-tamper (signature over the stored candidate set) + answer parse +
        # subset validation, through the SAME routine crash recovery uses.
        accepted_ids, answer_mode, outcome = _verified_decision(vault, b, entry)
        if accepted_ids is None and outcome == _SIG_FAILED:
            _cas_batch(vault, batch_id, expect_state=state, expect_gen=gen,
                       updates={"state": "invalid", "consumed_at": _ts(now)})
            report["invalid"].append({"batch_id": batch_id, "reason": _SIG_FAILED})
            continue

        batch_ids = [c["id"] for c in b.get("candidates", [])]
        if accepted_ids is None:
            # Unconsumable answer: candidates stay pending (requeue into the
            # next batch); the batch closes so it can't be replayed forever.
            # NO outcome is recorded — an unparseable answer is not a verdict.
            if _cas_batch(vault, batch_id, expect_state=state, expect_gen=gen,
                          updates={"state": "consumed", "outcome": outcome,
                                   "consumed_at": _ts(now)}):
                report["invalid"].append({"batch_id": batch_id, "reason": outcome})
                report["requeued"].extend(batch_ids)
            else:
                report["invalid"].append({"batch_id": batch_id, "reason": "cas-failed"})
            continue

        # B4 — journal (fsynced) FIRST, then CAS into the `applying`
        # generation, and only THEN touch a file. Previously the accepted
        # files were moved and the rejected ones deleted BEFORE the CAS, so a
        # lost CAS left an irreversible half-applied batch with no rollback.
        # Now a lost CAS means nothing has happened yet, and a crash after it
        # leaves an `applying` batch the recovery path resumes.
        _write_consume_journal(vault, {
            "batch_id": batch_id, "state": state, "generation": gen,
            "accepted": accepted_ids, "answer_mode": answer_mode,
            "outcome": outcome, "ts": _ts(now),
        })
        if not _cas_batch(vault, batch_id, expect_state=state, expect_gen=gen,
                          updates={"state": "applying", "outcome": outcome,
                                   # informational only — NEVER read back to
                                   # choose an answer (it is outside the digest)
                                   "answer_key": key, "applying_at": _ts(now)}):
            report["invalid"].append({"batch_id": batch_id, "reason": "cas-failed"})
            _clear_consume_journal(vault)
            continue
        _apply_batch_decision(core, b, accepted_ids, answer_mode, now, report)
        if not _cas_batch(vault, batch_id, expect_state="applying",
                          expect_gen=gen + 1,
                          updates={"state": "consumed", "consumed_at": _ts(now)}):
            report["invalid"].append({"batch_id": batch_id, "reason": "cas-failed"})
        _clear_consume_journal(vault)

    # -- correction answers (coscorrect:<round>:<msg_key>) --------------------
    cor_pending = proposals_dir(vault) / "corrections-pending"
    if cor_pending.is_dir():
        for f in sorted(cor_pending.glob("correction-*.json")):
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                f.unlink(missing_ok=True)
                continue
            key = (f"{CORRECT_KEY_PREFIX}{payload.get('round')}:"
                   f"{payload.get('msg_key')}")
            entry = answered.get(key)
            if entry is None:
                continue  # still awaiting the owner
            if str(entry.get("answer", "")).strip().lower() == "apply":
                try:
                    record_correction(
                        vault, payload["round"], payload["msg_key"],
                        payload["corrected_bucket"], payload["corrected_tier"],
                        actor=f"owner-inbox:{key}", ts=_ts(now))
                    report["corrections_applied"].append(key)
                except ValueError as exc:
                    report["corrections_failed"].append(
                        {"key": key, "reason": str(exc)})
            else:
                report["corrections_discarded"].append(key)
            f.unlink(missing_ok=True)
    return report


def enqueue_correction_questions(core, now: _dt.datetime | None = None) -> list[str]:
    """One owner-inbox question per claimed-but-unasked correction drop."""
    now = now or _utcnow()
    vault = core.vault
    asked: list[str] = []
    cor_pending = proposals_dir(vault) / "corrections-pending"
    if not cor_pending.is_dir():
        return asked
    for f in sorted(cor_pending.glob("correction-*.json")):
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        key = f"{CORRECT_KEY_PREFIX}{payload.get('round')}:{payload.get('msg_key')}"
        q = {
            "key": key,
            "question": (f"Apply COS correction (round {payload.get('round')}, "
                         f"msg {payload.get('msg_key')}): bucket→"
                         f"{payload.get('corrected_bucket')}, tier→"
                         f"{payload.get('corrected_tier')}?"),
            "options": ["apply", "discard"],
            "default": "discard",
            "context": "a correction becomes a correction_events row only via "
                       "this human answer — the VM drop alone never writes it",
        }
        if core.enqueue_question(q, source="cos-broker:correction",
                                 today=now.date()):
            asked.append(key)
    return asked


# -- correction transport (host store of record) --------------------------------
def _corrections_conn(vault) -> sqlite3.Connection:
    p = corrections_db_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS correction_events ("
        " round INTEGER NOT NULL,"
        " msg_key TEXT NOT NULL,"
        " corrected_bucket TEXT NOT NULL,"
        " corrected_tier TEXT NOT NULL,"
        " ts TEXT NOT NULL,"
        " actor TEXT NOT NULL,"
        " PRIMARY KEY (round, msg_key))")
    config.secure_file_permissions(p)
    return conn


def known_ledger_keys(vault) -> set[tuple[int, str]] | None:
    """(round, msg_key) pairs from the VM's shadow-ledger drop, or ``None``
    when no ledger file exists at all (then every key is unknown)."""
    vdir = verdict_drop_dir(vault)
    files = sorted(vdir.glob("shadow-ledger*.jsonl")) if vdir.is_dir() else []
    if not files:
        return None
    keys: set[tuple[int, str]] = set()
    for f in files:
        for e in _read_jsonl(f):
            r, k = e.get("round"), e.get("msg_key")
            if isinstance(r, int) and isinstance(k, str):
                keys.add((r, k))
    return keys


def record_correction(vault, round_: int, msg_key: str, bucket: str, tier: str,
                      *, actor: str, ts: str | None = None) -> dict[str, Any]:
    """Append ONE correction event. Append-only (no update/delete path exists);
    rejects a duplicate (round, msg_key) and any key not present in the shadow
    ledger. ``actor`` records the HUMAN act this row is attributed to."""
    if not isinstance(round_, int):
        raise ValueError("round must be an integer")
    ledger = known_ledger_keys(vault)
    if ledger is None:
        raise ValueError("unknown key: no shadow ledger present in verdict-drop/ "
                         "— corrections must reference a ledgered (round, msg_key)")
    if (round_, msg_key) not in ledger:
        raise ValueError(f"unknown key: ({round_}, {msg_key!r}) is not in the shadow ledger")
    conn = _corrections_conn(vault)
    try:
        with conn:
            conn.execute(
                "INSERT INTO correction_events "
                "(round, msg_key, corrected_bucket, corrected_tier, ts, actor) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (round_, msg_key, bucket, tier, ts or _ts(), actor))
    except sqlite3.IntegrityError:
        raise ValueError(f"duplicate key: a correction for ({round_}, {msg_key!r}) "
                         "already exists (the store is append-only)") from None
    finally:
        conn.close()
    return {"round": round_, "msg_key": msg_key, "corrected_bucket": bucket,
            "corrected_tier": tier, "actor": actor}


def list_corrections(vault) -> list[dict[str, Any]]:
    if not corrections_db_path(vault).exists():
        return []
    conn = _corrections_conn(vault)
    try:
        rows = conn.execute(
            "SELECT round, msg_key, corrected_bucket, corrected_tier, ts, actor "
            "FROM correction_events ORDER BY ts").fetchall()
    finally:
        conn.close()
    cols = ("round", "msg_key", "corrected_bucket", "corrected_tier", "ts", "actor")
    return [dict(zip(cols, r)) for r in rows]


def shadow_ledger_entries(vault) -> list[dict[str, Any]]:
    """All verdict rows from the VM's shadow-ledger drop, deduped by
    (round, msg_key) — the last write wins (same-night re-run idempotency)."""
    vdir = verdict_drop_dir(vault)
    files = sorted(vdir.glob("shadow-ledger*.jsonl")) if vdir.is_dir() else []
    by_key: dict[tuple[int, str], dict[str, Any]] = {}
    for f in files:
        for e in _read_jsonl(f):
            r, k = e.get("round"), e.get("msg_key")
            if isinstance(r, int) and isinstance(k, str):
                by_key[(r, k)] = e
    return list(by_key.values())


# -- behavioural grading (2026-07-17, owner decision) --------------------------
# The corrections-based calibration required ~10 mornings of the owner grading
# email by hand; across 6 rounds / 308 verdicts he filed ZERO — the ritual will
# never happen, so auto-archive was permanently gated on evidence that could
# not accrue. Behavioural grading replaces stated preference with revealed
# preference: the VM already reads the mailbox nightly, so it OBSERVES what the
# owner actually did to previously-verdicted mail (read / replied / flagged /
# archived himself / untouched) and drops raw observations; THIS module grades
# them, in one testable place. Owner ruling: also mine his own archive actions
# as pattern evidence — "albeit not exclusively".

BEHAVIOUR_OBSERVATIONS = ("owner_replied", "owner_flagged", "owner_read",
                          "owner_archived", "untouched")

# Legacy VM-drop key scheme (pre-canonical-msg_key): `content-rejoin:<sender>|
# <subject>` rows join 0/10 to any verdict row's msg_key (field evidence,
# behaviour-r12.jsonl 2026-07-18) — grading them double-counts mail that is
# already graded under its canonical key, so they are excluded from BOTH the
# numerator and denominator of the consistency/contradiction rates.
LEGACY_REJOIN_PREFIX = "content-rejoin:"


def grade_behaviour(bucket: str, observed: str, *,
                    auto_archived: bool = False) -> str:
    """One verdict row + one observed behaviour -> a grade.

    The error that matters for auto-archive is exactly one: "would have
    archived mail the owner needed". So a NOISE verdict the owner then replied
    to or flagged is `contradicted` — the hard, gate-relevant failure. A noise
    row he merely opened is `read_anyway`: a weak signal (people open
    newsletters), reported but never gated on. Noise he left untouched or
    archived himself is `consistent`. An act/read row he archived without
    engaging is `overcalled` — over-caution, harmless, informational.
    """
    b, o = str(bucket).lower(), str(observed).lower()
    # Aged-read lane (owner policy 2026-07-17): priority-list mail may be
    # auto-archived when read + no-action + >7d old. Those rows are bucket
    # `read`, not `noise` — so the drift contradiction must key on the ACTION
    # (we auto-archived it), not the bucket: the owner replying to or flagging
    # ANY row we auto-archived is the gate error, whichever lane moved it.
    if auto_archived and o in ("owner_replied", "owner_flagged"):
        return "contradicted"
    if b == "noise":
        if o in ("owner_replied", "owner_flagged"):
            return "contradicted"
        if o == "owner_read":
            return "read_anyway"
        if o in ("untouched", "owner_archived"):
            return "consistent"
    elif b in ("act", "read") and o == "owner_archived":
        return "overcalled"
    return "neutral"


def behaviour_entries(vault) -> list[dict[str, Any]]:
    """Raw behaviour observations from the VM drop (``behaviour-*.jsonl``),
    deduped by (round, msg_key) — last write wins, same idempotency shape as
    the shadow ledger. Rows are VM-authored and untrusted: consumed as data."""
    vdir = verdict_drop_dir(vault)
    files = sorted(vdir.glob("behaviour-*.jsonl")) if vdir.is_dir() else []
    by_key: dict[tuple[int, str], dict[str, Any]] = {}
    for f in files:
        for e in _read_jsonl(f):
            r, k = e.get("round"), e.get("msg_key")
            if isinstance(r, int) and isinstance(k, str):
                by_key[(r, k)] = e
    return list(by_key.values())


def behaviour_report(vault) -> dict[str, Any]:
    """Aggregate observed-behaviour evidence: per-bucket grade counts, the
    noise-safety numbers an auto-archive re-arm decision needs, and the
    owner's own archive patterns (top senders he archives himself — evidence
    for FUTURE noise-signals, never an actuator by itself)."""
    entries = behaviour_entries(vault)
    # Exclusion (2026-07-18 field report): legacy `content-rejoin:` keys — and,
    # when a shadow ledger exists, any row whose msg_key joins no verdict —
    # never enter the rates. No ledger at all ⇒ only the legacy scheme is
    # excludable (can't prove a join miss against nothing).
    ledger = known_ledger_keys(vault)
    verdict_keys = {k for _, k in ledger} if ledger else None
    excluded = 0
    joined: list[dict[str, Any]] = []
    for e in entries:
        k = str(e.get("msg_key", ""))
        if k.startswith(LEGACY_REJOIN_PREFIX) or (
                verdict_keys is not None and k not in verdict_keys):
            excluded += 1
            continue
        joined.append(e)
    entries = joined
    per_bucket: dict[str, dict[str, int]] = {}
    contradicted_rows: list[dict[str, Any]] = []
    owner_archive_patterns: dict[str, int] = {}
    rounds: set[int] = set()
    for e in entries:
        b = str(e.get("bucket", "?")).lower()
        o = str(e.get("observed", "?")).lower()
        g = grade_behaviour(b, o, auto_archived=bool(e.get("auto_archived")))
        per_bucket.setdefault(b, {})[g] = per_bucket.setdefault(b, {}).get(g, 0) + 1
        rounds.add(int(e["round"]))
        if g == "contradicted":
            contradicted_rows.append(
                {k: e.get(k) for k in ("round", "msg_key", "sender", "subject",
                                        "observed")})
        if o == "owner_archived":
            key = str(e.get("sender") or e.get("sender_domain") or "unknown").lower()
            owner_archive_patterns[key] = owner_archive_patterns.get(key, 0) + 1
    noise = per_bucket.get("noise", {})
    noise_observed = sum(noise.values())
    contradicted = noise.get("contradicted", 0)
    return {
        "observations": len(entries),
        "excluded_unjoined": excluded,
        "rounds_observed": len(rounds),
        "per_bucket": per_bucket,
        "noise_observed": noise_observed,
        "noise_contradicted": contradicted,
        "noise_consistency": (round((noise_observed - contradicted) / noise_observed, 4)
                              if noise_observed else None),
        "contradicted_rows": contradicted_rows[:20],
        "owner_archive_patterns": dict(sorted(owner_archive_patterns.items(),
                                              key=lambda kv: -kv[1])[:20]),
    }


def calibration_report(vault) -> dict[str, Any]:
    """Shadow-mode trust-gate report: calibration = reduce(verdicts,
    correction_events). A verdict is bucket-correct when no correction exists
    for its (round, msg_key) OR the correction only changed the tier.
    Rounds completed = distinct rounds present in the shadow ledger."""
    verdicts = shadow_ledger_entries(vault)
    corr = {(c["round"], c["msg_key"]): c for c in list_corrections(vault)}
    rounds: dict[int, dict[str, int]] = {}
    buckets: dict[str, dict[str, Any]] = {}
    for v in verdicts:
        r = int(v["round"])
        key = (r, v["msg_key"])
        b = str(v.get("bucket", "?")).lower()
        rr = rounds.setdefault(r, {"total": 0, "corrected": 0})
        bb = buckets.setdefault(b, {"predicted": 0, "bucket_correct": 0})
        rr["total"] += 1
        bb["predicted"] += 1
        c = corr.get(key)
        if c is not None:
            rr["corrected"] += 1
        if c is None or str(c["corrected_bucket"]).lower() == b:
            bb["bucket_correct"] += 1
    for s in buckets.values():
        s["precision"] = (round(s["bucket_correct"] / s["predicted"], 4)
                          if s["predicted"] else None)
    total = len(verdicts)
    bucket_correct = sum(s["bucket_correct"] for s in buckets.values())
    return {
        "rounds_completed": len(rounds),
        "rounds": {str(k): v for k, v in sorted(rounds.items())},
        "verdicts": total,
        "corrections": len(corr),
        "overall_bucket_precision": (round(bucket_correct / total, 4)
                                     if total else None),
        "per_bucket": buckets,
        # revealed preference alongside stated preference: the corrections
        # count above stays authoritative where it exists, but 0 corrections
        # no longer means 0 evidence.
        "behaviour": behaviour_report(vault),
    }


# -- evidence signer -------------------------------------------------------------
def _canonical_manifest(manifest: dict[str, Any]) -> str:
    unsigned = {k: v for k, v in manifest.items() if k not in ("sig", "public_key_pem")}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"))


def source_ledger_hash(vault) -> str:
    """sha256 over the (sorted) shadow-ledger drop bytes, or ``"none"``."""
    vdir = verdict_drop_dir(vault)
    files = sorted(vdir.glob("shadow-ledger*.jsonl")) if vdir.is_dir() else []
    if not files:
        return "none"
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode("utf-8"))
        h.update(f.read_bytes())
    return h.hexdigest()


def sign_evidence(vault, *, bundle_version: str, model_version: str,
                  dataset_window: str, files: list[Path] | None = None,
                  snapshot_generation: Any = None, name: str = "evidence",
                  now: _dt.datetime | None = None) -> dict[str, Any]:
    """Write a trust-gate evidence bundle under ``host/evidence/`` with a
    SIGNED, versioned manifest binding bundle version, model version, snapshot
    generation, dataset window, and the source-ledger hash. HOST-only (the
    caller gates); fails closed without a signing key."""
    from . import audit
    from .snapshot import read_manifest

    now = now or _utcnow()
    if snapshot_generation is None:
        snap = read_manifest(config.snapshot_dir(vault))
        snapshot_generation = getattr(snap, "generation", None)
    dest = evidence_dir(vault) / f"{safe_slug(name)}-{now.strftime('%Y%m%dT%H%M%SZ')}"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dest, 0o700)  # nosemgrep: insecure-file-permissions -- intentionally OWNER-ONLY (evidence dir), not overly-permissive
    except OSError:
        pass
    file_hashes: dict[str, str] = {}
    for f in files or []:
        f = Path(f)
        data = f.read_bytes()
        shutil.copy2(f, dest / f.name)
        file_hashes[f.name] = hashlib.sha256(data).hexdigest()
    manifest: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "bundle_version": bundle_version,
        "model_version": model_version,
        "snapshot_generation": snapshot_generation,
        "dataset_window": dataset_window,
        "source_ledger_hash": source_ledger_hash(vault),
        "created": _ts(now),
        "files": file_hashes,
    }
    key_obj, source = audit.resolve_signing_key()  # KeyUnavailable → fail closed
    manifest["sig"] = key_obj.sign(_canonical_manifest(manifest).encode("utf-8")).hex()
    manifest["public_key_pem"] = audit.public_key_pem().decode("ascii")
    _write_atomic(dest / "manifest.json",
                  (json.dumps(manifest, indent=2, sort_keys=True) + "\n")
                  .encode("utf-8"))
    config.secure_file_permissions(dest / "manifest.json")
    return {"dir": str(dest), "manifest": str(dest / "manifest.json"),
            "signed_with": source, "snapshot_generation": snapshot_generation}


def verify_evidence(bundle_dir: Path | str) -> dict[str, Any]:
    """Verify an evidence bundle: manifest signature (against the HOST key —
    never the manifest's own embedded key) + every payload file hash. A
    stale/edited JSON or payload fails."""
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    from . import audit

    bundle_dir = Path(bundle_dir)
    errors: list[str] = []
    mpath = bundle_dir / "manifest.json"
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "errors": [f"manifest unreadable: {exc}"]}
    try:
        pub = load_pem_public_key(audit.public_key_pem())
        pub.verify(bytes.fromhex(manifest.get("sig", "")),
                   _canonical_manifest(manifest).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — any failure = invalid signature
        errors.append(f"signature verification failed: {type(exc).__name__}: {exc}")
    if manifest.get("schema") != EVIDENCE_SCHEMA:
        errors.append(f"unexpected schema: {manifest.get('schema')!r}")
    for fname, expected in (manifest.get("files") or {}).items():
        if not fname or Path(str(fname)).name != str(fname):
            errors.append(f"payload name is not a bare filename: {fname!r}")
            continue
        fpath = bundle_dir / fname
        if not fpath.exists():
            errors.append(f"payload missing: {fname}")
            continue
        actual = hashlib.sha256(fpath.read_bytes()).hexdigest()
        if actual != expected:
            errors.append(f"payload hash mismatch: {fname}")
    return {"ok": not errors, "errors": errors,
            "manifest": {k: manifest.get(k) for k in
                         ("schema", "bundle_version", "model_version",
                          "snapshot_generation", "dataset_window",
                          "source_ledger_hash", "created")}}


# -- priority-map generator --------------------------------------------------------
_OVERRIDE_LINE_RE = re.compile(
    r"^\s*[-*]\s*(?P<id>[a-z0-9][a-z0-9-]*)\s*:\s*(?P<prio>high|normal|low|exclude)\s*$",
    re.IGNORECASE)


def load_priority_overrides(vault) -> dict[str, str]:
    """Owner overrides from the validated overlay ``cos/`` category: body list
    lines of the form ``- <note-id>: high|normal|low|exclude``."""
    from . import overlay as ov

    overrides: dict[str, str] = {}
    cos_cat = ov.overlay_dir(vault) / "cos"
    if not cos_cat.is_dir():
        return overrides
    for f in sorted(cos_cat.glob("*.md")):
        try:
            _meta, body = frontmatter.parse_text(f.read_text(encoding="utf-8"))
        except OSError:
            continue
        for line in body.splitlines():
            m = _OVERRIDE_LINE_RE.match(line)
            if m:
                overrides[m.group("id").lower()] = m.group("prio").lower()
    return overrides


def generate_priority_map(core, *, max_tier: str | None = None,
                          now: _dt.datetime | None = None) -> dict[str, Any]:
    """Generate ``shared/priority-map.md`` from ``type: person``/``company``
    notes via a HOST-produced filtered projection. Default tier policy: the
    FULL vault (host egress default, owner ruling 2026-07-10) — deliberately
    NOT capped to Internal; pass ``max_tier`` to narrow. Owner overrides come
    from the overlay ``cos/`` category. The output lists ids/titles/metadata
    only — never note bodies."""
    from . import classification as cls
    from . import egress

    now = now or _utcnow()
    tier = max_tier or cls.TIERS[-1]
    people = core.bases_query({"type": "person"}, k=1000)
    companies = core.bases_query({"type": "company"}, k=1000)
    gated_people, prep = egress.apply_gate(people, tier)
    gated_companies, crep = egress.apply_gate(companies, tier)
    overrides = load_priority_overrides(core.vault)

    def _bucketed(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        buckets: dict[str, list[dict[str, Any]]] = {"high": [], "normal": [], "low": []}
        for it in sorted(items, key=lambda x: str(x.get("updated") or ""), reverse=True):
            prio = overrides.get(str(it.get("id", "")).lower(), "normal")
            if prio == "exclude":
                continue
            buckets[prio].append(it)
        return buckets

    sections = {"People": _bucketed(gated_people),
                "Companies": _bucketed(gated_companies)}
    lines = [
        "<!-- GENERATED by `brain cos-priority-map` — do not hand-edit. -->",
        f"<!-- generated: {_ts(now)} max_tier: {tier} "
        f"overrides: {len(overrides)} -->",
        "# COS priority map", "",
    ]
    for heading, buckets in sections.items():
        lines.append(f"## {heading}")
        for prio in ("high", "normal", "low"):
            for it in buckets[prio]:
                lines.append(
                    f"- [[{it.get('id')}]] — {it.get('title') or it.get('id')} "
                    f"(priority: {prio}, {it.get('classification') or 'UNLABELLED'}, "
                    f"updated {it.get('updated') or '?'})")
        lines.append("")
    out_path = priority_map_path(core.vault)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(out_path, ("\n".join(lines).rstrip() + "\n").encode("utf-8"),
                  mode=MODE_VM_READABLE)
    return {"path": str(out_path), "max_tier": tier,
            "people": len(gated_people), "companies": len(gated_companies),
            "withheld": prep["withheld"] + crep["withheld"],
            "overrides": overrides}


# -- auto-capture hold store ---------------------------------------------------------
# -- INT-01: the hold record is an AUTHORIZATION, so it is signed too ---------
# `host/hold/` is on the shared mount. Release used to read an unsigned marker
# and an unsigned payload from there, hash the payload AS IT WAS AT THAT
# INSTANT, and ask `stage_approved` to sign that hash — so a forged
# `<id>.hold.json` with a past `not_before` plus an `<id>.md` produced a
# host-manufactured approval anchor over bytes no owner ever authorized.
# Moving the waiting room off-mount does nothing when the thing that decides
# what ENTERS it is on-mount and unauthenticated.
#
# So the hold record is signed AT CREATION — the one moment the host itself
# decided this content may auto-capture — over (id, content sha, not_before,
# authorization, vault). Release verifies that signature, takes `not_before`
# from the SIGNED body (never the plain field beside it), and re-hashes the
# payload against the signed sha. Anything that does not verify is refused, not
# released.
HOLD_RECORD_SCHEMA = "cos_hold_record/v1"


def _hold_body(nid: str, sha: str, not_before: str, created: str,
               authorization: str, vault_identity: str) -> str:
    return json.dumps({"schema": HOLD_RECORD_SCHEMA, "id": nid, "sha256": sha,
                       "not_before": not_before, "created": created,
                       "authorization": authorization, "vault": vault_identity},
                      sort_keys=True, separators=(",", ":"))


def verified_hold(vault, marker: dict[str, Any], *, nid: str | None = None,
                  pubkey=None) -> dict[str, Any] | None:
    """The signed authorization inside a hold marker, or ``None``.

    ``None`` covers missing (a legacy unsigned hold), malformed, badly signed,
    wrong-schema and foreign-vault records alike: none of them is an
    authorization this host issued, and the answer at the release gate is the
    same for all of them — refuse."""
    if not isinstance(marker, dict) or not isinstance(marker.get("body"), str):
        return None
    if pubkey is None:
        pubkey = approved_verify_key(vault)      # may raise KeyUnavailable
    try:
        pubkey.verify(bytes.fromhex(str(marker.get("sig", ""))),
                      marker["body"].encode("utf-8"))
        body = json.loads(marker["body"])
    except Exception:  # noqa: BLE001 — unverifiable is unusable
        return None
    if (not isinstance(body, dict)
            or body.get("schema") != HOLD_RECORD_SCHEMA
            or body.get("id") != (nid if nid is not None else marker.get("id"))
            or not _identity_binds(vault, body)
            or not isinstance(body.get("sha256"), str)
            or not _parse_ts(str(body.get("not_before", "")))):
        return None
    return body


def hold_add(vault, content: str, *, not_before: str,
             ident: str | None = None,
             evidence: dict[str, Any] | None = None,
             authorization: str = "auto-capture") -> dict[str, Any]:
    """Park a qualifying auto-capture item UNSIGNED until ``not_before``.

    The item enters the approved queue (and thence the signed drain) ONLY
    after the stated interval expires — the undo window. Cancellation before expiry
    is atomic (see ``hold_cancel``). ``evidence`` carries the graduation key
    this item was auto-captured under, so an undo can demote its category.

    The PAYLOAD is unsigned (it is not a vault note yet), but the RECORD is
    signed here: it is the host's authorization, and release refuses without
    it. Fails closed if no key resolves — nothing is parked."""
    from . import audit
    from . import capture as cap_mod

    nb = _parse_ts(not_before)
    if nb is None:
        raise ValueError(f"not_before must be an ISO timestamp, got {not_before!r}")
    meta, _ = frontmatter.parse_text(content)
    nid = safe_slug(ident or meta.get("id") or ("hold-" + sha256_text(content)[:12]))
    staged = cap_mod.enforce(content, override={"id": nid})
    hdir = hold_dir(vault)
    hdir.mkdir(parents=True, exist_ok=True)
    md = hdir / f"{nid}.md"
    marker = hdir / f"{nid}.hold.json"
    if md.exists() or marker.exists():
        raise ValueError(f"hold already exists for id {nid!r}")
    created = _ts()
    body = _hold_body(nid, sha256_text(staged), _ts(nb), created, authorization,
                      approved_vault_identity(vault))
    key_obj, _src = audit.resolve_signing_key()   # KeyUnavailable -> nothing parked
    sig = key_obj.sign(body.encode("utf-8")).hex()
    _write_atomic(md, staged.encode("utf-8"))
    _write_atomic(marker, (json.dumps(
        {"id": nid, "not_before": _ts(nb), "created": created,
         "evidence": evidence or {}, "body": body, "sig": sig},
        sort_keys=True) + "\n").encode("utf-8"))
    return {"id": nid, "not_before": _ts(nb), "path": str(md), "signed": False,
            "authorized": True}


def hold_list(vault, now: _dt.datetime | None = None) -> list[dict[str, Any]]:
    now = now or _utcnow()
    out = []
    hdir = hold_dir(vault)
    if not hdir.is_dir():
        return out
    for marker in sorted(hdir.glob("*.hold.json")):
        try:
            m = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        nid = _safe_meta_id(m)
        if nid is None:
            continue          # an unusable id is an unusable hold, not a path
        nb = _parse_ts(m.get("not_before", ""))
        m = {**m, "id": nid, "due": bool(nb and nb <= now)}
        out.append(m)
    return out


# -- HARDENED:codex-9 — the UNDO state machine --------------------------------
# An auto-captured item walks exactly four states, and undo has a defined,
# audited outcome at every boundary:
#
#   held ──(not_before elapses)──▶ releasing ──▶ capture-pending ──▶ signed
#     │                               │                │                │
#   discard                       discard          delete the       audited
#   (nothing                     (nothing         UNSIGNED draft   retirement
#    ever left                    ever left       (never signed)   (supersede/
#    the host)                     the host)                        archive) —
#                                                                 NEVER a raw
#                                                                  deletion
#
# The undo TIMESTAMP is made durable BEFORE any state race, so a cancel
# recorded before the hold deadline always wins: `hold_release_due` re-checks
# the durable record after claiming the rename and refuses to release. Every
# branch demotes the item's category (an undo IS the negative signal the
# post-graduation loop otherwise never gets).
def _undos_path(vault) -> Path:
    return hold_dir(vault) / "undos.jsonl"


def _undone_before(vault, nid: str, deadline: _dt.datetime | None) -> bool:
    if deadline is None:
        return False
    for e in _read_jsonl(_undos_path(vault)):
        if e.get("id") != nid:
            continue
        ts = _parse_ts(str(e.get("ts", "")))
        if ts and ts <= deadline:
            return True
    return False


def _write_released_marker(vault, nid: str, evidence: dict[str, Any],
                           now: _dt.datetime) -> None:
    """Keep the graduation key ALIVE past release — an undo of an already
    released (or already signed) item must still know which category to
    demote, and the hold marker is gone by then."""
    nid = safe_slug(nid)              # never let an id become a path unchecked
    hdir = hold_dir(vault)
    hdir.mkdir(parents=True, exist_ok=True)
    _write_atomic(hdir / f"{nid}.released.json", (json.dumps(
        {"id": nid, "released": _ts(now), "evidence": evidence or {}},
        sort_keys=True) + "\n").encode("utf-8"))


def _hold_evidence(vault, nid: str) -> dict[str, Any]:
    hdir = hold_dir(vault)
    for name in (f"{nid}.hold.json", f"{nid}.releasing.json",
                 f"{nid}.cancelled.json", f"{nid}.released.json"):
        p = hdir / name
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and isinstance(m.get("evidence"), dict):
            return m["evidence"]
    att = attachment_metas(vault)
    for m in att:
        if m.get("id") == nid:
            return m
    return _attachment_lifecycle(vault, nid)     # B3: survives the release


def _signed_target_id(vault, nid: str) -> str:
    """The note id an undo of ``nid`` must actually retire.

    For an email-text candidate that is the id itself. For an ATTACHMENT it is
    the ``raw/`` id the ingest drain minted from the released bytes (B3)."""
    life = _attachment_lifecycle(vault, nid)
    if life:
        return _ingested_raw_id(vault, str(life.get("sha256") or "")) or nid
    return nid


def undo_state(vault, nid: str, *, core: Any = None) -> str:
    """Where in the state machine ``nid`` currently sits."""
    hdir = hold_dir(vault)
    if (hdir / f"{nid}.hold.json").exists():
        return "held"
    if (hdir / f"{nid}.releasing.json").exists():
        return "releasing"
    for m in attachment_metas(vault):
        if m.get("id") == nid:
            return "held" if m.get("state") == "held" else "quarantined"
    # INT-01: broker-accepted/released items now wait in the host-only approved
    # queue; a VM draft of the same id may ALSO sit in capture-inbox. Either
    # way the item is unsigned and awaiting the drain.
    if (approved_queued(vault, nid)
            or (config.capture_inbox_dir(vault) / f"{nid}.md").exists()):
        return "capture-pending"
    # B3: a RELEASED attachment no longer answers to its own name anywhere —
    # not in capture-inbox/ (it goes to vault/inbox/ as a plain file) and not
    # in the index (ingestion renames it). Follow its lifecycle record.
    life = _attachment_lifecycle(vault, nid)
    if life:
        # R3 recovery: a `releasing` record means the payload is at `dest` OR
        # still at `src` (a crash can land either side of the move). Reconcile
        # from whichever location actually holds it — the identity, and so the
        # ability to withdraw, survives the window either way.
        if _lifecycle_payload(life, vault) is not None:
            return "inbox-pending"      # unsigned, still awaiting the drain
        if _ingested_raw_id(vault, str(life.get("sha256") or "")):
            return "signed"
    if core is not None:
        try:
            if core.index.get(nid):
                return "signed"
        except Exception:  # noqa: BLE001 — an unreadable index is not a state
            pass
    return "absent"


def hold_undo(vault, ident: str, *, core: Any = None,
              now: _dt.datetime | None = None) -> dict[str, Any]:
    """Undo ONE auto-captured item, whatever state it has reached.

    Under the writer lock (B4): an undo races the release path over the same
    hold markers, quarantine sidecars and capture drafts, and its signed
    branch writes a note."""
    with vault_writer_lock(vault, verb="cos-undo"):
        return _hold_undo_locked(vault, ident, core=core, now=now or _utcnow())


def _hold_undo_locked(vault, ident: str, *, core: Any,
                      now: _dt.datetime) -> dict[str, Any]:
    nid = safe_slug(ident)
    hdir = hold_dir(vault)
    hdir.mkdir(parents=True, exist_ok=True)
    # DURABLE FIRST — before touching any state, so the timestamp exists even
    # if this process dies mid-undo and so the release path can see it.
    _append_jsonl(_undos_path(vault), {"id": nid, "ts": _ts(now)})
    evidence = _hold_evidence(vault, nid)
    state = undo_state(vault, nid, core=core)
    result: dict[str, Any] = {"id": nid, "state_before": state, "undone": False,
                              "action": "none"}

    if state in ("held", "quarantined"):
        att = next((m for m in attachment_metas(vault) if m.get("id") == nid), None)
        if att is not None:
            _discard_attachment(vault, att)
            result.update(undone=True, action="discarded")
        else:
            claimed = hdir / f"{nid}.cancelled.json"
            try:
                os.rename(hdir / f"{nid}.hold.json", claimed)
            except FileNotFoundError:
                claimed = None  # a concurrent release claimed it; fall through
            (hdir / f"{nid}.md").unlink(missing_ok=True)
            if claimed is not None:
                claimed.unlink(missing_ok=True)
            result.update(undone=True, action="cancelled")
    elif state in ("releasing", "capture-pending"):
        # An UNSIGNED capture draft has joined no audit chain and no index —
        # deleting it is the correct, complete undo. Both waiting rooms: the
        # host-only approved queue and the VM-writable capture-inbox. An undo
        # that could not actually remove it must SAY SO: reporting
        # "draft-deleted" over a failed unlink leaves the next drain free to
        # sign the thing the owner just revoked.
        if state == "releasing":
            (hdir / f"{nid}.md").unlink(missing_ok=True)
        inbox_draft = config.capture_inbox_dir(vault) / f"{nid}.md"
        try:
            inbox_draft.unlink(missing_ok=True)
        except OSError:
            pass
        cleared = clear_approved(vault, nid) and not inbox_draft.exists()
        if cleared:
            result.update(undone=True, action="draft-deleted")
        else:
            still = [str(p) for p in
                     (approved_payload_path_or_none(vault, nid),
                      approved_anchor_path_or_none(vault, nid), inbox_draft)
                     if p is not None and p.exists()]
            log_defect(vault, "undo-incomplete",
                       f"{nid}: undo could NOT remove {', '.join(still)} — the "
                       f"item is still queued and would be signed", ts=_ts(now))
            result.update(undone=False, action="undo-failed", blocked_by=still)
    elif state == "inbox-pending":
        # B3: a released ATTACHMENT sitting in vault/inbox/ awaiting the ingest
        # drain. Nothing signed it yet, so removing it is complete — but it is
        # the owner's file, so it goes to the recoverable expired/ area (B7),
        # never straight to unlink.
        life = _attachment_lifecycle(vault, nid)
        payload = _lifecycle_payload(life, vault)   # guarded, or None
        if payload is None:
            result.update(undone=False, action="withdraw-refused",
                          reason="lifecycle record names no payload inside this vault")
        else:
            _discard_attachment(vault, {"id": nid, "path": str(payload)})
            # INT-04: the acceptance is revoked, so its anchor goes with it —
            # otherwise a later, unrelated drop under the same inbox name is
            # refused against bytes nobody is waiting for any more.
            clear_attachment_anchor(vault, payload)
            result.update(undone=True, action="inbox-file-withdrawn")
    elif state == "signed":
        result.update(**_retire_signed_note(
            core, nid, now=now, target_id=_signed_target_id(vault, nid)))

    if result["undone"]:
        cat = evidence.get("category") or CATEGORY_UNCLASSIFIED
        result["demoted"] = demote_category(
            vault, cat, reason=f"owner undo of auto-committed {nid} ({state})",
            ts=_ts(now))
        record_outcome(vault, pattern=evidence.get("pattern"), ident=f"{nid}:undo",
                       outcome="undo", bundle_version=evidence.get("bundle_version"),
                       ts=_ts(now), category=cat, lane=evidence.get("lane"),
                       tier=evidence.get("tier"),
                       rules_version=evidence.get("rules_version"))
    return result


def _retire_signed_note(core: Any, nid: str, *, now: _dt.datetime,
                        target_id: str | None = None) -> dict[str, Any]:
    """Undo AFTER signing: an audited retirement, never a raw deletion.

    B6 — this used to stamp ``is_latest_version: false`` + ``superseded_date``
    with NO successor. That shape is FORBIDDEN by the substrate: AGENTS.md §2
    says `false` implies a successor exists, and `tools/validate.py` errors on
    both keys without `superseded_by` — so one owner undo left the vault
    permanently failing the documented pre-commit conventions gate, far from
    the cause. There IS no successor here (an undo retracts a claim, it does
    not replace it), so `core.supersede` is not the right path either: it
    requires a real successor note on the other side of the chain.

    The conforming shape is a plain retirement marker the validator
    recognises — ``retired``/``retired_date``/``retired_reason``, no
    supersession keys — written through the ordinary signed write path, so the
    note stays in the hash-chained brain exactly as before.
    """
    if core is None:
        return {"undone": False, "action": "needs-core",
                "detail": "already signed: retiring it is an AUDITED write — "
                          "call with a host BrainCore"}
    target = target_id or nid
    row = core.index.get(target)
    if not row:
        return {"undone": False, "action": "absent"}
    path = Path(row["path"])
    before = path.read_text(encoding="utf-8")
    after = frontmatter.set_keys(before, {
        "retired": True,
        "retired_date": now.date().isoformat(),
        "retired_reason": f"cos auto-capture undo ({_ts(now)})",
    })
    rel = path.relative_to(core.vault).as_posix()
    core.write_note(rel, after, reason=f"cos-undo audited retirement: {target}")
    return {"undone": True, "action": "retired", "retired_id": target}


def hold_cancel(vault, ident: str) -> bool:
    """Cancel a held item (the one-word revert). Thin wrapper over the full
    undo state machine — kept returning a bool for the CLI's exit code."""
    return bool(hold_undo(vault, ident)["undone"])


def _still_eligible_at_release(vault, evidence: dict[str, Any], *,
                               now: _dt.datetime,
                               taxonomy: dict[str, Any]) -> tuple[bool, str]:
    """Does this hold's category STILL qualify for the auto lane, right now?

    B2 — release used to check only ``not_before`` and the undo log, so a hold
    placed while a category was graduated proceeded to signing even after the
    category had since been demoted: by a claim-time security defect, an owner
    undo, a `never` flip, removal from the overlay, or a rolling accept-rate
    drop. The whole point of demotion is that it applies NOW, and a hold is
    precisely the population that has not yet committed. Re-run the same
    both-keys policy against the item's stored HOST-BOUND evidence and the
    CURRENT taxonomy/statistics; exploration sampling is skipped (this item
    already won its lane, we are only re-testing whether the lane is open).
    """
    if not evidence.get("category"):
        # Not a graduation-placed hold at all: `auto_capture_fold` can only
        # hold a candidate whose category is OUT of `_UNPATTERNED`, so a hold
        # with no bound category came from the host-broker's own
        # `brain cos-hold add`. There is no graduation to revoke.
        return True, "operator-placed hold (no auto-lane graduation to revoke)"
    decision = route_decision(vault, evidence, now=now, taxonomy=taxonomy)
    if decision["decision"] == "auto" or decision.get("exploration"):
        return True, "eligible"
    return False, str(decision.get("reason") or "no longer eligible")


def _return_hold_to_owner(vault, nid: str, evidence: dict[str, Any],
                          md: Path | None, *, reason: str,
                          now: _dt.datetime) -> None:
    """Put a no-longer-eligible hold back in front of the OWNER (B2).

    Never released, never silently dropped: the candidate rejoins the ordinary
    pending queue and the next ``enqueue_batch`` puts it in the owner's
    question, where a demoted category belongs."""
    pending = proposals_dir(vault) / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    meta = dict(evidence or {})
    meta.update({
        "id": nid, "state": "pending",
        "returned_from_hold": _ts(now), "returned_reason": reason,
        "ttl_expires": _ts(now + _dt.timedelta(
            days=_env_days(PROPOSAL_TTL_DAYS_ENV, DEFAULT_PROPOSAL_TTL_DAYS))),
    })
    if md is not None and md.exists():
        text = md.read_text(encoding="utf-8")
        # Re-hash: the batch digest and the accept-time CAS both check the
        # sha against the file that will actually be promoted.
        meta["sha256"] = sha256_text(text)
        shutil.move(str(md), pending / f"{nid}.md")
    _write_atomic(pending / f"{nid}.json",
                  (json.dumps(meta, sort_keys=True) + "\n").encode("utf-8"))


def _quarantine_hold(vault, marker: Path, payload: Path | None, reason: str,
                     now: _dt.datetime, *, kind: str = "hold-record-unverified",
                     name: str | None = None) -> None:
    """Park an unreleasable hold OUT of the release path, marker AND payload.

    Renaming only the marker orphaned ``<id>.md`` in ``hold/``: invisible to
    ``hold_list``, outside every GC window, and impossible to find later. The
    destination is derived from a real directory entry, so no claimed id can
    steer it, and a failed rename is logged rather than raised — one poisoned
    marker must not wedge every future release."""
    stem = (name or marker.name).split(".")[0] or "hold"
    if payload is None:
        # An unsafe/unknown claimed id still has a payload — the sibling named
        # after the real directory entry. Quarantining the marker alone leaves
        # it orphaned in hold/: invisible to hold_list, outside every GC window.
        payload = marker.parent / f"{stem}.md"
    for src, suffix in ((marker, ".refused.json"),
                        (payload, ".refused.md")):
        if src is None or not (src.exists() or src.is_symlink()):
            continue
        try:
            os.replace(src, marker.parent / f"{stem}{suffix}")
        except OSError as exc:
            log_defect(vault, kind,
                       f"{stem}: could not quarantine {src.name} ({exc})",
                       ts=_ts(now))
    log_defect(vault, kind,
               f"{stem}: {reason} — NOT released (quarantined in hold/)",
               ts=_ts(now))


def hold_release_due(vault, now: _dt.datetime | None = None) -> list[str]:
    """Move every DUE held item into the approved queue (the signed drain path).

    Claim-by-rename per item (same atomic marker claim as ``hold_cancel``), so
    a concurrent cancel and release can never both act on one item — and a
    DURABLE undo recorded before the deadline wins even when the release won
    the rename. Runs under the writer lock (B4) so the eligibility re-check
    below cannot race a concurrent demotion or consume."""
    with vault_writer_lock(vault, verb="cos-hold-release"):
        return _hold_release_due_locked(vault, now or _utcnow())


def _hold_release_due_locked(vault, now: _dt.datetime) -> list[str]:
    released: list[str] = []
    returned: list[str] = []
    hdir = hold_dir(vault)
    taxonomy = ingest_taxonomy(vault)
    # Due attachments: the auto lane for the attachment lane parks the file in
    # its host-private quarantine with a not_before, then releases it into
    # vault/inbox/ for the signed ingest drain.
    for m in attachment_metas(vault, state="held"):
        # INT-04 (round 2): `not_before` AND the content hash come from the
        # SIGNED hold authorization, never from the plain fields beside them on
        # the mount — otherwise the VM shortens its own undo window and restates
        # the hash to match whatever it substituted. No authorization, no
        # release; the item stays held and the defect names why.
        try:
            authz = attachment_hold_authz(vault, m["id"])
        except ApprovedKeyUnavailable as exc:
            log_defect(vault, "attachment-release-refused",
                       f"{m['id']}: {type(exc).__name__}: {exc}", ts=_ts(now))
            continue
        if authz is None:
            log_defect(vault, "attachment-release-unauthorized",
                       f"{m['id']}: no verified host hold authorization "
                       f"(missing, tampered, or placed by a pre-INT-04 engine)"
                       f" — NOT released", ts=_ts(now))
            continue
        nb = _parse_ts(str(authz["not_before"]))
        if nb is None or nb > now:
            continue
        if _undone_before(vault, m["id"], nb):
            _discard_attachment(vault, m)
            continue
        # The CATEGORY the demotion re-check runs against is the SIGNED one:
        # deleting it from the sidecar made an auto-lane hold look
        # operator-placed, which is the one shape `_still_eligible_at_release`
        # waves through without consulting the current taxonomy.
        ok, why = _still_eligible_at_release(
            vault, {**m, "category": authz.get("category") or ""},
            now=now, taxonomy=taxonomy)
        if not ok:
            back = {k: v for k, v in m.items() if k != "not_before"}
            back["state"] = "pending"
            back["returned_from_hold"] = _ts(now)
            back["returned_reason"] = why
            _write_attachment_meta(vault, back)
            clear_attachment_hold_authz(vault, m["id"])
            returned.append(m["id"])
            continue
        _write_released_marker(vault, m["id"], m, now)
        try:
            _accept_attachment(vault, m, expected_sha=str(authz["sha256"]),
                               expected_name=str(authz.get("filename") or ""),
                               now=now)
        except Exception as exc:  # noqa: BLE001 — one bad item never wedges the loop
            # INT-04: no anchor, no release. The item stays in quarantine and
            # the next hourly run retries it — a released-but-unanchored
            # attachment is exactly what this lane must not produce.
            log_defect(vault, "attachment-release-refused",
                       f"{m['id']}: {type(exc).__name__}: {exc}", ts=_ts(now))
            continue
        released.append(m["id"])
    if not hdir.is_dir():
        return released
    try:
        pubkey = approved_verify_key(vault)
    except ApprovedKeyUnavailable as exc:
        # No key => nothing could be signed anyway. Leave every hold parked
        # rather than claim markers this run cannot honour.
        log_defect(vault, "hold-release-skipped",
                   f"no host key, holds left parked ({exc})", ts=_ts(now))
        return released
    for marker in sorted(hdir.glob("*.hold.json")):
        # ONE poisoned file must never wedge every future release. Everything
        # from here to the end of the iteration is attacker-influenced, so a
        # malformed record is quarantined and the loop CONTINUES — it used to
        # raise (non-dict JSON -> AttributeError from `.get`, a Unicode
        # filename with no id -> ValueError from `safe_slug`) straight out of
        # the function, permanently blocking every legitimate due hold.
        try:
            m = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # C-1 trust boundary: this id BECOMES A PATH, and the marker is
        # attacker-writable. Quarantine under the DIRECTORY ENTRY's own name
        # (one path component by construction) rather than the claimed id.
        nid = _safe_meta_id(m)
        if nid is None:
            _quarantine_hold(vault, marker, None,
                             "hold record is malformed or carries an unsafe id "
                             "(not a bare slug)", now, name=marker.name)
            continue
        try:
            released_here = _release_one_hold(
                vault, hdir, marker, m, nid, pubkey=pubkey, now=now,
                taxonomy=taxonomy, returned=returned)
        except Exception as exc:  # noqa: BLE001 — never wedge the whole run
            _quarantine_hold(vault, marker, None,
                             f"unhandled error while releasing "
                             f"({type(exc).__name__}: {exc})", now,
                             name=marker.name)
            continue
        if released_here:
            released.append(nid)
    if returned:
        log_defect(vault, "hold-returned-to-owner",
                   f"{len(returned)} due hold(s) no longer eligible at release: "
                   f"{', '.join(returned)}", ts=_ts(now))
    return released


def _release_one_hold(vault, hdir: Path, marker: Path, m: dict[str, Any],
                      nid: str, *, pubkey, now: _dt.datetime, taxonomy,
                      returned: list[str]) -> bool:
    """Release exactly ONE due hold. Returns True if it reached the queue.

    Split out of the loop so a failure on one marker is contained by the
    caller's ``except`` instead of aborting every remaining hold."""
    # The AUTHORIZATION decides, and only the signed body is read for it —
    # `not_before` beside the signature is decoration an attacker can edit.
    authz = verified_hold(vault, m, nid=nid, pubkey=pubkey)
    if authz is None:
        # Two causes, one safe answer, but NOT one wording. An unsigned marker
        # is what every hold parked by a pre-INT-01 engine looks like — an
        # upgrade, not an attack — and reading it out as tampering would be both
        # wrong and alarming. Either way the item is quarantined rather than
        # released or auto-promoted: routing it back into the owner queue here
        # would bypass claim validation (secret scrub, tier, claims ledger, run
        # attribution) that every other candidate passes. Recovery is an
        # operator action: re-propose the quarantined payload.
        legacy = not isinstance(m.get("body"), str)
        _quarantine_hold(
            vault, marker, hdir / f"{nid}.md",
            ("parked by a pre-INT-01 engine, so it carries no host "
             "authorization to verify (UPGRADE, not tampering); re-propose the "
             "quarantined payload if it is still wanted"
             if legacy else "no valid host authorization for this hold"),
            now,
            kind="hold-record-legacy-unsigned" if legacy
            else "hold-record-unverified")
        return False
    nb = _parse_ts(str(authz.get("not_before", "")))
    if nb is None or nb > now:
        return False
    claimed = hdir / f"{nid}.releasing.json"
    try:
        os.rename(marker, claimed)
    except OSError:
        return False              # a concurrent cancel/release won the claim
    md = hdir / f"{nid}.md"
    if _undone_before(vault, nid, nb):
        # The owner's undo is DURABLE and predates the deadline: it wins the
        # race by design, even though this release claimed the rename.
        md.unlink(missing_ok=True)
        claimed.unlink(missing_ok=True)
        return False
    ok, why = _still_eligible_at_release(
        vault, m.get("evidence") or {}, now=now, taxonomy=taxonomy)
    if not ok:
        _return_hold_to_owner(vault, nid, m.get("evidence") or {}, md,
                              reason=why, now=now)
        claimed.unlink(missing_ok=True)
        returned.append(nid)
        return False
    out = False
    if md.exists():
        # INT-01: a released hold takes the same anchored route as an
        # owner-accepted candidate — and the anchor binds the bytes the host
        # AUTHORIZED at hold time, not whatever is on the mount now.
        text = md.read_text(encoding="utf-8")
        if sha256_text(text) != authz["sha256"]:
            _quarantine_hold(vault, claimed, md,
                             "held bytes changed since the host authorized them",
                             now, kind="hold-payload-drift")
            return False
        try:
            stage_approved(vault, nid, text, sha256_hex=authz["sha256"],
                           batch_id=f"hold:{nid}", kind="hold", now=now)
        except Exception as exc:  # noqa: BLE001 — retry next run, never sign
            os.replace(claimed, marker)       # un-claim so the next run retries
            log_defect(vault, "hold-release-failed",
                       f"{nid}: approved queue unavailable "
                       f"({type(exc).__name__}: {exc})", ts=_ts(now))
            return False
        md.unlink(missing_ok=True)
        _write_released_marker(vault, nid, m.get("evidence") or {}, now)
        out = True
    claimed.unlink(missing_ok=True)
    return out


# -- ING-04: auto-capture criteria (pattern-level acceptance evidence) -----------
# A qualifying candidate is routed into the s0e hold store (above) instead of
# the owner-inbox batch — NOT straight to a signed note. It still sits
# UNSIGNED for ``undo_hours`` with a daily digest + one-word revert
# (``brain cos-hold cancel <id>``) before the ordinary hold-release drain ever
# signs it. This is the one IRREVERSIBLE step in the whole broker (a signed
# note joins the hash-chained audit brain; supersession retires but never
# removes it), so the bar is held deliberately higher than auto-archive:
#
#   - a documented MINIMUM VOLUME per pattern (1/1 = 100% is disqualified by
#     construction: the default floor is well above 1);
#   - ZERO claim-time classification/security defects for the pattern in the
#     evidence window;
#   - a Wilson-score LOWER BOUND on the accept rate (never the raw
#     percentage — a lower bound is conservative under small samples in a
#     way a raw ratio is not).
#
# Pattern taxonomy and ``bundle_version`` are OPAQUE strings supplied by the
# proposing skill (frontmatter ``pattern:``/``bundle_version:`` on the
# candidate) — this module never hardcodes what a "pattern" means. Evidence
# is scoped to the CURRENT bundle_version only (s07 version-binding rule): a
# freshly updated skill starts every pattern back at zero volume, never
# inheriting a prior version's history.
AUTOCAP_MIN_VOLUME_ENV = "BRAIN_COS_AUTOCAP_MIN_VOLUME"
DEFAULT_AUTOCAP_MIN_VOLUME = 8
AUTOCAP_MIN_LOWER_BOUND_ENV = "BRAIN_COS_AUTOCAP_MIN_LOWER_BOUND"
DEFAULT_AUTOCAP_MIN_LOWER_BOUND = 0.85
AUTOCAP_UNDO_HOURS_ENV = "BRAIN_COS_AUTOCAP_UNDO_HOURS"
DEFAULT_AUTOCAP_UNDO_HOURS = 24
_UNPATTERNED = {"", "unclassified", "unknown", None}

# LRN-02 tunables — the CATEGORY key's own dials (see the category section
# below). Kept beside the pattern dials because they share one config file.
AUTOCAP_EXPLORATION_K_ENV = "BRAIN_COS_AUTOCAP_EXPLORATION_K"
DEFAULT_AUTOCAP_EXPLORATION_K = 5
AUTOCAP_WINDOW_DAYS_ENV = "BRAIN_COS_AUTOCAP_WINDOW_DAYS"
DEFAULT_AUTOCAP_WINDOW_DAYS = 90
AUTOCAP_WINDOW_VERDICTS_ENV = "BRAIN_COS_AUTOCAP_WINDOW_VERDICTS"
DEFAULT_AUTOCAP_WINDOW_VERDICTS = 50
AUTOCAP_BULK_MAX_BATCH_ENV = "BRAIN_COS_AUTOCAP_BULK_MAX_BATCH"
DEFAULT_AUTOCAP_BULK_MAX_BATCH = 3


def autocap_config_path(vault=None) -> Path:
    return host_dir(vault) / "autocap-config.json"


def _autocap_defaults() -> dict[str, Any]:
    return {
        "min_volume": _env_int(AUTOCAP_MIN_VOLUME_ENV, DEFAULT_AUTOCAP_MIN_VOLUME),
        "min_lower_bound": _env_float(AUTOCAP_MIN_LOWER_BOUND_ENV,
                                      DEFAULT_AUTOCAP_MIN_LOWER_BOUND),
        "undo_hours": _env_int(AUTOCAP_UNDO_HOURS_ENV, DEFAULT_AUTOCAP_UNDO_HOURS),
        # LRN-02: post-graduation the only negative signal left is an undo, so
        # a graduated category keeps sampling (1-in-K back through the batch)
        # and its evidence is recency-windowed rather than all-time.
        "exploration_k": _env_int(AUTOCAP_EXPLORATION_K_ENV,
                                  DEFAULT_AUTOCAP_EXPLORATION_K),
        "window_days": _env_int(AUTOCAP_WINDOW_DAYS_ENV,
                                DEFAULT_AUTOCAP_WINDOW_DAYS),
        "window_verdicts": _env_int(AUTOCAP_WINDOW_VERDICTS_ENV,
                                    DEFAULT_AUTOCAP_WINDOW_VERDICTS),
        "bulk_accept_max_batch": _env_int(AUTOCAP_BULK_MAX_BATCH_ENV,
                                          DEFAULT_AUTOCAP_BULK_MAX_BATCH),
    }


def _env_int(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env, default))
    except ValueError:
        return default


def _env_float(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except ValueError:
        return default


def load_autocap_config(vault=None) -> dict[str, Any]:
    """Owner-editable, HOST-only criteria store (never skill text — the
    'learned pattern thresholds live in cos-ops config' requirement). Missing
    file = pure env-var defaults for every pattern.

    ``patterns`` and ``categories`` are two INDEPENDENT per-key override maps
    over the same defaults — the category key is additive (LRN-02), it never
    reuses or collides with the opaque ``pattern`` key's own overrides.
    """
    defaults = _autocap_defaults()
    p = autocap_config_path(vault)
    patterns: dict[str, Any] = {}
    categories: dict[str, Any] = {}
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            defaults.update({k: v for k, v in raw.items() if k in defaults})
            if isinstance(raw.get("patterns"), dict):
                patterns = raw["patterns"]
            if isinstance(raw.get("categories"), dict):
                categories = raw["categories"]
    return {"defaults": defaults, "patterns": patterns, "categories": categories}


def _pattern_config(vault, pattern: str) -> dict[str, Any]:
    cfg = load_autocap_config(vault)
    out = dict(cfg["defaults"])
    out.update(cfg["patterns"].get(pattern, {}) if isinstance(cfg["patterns"], dict) else {})
    return out


def _category_config(vault, category: str) -> dict[str, Any]:
    cfg = load_autocap_config(vault)
    out = dict(cfg["defaults"])
    cats = cfg["categories"]
    out.update(cats.get(category, {}) if isinstance(cats, dict) else {})
    return out


def _outcomes_path(vault=None) -> Path:
    return proposals_dir(vault) / "outcomes.jsonl"


def record_outcome(vault, *, pattern: str, ident: str, outcome: str,
                   bundle_version: str, ts: str | None = None,
                   category: str | None = None, lane: str | None = None,
                   tier: str | None = None, rules_version: str | None = None,
                   kind: str | None = None, answer_mode: str | None = None,
                   batch_size: int | None = None,
                   evidence_unit: str | None = None,
                   evidence_lineage: str | None = None) -> dict[str, Any]:
    """Append ONE owner-decision or claim-time-defect record. Never mutated,
    never deleted (the acceptance evidence this gate reads is itself
    audit-shaped, even though it lives outside the signed note chain).

    IDEMPOTENT PER (proposal id, outcome) — the expected double-count failure
    mode is one candidate appearing in TWO batches after a TTL requeue; a
    second ``accepted`` for the same id is a re-record of one owner decision,
    not a second one, so it is dropped rather than appended.

    LRN-01 adds the CATEGORY dimension: ``category``/``lane``/``tier``/
    ``rules_version`` are the graduation evidence key (HOST-bound at claim
    time, never read back off the VM-authored candidate), and ``kind``/
    ``answer_mode``/``batch_size`` are what the bulk-accept guard and the
    reports need. ``evidence_unit`` (HARDENED:codex-4) is the stable identity
    of the underlying material: the FIRST record for a unit counts, every
    later one is stored with ``counted: false`` so repeated forwards, thread
    re-extractions and whitespace variants cannot inflate the Wilson sample.
    ``evidence_lineage`` (B5) dedups the OTHER way round — one HOST-VERIFIED
    conversation is one unit even when its messages differ. Either match is
    enough to stop a second verdict counting; neither can be produced by a
    VM claim, so nothing a producer writes can raise the sample.
    """
    existing = _read_jsonl(_outcomes_path(vault))
    for e in existing:
        if e.get("id") == ident and e.get("outcome") == outcome:
            return e
    counted = True
    if evidence_unit or evidence_lineage:
        for e in existing:
            if (e.get("counted") is False
                    or e.get("outcome") not in ("accepted", "rejected")
                    or outcome not in ("accepted", "rejected")):
                continue
            if ((evidence_unit and e.get("evidence_unit") == evidence_unit)
                    or (evidence_lineage
                        and e.get("evidence_lineage") == evidence_lineage)):
                counted = False
                break
    rec: dict[str, Any] = {
        "pattern": pattern or CATEGORY_UNCLASSIFIED, "id": ident, "outcome": outcome,
        "bundle_version": bundle_version or "unknown", "ts": ts or _ts(),
        "category": category or CATEGORY_UNCLASSIFIED,
        "lane": lane or LANE_TEXT,
        "tier": tier or "unknown",
        "rules_version": rules_version or "unknown",
        "counted": counted,
    }
    for k, v in (("kind", kind), ("answer_mode", answer_mode),
                 ("batch_size", batch_size), ("evidence_unit", evidence_unit),
                 ("evidence_lineage", evidence_lineage)):
        if v is not None:
            rec[k] = v
    _append_jsonl(_outcomes_path(vault), rec)
    return rec


def _wilson_lower_bound(successes: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    phat = successes / n
    denom = 1 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * ((phat * (1 - phat) + z * z / (4 * n)) / n) ** 0.5
    return max(0.0, (center - margin) / denom)


def pattern_stats(vault, pattern: str, bundle_version: str) -> dict[str, Any]:
    """Owner-decision volume/accept-rate + claim-time defect count for
    ``pattern``, scoped to THIS ``bundle_version`` only."""
    n = accepted = defects = 0
    for e in _read_jsonl(_outcomes_path(vault)):
        if e.get("pattern") != pattern or e.get("bundle_version") != bundle_version:
            continue
        outcome = e.get("outcome")
        if outcome == "accepted":
            n += 1
            accepted += 1
        elif outcome == "rejected":
            n += 1
        elif outcome == "claim-rejected-security":
            defects += 1
    return {"n": n, "accepted": accepted, "defects": defects,
            "lower_bound": _wilson_lower_bound(accepted, n)}


def auto_capture_eligible(vault, pattern: str | None,
                          bundle_version: str | None) -> tuple[bool, dict[str, Any]]:
    """The ING-04 gate. Returns ``(eligible, stats)`` — ``stats`` always
    carries enough to explain the decision (never a bare bool)."""
    if pattern in _UNPATTERNED or bundle_version in _UNPATTERNED:
        return False, {"reason": "no pattern/bundle_version on candidate"}
    cfg = _pattern_config(vault, pattern)
    stats = pattern_stats(vault, pattern, bundle_version)
    stats["config"] = cfg
    if stats["n"] < cfg["min_volume"]:
        return False, {**stats, "reason": "below-min-volume"}
    if stats["defects"] > 0:
        return False, {**stats, "reason": "defects-present"}
    if stats["lower_bound"] < cfg["min_lower_bound"]:
        return False, {**stats, "reason": "lower-bound-below-threshold"}
    return True, {**stats, "reason": "eligible"}


# -- LRN-01/LRN-02: the CATEGORY dimension (a SECOND, additive evidence key) ---
# The pattern key above is an OPAQUE skill-supplied string. The category key is
# the owner's own ingest taxonomy (docs/cos-ingest-taxonomy.md, overlay home
# `overlay/cos/ingest.md`), and it is scoped far more narrowly:
#
#     (category, lane, classification tier, extraction_rules_version)
#
#   - LANE (HARDENED:codex-5): attachment evidence never authorizes the
#     email-text lane, or the reverse — the two lanes are different material
#     with different failure modes.
#   - TIER (HARDENED:codex-5): exact-tier keying, so evidence gathered on
#     Internal material can never authorize auto-ingest of MNPI material.
#   - RULES VERSION (HARDENED:claude-1): the SKILL's `extraction_rules_version`,
#     bumped ONLY when the Phase 1.5/1.6 extraction rules actually change —
#     NOT `bundle_version`, which is re-stamped ~23 times per 11 days. At ~8
#     candidates a night, keying evidence on the bundle string means no
#     category ever reaches min-volume. A bundle bump CARRIES evidence forward;
#     a ruleset bump resets it.
#
# EVERY value in that key is HOST-BOUND at claim time (HARDENED:consensus): a
# VM-supplied "this is category X" string is a CLAIM that can route a candidate
# INTO the owner batch, but the host validates it against the overlay, binds it
# to the proposal's content sha in `pending/<id>.json`, and computes every
# eligibility decision from HOST-recorded outcomes only.
CATEGORY_UNCLASSIFIED = "unclassified"   # already in _UNPATTERNED: never graduates
LANE_TEXT = "text"
LANE_ATTACHMENT = "attachment"
LANES = (LANE_TEXT, LANE_ATTACHMENT)
DISPOSITION_NEVER = "never"
DISPOSITION_PROPOSE = "propose"


def _defects_path(vault=None) -> Path:
    return proposals_dir(vault) / "defects.jsonl"


def log_defect(vault, kind: str, detail: str, ts: str | None = None) -> dict[str, Any]:
    """Append ONE ingestion-taxonomy defect. Doctrine alone is not a gate: an
    unparseable taxonomy or a refused `never` candidate has to leave a
    machine-readable trace, not just a comment in a spec."""
    rec = {"kind": kind, "detail": scrub(str(detail)), "ts": ts or _ts()}
    _append_jsonl(_defects_path(vault), rec)
    return rec


def defects(vault) -> list[dict[str, Any]]:
    return _read_jsonl(_defects_path(vault))


def ingest_taxonomy(vault=None, *, log: bool = False) -> dict[str, Any]:
    """The active ingest taxonomy, in the STRICT convention s03 declared
    (docs/cos-ingest-taxonomy.md §5) — mirrored engine-side here:

    - ABSENT       -> ``mode="off"``: the whole category feature is off. No
                      stamping, no engine refusal, no defect.
    - UNPARSEABLE  -> ``mode="fail-closed"``: EVERY candidate is `propose`
                      (never `always`, never `never`), plus a logged defect.
    - one bad rule -> that rule already resolved to `propose` in the parser,
                      with a warning; the rest of the file still applies.
    """
    from . import overlay as ov

    rep = ov.load_ingest_rules(vault)
    if not rep.get("present"):
        return {"mode": "off", "rules": {}, "warnings": []}
    rules = rep.get("rules") or {}
    issues = list(rep.get("issues") or [])
    if issues or not rules:
        if log:
            log_defect(vault, "ingest-taxonomy-unparseable",
                       "; ".join(issues) or "no category rules parsed from ingest.md")
        return {"mode": "fail-closed", "rules": {}, "warnings": rep.get("warnings", [])}
    return {"mode": "active", "rules": rules, "warnings": rep.get("warnings", [])}


def resolve_category(vault, claimed: Any, *, lane: str = LANE_TEXT,
                     taxonomy: dict[str, Any] | None = None) -> tuple[str, str]:
    """Validate a VM-CLAIMED category against the owner's taxonomy.

    Returns ``(category, disposition)``. An unknown/absent claim resolves to
    ``unclassified``/`propose` — the sentinel that is already in
    ``_UNPATTERNED`` and therefore can never graduate. A rule scoped to the
    OTHER lane is simply not consulted (the lane's own `propose` default
    applies), per docs/cos-ingest-taxonomy.md §3.
    """
    tax = taxonomy if taxonomy is not None else ingest_taxonomy(vault)
    if tax.get("mode") != "active":
        return CATEGORY_UNCLASSIFIED, DISPOSITION_PROPOSE
    cat = str(claimed or "").strip()
    rule = tax["rules"].get(cat)
    if not cat or not isinstance(rule, dict):
        return CATEGORY_UNCLASSIFIED, DISPOSITION_PROPOSE
    rule_lane = rule.get("lane", "both")
    if rule_lane not in ("both", lane):
        return cat, DISPOSITION_PROPOSE
    return cat, str(rule.get("disposition") or DISPOSITION_PROPOSE)


# B1: `category_tier` (a VM label + the category floor) is DELETED. Both lanes
# now resolve their ceiling through `provenance.email_classification`, the one
# host-side classifier — it applies the owner's overlay keyword mapping to the
# actual material, defaults email-derived content to MNPI, and lets a proposed
# tier or the category's `min_tier` floor only RAISE the result.


# A forward/reply wrapper is boilerplate the mail client adds, not new
# material: quoting markers, the `-----Original Message-----` rules, the
# reconstructed header block, and the `On <date>, <someone> wrote:` line. They
# are stripped BEFORE fingerprinting so the same document forwarded twice
# hashes to ONE evidence unit (B5). Stripping can only MERGE units, never
# split them — the conservative direction for a graduation gate.
_FORWARD_WRAPPER_RE = re.compile(
    r"^(?:-{2,}\s*(?:original message|forwarded message)\s*-{2,}"
    r"|begin forwarded message:"
    r"|(?:from|sent|to|cc|bcc|subject|date|reply-to)\s*:.*"
    r"|on\b.{0,160}\bwrote:)$",
    re.IGNORECASE)


def canonical_evidence_text(body: str) -> str:
    """Normalize one candidate body down to the material it actually carries."""
    kept: list[str] = []
    for raw in str(body or "").splitlines():
        line = raw.strip()
        while line.startswith(">"):
            line = line[1:].lstrip()
        if not line or _FORWARD_WRAPPER_RE.match(line):
            continue
        kept.append(line)
    return " ".join(" ".join(kept).split()).casefold()


def evidence_unit_key(*, category: str, lane: str, rules_version: str,
                      body: str = "") -> str:
    """The stable EVIDENCE UNIT one verdict is allowed to count for
    (HARDENED:codex-4, hardened again by B5): a CANONICAL content fingerprint
    + category + lane + ruleset version.

    The source conversation id used to be hashed in here too — but it is a VM
    claim, so repeated forwards under fresh ids counted as independent
    verdicts and inflated the Wilson sample. Unverified conversation identity
    must never INCREASE evidence cardinality, so it is gone from this key; the
    only conversation identity that dedups is the host-verified lineage below,
    which can only ever merge units.
    """
    return sha256_text("|".join([
        sha256_text(canonical_evidence_text(body)),
        str(category or ""), str(lane or ""), str(rules_version or ""),
    ]))


def evidence_lineage_key(*, category: str, lane: str, rules_version: str,
                         conversation_id: Any = None,
                         verified: bool = False) -> str | None:
    """The HOST-VERIFIED conversation lineage a verdict belongs to, or ``None``.

    ``verified`` is the host's own assertion (``provenance.verified``, S04 —
    parsed from an archived original, never assertable by a VM). Without it
    there is no lineage: a claimed conversation id buys the producer nothing.
    """
    if not verified or not str(conversation_id or "").strip():
        return None
    return sha256_text("|".join([
        "lineage", str(conversation_id).strip(),
        str(category or ""), str(lane or ""), str(rules_version or ""),
    ]))


def _demotions_path(vault=None) -> Path:
    return proposals_dir(vault) / "demotions.jsonl"


def demote_category(vault, category: str | None, *, reason: str,
                    ts: str | None = None) -> dict[str, Any]:
    """Un-graduate ``category`` NOW and reset its evidence.

    Append-only: the reset is a DEMOTION MARKER, not a deletion of history —
    ``category_stats`` ignores every outcome at or before the newest marker.
    Fired by a claim-time security defect and by EVERY undo path."""
    rec = {"category": str(category or CATEGORY_UNCLASSIFIED),
           "reason": reason, "ts": ts or _ts()}
    _append_jsonl(_demotions_path(vault), rec)
    return rec


def _last_demotion(vault, category: str) -> str | None:
    stamps = [str(e.get("ts", "")) for e in _read_jsonl(_demotions_path(vault))
              if e.get("category") == category and e.get("ts")]
    return max(stamps) if stamps else None


def category_stats(vault, *, category: str, lane: str, tier: str,
                   rules_version: str, now: _dt.datetime | None = None) -> dict[str, Any]:
    """Owner-decision volume/accept-rate + defect count for ONE evidence key,
    over the ROLLING window (default: last 90 days, at most the last 50
    verdicts). Windowing is what makes calibration drift inside a stable
    ruleset show up as an accept-rate DROP that un-graduates the category —
    an all-time count would dilute a recent collapse into ancient successes."""
    cfg = _category_config(vault, category)
    now = now or _utcnow()
    cutoff = now - _dt.timedelta(days=int(cfg["window_days"]))
    demoted_at = _last_demotion(vault, category)
    bulk_cap = int(cfg["bulk_accept_max_batch"])
    verdicts: list[str] = []
    defect_count = 0
    excluded_bulk = 0
    for e in _read_jsonl(_outcomes_path(vault)):
        if (e.get("category") != category or e.get("lane") != lane
                or e.get("tier") != tier or e.get("rules_version") != rules_version):
            continue
        stamp = str(e.get("ts", ""))
        parsed = _parse_ts(stamp)
        if parsed is None or parsed < cutoff:
            continue
        if demoted_at and stamp <= demoted_at:
            continue                      # evidence reset by a demotion
        if e.get("counted") is False:
            continue                      # duplicate evidence unit
        outcome = e.get("outcome")
        if outcome in ("claim-rejected-security", "undo"):
            defect_count += 1
        elif outcome in ("accepted", "rejected"):
            if (outcome == "accepted" and e.get("answer_mode") == "accept-all"
                    and int(e.get("batch_size") or 0) > bulk_cap):
                # Approval fatigue is MEASURED, not hypothetical: owners bulk
                # approve ~90%+ of HITL prompts, so an `accept all` over a
                # large batch is not per-candidate agreement. Excluded from the
                # numerator AND the denominator (counting it as a rejection
                # would be just as wrong). Rejects always count.
                excluded_bulk += 1
                continue
            verdicts.append(outcome)
    verdicts = verdicts[-int(cfg["window_verdicts"]):]
    n = len(verdicts)
    accepted = verdicts.count("accepted")
    return {"n": n, "accepted": accepted, "defects": defect_count,
            "lower_bound": _wilson_lower_bound(accepted, n),
            "excluded_bulk_accepts": excluded_bulk, "demoted_at": demoted_at,
            "config": cfg}


def category_eligible(vault, *, category: str | None, lane: str | None,
                      tier: str | None, rules_version: str | None,
                      now: _dt.datetime | None = None,
                      taxonomy: dict[str, Any] | None = None,
                      ) -> tuple[bool, dict[str, Any]]:
    """Has this category GRADUATED on this exact (lane, tier, ruleset) key?

    Same statistical bar as the pattern gate — min volume, zero security
    defects, Wilson lower bound — plus the overlay checks that make a
    graduation revocable by an owner EDIT: a category removed from
    `ingest.md`, or flipped to `never`, loses graduation at the next engine
    load with no grace period. Any category may graduate, including
    high-tier ones (owner decision) — a graduated category's candidates still
    commit through the undo-windowed hold path, never an instant signature.
    """
    if category in _UNPATTERNED or rules_version in _UNPATTERNED:
        return False, {"reason": "no category/extraction_rules_version on candidate"}
    if lane not in LANES:
        return False, {"reason": f"unknown lane {lane!r}"}
    if not tier or tier == "unknown":
        return False, {"reason": "no classification ceiling bound to the candidate"}
    tax = taxonomy if taxonomy is not None else ingest_taxonomy(vault)
    if tax.get("mode") != "active":
        return False, {"reason": f"ingest taxonomy {tax.get('mode')} — nothing graduates"}
    rule = tax["rules"].get(category)
    if not isinstance(rule, dict):
        return False, {"reason": "category-not-in-overlay (removed ⇒ demoted at load)"}
    if rule.get("disposition") == DISPOSITION_NEVER:
        return False, {"reason": "category is `never` (⇒ demoted at load)"}
    stats = category_stats(vault, category=category, lane=lane, tier=tier,
                           rules_version=rules_version, now=now)
    cfg = stats["config"]
    if stats["n"] < cfg["min_volume"]:
        return False, {**stats, "reason": "below-min-volume"}
    if stats["defects"] > 0:
        return False, {**stats, "reason": "defects-present"}
    if stats["lower_bound"] < cfg["min_lower_bound"]:
        return False, {**stats, "reason": "lower-bound-below-threshold"}
    return True, {**stats, "reason": "eligible"}


# -- B8: the both-keys policy and why a candidate lands in the owner batch ----
# Auto-capture needs TWO things, and this string names both so an operator
# never has to guess which one is holding a candidate:
#
#   1. Both producer keys — `category` + `extraction_rules_version`. A
#      candidate missing either never reaches the graduation test at all, and
#      is counted by `unstamped_batched`.
#   2. A category that has GRADUATED under that candidate's own
#      `extraction_rules_version`. Graduation comes only from accumulated
#      owner verdicts, so every category starts in the owner batch and stays
#      there until it earns the lane. A ruleset bump RESETS that evidence.
#
# Holding an un-graduated category is exactly what LRN-02 exists to do — the
# fault mode was never the holding, it was holding SILENTLY: `status_block`
# reported `ingest_taxonomy: active` regardless and no counter named the cause,
# the same silent-skip shape the S01 audit found in Phase 1.6. So the reason is
# stated here, in `brain status`, in the morning brief and in docs/cos-ops.md.
#
# This string describes the RULE, not a moment. It deliberately no longer names
# a session: it said "lands in S07" for as long as the producer was unstamped,
# and went on saying it after v5.37 shipped the stamps and
# `unstamped_batched` fell to 0 in the field (run 57, 2026-07-30) — an
# operator-facing message that outlived its own truth, which is the exact
# instrument-lies failure this module exists to prevent.
PATTERN_AUTOCAPTURE_STATUS = (
    "owner-batch by default: auto-capture requires BOTH producer keys "
    "(`category` + `extraction_rules_version`) AND a category that has "
    "GRADUATED under that candidate's own ruleset version. Graduation comes "
    "only from accumulated owner verdicts, so a new or re-versioned category "
    "always starts here. A candidate missing either key never reaches the "
    "graduation test — see `unstamped_batched`")


def _route_stats_path(vault=None) -> Path:
    return proposals_dir(vault) / "route-stats.json"


def route_stats(vault=None) -> dict[str, Any]:
    try:
        out = json.loads(_route_stats_path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _bump_route_stats(vault, *, now: _dt.datetime, unstamped: int = 0,
                      auto: int = 0, batched: int = 0,
                      **extra: int) -> dict[str, Any]:
    """Add to the cumulative routing counters (`brain status`'s `route_stats`).

    STA-01 adds `unjoined_claims` + `quarantined_claims` through ``**extra`` —
    same shape, same surfacing, so a candidate the host could not attribute is
    as loud as one that arrived unstamped."""
    cur = route_stats(vault)
    for key, add in (("unstamped_batched", unstamped), ("auto_captured", auto),
                     ("batched", batched), *extra.items()):
        cur[key] = int(cur.get(key, 0)) + int(add)
    if unstamped:
        cur["last_unstamped"] = _ts(now)
    if extra.get("quarantined_claims"):
        cur["last_quarantine"] = _ts(now)
    p = _route_stats_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(p, (json.dumps(cur, sort_keys=True) + "\n").encode("utf-8"))
    return cur


def _stamp_missing(bound: dict[str, Any]) -> bool:
    """Did this candidate arrive with no category / no ruleset version at all?"""
    return (bound.get("category") in _UNPATTERNED
            or bound.get("rules_version") in _UNPATTERNED)


def _is_exploration_sample(ident: str, k: int) -> bool:
    """1-in-K deterministic exploration sampling, keyed on the candidate id.

    Deterministic (not random) so the cadence is testable and a re-run of the
    same fold makes the same choice. ``k<=0`` disables exploration entirely;
    ``k==1`` explores everything (i.e. graduation is inert)."""
    if k <= 0:
        return False
    if k == 1:
        return True
    digest = hashlib.sha256(f"cos-explore:{ident}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % k == 0


# THE BOTH-KEYS POLICY, in one sentence: a candidate is auto-committed only
# when the existing PATTERN gate is eligible AND its CATEGORY has graduated on
# the same (lane, tier, ruleset) key; `never` or `unclassified` on EITHER key
# vetoes the auto lane outright, anything merely propose-only or un-graduated
# on either key routes to the owner batch, and even an auto candidate still
# commits through the undo-windowed hold — never an instant signature.
def route_decision(vault, bound: dict[str, Any], *,
                   now: _dt.datetime | None = None,
                   taxonomy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply the both-keys policy to ONE host-bound candidate meta.

    ``bound`` is the HOST-recorded sidecar (``pending/<id>.json`` or an
    attachment meta) — never the VM-authored frontmatter."""
    category = bound.get("category") or CATEGORY_UNCLASSIFIED
    lane = bound.get("lane") or LANE_TEXT
    pattern_ok, pattern_stats_ = auto_capture_eligible(
        vault, bound.get("pattern"), bound.get("bundle_version"))
    category_ok, category_stats_ = category_eligible(
        vault, category=category, lane=lane, tier=bound.get("tier"),
        rules_version=bound.get("rules_version"), now=now, taxonomy=taxonomy)
    out = {"lane": lane, "category": category,
           "pattern": {"eligible": pattern_ok, **pattern_stats_},
           "category_gate": {"eligible": category_ok, **category_stats_}}
    if not pattern_ok or not category_ok:
        out["decision"] = "batch"
        out["reason"] = ("pattern: " + str(pattern_stats_.get("reason"))
                         + " | category: " + str(category_stats_.get("reason")))
        return out
    k = int(category_stats_.get("config", {}).get(
        "exploration_k", DEFAULT_AUTOCAP_EXPLORATION_K))
    # Keyed on the HOST-BOUND content sha, not the producer-chosen id (medium
    # finding 6): same determinism, but a producer can no longer choose ids
    # that miss the exploration bucket and so never be sampled back through
    # the owner batch — which would leave the post-graduation accept-rate
    # window seeing only material the loop already agreed with.
    if _is_exploration_sample(str(bound.get("sha256") or bound.get("id") or ""), k):
        out["decision"] = "batch"
        out["reason"] = f"exploration-sample (1-in-{k})"
        out["exploration"] = True
        return out
    out["decision"] = "auto"
    out["reason"] = "both keys eligible"
    return out


# -- SP-01: commitment spine ingestion (hybrid capture, decision 1) -------------
# A commitment-kind candidate ALWAYS gets a spine ledger row on acceptance.
# Only a "keeper" (P0/P1-equivalent counterparty — reusing the existing
# priority-map high/normal/low vocabulary, §`load_priority_overrides` — with
# a due date at least a week out) is ALSO signed as a full brain note; every
# other accepted commitment stays spine-only (never bloats the vault with a
# note per small ask). Expected candidate frontmatter: ``kind: commitment``,
# ``direction: owed_by_me|owed_to_me``, ``counterparty``, ``due`` (ISO,
# optional), ``topic`` (optional — falls back to a slug of the body).
KEEPER_HORIZON_DAYS_ENV = "BRAIN_COS_KEEPER_HORIZON_DAYS"
DEFAULT_KEEPER_HORIZON_DAYS = 7


def _is_keeper_counterparty(vault, counterparty: str | None) -> bool:
    if not counterparty:
        return False
    overrides = load_priority_overrides(vault)
    name = str(counterparty).lower()
    if overrides.get(name) == "high":
        return True
    # Override keys are NOTE-ID SLUGS (the only form _OVERRIDE_LINE_RE parses),
    # but a commitment's counterparty is a display name from mail — e.g. a name
    # like "Renée Dûval" could never equal "renee-duval", so keeper detection
    # silently never fired (found 2026-07-17, the day the first real roster was
    # written). Compare in slug space, accents folded.
    import unicodedata
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    return overrides.get(slug) == "high"


def _spine_ingest_commitment(vault, meta: dict[str, Any], *, source_ref: str,
                             now: _dt.datetime) -> bool:
    """Record ONE accepted commitment candidate into the spine. Returns
    whether it also qualifies to be signed as a brain note (keeper)."""
    from . import spine as spine_mod

    direction = meta.get("direction") or "owed_by_me"
    if direction not in spine_mod.DIRECTIONS:
        direction = "owed_by_me"
    counterparty = str(meta.get("counterparty") or meta.get("title") or "unknown")
    text = str(meta.get("text") or meta.get("title") or source_ref)
    due = meta.get("due")
    topic = meta.get("topic")
    spine_mod.record_event(vault, event="created", direction=direction,
                           counterparty=counterparty, text=text, topic=topic,
                           due=due, source_ref=source_ref, ts=_ts(now))
    due_dt = _parse_ts(due) if due else None
    horizon = _env_days(KEEPER_HORIZON_DAYS_ENV, DEFAULT_KEEPER_HORIZON_DAYS)
    horizon_ok = bool(due_dt and (due_dt - now).days >= horizon)
    return _is_keeper_counterparty(vault, counterparty) and horizon_ok


def auto_capture_fold(vault, now: _dt.datetime | None = None) -> dict[str, Any]:
    """Route every currently-PENDING candidate that passes the BOTH-KEYS policy
    into the hold store (undo-window gated — see the hold store above), instead
    of the next owner-inbox batch. Runs BEFORE ``enqueue_batch`` in the broker
    fold so only non-qualifying candidates ever reach the owner. Never signs
    anything itself, and never bypasses the undo window.

    Under the writer lock (B4): it moves pending files into the hold store
    while the consumer may be promoting from the same directory."""
    with vault_writer_lock(vault, verb="cos-autocapture"):
        return _auto_capture_fold_locked(vault, now or _utcnow())


def _auto_capture_fold_locked(vault, now: _dt.datetime) -> dict[str, Any]:
    held: list[dict[str, Any]] = []
    explored: list[dict[str, Any]] = []
    unstamped = 0
    batched = 0
    pending = proposals_dir(vault) / "pending"
    taxonomy = ingest_taxonomy(vault)
    for m in _pending_metas(vault):
        nid = m.get("id")
        md = pending / f"{nid}.md"
        if not md.exists():
            continue
        try:
            content = md.read_text(encoding="utf-8")
        except OSError:
            continue
        bound = _bound_meta(vault, nid, body=content)
        decision = route_decision(vault, bound, now=now, taxonomy=taxonomy)
        if decision["decision"] != "auto":
            batched += 1
            if _stamp_missing(bound):
                unstamped += 1
            if decision.get("exploration"):
                explored.append({"id": nid, "category": decision["category"]})
            continue
        cfg = decision["category_gate"].get("config", _autocap_defaults())
        not_before = _ts(now + _dt.timedelta(hours=cfg.get("undo_hours",
                                                            DEFAULT_AUTOCAP_UNDO_HOURS)))
        try:
            hold_add(vault, content, not_before=not_before, ident=nid, evidence=bound)
        except ValueError:
            continue  # a hold already exists for this id — leave it pending
        except Exception as exc:  # noqa: BLE001 — no key / unwritable store
            # Fail CLOSED and stay visible: the candidate keeps its place in
            # pending/ and reaches the owner's next batch instead.
            log_defect(vault, "hold-add-failed",
                       f"{nid}: {type(exc).__name__}: {exc}", ts=_ts(now))
            continue
        # `.md` FIRST, like the other two teardown sites (`_quarantine_claim`'s
        # caller and the journal replay) — review 2026-08-13, round 7. This one
        # alone unlinked the `.json` first, and since the union receipt scan an
        # orphan of EITHER half is an incomplete pair that pins K2 INCONCLUSIVE
        # for every later run with nothing to clean it up. Neither orphan
        # self-heals, so the tie is broken on WHAT is left behind: the `.md` is
        # the candidate's mail-derived BODY and the `.json` is metadata about
        # it. Take the body off the mount first, and make all three teardowns
        # one order so the next reader has one rule to remember.
        md.unlink(missing_ok=True)
        (pending / f"{nid}.json").unlink(missing_ok=True)
        record_outcome(vault, pattern=bound.get("pattern"), ident=nid,
                       outcome="auto-captured",
                       bundle_version=bound.get("bundle_version"), ts=_ts(now),
                       category=bound.get("category"), lane=bound.get("lane"),
                       tier=bound.get("tier"),
                       rules_version=bound.get("rules_version"),
                       kind=bound.get("kind"), answer_mode="auto")
        held.append({"id": nid, "pattern": bound.get("pattern"),
                     "category": bound.get("category"), "lane": bound.get("lane"),
                     "not_before": not_before})

    # Attachments take the same policy on their own lane: a graduated
    # attachment category parks the file with a not_before in its host-private
    # quarantine and releases it into vault/inbox/ when the window closes.
    for att in attachment_metas(vault, state="pending"):
        decision = route_decision(vault, att, now=now, taxonomy=taxonomy)
        if decision["decision"] != "auto":
            batched += 1
            if _stamp_missing(att):
                unstamped += 1
            if decision.get("exploration"):
                explored.append({"id": att["id"], "category": decision["category"]})
            continue
        cfg = decision["category_gate"].get("config", _autocap_defaults())
        not_before = _ts(now + _dt.timedelta(hours=cfg.get("undo_hours",
                                                            DEFAULT_AUTOCAP_UNDO_HOURS)))
        # INT-04 (round 2): sign the authorization NOW, over the bytes on disk
        # now — the auto lane has no owner batch to CAS against later, and the
        # sidecar it used to read the hash from sits on the mount beside the
        # payload. Fail CLOSED: no key / unsafe store / unreadable payload ->
        # the candidate stays pending and reaches the owner's next batch.
        try:
            held_sha = hashlib.sha256(_read_nofollow(Path(att["path"]))).hexdigest()
            stage_attachment_hold_authz(
                vault, att["id"], sha256_hex=held_sha, not_before=not_before,
                # the name (and therefore the ingest handler) and the category
                # the demotion re-check runs against are signed HERE, at the
                # one moment the host itself decided this may auto-capture
                filename=str(att.get("filename") or Path(att["path"]).name),
                category=str(att.get("category") or ""), now=now)
        except Exception as exc:  # noqa: BLE001 — no key / unwritable / swapped
            log_defect(vault, "attachment-hold-unauthorized",
                       f"{att['id']}: {type(exc).__name__}: {exc}", ts=_ts(now))
            continue
        att = {**att, "state": "held", "not_before": not_before}
        _write_attachment_meta(vault, att)
        record_outcome(vault, pattern=att.get("pattern"), ident=att["id"],
                       outcome="auto-captured",
                       bundle_version=att.get("bundle_version"), ts=_ts(now),
                       category=att.get("category"), lane=LANE_ATTACHMENT,
                       tier=att.get("tier"), rules_version=att.get("rules_version"),
                       kind="attachment", answer_mode="auto")
        held.append({"id": att["id"], "category": att.get("category"),
                     "lane": LANE_ATTACHMENT, "not_before": not_before})
    stats = _bump_route_stats(vault, now=now, unstamped=unstamped,
                              auto=len(held), batched=batched)
    return {"held": held, "exploration_samples": explored,
            # B8: name the DEGRADATION out loud, per run and cumulatively.
            "batched": batched, "unstamped_batched": unstamped,
            "unstamped_batched_total": int(stats.get("unstamped_batched", 0)),
            "pattern_autocapture": PATTERN_AUTOCAPTURE_STATUS}


# -- ingest sweeper (host-broker) ------------------------------------------------
# The Cowork VM writes an ingest MANIFEST line per triggered download into the
# VM-writable drop. The host sweeper is disabled until the operator configures
# a DEDICATED host-only staging directory; it never falls back to the user's
# shared ~/Downloads directory. This out-of-band host configuration is the
# provenance boundary a VM-written basename cannot provide by itself.
INGEST_SWEEP_MAX_BYTES_ENV = "BRAIN_COS_SWEEP_MAX_BYTES"
DEFAULT_INGEST_SWEEP_MAX_BYTES = 200 * 1024 * 1024
INGEST_SWEEP_DOWNLOADS_ENV = "BRAIN_COS_DOWNLOADS_DIR"
INGEST_SWEEP_SKEW_SECONDS = 300          # manifest ts vs file mtime clock skew
INGEST_SWEEP_SIZE_TOLERANCE = 0.10       # when the manifest carries a size
# Host-observed freshness floor (codex 2026-07-19): a candidate whose mtime is
# older than this relative to the sweep's OWN clock is a pre-existing host file,
# not something the VM just downloaded — the un-forgeable provenance anchor. The
# maintain umbrella fires hourly; a 6h window tolerates a few missed runs / a
# sleeping Mac while still excluding files that have sat in Downloads for days.
INGEST_SWEEP_RECENCY_ENV = "BRAIN_COS_SWEEP_RECENCY_SECONDS"
DEFAULT_INGEST_SWEEP_RECENCY_SECONDS = 6 * 3600


def ingest_manifest_dir(vault=None) -> Path:
    return drop_dir(vault) / "ingest-manifest"


# -- DOC-01: attachments join the propose->learn loop --------------------------
# A swept attachment used to land straight in ``vault/inbox/``, i.e. straight
# into the signed ingest drain — documents bypassed the confirmation loop that
# every email-TEXT candidate goes through. The flow is now:
#
#   manifest -> HOST-PRIVATE quarantine (host/attachments/quarantine/, which
#               resolves INSIDE <vault>/.brain/ — host-only, gitignored
#               wholesale and never indexed, but NOT outside the vault tree and
#               therefore visible on the Cowork VirtioFS mount; unverdicted
#               third-party binaries now dwell there for up to the proposal
#               TTL, where they used to transit vault/inbox/ into signed
#               raw/originals/ within the hour)
#            -> owner batch verdict (or the auto lane, once the category has
#               graduated on the attachment lane)
#            -> only an ACCEPTED file moves to vault/inbox/ for signed ingest.
#
# A REJECTED attachment leaves ZERO residue IN THE VAULT: it never reached
# vault/, so there is no raw/ note, no archived original, no index row and no
# audit entry to undo. It is NOT destroyed, though (B7) — the sweep moved it
# out of the owner's download location, so the copy and its sidecar go to the
# GC-windowed expired/ holding area, recoverable for $BRAIN_COS_GC_DAYS.
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
    _write_atomic(p, (json.dumps(meta, sort_keys=True) + "\n").encode("utf-8"))


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
    _write_atomic(ldir / f"{record['id']}.json",
                  (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))


def _accept_attachment(vault, meta: dict[str, Any], *,
                       expected_sha: str | None = None,
                       expected_name: str | None = None, batch_id: str = "",
                       now: _dt.datetime | None = None) -> str:
    """Release ONE verdict-accepted attachment into ``vault/inbox/`` under a
    host-signed anchor, carrying its claimed provenance to the ingest drain.

    R3 (2026-07-30 review, HIGH): the lifecycle record is written and fsynced
    in state ``releasing`` BEFORE the payload moves, and CASed to ``released``
    after. Written after the move — as it was — a crash in that window loses
    the identity entirely: ``undo_state`` answered ``absent`` for a file
    already sitting in ``vault/inbox/``, so it could be neither withdrawn nor
    retired. ``releasing`` means "the payload is at ``src`` OR at ``dest``";
    recovery (``undo_state``) reconciles by looking at both.

    INT-04 — the accept-to-signature window is CLOSED here, the same way the
    note lane's was. Three things make it hold, and all three are needed:

    1. **One read, not a move.** The bytes are read ONCE, no-follow and
       regular-file-only, and the buffer is what gets written to the
       destination. ``shutil.move`` published whatever was at the path at move
       time — a different object from the one the caller's CAS had just
       hashed.
    2. **Verified against a PROTECTED sha, always.** ``expected_sha`` comes
       from the Ed25519-signed batch digest (owner lane) or from the signed
       hold authorization (auto lane) — never from a sidecar on the mount (that
       file is attacker-writable, and the sidecar's hash and the payload can be
       rewritten in one pass). It is REQUIRED: a missing expected hash used to
       fall back to ``meta["sha256"]`` and then, when that was absent too, skip
       the comparison entirely. "No protected hash" now means "no release".
    2b. **Under a PROTECTED destination name.** ``expected_name`` comes from
       the same two signed sources, and is equally REQUIRED (round 3,
       CRITICAL). The name used to come from ``meta["filename"]`` on the
       mount — so an authorization over the bytes still let the VM choose the
       SUFFIX, and the suffix chooses which ingest handler parses them. Signing
       the content while leaving the parser to the attacker is not an
       authorization.
    3. **A signed anchor the drain re-checks.** The destination is on the
       mount and the signature happens an hour later in the ingest drain, so
       the anchor (off-mount, `stage_attachment_anchor`) binds the accepted
       bytes to that destination and the drain refuses anything else. The
       lifecycle record below could never do this job: it lives on the same
       mount as the payload, so one write substitutes both and the check would
       verify the attacker's hash against the attacker's bytes.

    Fails closed: an unreadable payload, a hash that is not the accepted one,
    an unreachable anchor store or no signing key -> nothing is released and
    the attachment stays in quarantine for the next run."""
    now = now or _utcnow()
    src = Path(meta["path"])
    inbox = config.vault_root(vault) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    want_name = _safe_basename(str(expected_name or ""))
    if not want_name:
        raise ApprovedRefused(
            f"{meta.get('id')}: no host-protected destination name for this "
            f"attachment — refusing to release (the sidecar's own `filename` "
            f"is on the mount beside the payload, and its SUFFIX chooses which "
            f"ingest handler parses these bytes)")
    dest = _unique_dest(inbox, want_name)
    data = _read_nofollow(src)                    # ApprovedRefused if swapped
    sha = hashlib.sha256(data).hexdigest()
    want = str(expected_sha or "")
    if not re.fullmatch(r"[0-9a-f]{64}", want):
        raise ApprovedRefused(
            f"{meta.get('id')}: no host-protected content hash for this "
            f"attachment — refusing to release (the sidecar's own `sha256` is "
            f"on the mount beside the payload and authorizes nothing)")
    if sha != want:
        raise ApprovedRefused(
            f"{meta.get('id')}: quarantined attachment drifted since the owner "
            f"accepted it (accepted {str(want)[:12]}…, on disk {sha[:12]}…)")
    record = dict(meta.get("provenance") or {})
    for key in ("category", "classification", "msg_key"):
        val = meta.get("tier") if key == "classification" else meta.get(key)
        if val:
            record[key] = provenance.sanitize_value(val)
    life = {
        **{k: meta.get(k) for k in (
            "id", "sha256", "filename", "category", "lane", "tier",
            "rules_version", "pattern", "bundle_version", "evidence_unit",
            "evidence_lineage")},
        "src": str(src), "dest": str(dest), "released": _ts(now),
        # the AUTHORIZED name, not the sidecar's current claim of it
        "filename": want_name,
        # the VERIFIED content hash, not the sidecar's claim of it — this is
        # what the ingest drain's manifest maps to the final note id, and what
        # `attachment_release_recorded` matches an unanchored inbox file against.
        "sha256": sha,
    }
    _write_attachment_lifecycle(vault, {**life, "state": "releasing"})
    stage_attachment_anchor(vault, dest, sha256_hex=sha, aid=str(meta["id"]),
                            batch_id=batch_id, claim=record, now=now)
    _write_atomic(dest, data)
    src.unlink(missing_ok=True)
    # B3: keep the identity alive past the sidecar so a later undo can find
    # the unsigned inbox copy — or, once ingested, the raw/ note it became.
    _write_attachment_lifecycle(vault, {**life, "state": "released"})
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


def _unique_dest(inbox: Path, filename: str) -> Path:
    """A free name for ``filename`` INSIDE ``inbox`` — never outside it.

    R4: ``attachment_metas`` guards the sidecar's ``id`` and its ``path``, but
    ``filename`` is a THIRD mount-controlled field, and `_accept_attachment`
    hands it straight to this join. An absolute or traversing filename whose
    destination did not already exist made the caller's ``shutil.move`` write
    outside ``vault/inbox``. The guard belongs HERE, at the join every caller
    routes through, not at the one call site a reviewer happened to reach.

    The bare-name rule itself is `_safe_basename` — ONE definition, shared with
    the lifecycle/anchor lanes, so the three of them cannot drift apart."""
    name = _safe_basename(filename)
    if name is None:
        raise ValueError(
            f"unsafe destination filename {str(filename)[:60]!r}: must be a "
            f"bare filename (no separators, no '.'/'..', not absolute)")
    filename = name
    dest = inbox / filename
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    for i in range(2, 1000):
        cand = inbox / f"{stem}-{i}{suffix}"
        if not cand.exists():
            return cand
    raise ValueError(f"cannot uniquify destination for {filename!r}")


def ingest_sweep(vault, *, downloads_dir: Path | str | None = None,
                 dry_run: bool = False,
                 now: _dt.datetime | None = None) -> dict[str, Any]:
    """HOST sweeper: claim unclaimed ingest-manifest lines against the host
    dedicated staging dir and MOVE exact-filename matches into
    ``<vault>/inbox/``.

    Safety contract:
    - filenames are basename-only — any path separator / ``..`` is refused;
    - a symlinked candidate (or manifest file) is refused, never followed;
    - files above the size cap (default 200 MB) are refused;
    - a candidate must be a FRESH download — mtime within the recency window of
      the sweep's own clock — so a VM manifest cannot claim a pre-existing host
      Downloads file it did not just download (codex 2026-07-19, VM/host
      boundary): the mtime is the one provenance signal the VM cannot forge;
    - a match requires exact filename, size within tolerance when the manifest
      carries one, and mtime newer than the manifest ts minus skew;
    - claims are append-only, one per manifest line — a re-run never
      double-moves (idempotent);
    - NOTHING the manifest does not name is ever touched, moved, or deleted.

    PRV-02: a manifest line MAY carry ``"provenance": {sender, sent,
    conversation_id, subject}`` (and an optional ``category``/
    ``classification``) for the file it names. Those values are VM-authored
    CLAIMS: they are scrubbed onto the quarantine sidecar and, once the owner
    ACCEPTS, signed into the acceptance anchor the ingest drain reads
    (`stage_attachment_anchor`; INT-04 replaced the plain content-keyed JSON
    store they used to travel in, which sat on the same mount as the payload).
    They never become ``provenance.verified``, which only the host's own parse
    of the archived original can assert.
    """
    now = now or _utcnow()
    configured_dir = downloads_dir or os.environ.get(INGEST_SWEEP_DOWNLOADS_ENV)
    ddir = Path(configured_dir).expanduser() if configured_dir else None
    mdir = ingest_manifest_dir(vault)
    taxonomy = ingest_taxonomy(vault, log=True)
    report: dict[str, Any] = {"downloads_dir": str(ddir) if ddir else None,
                              "dry_run": dry_run,
                              "moved": [], "refused": [], "unmatched": [],
                              "already_claimed": 0}
    if ddir is None:
        report["disabled_reason"] = (
            f"set {INGEST_SWEEP_DOWNLOADS_ENV} to a dedicated host-only "
            "download staging directory; shared ~/Downloads is never swept")
        return report
    if ddir.is_symlink() or ddir.resolve() == (Path.home() / "Downloads").resolve():
        report["disabled_reason"] = (
            "refusing shared or symlinked ~/Downloads; configure a dedicated "
            "host-only staging directory")
        return report
    ddir = ddir.resolve()
    report["downloads_dir"] = str(ddir)
    if not mdir.is_dir():
        return report
    claims = _read_jsonl(_sweep_claims_path(vault))
    claimed_keys = {c.get("key") for c in claims}
    max_bytes = _sweep_max_bytes()

    def _claim(key: str, entry: dict[str, Any], disposition: str,
               dest: str | None = None) -> None:
        if dry_run:
            return
        rec = {"key": key, "msg_key": entry.get("msg_key"),
               "filename": entry.get("filename"), "disposition": disposition,
               "ts": _ts(now)}
        if dest:
            rec["dest"] = dest
        _append_jsonl(_sweep_claims_path(vault), rec)
        claimed_keys.add(key)

    for mf in sorted(mdir.glob("manifest-*.jsonl")):
        # same trust boundary as claim_drops: the drop dir is VM-writable —
        # never follow a symlinked manifest.
        if mf.is_symlink() or not mf.is_file():
            continue
        for entry in _read_jsonl(mf):
            key = _manifest_line_key(entry)
            if key in claimed_keys:
                report["already_claimed"] += 1
                continue
            # field-run finding 2026-07-14: the kernel writes richer manifests
            # (expected_filename = the EML it downloaded, attachment_filename =
            # the attachment inside) — accept any of the names it offers.
            names = [entry.get(k) for k in
                     ("filename", "expected_filename", "attachment_filename")]
            names = [n for n in names if isinstance(n, str) and n.strip()]
            # INT-05 round 2: this was an inline `n == os.path.basename(n)`
            # test, which is NOT the bare-name rule this codebase has one
            # definition of — on POSIX `os.path.basename("..\\..\\win")` is the
            # whole string, so a Windows-style traversal passed it, and this
            # engine ships to Windows. `_safe_basename` is the definition
            # (separators of BOTH kinds, `.`/`..`, absolute, `~`, control
            # chars), and routing through it is also what makes the value that
            # flows on the GUARDED one rather than the raw manifest field.
            safe = [b for b in (_safe_basename(n) for n in names) if b]
            if not safe:
                _claim(key, entry, "refused: unsafe filename (basename only)")
                report["refused"].append({"filename": names[0] if names else None,
                                          "reason": "unsafe filename"})
                continue
            fname = next((n for n in safe if (ddir / n).exists()), None)
            if fname is None:
                # WHY matters: "not in the downloads dir at all" is a different
                # problem from "present but rejected by a guard", and the old
                # bare-name list could not tell them apart — the 2026-07-16
                # field read was "sweeper stalled ~32h" when the sweeper was in
                # fact correctly refusing stale namesakes. Reasons are additive:
                # the plain names stay in `unmatched` for compatibility.
                report["unmatched"].extend(safe)
                report.setdefault("unmatched_reasons", []).extend(
                    {"filename": n, "reason": "not present in the downloads dir"}
                    for n in safe)
                continue
            cand = ddir / fname
            if cand.is_symlink() or not cand.is_file():
                _claim(key, entry, "refused: symlink / not a regular file")
                report["refused"].append({"filename": fname,
                                          "reason": "symlink refused"})
                continue
            size = cand.stat().st_size
            if size > max_bytes:
                _claim(key, entry, f"refused: size {size} > cap {max_bytes}")
                report["refused"].append({"filename": fname,
                                          "reason": "over size cap"})
                continue
            # SECURITY (codex 2026-07-19, VM/host boundary): the manifest is
            # VM-writable, so its own fields cannot prove provenance — the VM
            # can name any basename and BACKDATE `ts` to defeat the staleness
            # check on an old host file. The one thing a VM-side attacker CANNOT
            # forge is a host file's mtime, so the sweep's OWN clock is the
            # anchor: only a file whose mtime is recent relative to `now` — i.e.
            # one genuinely downloaded within this maintenance window — is
            # eligible. A guessed sensitive file that has been sitting in
            # ~/Downloads (tax.pdf, statement.pdf …) is refused regardless of
            # what the manifest claims. Widen the window via env for slow hosts.
            age = now.timestamp() - cand.stat().st_mtime
            if age > _sweep_recency_seconds():
                report["unmatched"].append(fname)
                report.setdefault("unmatched_reasons", []).append({
                    "filename": fname,
                    "reason": f"not a fresh download: host mtime is {age / 3600.0:.1f}h "
                              f"old (recency window {_sweep_recency_seconds() // 3600}h) "
                              f"— a pre-existing host file the VM manifest cannot claim",
                })
                continue
            want = entry.get("approx_size_bytes")
            if isinstance(want, int) and want > 0:
                if abs(size - want) > max(want * INGEST_SWEEP_SIZE_TOLERANCE, 4096):
                    report["unmatched"].append(fname)  # wrong file — leave it
                    report.setdefault("unmatched_reasons", []).append({
                        "filename": fname,
                        "reason": f"size mismatch: on disk {size}B, manifest expects "
                                  f"{want}B (tolerance "
                                  f"{int(max(want * INGEST_SWEEP_SIZE_TOLERANCE, 4096))}B) "
                                  f"— a DIFFERENT file of the same name",
                    })
                    continue
            ets = _parse_ts(str(entry.get("ts", "")))
            if ets is not None and cand.stat().st_mtime < (
                    ets.timestamp() - INGEST_SWEEP_SKEW_SECONDS):
                report["unmatched"].append(fname)  # older than the download
                age_h = (ets.timestamp() - cand.stat().st_mtime) / 3600.0
                report.setdefault("unmatched_reasons", []).append({
                    "filename": fname,
                    "reason": f"stale namesake: file mtime is {age_h:.1f}h OLDER than the "
                              f"manifest's download ts {ets.isoformat()} (skew allowance "
                              f"{INGEST_SWEEP_SKEW_SECONDS}s) — the VM's download did not "
                              f"land; this is a pre-existing file with the same name",
                })
                continue
            claimed_prov = provenance.claim_from(entry.get("provenance"))
            # DOC-01: the VM's `category:` on a manifest line is a CLAIM like
            # any other — validated HOST-side against the owner's taxonomy on
            # the ATTACHMENT lane, then bound to this file's content sha.
            category, disposition = resolve_category(
                vault, entry.get("category"), lane=LANE_ATTACHMENT,
                taxonomy=taxonomy)
            if disposition == DISPOSITION_NEVER:
                _claim(key, entry, f"refused: never-ingest category {category}")
                log_defect(vault, "never-category-attachment",
                           f"{fname}: category={category}", ts=_ts(now))
                report["refused"].append({"filename": fname,
                                          "reason": "never-ingest category"})
                continue
            file_sha = hashlib.sha256(cand.read_bytes()).hexdigest()
            aid = safe_slug("att-" + file_sha[:12])
            if _attachment_meta_path(vault, aid).exists():
                # Same bytes already awaiting a verdict — never move a second
                # copy over the first, and never delete the owner's download.
                _claim(key, entry, f"duplicate: already quarantined as {aid}")
                report.setdefault("duplicates", []).append(
                    {"filename": fname, "id": aid})
                continue
            qdir = attachment_quarantine_dir(vault)
            dest = qdir / f"{aid}{cand.suffix}"
            # R1: the manifest line (subject/sender) and the download filename
            # are all VM-authored CLAIMS — they never lower the tier. Same rule
            # as the text lane, inherited from the parameter name.
            tier, _why = provenance.email_classification(
                vault, proposed=entry.get("classification"), category=category)
            if not dry_run:
                qdir.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(qdir, 0o700)  # nosemgrep: insecure-file-permissions -- host-private quarantine, OWNER-ONLY by design
                except OSError:
                    pass
                shutil.move(str(cand), dest)
                _write_attachment_meta(vault, {
                    "id": aid, "sha256": file_sha, "filename": fname,
                    "path": str(dest), "lane": LANE_ATTACHMENT,
                    "category": category, "disposition": disposition,
                    "tier": tier,
                    "rules_version": entry.get("extraction_rules_version"),
                    "pattern": entry.get("pattern"),
                    "bundle_version": entry.get("bundle_version"),
                    "kind": "attachment",
                    "msg_key": provenance.sanitize_value(entry.get("msg_key")),
                    "provenance": claimed_prov,
                    "claimed": _ts(now),
                    "ttl_expires": _ts(now + _dt.timedelta(
                        days=_env_days(PROPOSAL_TTL_DAYS_ENV,
                                       DEFAULT_PROPOSAL_TTL_DAYS))),
                    "state": "pending",
                    # The attachment's own bytes ARE its canonical content
                    # fingerprint; its conversation id is a VM claim and never
                    # a host-verified lineage (B5), so it buys no cardinality.
                    "evidence_unit": evidence_unit_key(
                        category=category, lane=LANE_ATTACHMENT,
                        rules_version=entry.get("extraction_rules_version"),
                        body=file_sha),
                    "evidence_lineage": None,
                })
            _claim(key, entry, "quarantined", dest=str(dest))
            report["moved"].append(provenance.scrub(
                {"filename": fname, "dest": str(dest), "id": aid,
                 "awaiting_verdict": True, "category": category,
                 "msg_key": entry.get("msg_key"),
                 **({"provenance": claimed_prov} if claimed_prov else {})}))
    return report


# -- status summary --------------------------------------------------------------
# HARDENED:claude-2 — LIVENESS, not failure. A batch nobody answers is not an
# error anywhere: the broker keeps working, the queue stays valid, and the
# one-open-batch backpressure quietly re-kills the funnel behind it. So the age
# of the oldest open batch and the count of candidates stuck behind it are
# surfaced in `brain status` and the morning brief with an explicit threshold.
BATCH_STALE_HOURS_ENV = "BRAIN_COS_BATCH_STALE_HOURS"
DEFAULT_BATCH_STALE_HOURS = 48


def batch_liveness(vault, now: _dt.datetime | None = None) -> dict[str, Any]:
    now = now or _utcnow()
    open_ = open_batches(vault)
    ages = [(now - c).total_seconds() / 3600.0
            for c in (_parse_ts(str(b.get("created", ""))) for b in open_)
            if c is not None]
    oldest = max(ages) if ages else None
    queued = {c["id"] for b in open_ for c in b.get("candidates", [])}
    waiting = [m["id"] for m in
               _pending_metas(vault) + attachment_metas(vault, state="pending")
               if m["id"] not in queued]
    threshold = float(_env_int(BATCH_STALE_HOURS_ENV, DEFAULT_BATCH_STALE_HOURS))
    stats = route_stats(vault)
    out = {
        "open_batches": len(open_),
        "oldest_open_batch_hours": round(oldest, 1) if oldest is not None else None,
        "pending_behind_backpressure": len(waiting),
        "threshold_hours": threshold,
        "alert": bool(oldest is not None and oldest > threshold),
        # B8: the both-keys policy silently suspends the pattern auto-capture
        # lane that is live today, until the producer stamps category +
        # extraction_rules_version. This counter is what makes it not silent —
        # it rides `batch_liveness` so `brain status` AND the morning brief
        # both see it with no extra plumbing.
        "unstamped_batched": int(stats.get("unstamped_batched", 0)),
        "unstamped_last": stats.get("last_unstamped"),
        "pattern_autocapture": PATTERN_AUTOCAPTURE_STATUS,
        # STA-01: candidates the host could not attribute to a VALID run. Same
        # loudness as `unstamped_batched` — a silent quarantine would be the
        # same instrument-lies failure in a new place.
        "quarantined_claims": len(quarantined_claims(vault)),
        "unjoined_claims_total": int(stats.get("unjoined_claims", 0)),
        "quarantined_claims_total": int(stats.get("quarantined_claims", 0)),
        "quarantine_last": stats.get("last_quarantine"),
    }
    # INS-01: the host run validator's own finding, on the SAME carrier as
    # `unstamped_batched` — `batch_liveness` is what both `brain status` and the
    # morning brief read, so a run scored INVALID/INCONCLUSIVE cannot be a
    # silent log entry.
    try:
        from . import cos_runverify
        out.update(cos_runverify.alert(vault))
    except Exception as exc:  # noqa: BLE001 — liveness must never crash status
        out["run_validity_error"] = f"{type(exc).__name__}: {exc}"
    if out["quarantined_claims"]:
        by_code: dict[str, int] = {}
        for q in quarantined_claims(vault):
            code = str(q.get("code") or "unknown")
            by_code[code] = by_code.get(code, 0) + 1
        out["quarantine_reasons"] = by_code
        out["quarantine_text"] = (
            f"{out['quarantined_claims']} COS candidate(s) held in claim "
            "quarantine — the host cannot attribute them to a VALID run "
            f"({', '.join(f'{k}×{v}' for k, v in sorted(by_code.items()))}); "
            "they are released automatically once the run validator scores "
            "their run VALID, and never bound while it does not")
    if out["alert"]:
        out["alert_text"] = (
            f"COS ingestion batch unanswered for {out['oldest_open_batch_hours']}h "
            f"(threshold {int(threshold)}h) — {out['pending_behind_backpressure']} "
            f"candidate(s) held behind it; answer it via /brain-inbox")
    return out


def status_block(vault, role: str) -> dict[str, Any]:
    """Cheap counts for ``brain status --json``. The VM view only reads the
    zones it may touch (drop/ + shared/); host/ counts are host-only."""
    out: dict[str, Any] = {
        "ops_dir": str(ops_dir(vault)),
        "zones": {"host_private": str(host_dir(vault)),
                  "vm_readable": str(shared_dir(vault)),
                  "vm_writable": str(drop_dir(vault))},
    }
    try:
        pdir = proposal_drop_dir(vault)
        out["proposal_drops"] = len(list(pdir.glob("*.md"))) if pdir.is_dir() else 0
        out["priority_map_present"] = priority_map_path(vault).exists()
        if role == "host":
            out["pending_proposals"] = len(_pending_metas(vault))
            out["open_batches"] = len(open_batches(vault))
            out["attachments_awaiting_verdict"] = len(
                attachment_metas(vault, state="pending"))
            out["version_links_awaiting_verdict"] = len(version_link_metas(vault))
            out["batch_liveness"] = batch_liveness(vault)
            out["ingest_taxonomy"] = ingest_taxonomy(vault)["mode"]
            out["taxonomy_defects"] = len(defects(vault))
            # B8: `ingest_taxonomy: active` alone was misleading — the taxonomy
            # parsing fine says nothing about whether ANY candidate can reach
            # the auto lane. State the lane's actual status and its cost.
            out["pattern_autocapture"] = PATTERN_AUTOCAPTURE_STATUS
            out["route_stats"] = route_stats(vault)
            holds = hold_list(vault)
            out["holds"] = len(holds)
            # ING-04 daily digest: id + not_before only (never content) so a
            # pending auto-capture is never silent — revert with
            # `brain cos-hold cancel <id>` before it releases.
            out["holds_pending"] = [
                {"id": h.get("id"), "not_before": h.get("not_before")}
                for h in holds]
            out["corrections"] = len(list_corrections(vault))
            # INT-01: the accept -> signature waiting room, and anything the
            # signing gate REFUSED there (a refusal is a security event, so it
            # is visible here and not only in defects.jsonl).
            refused = approved_refused(vault)
            out["approved_awaiting_signature"] = len(approved_pending(vault))
            # INT-04: the attachment lane's equivalent — an armed acceptance
            # anchor is the ONLY thing that keeps its inbox file at the
            # email-derived MNPI floor, and it is not rebuildable from vault/.
            out["attachment_anchors_awaiting_drain"] = \
                attachment_anchors_awaiting_drain(vault)
            # CAP-02: the capture corpus. Unfiltered MNPI mail bodies, and the
            # one thing under the index dir nothing else reports — so an
            # operator repointing $BRAIN_INDEX_DIR or uninstalling has to be
            # able to see how much is on disk, and whether the nightly
            # retention fold has actually run HERE.
            # corpus_summary never raises; it reports its own error inline.
            from . import cos_corpus as _corpus
            out["capture_corpus"] = _corpus.corpus_summary(vault)
            out["approved_refused"] = len(refused)
            if refused:
                out["approved_refused_files"] = [p.name for p in refused]
            try:
                # NOT one of the ops-dir `zones`: this one is deliberately
                # outside the vault (and so off the VM mount) entirely.
                out["approved_queue"] = str(approved_queue_root(vault))
            except ApprovedQueueUnsafe as exc:
                out["approved_queue_error"] = str(exc)
            try:
                from . import spine as spine_mod
                rep = spine_mod.radar(vault)
                out["spine"] = {"late": len(rep["late"]), "at_risk": len(rep["at_risk"]),
                                "open": len(spine_mod.list_all(vault, status="open"))}
            except Exception:  # noqa: BLE001 — spine status is best-effort
                out["spine"] = {"error": "unavailable"}
    except Exception as exc:  # noqa: BLE001 — status must never crash on cos state
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out

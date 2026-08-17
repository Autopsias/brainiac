"""Path + location policy for the brain engine.

The derived SQLite index lives under a per-user **application-data** directory,
NOT under Documents/Desktop (Windows Controlled-Folder-Access protected paths) —
CORE-01 hardening. The index is derived and disposable; delete-and-rebuild from
`vault/` is always safe, so its exact location is policy, not truth.

Resolution order for the index directory:
  1. ``$BRAIN_INDEX_DIR``                  (explicit override; tests use this)
  2. Windows  : ``%LOCALAPPDATA%\\profile-a-brain``
  3. macOS    : ``~/Library/Application Support/profile-a-brain``
  4. Linux/*  : ``$XDG_DATA_HOME/profile-a-brain`` or ``~/.local/share/...``

Per-vault isolation (0.3.0): under the app-data base, each vault gets its own
subdirectory ``vaults/<name>-<hash8>/`` derived from the resolved vault path —
N vaults on one machine get N independent indexes + audit chains with no env
var to remember. ``$BRAIN_INDEX_DIR`` still overrides completely (no nesting).
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

APP_NAME = "profile-a-brain"
INDEX_FILENAME = "index.sqlite"

# Host/VM trust split (S06). The HOST broker is the sole writer (signs the audit
# chain, mutates the index, publishes snapshots). The Cowork Linux VM is a
# READ + DRAFT surface only — it may never write notes, open the index in
# WAL/write mode, or resolve a signing key. Role is resolved from $BRAIN_ROLE,
# default "host". See AGENTS.md §6 + docs/cowork-windows-install.md.
ROLE_HOST = "host"
ROLE_VM = "vm"

MANAGED_ENV = "BRAIN_MANAGED"


def is_managed() -> bool:
    """Corporate lockdown mode (``$BRAIN_MANAGED=1``, set by MDM/endpoint policy).

    When on, the endpoint cannot self-modify or accept ad-hoc key custody:
    ``brain update`` self-update is refused, and the env/shell key-custody
    sources (``BRAIN_AUDIT_KEY_PEM/CMD``, ``BRAIN_ENCRYPTION_KEY/CMD``) are
    ignored so ONLY the OS keystore (Keychain / Credential Manager) can provide
    a key. Addresses the cross-family review's supply-chain + shell-custody
    conditions. A no-op (default off) on an unmanaged personal machine."""
    return os.environ.get(MANAGED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def role(explicit: str | None = None) -> str:
    """Resolve the trust role: explicit arg > ``$BRAIN_ROLE`` > ``host``."""
    val = (explicit or os.environ.get("BRAIN_ROLE") or ROLE_HOST).strip().lower()
    return ROLE_VM if val == ROLE_VM else ROLE_HOST


def apply_role_embedder_policy(resolved_role: str) -> None:
    """The VM leg fails CLOSED on a dead embedder by default (DV-03, 2026-07-09).

    A Cowork VM that silently answered semantic queries with random HASH vectors
    (onnxruntime missing in the zero-install shim's python) is the exact failure
    this guards: ``role=vm`` defaults ``$BRAIN_REQUIRE_REAL_EMBEDDER=1`` so the
    implicit hash fallback RAISES instead of degrading. It is a no-op whenever a
    real embedder is present (the flag only bites on a dead one), and lexical
    verbs (``grep``/``bases-query``) never embed, so they keep working — only the
    semantic path (``search``/``hybrid-search``) fails loud. Skipped when the
    operator explicitly chose hash (``$BRAIN_EMBEDDER=hash``) or already pinned
    the flag either way. Host leg is unchanged (warns, never fails closed)."""
    if resolved_role != ROLE_VM:
        return
    if os.environ.get("BRAIN_EMBEDDER", "").strip().lower() == "hash":
        return
    os.environ.setdefault("BRAIN_REQUIRE_REAL_EMBEDDER", "1")


def _app_data_base() -> Path:
    """Per-user app-data base dir (no vault scoping).

    Never returns a Controlled-Folder-Access path (Documents/Desktop/Pictures).
    """
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    # Linux / BSD / Cowork VM
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def _vault_id_path(vault: str | os.PathLike[str] | None = None) -> Path:
    return vault_root(vault) / ".brain" / "vault-id"


def vault_id(vault: str | os.PathLike[str] | None = None, *, create: bool = False) -> str | None:
    """A stable per-vault identity persisted at ``<vault>/.brain/vault-id``.

    The app-data index+audit dir is keyed on this instead of the vault's
    ABSOLUTE PATH (field bug 3): the path changes when the vault folder moves,
    this id does not — so the index and the hash-chained audit log survive a
    move (no full re-embed, no silent audit-chain fork). ``.brain/`` travels
    WITH the vault folder, so the id persists across a move. Returns ``None``
    if absent and ``create`` is False; best-effort on a read-only vault."""
    p = _vault_id_path(vault)
    try:
        vid = p.read_text(encoding="utf-8").strip()
        if vid:
            return vid
    except OSError:
        pass
    if not create:
        return None
    import secrets
    vid = secrets.token_hex(8)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(vid + "\n", encoding="utf-8")
    except OSError:
        return None  # read-only vault — caller falls back to the legacy slug
    return vid


def _legacy_index_slug(v: Path) -> str:
    return f"{v.name}-{hashlib.sha256(str(v).encode()).hexdigest()[:8]}"


def index_dir(vault: str | os.PathLike[str] | None = None) -> Path:
    """Per-vault app-data directory holding this vault's index + audit chain.

    ``$BRAIN_INDEX_DIR`` overrides completely (returned as-is, no per-vault
    nesting — tests and constrained deployments rely on that). Otherwise the
    dir is keyed on the persistent ``vault-id`` (move-stable, field bug 3) when
    one exists, else on the legacy absolute-path hash — so an existing install
    keeps pointing at its current dir until ``migrate_index_location`` mints an
    id and renames it. Any number of vaults coexist without sharing state.

    NOT ENTIRELY DISPOSABLE (INT-01, INT-04, CAP-02). The index and audit chain
    here are a derived cache, but this dir ALSO holds THREE things that are not:

    * ``cos-approved/`` — owner-accepted COS content waiting for its signature,
      which between the accept and the next drain is the ONLY copy;
    * ``cos-attachment-anchors/`` — the host-signed acceptance anchors for
      attachments already released into ``vault/inbox/``. The payload survives
      losing one, but the anchor is what keeps it at its email-derived MNPI
      floor and what proves the bytes are the accepted ones. Lose it and the
      file is REFUSED at the next drain (it fails closed, never downgrading to
      an unlabelled ``Internal`` drop) and has to be re-accepted.
    * ``cos-corpus/`` — the capture corpus (CAP-02): the actual mail text each
      run read, classified MNPI. Unlike the two above it is EVIDENCE rather
      than pending work, so it is not a drain-first item — but it cannot be
      rebuilt from anything. The nightly ages it out at
      ``$BRAIN_COS_CORPUS_DAYS`` (default 30); deleting this dir or repointing
      ``$BRAIN_INDEX_DIR`` destroys it early, and leaving it behind on an
      uninstall leaves unfiltered mail bodies on disk with nothing left running
      to age them out. Either way it is a
      decision, not a side effect. ``brain status`` reports
      ``cos.capture_corpus``.

    All three are here precisely because the Cowork VM cannot reach here.
    Drain first
    (``brain sync``) before deleting this dir, repointing ``$BRAIN_INDEX_DIR``,
    or uninstalling; ``brain status`` reports
    ``cos.approved_awaiting_signature`` + ``cos.attachment_anchors_awaiting_drain``
    and ``brain rebuild`` returns a ``warning`` in its result while either is
    non-zero.
    """
    override = os.environ.get("BRAIN_INDEX_DIR")
    if override:
        return Path(override).expanduser()
    v = vault_root(vault)
    vid = vault_id(v)
    slug = f"{v.name}-{vid[:8]}" if vid else _legacy_index_slug(v)
    return _app_data_base() / "vaults" / slug


def migrate_index_location(vault: str | os.PathLike[str] | None = None) -> Path | None:
    """Mint the persistent ``vault-id`` and, if this vault's index/audit dir is
    still at the legacy absolute-path-hash location, RENAME it to the id-based
    location — so index + audit chain survive a vault move without a rebuild or
    an audit-chain fork (field bug 3). Host-only; best-effort (a failure just
    leaves the legacy layout in place). No-op when ``$BRAIN_INDEX_DIR`` pins the
    dir. Returns the new dir if a rename happened, else ``None``."""
    if os.environ.get("BRAIN_INDEX_DIR"):
        return None
    v = vault_root(vault)
    legacy = _app_data_base() / "vaults" / _legacy_index_slug(v)
    vid = vault_id(v, create=True)
    if not vid:
        return None
    new = _app_data_base() / "vaults" / f"{v.name}-{vid[:8]}"
    if legacy != new and legacy.exists() and not new.exists():
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            legacy.rename(new)
            return new
        except OSError:
            return None
    return None


def index_path(vault: str | os.PathLike[str] | None = None) -> Path:
    """Absolute path to this vault's SQLite index file."""
    return index_dir(vault) / INDEX_FILENAME


def ensure_index_dir(vault: str | os.PathLike[str] | None = None) -> Path:
    d = index_dir(vault)
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_audit_log(vault: str | os.PathLike[str] | None = None) -> Path:
    """Default per-vault audit-chain path, with a one-time legacy notice.

    Pre-0.3.0 installs kept ONE global index + audit chain directly under the
    app-data base. The index is a disposable cache (just rebuild), but the old
    audit chain must not silently disappear: it stays frozen at the legacy
    path — verifiable there forever — and new writes start a fresh per-vault
    chain (same model as a key rotation, see SECURITY.md).
    """
    log = index_dir(vault) / "audit_chain.jsonl"
    legacy = _app_data_base() / "audit_chain.jsonl"
    if not os.environ.get("BRAIN_INDEX_DIR") and legacy.exists() and not log.exists():
        print(
            f"brain: NOTE — a pre-0.3.0 global audit chain exists at {legacy}. "
            f"It stays there, frozen and verifiable; new writes chain at {log}.",
            file=sys.stderr,
        )
    return log


# --------------------------------------------------------------------------
# file permission policy (hardening pass)
# --------------------------------------------------------------------------
# The derived SQLite index and the published read-only snapshot can carry note
# bodies up to and including MNPI-tier content (the classification gate is an
# egress *decision*, not containment -- see docs/operations/egress-provider-
# posture.md §2). Neither must ever be left world-readable. The snapshot was
# previously chmod'd 0o444 (read-only, but readable by every local account on a
# shared/multi-user machine); the index inherited whatever the process umask
# happened to be (often 0o644 on a typical single-user default). Both are now
# tightened to owner-only immediately after creation, regardless of umask.
SECURE_FILE_MODE = 0o600  # owner rw only; use 0o640 if a deployment intentionally
                           # shares index/snapshot files with a trusted local group


def secure_file_permissions(path: "os.PathLike[str] | str", mode: int = SECURE_FILE_MODE) -> None:
    """Best-effort tighten ``path`` to ``mode`` (default owner-only 0600).

    Never raises: a chmod call that fails (unsupported filesystem, Windows ACL
    semantics where POSIX mode bits are only partially honored, a race where the
    file vanished) must not break index/snapshot creation -- it degrades to
    "as restrictive as the platform default allowed", not a crash.
    """
    try:
        os.chmod(path, mode)
    except OSError:
        pass


class VaultNotFoundError(RuntimeError):
    """The CWD/vault fallback resolved to a path that is not a vault."""


def vault_root(
    explicit: str | os.PathLike[str] | None = None,
    *,
    allow_missing: bool = False,
) -> Path:
    """Resolve the vault root: explicit arg > ``$BRAIN_VAULT`` > CWD/vault.

    The CWD/vault fallback FAILS CLOSED when ``./vault`` is not yet a Brainiac
    vault (no ``./vault/.brain``): brain must never write to a phantom
    ``./vault/.brain/`` in whatever directory it happened to run from. This was
    a stderr WARNING and that was not enough — a warning is invisible to any
    caller that reads the success JSON, so ``cos-propose`` with $BRAIN_VAULT
    unset silently materialised a phantom vault and reported success (and the
    same footgun once scattered 231 drafts into a stray ``migration/vault/``).

    Creation flows (``brain init``, the installer's sample-vault build) pass
    ``allow_missing=True`` — they are the only callers entitled to bring a vault
    into existence. An explicit ``--vault``/``$BRAIN_VAULT`` is a deliberate act
    and is still trusted as given.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("BRAIN_VAULT")
    if env:
        return Path(env).expanduser().resolve()
    cwd_vault = (Path.cwd() / "vault").resolve()
    if not allow_missing and not (cwd_vault / ".brain").is_dir():
        raise VaultNotFoundError(
            f"no --vault/$BRAIN_VAULT given and the CWD/vault fallback "
            f"({cwd_vault}) is not a vault (no {cwd_vault / '.brain'}). Refusing "
            f"to create one implicitly. Pin the vault you meant: "
            f"export BRAIN_VAULT=/path/to/vault (or pass --vault), or run "
            f"`brain init` to create a new vault here."
        )
    return cwd_vault


def vault_slug8(vault: str | os.PathLike[str] | None = None) -> str:
    """The 8-hex per-vault id — the SAME hash the per-vault app-data dir uses
    (see ``index_dir``). One vault => one stable id, distinct vaults => distinct
    ids, so per-vault artifacts (index, audit chain, nightly task) never collide."""
    return hashlib.sha256(str(vault_root(vault)).encode()).hexdigest()[:8]


def nightly_label(vault: str | os.PathLike[str] | None = None) -> str:
    """launchd label (macOS) for this vault's nightly maintenance task, made
    PER-VAULT so two registered vaults don't install to one shared label and
    clobber each other's job. The legacy single label
    ``com.profile-a-brain.daily-brief`` is migrated away from on next install."""
    return f"com.brainiac.nightly.{vault_slug8(vault)}"


# --------------------------------------------------------------------------
# workspace runtime locations (S06 — Cowork-Windows workspace-install path)
# --------------------------------------------------------------------------
# The Cowork Linux VM mounts ONLY the workspace and sees ``vault/.brain/``. The
# runtime dir holds the per-arch ``brain`` binary, the bundled ``model.onnx``,
# the read-only published ``snapshot/`` the VM reads, and the writable
# ``capture-inbox/`` the VM drops drafts into. All four resolve from env first so
# a workspace install can point them at a workspace-root ``.brain/`` if desired;
# the default keeps everything under the gitignored ``vault/.brain/`` (spec §2),
# which ``notes.scan_vault`` already excludes from indexing.
def brain_runtime_dir(vault: str | os.PathLike[str] | None = None) -> Path:
    override = os.environ.get("BRAIN_RUNTIME_DIR")
    if override:
        return Path(override).expanduser()
    return vault_root(vault) / ".brain"


def snapshot_dir(vault: str | os.PathLike[str] | None = None) -> Path:
    """Dir holding the read-only published snapshot (DB + manifest)."""
    override = os.environ.get("BRAIN_SNAPSHOT_DIR")
    if override:
        return Path(override).expanduser()
    return brain_runtime_dir(vault) / "snapshot"


def snapshot_db_path(vault: str | os.PathLike[str] | None = None) -> Path:
    """Absolute path to the read-only snapshot DB the VM ``brain`` reads."""
    from .snapshot import SNAPSHOT_DB

    return snapshot_dir(vault) / SNAPSHOT_DB


def capture_inbox_dir(vault: str | os.PathLike[str] | None = None) -> Path:
    """Writable dir the VM drops capture drafts into (host drains it on invoke).

    Lives under ``.brain/`` so it is host-visible on the shared mount AND
    excluded from ``scan_vault`` — a draft is never auto-indexed; only the host
    promotes it (sign + index) via drain-on-invoke.
    """
    override = os.environ.get("BRAIN_CAPTURE_INBOX")
    if override:
        return Path(override).expanduser()
    return brain_runtime_dir(vault) / "capture-inbox"


def memory_dir(vault: str | os.PathLike[str] | None = None) -> Path:
    """Session-memory dir (ADR-0003 Ruling 4, MEM-01/02) — handoff.md, hot.md,
    lessons.md, archive/. Host-only, never indexed (under ``.brain/``)."""
    return brain_runtime_dir(vault) / "memory"


def brief_dir(vault: str | os.PathLike[str] | None = None) -> Path:
    """Generated HTML brief/digest dir (AUT-01/AUT-03, ADR-0003 Ruling c) —
    gitignored, local, snapshot-adjacent (under ``.brain/``). HOST-ONLY: never
    committed, never published into the VM snapshot."""
    return brain_runtime_dir(vault) / "brief"


def recommendations_open_path(vault: str | os.PathLike[str] | None = None) -> Path:
    """Open-recommendations JSONL (MEM-03) — one JSON object per line, lifecycle
    ``open -> surfaced -> (resolved, removed here + logged)``."""
    return memory_dir(vault) / "recommendations-open.jsonl"


def recommendations_log_path(vault: str | os.PathLike[str] | None = None) -> Path:
    """Resolved-recommendations log (MEM-03) — append-only Markdown, one dated
    entry per closed recommendation."""
    return memory_dir(vault) / "recommendations-log.md"


def maintain_state_path(vault: str | os.PathLike[str] | None = None) -> Path:
    """Per-branch ``brain maintain`` state (ADR-0003 Ruling 5/d) — ONE file
    serving both the catch-up last-run markers and the heartbeat (last
    attempt/status/consecutive-failures per branch). Read by
    ``.claude/hooks/session-start.sh`` for the stale-nightly warning."""
    override = os.environ.get("BRAIN_MAINTAIN_STATE")
    if override:
        return Path(override).expanduser()
    return brain_runtime_dir(vault) / "maintain-state.json"


def maintain_lock_path(vault: str | os.PathLike[str] | None = None) -> Path:
    """Single-runner lock for ``brain maintain`` (HARDENED:codex) — a second
    concurrent run skips with a logged notice instead of racing the first."""
    return brain_runtime_dir(vault) / "maintain.lock"


class HostPathUnsafe(RuntimeError):
    """A path that must be host-private resolves somewhere a Cowork VM can see.

    Fail closed rather than use it — the same posture ``querylog`` takes when
    ``$BRAIN_INDEX_DIR`` is pointed into the vault. ``cos.ApprovedQueueUnsafe``
    subclasses this so the one rule has one exception type."""


#: Host-side declaration of the mount root(s), highest precedence. ``os.pathsep``
#: -separated, like every other multi-path env in this engine.
WORKSPACE_ROOT_ENV = "BRAIN_WORKSPACE_ROOT"

#: The host-private workspace registry the install/cowork-setup skills write
#: (``tools/workspace_registry.py``). It lives in the operator's home, NOT in
#: any workspace, so a VM cannot edit it — which is the whole reason the mount
#: boundary is read from here rather than from staging files on the mount.
_REGISTRY_HOME_ENV = "BRAINIAC_HOME"


def _declared_workspace_roots(vroot: Path) -> list[Path]:
    """Mount roots THIS HOST declares for ``vroot`` — never vault content.

    ``$BRAIN_WORKSPACE_ROOT`` wins outright when set; otherwise every
    ``workspace_path`` the workspace registry records for this vault. A
    missing/unreadable registry contributes nothing (the marker fallback in
    ``vm_visible_roots`` still applies) — it can only ever ADD roots."""
    raw = os.environ.get(WORKSPACE_ROOT_ENV, "")
    declared = [p for p in (part.strip() for part in raw.split(os.pathsep)) if p]
    if declared:
        return [Path(p).expanduser() for p in declared]
    home = os.environ.get(_REGISTRY_HOME_ENV) or str(Path.home() / ".brainiac")
    try:
        import json

        data = json.loads((Path(home).expanduser() / "workspaces.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[Path] = []
    for entry in (data.get("entries") if isinstance(data, dict) else None) or []:
        if not isinstance(entry, dict) or not entry.get("workspace_path"):
            continue
        try:
            if Path(str(entry.get("vault_path"))).expanduser().resolve() != vroot:
                continue
        except (OSError, ValueError, TypeError):
            continue
        out.append(Path(str(entry["workspace_path"])).expanduser())
    return out


def vm_visible_roots(vault: str | os.PathLike[str] | None = None) -> list[Path]:
    """Every directory tree a Cowork VM session can reach on this host.

    Always the vault itself and its runtime dir. "Outside the vault" is not the
    same claim as "outside the mount": ``$BRAIN_INDEX_DIR`` can name a sibling
    of ``vault/`` inside the same mounted workspace and clear a vault-only check
    while sitting squarely on the mount. So the WORKSPACE ROOT counts too, and
    it comes from HOST-ONLY configuration — ``$BRAIN_WORKSPACE_ROOT`` or the
    workspace registry in the operator's home — because it is security
    evidence and the mount is the untrusted side.

    The workspace-install staging markers (``.brain/bin``, ``.brain/model``,
    ``.brain/vendor``) remain a fallback for an unregistered staged vault, but
    they may only ever WIDEN this set. Round 3 found the bug in the other
    direction: with the markers as the ONLY signal, a VM that deleted its own
    ``.brain/vendor`` shrank this list to ``[vault, runtime]``, after which a
    sibling ``$BRAIN_INDEX_DIR`` inside the same mount passed
    ``proven_off_mount`` — the exact case this arm exists to catch — and the
    writer lock, the anchor store and the approved queue all landed back on the
    mount. Attacker-writable file existence is not evidence; host configuration
    is. What stays open, recorded in ``docs/cos-ops.md``: a staged workspace
    that was never registered AND has had its markers deleted has no host-side
    boundary left to read, so it falls back to vault-only.

    ONE definition (it lives here, not in ``cos``, because ``config`` is what
    resolves the single-writer lock and cannot import ``cos``)."""
    vroot = vault_root(vault).resolve()
    runtime = brain_runtime_dir(vault).expanduser().resolve()
    roots = [vroot, runtime]
    for declared in _declared_workspace_roots(vroot):
        try:
            roots.append(declared.resolve())
        except OSError:
            continue
    if any((runtime / marker).exists() for marker in ("bin", "model", "vendor")):
        # A staged workspace: the mount root is the dir holding the vault (and,
        # for a relocated $BRAIN_RUNTIME_DIR, the one holding that).
        roots += [vroot.parent, runtime.parent]
    return roots


def proven_off_mount(d: Path, vault: str | os.PathLike[str] | None = None, *,
                     what: str) -> Path:
    """``d`` resolved and PROVEN outside every VM-visible root, or refused.

    Raises :class:`HostPathUnsafe` otherwise — a misconfigured
    ``$BRAIN_INDEX_DIR``, or a symlink that lands back on the mount
    (``resolve()`` follows those). ONE implementation: the approved queue, the
    attachment-anchor store (INT-04) and the single-writer lock (INT-05) all
    route through it, and a second copy of a verification rule is how the first
    one ends up subtly weaker."""
    d = d.expanduser().resolve()
    for root in vm_visible_roots(vault):
        if d == root or root in d.parents:
            raise HostPathUnsafe(
                f"{what} {d} resolves inside {root}, which a Cowork VM "
                f"session can reach. Point $BRAIN_INDEX_DIR at a host-only path "
                f"outside the workspace.")
    return d


def host_private_base() -> Path:
    """The host-controlled root for state a Cowork VM must not reach at all.

    An explicit ``$BRAIN_INDEX_DIR`` (host CONFIGURATION, not vault content)
    else the per-user app-data base. Deliberately NOT ``index_dir``, which is
    keyed on the mount-resident ``.brain/vault-id`` — a file the VM can
    rewrite. ONE definition, shared by the COS approved queue, the per-ledger
    append locks and the single-writer lock below.

    ``$BRAIN_INDEX_DIR`` is host-wide configuration and MUST be set identically
    in every context that writes a given vault (see ``docs/cos-ops.md`` INT-05).
    That is not a second identity for the lock: the lock protects the sqlite
    index, ``$BRAIN_INDEX_DIR`` IS where that index lives, so lock and index can
    never disagree about which index is being protected — two contexts with
    different values are writing different indexes."""
    override = os.environ.get("BRAIN_INDEX_DIR")
    return Path(override).expanduser() if override else _app_data_base()


def host_lock_dir(vault: str | os.PathLike[str] | None = None, *,
                  create: bool = False) -> Path:
    """The off-mount directory every host lockfile lives in (0700).

    Resolution ONLY by default. A name-resolution function that mkdirs and
    chmods as a side effect materialises host state from any caller that merely
    wanted the path — including the read-side ``update-probe`` liveness check —
    so creation is the acquisition path's job (``brain.lock.writer_lock``).

    The resolved directory is PROVEN off every VM-visible root: an override (or
    a symlink) landing inside the mounted workspace would put the lockfile back
    under VM control, where the locked inode can be unlinked and replaced and
    ``flock`` stops excluding anything.

    When ``$BRAIN_INDEX_DIR`` is misconfigured onto the mount the lock does NOT
    follow it there and does not take the whole write path down with it: it
    falls back to the per-user app-data base, which is host-controlled by
    construction. Refusing outright would be fail-closed for the lock and
    fail-BROKEN for everything else — the approved queue already refuses that
    misconfiguration on its own (``cos.approved_queue_root``), and a lock that
    is unconditionally off the mount is the property INT-05 needs. If even the
    app-data base resolves onto the mount, that is unrecoverable and raises.

    The fallback is DIAGNOSED, not silent — but by ``warn_if_lock_dir_fallback``
    below, called from the ACQUISITION path, for the same reason creation lives
    there: a plain name resolution must leave no trace."""
    try:
        return _ensure(proven_off_mount(host_private_base() / "locks", vault,
                                        what="host lock dir"), create)
    except HostPathUnsafe:
        return _ensure(proven_off_mount(_app_data_base() / "locks", vault,
                                        what="host lock dir (app-data fallback)"),
                       create)


#: (vault, reason) pairs already reported in THIS process. Marked BEFORE the
#: defect is written: ``cos.log_defect`` appends under an append lock, which
#: resolves through ``host_lock_dir`` again.
_LOCK_FALLBACK_REPORTED: set[tuple[str, str]] = set()


def warn_if_lock_dir_fallback(vault: str | os.PathLike[str] | None = None) -> None:
    """Diagnose an index directory that pushed the writer lock to app-data.

    Round 3: the fallback keeps the LOCK off the mount, but ``index_path()``
    still points at the mounted sqlite — so the write path continues against a
    VM-writable index under a host lock. That was visible only as an
    approved-queue refusal on some other code path. Now the lock's own
    acquisition says it, once per (process, reason). Never raises: a diagnosis
    that can break the write path is worse than the thing it diagnoses."""
    try:
        proven_off_mount(host_private_base() / "locks", vault,
                         what="host lock dir")
        return
    except HostPathUnsafe as exc:
        reason = str(exc)
    except Exception:  # noqa: BLE001
        return
    key = (str(vault or os.environ.get("BRAIN_VAULT", "")), reason)
    if key in _LOCK_FALLBACK_REPORTED:
        return
    _LOCK_FALLBACK_REPORTED.add(key)
    try:
        from . import cos

        cos.log_defect(
            vault, "host-lock-dir-fallback",
            f"$BRAIN_INDEX_DIR is on the mount, so the writer lock fell back to "
            f"the app-data base: {reason} The INDEX itself did NOT move — it is "
            f"still the VM-writable path — so repoint $BRAIN_INDEX_DIR at a "
            f"host-only directory.")
    except Exception:  # noqa: BLE001 — diagnosis must never break the lock path
        pass


def _ensure(d: Path, create: bool) -> Path:
    if create:
        d.mkdir(parents=True, exist_ok=True)
        secure_file_permissions(d, 0o700)
    return d


def writer_lock_path(vault: str | os.PathLike[str] | None = None) -> Path:
    """Single-writer advisory lock (CC-02) between the hourly scheduled job
    and a hand-run CLI write — covers sync/rebuild/maintain/snapshot/restore,
    ALL of which mutate the same index file. Distinct from
    ``maintain_lock_path`` (that one is a single-*maintain*-runner lock, a
    narrower concept); this one gates the index file itself. NEVER created on
    a read path or the VM leg.

    OFF THE MOUNT (INT-05). It used to be ``<vault>/.brain/writer.lock``,
    beside the vault a Cowork VM session can write: unlink the inode while one
    holder has it, drop a replacement at the same name, and a second holder
    locks the NEW inode and runs concurrently — two writers on one sqlite
    index, which is the exact exposure that moved the per-ledger append lock
    off the mount first. Open-time checking cannot fix that; being unreachable
    can. Keyed by vault identity so two vaults never share one lock."""
    return host_lock_dir(vault) / f"writer-{vault_slug8(vault)}.lock"


def supersede_journal_path(vault: str | os.PathLike[str] | None = None) -> Path:
    """The crash journal for an unfinished ``supersede``/``unsupersede``.

    OFF THE MOUNT (ENF-01, adversarial review round 3, 2026-08-10). It used to
    be ``<vault>/.brain/supersede-pending.json``, and it does not hold a hash
    or a marker — it holds BOTH NOTES' COMPLETE PRE-IMAGES, because rolling a
    half-written version chain back means writing those bytes again. On the
    VirtioFS mount that is unrestricted note text — Confidential, Restricted
    or MNPI — sitting outside the classification egress gate, readable by the
    untrusted Cowork VM leg. It was a narrow window while a failed journal was
    deleted on sight; making an unparseable journal PERSIST (the round-2 fix)
    turned the window into "until a human notices".

    Same treatment and same helper as the COS approved queue (INT-01), the
    attachment acceptance anchors (INT-04), the single-writer lock (INT-05)
    and the drift dispositions above: being unreachable is the control.
    Falls back to the app-data base exactly as ``host_lock_dir`` does when
    ``$BRAIN_INDEX_DIR`` is misconfigured onto the mount — the fallback is
    host-controlled by construction, so the confidentiality property holds,
    and refusing outright would take every supersession down with it.
    Keyed by vault identity so two vaults never share one journal."""
    try:
        d = proven_off_mount(host_private_base() / "supersede", vault,
                             what="supersede crash journal store")
    except HostPathUnsafe:
        d = proven_off_mount(_app_data_base() / "supersede", vault,
                             what="supersede crash journal store (app-data fallback)")
    return d / f"supersede-pending-{vault_slug8(vault)}.json"


def cos_run_records_dir(vault: str | os.PathLike[str] | None = None) -> Path:
    """The COS run manifest / validity / plan-binding store.

    OFF THE MOUNT (gap-05, 2026-08-16). It used to be
    ``<vault>/.brain/cos/host/runs`` — described in three separate docstrings as
    "host-private" and "never VM-writable", which was true of the VM's RULES
    (AGENTS.md §9: no VM_ALLOWED verb writes under ``.brain/``) and false of the
    FILESYSTEM: that path is inside the VirtioFS workspace, and
    ``docs/cos-ops.md`` §2c had already ruled the same directory out for the
    approved queue in as many words — "``.brain/cos/host/`` would NOT do: it is
    visible on that mount, and ``0700`` is only a boundary if the VM runs as a
    different uid AND VirtioFS honours mode bits (neither is established)".

    Three files live here and each is an authority a run is judged BY, not an
    artifact a run writes:

    * ``<run>.json`` — the manifest, which freezes which bundle, which commit
      and which capability digest the run was allowed to be;
    * ``<run>.validity.json`` — the recorded verdict, and ``CLAIMABLE_VERDICTS``
      reads it to decide whether that night's candidates may be bound;
    * ``_cos_plan_binding_<run>.json`` — the record of WHICH frozen plan the
      apply dispatched, the one control standing between a rebuilt plan and a
      clean K1 verdict, and (s10) the remaining forgery path against the
      mutation counters.

    Same treatment, same helper and same fallback as the writer lock (INT-05),
    the supersede journal (ENF-01) and the drift dispositions: being
    unreachable is the control, not a mode bit on a mount that may only
    partially honour POSIX bits. Keyed by vault identity so two vaults never
    share one store."""
    try:
        d = proven_off_mount(host_private_base() / "cos-runs", vault,
                             what="COS run record store")
    except HostPathUnsafe:
        d = proven_off_mount(_app_data_base() / "cos-runs", vault,
                             what="COS run record store (app-data fallback)")
    return d / vault_slug8(vault)


# The crash journal lived at ``brain_runtime_dir(vault)/supersede-pending.json``
# until 2026-08-10, and there is deliberately NO resolver for it any more
# (adversarial review round 4). That path is on the VirtioFS mount the
# untrusted Cowork leg can write, the journal's pre-images are replayed through
# the audited ``write_note``, and the unattended hourly ``maintain`` calls the
# recovery path — so a reader for it is an unsigned host write command, not a
# migration. A probe proved it: a planted journal downgraded two MNPI notes to
# Public and signed them into the audit chain. Nothing was pending anywhere on
# the reference host when the reader was removed, so nothing was stranded.


def audit_drift_dispositions_dir(vault: str | os.PathLike[str] | None = None) -> Path:
    """Directory holding the INT-02 drift-triage files. Resolution only — the
    writing path creates it, same convention as ``host_lock_dir``."""
    return proven_off_mount(host_private_base() / "audit-drift", vault,
                            what="audit drift disposition store")


def audit_drift_dispositions_path(vault: str | os.PathLike[str] | None = None) -> Path:
    """The file that decides whether post-signing content drift is EXPLAINED.

    OFF THE MOUNT (2026-08-07, Codex cloud security review). It used to be
    ``<vault>/.brain/audit-drift-dispositions.json``, on the VirtioFS mount a
    Cowork VM session can write. That made it a tamper-suppression authority
    sitting in reach of the untrusted leg: a disposition matches on path, issue
    and OBSERVED hash — every one of which is known to whoever just edited the
    note — so adding one record drives ``drift_summary()['unexplained']`` to 0,
    and ``verify_audit`` keeps reporting ``ok`` while a signed note's bytes have
    changed. The signature check cannot catch it; the whole point of the content
    pass was to catch what signatures cannot.

    Same treatment, same reason, and now the same helper as the COS approved
    queue (INT-01), the attachment acceptance anchors (INT-04) and the
    single-writer lock (INT-05): being unreachable is the control, not a
    permission bit on a mount that "may only partially honour POSIX bits".

    Raises :class:`HostPathUnsafe` when the resolved location is VM-visible.
    Callers must treat that as "nothing is explained" — never as a clean bill.
    Keyed by vault identity so two vaults never share one triage file."""
    return audit_drift_dispositions_dir(vault) / f"dispositions-{vault_slug8(vault)}.json"


def graph_dir(vault: str | os.PathLike[str] | None = None) -> Path:
    """GRF-01 discovery-graph runtime artifacts (ADR-0003 Ruling 6/(a)) —
    gitignored, host-only, never published into the VM snapshot. Holds the
    published ``graph.json`` + its corpus-drift ``manifest.json``, and (only on
    a failed/partial build) a ``BUILD_FAILED.json`` marker written to a path
    SEPARATE from the consumable ``graph.json`` (HARDENED:codex)."""
    return brain_runtime_dir(vault) / "graph"


def graph_manifest_path(vault: str | os.PathLike[str] | None = None) -> Path:
    """Per-note content-hash manifest the drift gate compares against."""
    return graph_dir(vault) / "manifest.json"


def graph_json_path(vault: str | os.PathLike[str] | None = None) -> Path:
    """The published, non-authoritative discovery graph."""
    return graph_dir(vault) / "graph.json"


def graph_build_failed_marker_path(vault: str | os.PathLike[str] | None = None) -> Path:
    """A failed/partial build's marker — NEVER the consumable ``graph.json``
    path, so a partial build can never be mistaken for a valid publish."""
    return graph_dir(vault) / "BUILD_FAILED.json"


def _health_history_root(vault: str | os.PathLike[str] | None = None) -> Path:
    """Directory holding ``health-history.jsonl`` + its lock + archive
    segments. Honors ``$BRAIN_HEALTH_HISTORY`` (the override's PARENT dir
    becomes this root) so the lock and archive dir never drift from the
    actual history file location — fix for review finding [5]: they used to
    always resolve under ``brain_runtime_dir`` even when the history file
    itself was overridden elsewhere (e.g. in tests), so a test pointing
    ``$BRAIN_HEALTH_HISTORY`` at a tmp path still rotated/locked against the
    real vault's ``.brain/`` dir."""
    override = os.environ.get("BRAIN_HEALTH_HISTORY")
    if override:
        return Path(override).expanduser().parent
    return brain_runtime_dir(vault)


def health_history_path(vault: str | os.PathLike[str] | None = None) -> Path:
    """OBS-01 per-run health-metrics JSONL — one record per ``maintain`` run.
    Rotated at ~1MB into ``health_archive_dir``."""
    override = os.environ.get("BRAIN_HEALTH_HISTORY")
    if override:
        return Path(override).expanduser()
    return brain_runtime_dir(vault) / "health-history.jsonl"


def health_history_lock_path(vault: str | os.PathLike[str] | None = None) -> Path:
    """Dedicated short-lived exclusive lock serializing append+rotation
    (OBS-01 correction 3) — separate from ``maintain_lock_path`` because a
    stale/broken maintain lock must never also jam health-history writes."""
    return _health_history_root(vault) / "health-history.lock"


def health_archive_dir(vault: str | os.PathLike[str] | None = None) -> Path:
    """Rotated health-history segments — ``.brain/archive/health-history-*.jsonl``
    (or alongside an overridden ``$BRAIN_HEALTH_HISTORY`` file's own dir)."""
    return _health_history_root(vault) / "archive"


def health_sparse_path(vault: str | os.PathLike[str] | None = None) -> Path:
    """Never-rotated sidecar holding ONLY the sparse health metrics
    (``golden_score``, ``synthesis_cost_usd``) whenever they are non-null —
    review finding [7]. The main ``health-history.jsonl`` read is bounded to a
    ~14-day window (fix [6]) which would silently truncate a sparse metric's
    prior observation older than that (golden scores land on a >quarterly
    cadence), disabling its regression check. This sidecar grows ~one line per
    week and is trivially small forever, so it is never windowed or rotated —
    ``health_trend`` reads it in full for the sparse comparisons."""
    return _health_history_root(vault) / "health-sparse.jsonl"


def cos_ops_dir(vault: str | os.PathLike[str] | None = None) -> Path:
    """CUT-01E: THE canonical COS operations dir — ``$BRAIN_COS_OPS_DIR`` or
    ``<vault>/.brain/cos``. Host-only by contract (under the gitignored,
    never-indexed, never-exported ``.brain/``), split by PERMISSION into three
    sub-paths (see ``brain.cos``): ``host/`` (host-private), ``shared/``
    (VM-readable projection, host writes), ``drop/`` (VM-writable input, host
    claims). Surfaced by ``brain status --json``."""
    override = os.environ.get("BRAIN_COS_OPS_DIR")
    if override:
        return Path(override).expanduser()
    return brain_runtime_dir(vault) / "cos"


def anchor_dir() -> Path | None:
    """Off-host audit-chain anchor dir (SEC-03), if configured.

    No path lives under the vault by default (anchoring INTO the vault buys
    nothing — see brain.anchor). ``None`` means no anchor is configured; the
    scheduled `integrity`/`maintain` check then has no truncation guarantee
    to fold in and says so explicitly (M-2)."""
    override = os.environ.get("BRAIN_ANCHOR_DIR")
    return Path(override).expanduser() if override else None

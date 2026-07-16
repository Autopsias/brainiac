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

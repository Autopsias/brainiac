"""Host-private and per-surface path resolution (mount-boundary rules)."""
from __future__ import annotations

import os
from pathlib import Path

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
    for root in _config.vm_visible_roots(vault):
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
    return Path(override).expanduser() if override else _config._app_data_base()


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
        return _ensure(proven_off_mount(_config._app_data_base() / "locks", vault,
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
        d = proven_off_mount(_config._app_data_base() / "supersede", vault,
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
        d = proven_off_mount(_config._app_data_base() / "cos-runs", vault,
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

# Parent-namespace binds, deferred past this module's own defs (circular-import
# safety, whichever of brain.config / brain.config_hostpaths loads first).
from . import config as _config  # noqa: E402
from .config import (  # noqa: E402
    brain_runtime_dir as brain_runtime_dir,
    index_dir as index_dir,
    index_path as index_path,
    secure_file_permissions as secure_file_permissions,
    vault_root as vault_root,
    vault_slug8 as vault_slug8,
)

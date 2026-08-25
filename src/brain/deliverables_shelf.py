"""DLV-03 — the deliverables shelf path resolver: bound to a vault, fail-closed.

The shelf is a CONVENIENCE COPY of produced deliverables, kept OUTSIDE
``vault/`` on purpose (ADR-0010) — that is what excludes it from retrieval:
``notes.scan_vault`` walks ``vault.rglob("*.md")``, and a sibling directory is
never inside that walk. There is no exclusion rule to write; this module only
decides WHERE that sibling directory is, and refuses rather than guesses when
the answer is unsafe.

Default target: ``<vault>/../brain-deliverables`` — NOT ``deliverables``. That
rename is a measured decision: a hand-organised ``deliverables/`` folder
already exists beside the only vault this rolls out to, and nesting a
machine-owned tree inside the owner's own folder is not a thing to do quietly. ``$BRAIN_DELIVERABLES_DIR`` overrides the default outright.

Five fail-closed refusals, each returned as an action-required result (never a
write) carrying its own ``notify_key`` — see ``src/brain/remediation.py`` for
the disposition each one resolves to:

* the resolved parent is the user's home directory;
* the resolved parent holds a ``.git`` directory and no explicit override was
  given (this repo's own root is exactly that: an untracked tree of note
  copies there is one ``git add -A`` away from a confidentiality incident);
* the resolved target equals, lies inside, or contains an existing non-empty
  directory the binding registry does not name (that existing folder,
  generalised — the recovery is named IN the refusal text, because a
  crash between the first copy and manifest publication must never wedge the
  shelf permanently);
* no stable vault id can be established (``config.vault_id(..., create=True)``
  can return ``None`` on a read-only vault; that must never resolve to a
  directory literally named ``None``);
* the target's binding names a DIFFERENT vault (two sibling vaults under one
  parent must never interleave writes into one shelf).

THE BINDING RECORD is one host-private registry keyed by the CANONICAL TARGET
PATH — not a per-vault file. A per-vault index directory cannot hold it:
vault B cannot read vault A's index dir, so two vaults resolving to the same
target would each believe they own it. It lives under
``config_hostpaths.host_private_base()`` (the SAME host-private base the
approved queue / writer lock / supersede journal use — never the mount, never
``vault/``), claimed under ``lock.writer_lock`` so a concurrent first run
cannot double-claim.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, config_hostpaths as hostpaths, lock as _lock

#: The shelf's default name, sibling to the vault. Deliberately NOT
#: "deliverables" (see module docstring).
DEFAULT_DIRNAME = "brain-deliverables"
ENV_OVERRIDE = "BRAIN_DELIVERABLES_DIR"

_REGISTRY_SUBDIR = "deliverables"
_BINDINGS_FILENAME = "shelf-bindings.json"
_LOCK_FILENAME = "shelf-bindings.lock"

NOTIFY_HOME_DIR = "deliverables:home_dir_parent"
NOTIFY_GIT_ROOT = "deliverables:git_root_parent"
NOTIFY_SHADOW_CONFLICT = "deliverables:shadow_conflict"
NOTIFY_NO_VAULT_ID = "deliverables:no_vault_id"
NOTIFY_BOUND_ELSEWHERE = "deliverables:bound_elsewhere"

# DLV-10 — the four keys the SYNC half of the shelf can raise. They live here,
# beside the resolver's five, so one module names every alert key the shelf
# owns and `tests/test_remediation_enforcement.py` can enumerate them from a
# constant rather than retyping them.
NOTIFY_REFUSED = "shelf:refused"
NOTIFY_MOVE_CAP = "shelf:move-cap"
NOTIFY_DIVERGED = "shelf:diverged"
NOTIFY_SOLE_COPY = "shelf:sole-copy"

#: Every alert key the deliverables shelf can produce. Read by the remediation
#: enforcement gate; a key added above and not here is a key with no
#: disposition, which is exactly what that gate exists to catch.
NOTIFY_KEYS = (
    NOTIFY_HOME_DIR, NOTIFY_GIT_ROOT, NOTIFY_SHADOW_CONFLICT,
    NOTIFY_NO_VAULT_ID, NOTIFY_BOUND_ELSEWHERE,
    NOTIFY_REFUSED, NOTIFY_MOVE_CAP, NOTIFY_DIVERGED, NOTIFY_SOLE_COPY,
)


@dataclass(frozen=True)
class ShelfRefusal:
    """One fail-closed refusal. Shaped like an ``outcomes["action_required"]``
    item (``finding`` + ``notify_key``) so a caller can append it directly —
    ``maintenance_notify.degradation_findings`` already reads exactly this
    shape."""

    notify_key: str
    finding: str

    def as_action_required(self) -> dict[str, str]:
        return {"finding": self.finding, "notify_key": self.notify_key}


@dataclass(frozen=True)
class ShelfResult:
    path: Path | None
    refusal: ShelfRefusal | None = None

    @property
    def ok(self) -> bool:
        return self.refusal is None


def registry_dir(vault: Any) -> Path:
    """Host-private, off the mount — the same base the approved queue /
    writer lock / supersede journal use, with the same fallback."""
    try:
        return hostpaths.proven_off_mount(
            hostpaths.host_private_base() / _REGISTRY_SUBDIR, vault,
            what="deliverables shelf binding registry")
    except hostpaths.HostPathUnsafe:
        return hostpaths.proven_off_mount(
            config._app_data_base() / _REGISTRY_SUBDIR, vault,
            what="deliverables shelf binding registry (app-data fallback)")


def _bindings_path(vault: Any) -> Path:
    return registry_dir(vault) / _BINDINGS_FILENAME


def _lock_path(vault: Any) -> Path:
    return registry_dir(vault) / _LOCK_FILENAME


def _load_bindings(vault: Any) -> dict[str, Any]:
    try:
        data = json.loads(_bindings_path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store_bindings(vault: Any, bindings: dict[str, Any]) -> None:
    path = _bindings_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    config.secure_file_permissions(path.parent, 0o700)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(bindings, indent=2, sort_keys=True), encoding="utf-8")
    config.secure_file_permissions(tmp, 0o600)
    tmp.replace(path)


def _existing_non_empty_dir(p: Path) -> bool:
    return p.is_dir() and not p.is_symlink() and next(p.iterdir(), None) is not None


def _shadow_conflict(target: Path, vault_parent: Path, home: Path) -> Path | None:
    """The existing directory ``target`` would collide with, or ``None``.

    Equals/contains collapse to one physical check: for ``target`` to
    CONTAIN existing content, ``target`` must already exist — so both
    directions reduce to "target exists and is non-empty" (any entry under
    an existing ``target``, file or subdirectory, trips this, whatever its
    depth). "Lies inside" checks target's IMMEDIATE parent only — never a
    walk to the filesystem root, which would flag ordinary ancestors like
    ``/private`` or ``/Users`` as "existing non-empty directories" and refuse
    every override unconditionally. The immediate parent is skipped outright
    when it equals ``vault_parent`` (a shelf placed directly beside the vault
    — the default — always has a non-empty parent, since it holds the vault;
    that is the ordinary case, never a shadow) or ``home`` (refused
    separately, by the home-dir rule).
    # ponytail: bounded to ONE level of "lies inside" — a target nested TWO
    # or more levels inside an unrelated existing tree (rare for a resolver
    # whose only two shapes are "default sibling" and "one explicit override
    # path") is not caught. Widen to a bounded walk if that shape shows up.
    """
    if _existing_non_empty_dir(target):
        return target
    parent = target.parent
    if parent in (vault_parent, home):
        return None
    if _existing_non_empty_dir(parent):
        return parent
    return None


def _recovery_text(conflict: Path) -> str:
    return (
        f"the existing directory at {conflict} is non-empty and the shelf "
        f"binding registry does not name it as the deliverables shelf. "
        f"Recover by moving it aside (e.g. `mv {conflict} {conflict}.bak`) or "
        f"by pointing ${ENV_OVERRIDE} at a different path, then re-run."
    )


def resolve(vault: str | os.PathLike[str] | None = None) -> ShelfResult:
    """Resolve the shelf directory for ``vault``, or refuse.

    Never writes the shelf itself (no mkdir here — that is the sync fold's
    job); the ONE write this makes is claiming the target in the host-private
    binding registry, so a second vault resolving the same target sees the
    claim rather than racing it.
    """
    vault_path = config.vault_root(vault)
    override = os.environ.get(ENV_OVERRIDE, "").strip()
    explicit_override = bool(override)
    target = (Path(override).expanduser() if explicit_override
              else vault_path.parent / DEFAULT_DIRNAME).resolve()

    home = Path.home().resolve()
    if target.parent == home:
        return ShelfResult(None, ShelfRefusal(
            NOTIFY_HOME_DIR,
            f"deliverables shelf refused: {target} sits directly in the "
            f"user's home directory ({home}). Point ${ENV_OVERRIDE} at a "
            f"non-home path."))

    if not explicit_override and (target.parent / ".git").is_dir():
        return ShelfResult(None, ShelfRefusal(
            NOTIFY_GIT_ROOT,
            f"deliverables shelf refused: {target.parent} is a git working "
            f"tree root, and an untracked copy of note content there is one "
            f"`git add -A` away from a confidentiality incident. Set "
            f"${ENV_OVERRIDE} explicitly to opt into this location, or point "
            f"it somewhere outside the repo."))

    vid = config.vault_id(vault_path, create=True)
    if not vid:
        return ShelfResult(None, ShelfRefusal(
            NOTIFY_NO_VAULT_ID,
            f"deliverables shelf refused: no stable vault id could be "
            f"established for {vault_path} (read-only vault?), and the shelf "
            f"must never resolve to a directory namespaced 'None'. Set "
            f"${ENV_OVERRIDE} once the vault is writable, or make "
            f"{vault_path}/.brain writable so a vault id can be minted."))

    lock_path = _lock_path(vault_path)
    with _lock.writer_lock(lock_path, verb="deliverables-shelf-claim"):
        bindings = _load_bindings(vault_path)
        key = str(target)
        existing = bindings.get(key)
        if isinstance(existing, dict) and existing.get("vault_id"):
            if existing["vault_id"] != vid:
                return ShelfResult(None, ShelfRefusal(
                    NOTIFY_BOUND_ELSEWHERE,
                    f"deliverables shelf refused: {target} is already bound "
                    f"to a different vault ({existing.get('vault_path', '?')}). "
                    f"Point ${ENV_OVERRIDE} at a distinct path for this "
                    f"vault ({vault_path})."))
            return ShelfResult(target)

        conflict = _shadow_conflict(target, vault_path.parent, home)
        if conflict is not None:
            return ShelfResult(None, ShelfRefusal(
                NOTIFY_SHADOW_CONFLICT,
                f"deliverables shelf refused: {_recovery_text(conflict)}"))

        bindings[key] = {"vault_id": vid, "vault_path": str(vault_path)}
        _store_bindings(vault_path, bindings)
        return ShelfResult(target)


def status_block(vault: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """The ``brain status`` fragment: the resolved path, or the refusal."""
    try:
        result = resolve(vault)
    except Exception as exc:  # noqa: BLE001 — status must never crash on this
        return {"error": f"{type(exc).__name__}: {exc}"}
    if result.ok:
        return {"path": str(result.path)}
    assert result.refusal is not None
    return {
        "refused": True,
        "notify_key": result.refusal.notify_key,
        "reason": result.refusal.finding,
    }

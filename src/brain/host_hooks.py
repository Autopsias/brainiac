"""Allowlisted host-only extension seam (ADR-0015 §1.4).

Core ``brain`` modules invoke registered :class:`HostHookProvider` instances
loaded from ``importlib.metadata.entry_points(group="brainiac.host_hooks")``,
ordered deterministically by ``(priority, name)``.

Security gates (fail-closed BEFORE importing any plugin module):
1. ``config.role(role)`` MUST equal ``ROLE_HOST`` (never ``ROLE_VM``).
2. ``BRAINIAC_DISABLE_HOOKS`` MUST NOT equal ``"1"``.
3. An explicit host-private allowlist file (outside any VM-writable mount,
   opened with ``O_RDONLY | O_NOFOLLOW | O_NONBLOCK`` and regular-file + byte
   cap checks) MUST approve the exact ``(name, dist, value)`` triple.
4. Duplicate entry-point names or duplicate ``(dist, value)`` pairs are
   rejected fail-closed BEFORE importing either colliding entry point.
"""
from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from . import config

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "brainiac.host_hooks"
DISABLE_HOOKS_ENV = "BRAINIAC_DISABLE_HOOKS"
ALLOWLIST_ENV = "BRAIN_HOST_HOOKS_ALLOWLIST"
ALLOWLIST_FILENAME = "host-hooks-allowlist.json"
ALLOWLIST_SCHEMA = "brainiac-host-hooks-allowlist/v1"
MAX_ALLOWLIST_BYTES = 64 * 1024

_DIST_NORM_RE = re.compile(r"[-_.]+")
_LAST_REFUSALS: list[dict[str, str]] = []


class HostHookSecurityError(RuntimeError):
    """Raised in strict mode when a hook provider fails pre-import checks."""


class HookAnchorRefused(ValueError):
    """Raised by a hook when an attachment/draft anchor fails verification."""


class HookTemporaryError(RuntimeError):
    """Raised by a hook on a transient/systemic failure (never quarantines)."""


@runtime_checkable
class HostHookProvider(Protocol):
    """Typed contract for host-side extension plugins (ADR-0015 §1.4)."""

    name: str
    priority: int  # default 100

    # 0. Optional CLI & BrainCore extension surfaces
    def cli_groups(self) -> tuple[tuple[Any, ...], tuple[Any, ...]] | None: ...
    def core_mixins(self) -> tuple[type, ...]: ...

    # 1. Config / mount safety
    def private_state_warnings(self, vault: Path) -> list[str]: ...

    # 2. Daily & retention maintenance folds
    def pre_sync_folds(self, core: Any, run: Any) -> None: ...
    def retention_folds(self, core: Any, run: Any) -> None: ...

    # 3. Draft & approved-queue seam
    def drain_sources(self, core: Any) -> list[tuple[str, Path, bool]]: ...
    def undecided_draft_ids(self, vault: Path) -> set[str]: ...
    def validate_and_consume_approved(
        self, core: Any, nid: str, draft_path: Path, dry_run: bool
    ) -> dict[str, Any] | None: ...
    def approval_anchor_still_binds(
        self, core: Any, nid: str, approved_sha: str, pubkey: Any
    ) -> bool: ...
    def refuse_approved_draft(self, vault: Path, draft_path: Path, reason: str) -> None: ...
    def quarantine_bypass_draft(self, vault: Path, draft_path: Path) -> Path | None: ...
    def classify_draft_skip(self, core: Any, skip: dict[str, Any]) -> dict[str, Any] | None: ...
    def count_pending_approved(self, vault: Path) -> int: ...

    # 4. Ingest pipeline byte-anchor seam
    def ingest_released_shas(self, vault: Path, manifest: dict[str, str]) -> set[str]: ...
    def ingest_verify_bytes(
        self, vault: Path, path: Path, sha256: str, raw_bytes: bytes, pubkey: Any
    ) -> dict[str, Any] | None: ...
    def ingest_retire_anchor(self, vault: Path, dest: Any, sha256: str) -> None: ...
    def record_ingest_receipt(self, vault: Path, sha256: str, note_id: str) -> None: ...
    def log_ingest_defect(self, vault: Path, kind: str, detail: str) -> None: ...

    # 5. Status, alerts, doctor, briefing, interview, supersede
    def status_extension(self, core: Any) -> dict[str, Any] | None: ...
    def rebuild_warnings(self, core: Any) -> list[str]: ...
    def vault_alerts(self, vault: Path, today: Any) -> list[dict[str, Any]]: ...
    def doctor_rows(self, context: Any) -> list[dict[str, Any]]: ...
    def brief_hook_liveness(self, core: Any) -> dict[str, Any] | None: ...
    def interview_pending_answers(self, vault: Path, state: dict[str, Any]) -> Any: ...
    def interview_late_candidates(self, vault: Path, candidates: list[Any]) -> list[Any]: ...
    def interview_on_apply(
        self, core: Any, action: str, payload: dict[str, Any]
    ) -> bool | dict[str, Any] | str | None: ...
    def interview_sheet_block(self, vault: Path) -> dict[str, Any] | None: ...
    def handle_declared_supersede(
        self, core: Any, old_id: str, new_id: str, reason: str, declared_by: str
    ) -> dict[str, Any] | None: ...


def normalize_dist_name(name: str) -> str:
    """PEP 503 distribution-name normalization."""
    return _DIST_NORM_RE.sub("-", name).strip("-").lower()


def allowlist_path() -> Path:
    """Host-private path of the explicit hook-provider allowlist file."""
    override = os.environ.get(ALLOWLIST_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return config.app_data_base() / ALLOWLIST_FILENAME


def last_hook_refusals() -> tuple[dict[str, str], ...]:
    """Return pre-import refusals recorded by the most recent load."""
    return tuple(_LAST_REFUSALS)


def _open_allowlist_fd(path: Path) -> int | None:
    """Open allowlist via pinned parent dir_fd with O_NOFOLLOW|O_NONBLOCK."""
    if path.is_symlink():
        raise HostHookSecurityError(f"hook allowlist {path} is a symlink (refused)")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    can_pin = (
        os.name != "nt"
        and hasattr(os, "O_DIRECTORY")
        and os.open in os.supports_dir_fd
    )
    if not can_pin:
        try:
            return os.open(str(path), flags)
        except FileNotFoundError:
            return None
    try:
        dir_fd = os.open(
            str(path.parent),
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        return None
    try:
        return os.open(path.name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    finally:
        os.close(dir_fd)


def _read_allowlist_bytes(path: Path) -> bytes | None:
    """Read allowlist via pinned parent dir_fd, checking ownership and mode."""
    fd = _open_allowlist_fd(path)
    if fd is None:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise HostHookSecurityError(f"hook allowlist {path} is not a regular file")
        if os.name != "nt":
            if st.st_uid != os.getuid():
                raise HostHookSecurityError(
                    f"hook allowlist {path} is not owned by current user (uid={st.st_uid})"
                )
            if st.st_mode & 0o022:
                raise HostHookSecurityError(
                    f"hook allowlist {path} is group/world-writable (mode={oct(st.st_mode & 0o777)})"
                )
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(fd, 8192):
            total += len(chunk)
            if total > MAX_ALLOWLIST_BYTES:
                raise HostHookSecurityError(
                    f"hook allowlist {path} exceeds {MAX_ALLOWLIST_BYTES} bytes"
                )
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _load_allowlist(vault: Path | None = None) -> set[tuple[str, str, str]]:
    """Return approved ``(name, normalized_dist, value)`` triples, or empty set."""
    from . import config_hostpaths

    raw_path = allowlist_path()
    # Always prove the resolved allowlist path sits outside EVERY VM-visible
    # mount registered on this host (not only the currently selected vault).
    path = config_hostpaths.proven_off_all_mounts(
        raw_path, vault, what="host hooks allowlist"
    )
    raw = _read_allowlist_bytes(path)
    if raw is None:
        return set()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HostHookSecurityError(f"malformed hook allowlist {path}: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != ALLOWLIST_SCHEMA:
        raise HostHookSecurityError(f"invalid hook allowlist schema in {path}")
    entries = doc.get("approved")
    if not isinstance(entries, list):
        raise HostHookSecurityError(f"invalid approved list in {path}")
    approved: set[tuple[str, str, str]] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise HostHookSecurityError(f"non-object allowlist entry in {path}")
        name = item.get("name")
        dist = item.get("dist")
        value = item.get("value")
        if not (isinstance(name, str) and name and isinstance(dist, str) and dist
                and isinstance(value, str) and value):
            raise HostHookSecurityError(f"incomplete allowlist entry in {path}: {item!r}")
        approved.add((name, normalize_dist_name(dist), value))
    return approved


def _discover_entry_points() -> list[Any]:
    """Return raw entry points for ``brainiac.host_hooks`` across ALL distributions.

    Iterates ``importlib.metadata.distributions()`` directly rather than
    ``importlib.metadata.entry_points()``, because ``entry_points()`` wraps
    ``distributions()`` in ``_unique()`` which silently hides a second
    distribution sharing the same normalized name.
    """
    out: list[Any] = []
    for dist in importlib.metadata.distributions():
        for ep in dist.entry_points.select(group=ENTRY_POINT_GROUP):
            out.append(ep)
    return out


def _ep_dist_name(ep: Any) -> str:
    dist = getattr(ep, "dist", None)
    raw = getattr(dist, "name", None) if dist is not None else None
    return normalize_dist_name(raw) if isinstance(raw, str) and raw else ""


def _record_refusal(refusals: list[dict[str, str]], ep: Any, reason: str) -> None:
    refusals.append({
        "name": str(getattr(ep, "name", "")),
        "dist": _ep_dist_name(ep),
        "value": str(getattr(ep, "value", "")),
        "reason": reason,
    })
    logger.warning(
        "host_hooks: refused entry point %r (dist=%r value=%r): %s",
        getattr(ep, "name", ""), _ep_dist_name(ep), getattr(ep, "value", ""), reason,
    )


def _filter_approved_entry_points(
    eps: list[Any],
    approved: set[tuple[str, str, str]],
    refusals: list[dict[str, str]],
) -> list[Any]:
    """Reject duplicate or unapproved entry points BEFORE any import occurs."""
    by_name: dict[str, int] = {}
    by_target: dict[tuple[str, str], int] = {}
    for ep in eps:
        name = str(getattr(ep, "name", ""))
        dist = _ep_dist_name(ep)
        value = str(getattr(ep, "value", ""))
        by_name[name] = by_name.get(name, 0) + 1
        by_target[(dist, value)] = by_target.get((dist, value), 0) + 1

    accepted: list[Any] = []
    for ep in eps:
        name = str(getattr(ep, "name", ""))
        dist = _ep_dist_name(ep)
        value = str(getattr(ep, "value", ""))
        if by_name.get(name, 0) > 1:
            _record_refusal(refusals, ep, "duplicate-provider-name")
            continue
        if by_target.get((dist, value), 0) > 1:
            _record_refusal(refusals, ep, "duplicate-provider-target")
            continue
        if not dist:
            _record_refusal(refusals, ep, "missing-distribution-metadata")
            continue
        if (name, dist, value) not in approved:
            _record_refusal(refusals, ep, "not-in-host-allowlist")
            continue
        accepted.append(ep)
    return accepted


def _instantiate_provider(ep: Any) -> tuple[int, str, Any]:
    loaded = ep.load()
    provider = loaded() if callable(loaded) else loaded
    if not hasattr(provider, "name"):
        provider.name = str(getattr(ep, "name", ""))
    if not hasattr(provider, "priority"):
        provider.priority = 100
    raw_priority = getattr(provider, "priority", 100)
    if isinstance(raw_priority, bool):
        raise ValueError(f"invalid provider priority: {raw_priority!r}")
    priority = int(raw_priority)
    name = str(getattr(provider, "name", ""))
    return priority, name, provider


def _gate_refusal(
    label: str, exc: Exception, *, strict: bool, msg: str,
) -> tuple[Any, ...]:
    """Record a pre-load gate failure and fail closed."""
    global _LAST_REFUSALS
    _LAST_REFUSALS = [{"name": label, "dist": "", "value": "", "reason": str(exc)}]
    logger.warning("host_hooks: %s (fail-closed): %s", msg, exc)
    if strict:
        raise HostHookSecurityError(str(exc)) from exc
    return ()


def load_host_hooks(
    *,
    role: str | None = None,
    vault: Path | None = None,
    strict: bool = False,
) -> tuple[Any, ...]:
    """Discover, verify against host allowlist, and load approved providers.

    Fail-closed on ``ROLE_VM``, ``BRAINIAC_DISABLE_HOOKS=1``, missing/invalid
    allowlist, missing distribution metadata, duplicate providers, or
    unapproved providers — all rejected BEFORE importing any plugin module.
    """
    global _LAST_REFUSALS
    _LAST_REFUSALS = []

    if config.role(role) != config.ROLE_HOST:
        return ()
    if os.environ.get(DISABLE_HOOKS_ENV, "").strip() == "1":
        return ()

    try:
        approved = _load_allowlist(vault=vault)
    except Exception as exc:
        return _gate_refusal("(allowlist)", exc, strict=strict, msg="allowlist unavailable")

    if not approved:
        return ()

    try:
        eps = _discover_entry_points()
    except Exception as exc:
        return _gate_refusal("(discovery)", exc, strict=strict, msg="entry-point discovery failed")
    refusals: list[dict[str, str]] = []
    accepted = _filter_approved_entry_points(eps, approved, refusals)
    _LAST_REFUSALS = refusals

    if strict and refusals:
        reasons = "; ".join(f"{r['name']}:{r['reason']}" for r in refusals)
        raise HostHookSecurityError(f"refused host hook provider(s): {reasons}")

    loaded_rows: list[tuple[int, str, Any]] = []
    for ep in accepted:
        try:
            loaded_rows.append(_instantiate_provider(ep))
        except Exception as exc:
            _record_refusal(refusals, ep, f"load-failed:{type(exc).__name__}:{exc}")
            if strict:
                raise
    _LAST_REFUSALS = refusals
    loaded_rows.sort(key=lambda row: (row[0], row[1]))
    return tuple(row[2] for row in loaded_rows)


def load_core_hooks(core: Any) -> tuple[Any, ...]:
    """Load approved host hooks using ``core.role`` and ``core.vault``."""
    if core is None:
        return load_host_hooks()
    raw_vault = getattr(core, "vault", None)
    vault = Path(raw_vault) if isinstance(raw_vault, (str, os.PathLike)) else None
    raw_role = getattr(core, "role", None)
    role = raw_role if isinstance(raw_role, str) else None
    return load_host_hooks(role=role, vault=vault)


def _is_implemented_method(provider: Any, method: str) -> bool:
    """True when ``provider`` defines callable ``method``, not an inherited Protocol stub."""
    fn = getattr(provider, method, None)
    if not callable(fn):
        return False
    stub = HostHookProvider.__dict__.get(method)
    if stub is not None:
        if getattr(fn, "__func__", fn) is stub:
            return False
        if isinstance(provider, type):
            if provider.__dict__.get(method) is stub or getattr(provider, method, None) is stub:
                return False
        elif (
            method not in getattr(provider, "__dict__", {})
            and getattr(type(provider), method, None) is stub
        ):
            return False
    return True


def providers_with(hooks: tuple[Any, ...], method: str) -> tuple[Any, ...]:
    """Return the subset of ``hooks`` that implement callable ``method``."""
    return tuple(p for p in hooks if _is_implemented_method(p, method))


def call_hook_method(
    provider: Any, method: str, *args: Any, default: Any = None, **kwargs: Any,
) -> Any:
    """Invoke ``method`` on ``provider`` if implemented, else return ``default``."""
    if not _is_implemented_method(provider, method):
        return default
    fn = getattr(provider, method)
    return fn(*args, **kwargs)

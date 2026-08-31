"""The six wires that make a vault usable, and the one command that runs them.

``brain provision-local <vault> --workspace <dir>`` exists because every one of
these steps already worked and none of them was reachable as a single act: the
owner pasted six things by hand, or waited for a Cowork session to file a
provisioning request the nightly drain would pick up. The steps are:

1. ``brain init --full --apply`` (scaffold, audit key, nightly registration)
2. the Cowork workspace staging (``tools/cowork_workspace_install.sh``)
3. the ``host`` registry row  4. the ``cowork-vm`` row, ONLY when it staged
5. the per-vault Claude Desktop ``brain-mcp`` entry
6. ``<workspace>/deliverables`` as a nightly sweep source, so a deliverable
   produced in Cowork flows back into the vault inbox

**Idempotent here means CONVERGENT, never short-circuited.** ``_provision_one``
(the drain) returns ``already-registered`` and skips EVERY step once a vault is
in the registry --- right for a drain (PRV-10) and useless as a repair, because
a half-completed run could never be finished. So :func:`wire_vault` checks and
acts per wire and runs all six whatever any of them reports. The drain keeps its
short-circuit around this; ``provision-local`` does not, which is what makes it
the repair path for a sweep list wiped later.

:func:`wire_vault` never raises and never fail-fasts, and it decides no policy
of its own: which failures are fatal, what the summary looks like, whether to
fall back to a plain model copy --- all the caller's, so the drain's report
shape and ``provision-local``'s exit code differ without the wire logic
existing twice. It is not import-free of ``provision``: the drain's building
blocks (``_stage_cowork_runtime``, ``_find_model_source``, ``_staging_root``)
still live there and are reached through call-time imports, which is also what
lets a test monkeypatch them.

Wire 6 itself is in ``sweepdirs`` (:func:`sweepdirs.wire_sweep`) and wire 2 in
``provision_stage`` (:func:`provision_stage._wire_stage`), each with the
primitives it is entirely made of; this module orchestrates them like the other
four.
"""
from __future__ import annotations

import os
import platform
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import workspaces
from .pathkey import canonical, file_lock, real_key, same_path
from .provision_mcp import mcp_server_name, register_mcp
from .provision_stage import STAGE_MANIFEST, _staging_complete, _wire_stage
from .sweepdirs import (DELIVERABLES_DIRNAME, SWEEP_ENV,
                        installed_nightly_plist, installed_sweep_dirs,
                        merge_sweep_dirs, sweep_entries, wire_sweep)

__all__ = ["wire_vault", "provision_local", "sweep_status",
           "installed_nightly_plist",
           "register_mcp", "mcp_server_name", "STAGE_MANIFEST",
           "_staging_complete",
           "WIRE_NAMES", "installed_sweep_dirs", "merge_sweep_dirs",
           "sweep_entries", "canonical", "same_path", "file_lock"]

WIRE_NAMES = {"1": "init", "2": "workspace staging", "3": "host registry row",
              "4": "cowork-vm registry row", "5": "claude desktop mcp entry",
              "6": "deliverables sweep dir"}
# `skipped` is a wire that does not apply on this host (wire 6 off macOS); it
# is NOT a failure and must never colour the exit code.
_OK = ("already", "done", "skipped")
# `launchctl print` on a job that is not registered answers immediately; the
# bound is for a wedged launchd, not for a slow answer.
_LAUNCHCTL_TIMEOUT_S = 15


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# the registry half (wires 3 + 4)
# --------------------------------------------------------------------------

def find_row(entries: list[dict], vault: Path, workspace: Path,
             target: str) -> Optional[dict]:
    """The row for this (host, arch, target, vault, workspace), or None.
    Keyed through ``workspaces._key`` itself --- a lookup that normalised
    differently from the writer would report ``already`` for a row the upsert
    could not find, or miss an NFD spelling of a row stored in NFC."""
    want = (socket.gethostname(), platform.machine(), target,
            real_key(vault), real_key(workspace))
    for entry in entries:
        if workspaces._key(entry) == want:
            return entry
    return None


def _row_current(row: dict, *, model_dir: Optional[str],
                 snapshot_dir: Optional[str]) -> bool:
    """Would an upsert change anything a reader can see?

    ``last_refreshed`` is excluded on purpose: ``upsert_entry`` rewrites it on
    every call, which would make "a second run changes nothing" unprovable.
    ``None`` is "I do not know it" for BOTH optional fields, and is preserved.
    """
    if model_dir is not None and (row.get("model_dir") or "") != model_dir:
        return False
    if snapshot_dir is not None and not same_path(row.get("snapshot_dir") or "",
                                                  snapshot_dir):
        return False
    return True


def _effective_snapshot(vault: Path, workspace: Path, named: Optional[str],
                        registry_path: Path) -> tuple[str, Optional[str]]:
    """``(the one snapshot path, what to store)`` — resolved ONCE, before any
    wire runs, and handed identically to init, the staging and both registry
    writes.

    Precedence: an explicitly NAMED path (``--snapshot-dir`` /
    ``$BRAIN_SNAPSHOT_DIR``), else the path this vault's ``host`` row already
    holds, else the engine default. Two failures shaped the order.

    * Going straight to the engine default split an UPGRADED vault in two: a
      row carrying a custom path and no staging receipt got its snapshot
      re-published to the default while the registry still advertised the
      custom one — the frozen-snapshot failure (R2-3) in a second shape.
    * The env value used to be canonicalised into the REGISTRY value only,
      while ``_stage_cowork_runtime`` re-read the raw env under
      ``cwd=<engine checkout>``, so one relative spelling reached the two sides
      as two different directories.

    The stored half is ``None`` — "I do not know it", preserved under the
    registry lock — only when nothing was named AND nothing is stored, i.e.
    when the resolved value is the engine default a reader computes anyway.
    """
    from . import config as _config

    stored = None
    if not named:
        row = find_row(workspaces.list_entries(registry_path=registry_path),
                       vault, workspace, "host")
        stored = (row or {}).get("snapshot_dir") or None
    resolved = str(canonical(named or stored or _config.snapshot_dir(vault)))
    return resolved, (None if named is None and stored is None else resolved)


def _wire_registry(vault: Path, workspace: Path, target: str, *,
                   registry_path: Path, lock_path: Path,
                   model_dir: Optional[str],
                   snapshot_dir: Optional[str]) -> dict[str, Any]:
    entries = workspaces.list_entries(registry_path=registry_path)
    row = find_row(entries, vault, workspace, target)
    if row is not None and _row_current(row, model_dir=model_dir,
                                        snapshot_dir=snapshot_dir):
        return {"status": "already", "detail": f"{target} row present in {registry_path}"}
    # `model_dir=None` is "I do not know it" and must not BLANK a value some
    # earlier lane recorded. The read-through lives in `upsert_entry`, INSIDE
    # the registry lock, beside the identical `snapshot_dir` branch: preserving
    # from a row read out here was a read-then-write race that overwrote a
    # concurrent writer with the stale value this function had seen.
    workspaces.upsert_entry(
        str(vault), workspace_path=str(workspace), target=target,
        model_dir=model_dir, snapshot_dir=snapshot_dir,
        registry_path=registry_path, lock_path=lock_path)
    return {"status": "done", "detail": f"{target} row written to {registry_path}"}


# --------------------------------------------------------------------------
# the six wires
# --------------------------------------------------------------------------

def nightly_task_registered(vault: Path, plist: Path, *,
                           runner: Optional[Callable[..., Any]] = None) -> bool:
    """Is this vault's nightly job REGISTERED with the machine's scheduler?

    Not "the plist file is there", which is what this asked until the
    2026-08-30 review, and which is false twice over:

    * ``install-brief-mac.sh`` writes the file and THEN bootstraps it. A load
      that fails leaves the file behind, so the next run reported ``already``
      for a vault carrying no job at all.
    * off macOS the file is not the artefact — the host leg registers a
      Windows Scheduled Task — so answering ``True`` there skipped ``init`` on
      precisely the second-machine case this command exists for.

    ``launchctl print gui/<uid>/<label>`` IS the registration: it asks launchd
    rather than the filesystem. Where a platform has no verifier the answer is
    False, never a guess — ``brain init --full --apply`` is idempotent, and
    re-running it costs far less than certifying a job that is not there.
    """
    if platform.system() != "Darwin" or not plist.is_file():
        return False
    from . import config as _config

    label = _config.nightly_label(vault)
    try:
        proc = (runner or subprocess.run)(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True, text=True, timeout=_LAUNCHCTL_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — no launchctl, no launchd, no job
        return False
    return getattr(proc, "returncode", 1) == 0


def vault_is_initialised(vault: Path, plist: Path, *,
                         runner: Optional[Callable[..., Any]] = None) -> bool:
    """Wire 1's OBSERVABLE predicate — what ``already`` means for the init wire.

    NOT just the substrate. ``.brain/vault-id`` is minted by six read-ish lanes
    (``config.migrate_index_location``, the exceptions page, the VM ceiling,
    the deliverables shelf...), and ``brain/`` + ``raw/`` exist in any vault
    cloned from git or copied to a second machine — so those three alone
    called a NEVER-initialised vault ``already wired``, skipped init, and left
    it with no audit signing key and no nightly job on precisely the
    second-machine case this command exists for. The registered nightly task
    is what only ``init --full --apply`` produces, so it joins the predicate.
    """
    return ((vault / ".brain" / "vault-id").is_file()
            and (vault / "brain").is_dir()
            and (vault / "raw").is_dir()
            and nightly_task_registered(vault, plist, runner=runner))


def _wire_init(vault: Path, plist: Path, *, engine: Callable[..., Any],
               runner: Callable[..., Any],
               env_extra: dict[str, str]) -> dict[str, Any]:
    from . import config as _config
    from . import provision

    if vault_is_initialised(vault, plist, runner=runner):
        return {"status": "already",
                "detail": f"{vault}/.brain/vault-id + brain/ + raw/ + "
                          f"the registered nightly job are present"}
    res = engine(vault, ["init", "--full", "--apply", "--json"],
                 timeout=provision._INIT_TIMEOUT_S, runner=runner,
                 env_extra=env_extra)
    if not res.get("ok"):
        why = (res.get("stderr") or res.get("reason")
               or f"exit {res.get('exit')}")
        return {"status": "failed", "init": res,
                "detail": f"brain init --full --apply: {why}"}
    # `init --full --apply` does NOT mint the vault-id -- that is
    # config.migrate_index_location(create=True) from BrainCore, which this
    # filesystem+subprocess init path never constructs, and seeding returns
    # early on a vault that already holds notes (init_samples.py:165-169).
    # Without this the predicate above stays false and every run re-runs init.
    _config.vault_id(vault, create=True)
    for zone in ("brain", "raw"):
        (vault / zone).mkdir(parents=True, exist_ok=True)
    if not (vault / ".brain" / "vault-id").is_file():
        return {"status": "failed", "init": res,
                "detail": f"init reported success but {vault}/.brain/vault-id "
                          f"is still missing"}
    if not nightly_task_registered(vault, plist, runner=runner):
        # `done`, not `failed`: the vault DID index, and failing here would
        # block wires 2/3/5 over a fault that is launchd's. Reported, never
        # swallowed — wire 6 OWNS the nightly, so wire 6 is what fails and
        # names the remedy, and the next run re-runs init rather than reporting
        # `already` forever.
        return {"status": "done", "init": res,
                "detail": "brain init --full --apply — but launchd does not "
                          f"hold this vault's nightly job (plist {plist} "
                          f"{'present but unloaded' if plist.is_file() else 'absent'})"
                          "; see wire 6"}
    return {"status": "done", "init": res, "detail": "brain init --full --apply"}


def _stamp_mcp(vault: Path, workspace: Path, name: str, *,
               registry_path: Path, lock_path: Path) -> None:
    """Record the server name on the host row — once. An existing
    ``mcp_registered_at`` is RETAINED, so a later run writes nothing."""
    row = find_row(workspaces.list_entries(registry_path=registry_path),
                   vault, workspace, "host")
    if row is None:
        return
    if row.get("mcp_server_name") == name and row.get("mcp_registered_at"):
        return
    workspaces.upsert_entry(
        str(vault), workspace_path=str(workspace), target="host",
        # Both `None`s are "I do not know it": preserved under the lock.
        model_dir=None, snapshot_dir=None,
        registry_path=registry_path, lock_path=lock_path,
        extra={"mcp_server_name": name,
               "mcp_registered_at": row.get("mcp_registered_at") or _now_iso()})


def _wire_mcp(vault: Path, workspace: Path, *,
              desktop_config_path: Optional[Path], registry_path: Path,
              lock_path: Path) -> dict[str, Any]:
    res = register_mcp(vault, workspace, config_path=desktop_config_path)
    if res.get("status") == "failed":
        return {"status": "failed", "mcp": res,
                "detail": str(res.get("error", "registration failed"))}
    _stamp_mcp(vault, workspace, res["name"],
               registry_path=registry_path, lock_path=lock_path)
    return {"status": "already" if res["status"] == "already-registered" else "done",
            "mcp": res, "detail": f"{res['name']} -> {res['config']}"}


def sweep_status(vault: str | os.PathLike[str], workspace: str | os.PathLike[str],
                 *, plist_dir: Optional[str | os.PathLike[str]] = None
                 ) -> dict[str, Any]:
    """Wire 6's report, READ-ONLY — no folder created, no plist written.

    The drain returns ``already-registered`` BEFORE any wire runs, so the
    sweep gap it files as `action_required` could only ever be seen on a
    vault's FIRST provisioning — never on the one whose list was wiped later,
    which is the only case there is. This is what the short-circuit calls, so
    the report can actually fire without the drain touching the launchd job it
    is itself running under.
    """
    from . import config as _config

    vault, workspace = canonical(vault), canonical(workspace)
    return _safe(wire_sweep, vault, workspace / DELIVERABLES_DIRNAME,
                 installed_nightly_plist(vault, launch_agents_dir=plist_dir),
                 repair=False, create=False)


# --------------------------------------------------------------------------
# the helper, and the command built on it
# --------------------------------------------------------------------------

def _safe(fn: Callable[..., dict], *a: Any, **kw: Any) -> dict[str, Any]:
    try:
        return fn(*a, **kw)
    except Exception as exc:  # noqa: BLE001 — a wire NEVER raises at its caller
        return {"status": "failed", "detail": f"{type(exc).__name__}: {exc}"}


def wire_vault(vault: str | os.PathLike[str], workspace: str | os.PathLike[str], *,
               model_dir: Optional[str] = None,
               snapshot_dir: Optional[str] = None,
               registry_path: Path, lock_path: Path,
               desktop_config_path: Optional[Path] = None,
               plist_dir: Optional[str | os.PathLike[str]] = None,
               sweep_repair: bool = True,
               runner: Optional[Callable[..., Any]] = None,
               engine: Optional[Callable[..., Any]] = None,
               stager: Optional[Callable[..., Any]] = None,
               ) -> dict[str, dict[str, Any]]:
    """Run all six wires check-then-act. Returns one result per wire, keyed
    ``"1"``..``"6"``, each ``{"status": already|done|skipped|pending|failed,
    "detail": str}``.

    Never raises and never fail-fasts. Four wires DEPEND on another: nothing
    registers a vault whose init failed (the 2026-08-17 claim-more-than-you-
    built defect), and the ``cowork-vm`` row needs the staging to have staged.
    A dependency is not a short-circuit --- a blocked wire is still reported,
    by name, with what blocked it.
    """
    from . import config as _config
    from . import provision

    # ONE canonical spelling, resolved before any wire runs. The installer runs
    # with `cwd=<engine checkout>` while `upsert_entry` resolves against the
    # CALLER's cwd, so a relative or `~` path used to reach the two sides as two
    # different directories --- install and later refresh publishing the
    # snapshot to different places, freezing what the VM reads.
    vault, workspace = canonical(vault), canonical(workspace)
    model_dir = str(canonical(model_dir)) if model_dir else model_dir
    # All three seams resolve at CALL time, never in the signature: a default
    # bound at import keeps pointing at the original through any monkeypatch,
    # which is how a test meaning to fake the engine spawned a real one.
    runner, engine = runner or subprocess.run, engine or provision._run_engine
    deliverables = workspace / DELIVERABLES_DIRNAME
    # The INSTALLED plist, not the computed name --- merging into a fresh
    # canonical file while launchd runs the old one would sweep nothing.
    plist = installed_nightly_plist(vault, launch_agents_dir=plist_dir)
    # FIRST, before anything re-renders the plist: install-brief-mac.sh renders
    # the body from the CALLER's env, so the merged list must travel INTO the
    # init call, not only into the file.
    merged, _ = merge_sweep_dirs(installed_sweep_dirs(plist), deliverables)
    snapshot_dir, reg_snapshot = _effective_snapshot(
        vault, workspace,
        snapshot_dir or os.environ.get("BRAIN_SNAPSHOT_DIR") or None,
        registry_path)

    out: dict[str, dict[str, Any]] = {}
    out["1"] = _safe(_wire_init, vault, plist, engine=engine, runner=runner,
                     env_extra={SWEEP_ENV: merged,
                                "BRAIN_SNAPSHOT_DIR": snapshot_dir})
    ready = out["1"]["status"] in _OK
    blocked = {"status": "failed", "detail": "blocked: wire 1 (init) failed"}

    out["2"] = (_safe(_wire_stage, vault, workspace, model_dir=model_dir,
                      snapshot_dir=snapshot_dir, runner=runner, stager=stager)
                if ready else
                dict(blocked, cowork={"status": "skipped", "reason": "init failed"}))
    out["3"] = (_safe(_wire_registry, vault, workspace, "host",
                      registry_path=registry_path, lock_path=lock_path,
                      model_dir=None, snapshot_dir=reg_snapshot)
                if ready else dict(blocked))
    out["4"] = (_safe(_wire_registry, vault, workspace, "cowork-vm",
                      registry_path=registry_path, lock_path=lock_path,
                      model_dir=str(provision._staging_root(vault, workspace) / "model"),
                      snapshot_dir=reg_snapshot)
                if out["2"]["status"] in _OK else
                {"status": "failed",
                 "detail": f"blocked: wire 2 ({WIRE_NAMES['2']}) — {out['2']['detail']}"})
    out["5"] = (_safe(_wire_mcp, vault, workspace,
                      desktop_config_path=desktop_config_path,
                      registry_path=registry_path, lock_path=lock_path)
                if ready else dict(blocked))
    # Independent of the rest: the sweep dir and the plist repair are about the
    # nightly, not about whether this vault indexed — and wire 6 is the wire
    # that OWNS the nightly, so an unloaded job is its failure to report. Wire 1
    # cannot: it must stay `already`/`done` for a vault that indexed fine, or
    # wires 2/3/5 are blocked on a machine whose only fault is a launchd load —
    # and then `provision-local` exited 0 on a vault with no nightly job at all.
    # `None` when init itself failed: wire 1 already names that, and blaming
    # wire 6 for the job init never got to install is noise.
    out["6"] = _safe(wire_sweep, vault, deliverables, plist, repair=sweep_repair,
                     registered=(nightly_task_registered(vault, plist, runner=runner)
                                 if ready else None))
    return out


def provision_local(vault: str | os.PathLike[str], workspace: str | os.PathLike[str],
                    *, model_dir: Optional[str] = None,
                    snapshot_dir: Optional[str] = None,
                    registry_path: Optional[Path] = None,
                    lock_path: Optional[Path] = None,
                    desktop_config_path: Optional[Path] = None,
                    plist_dir: Optional[str | os.PathLike[str]] = None,
                    runner: Optional[Callable[..., Any]] = None,
                    stager: Optional[Callable[..., Any]] = None,
                    ) -> dict[str, Any]:
    """``brain provision-local`` — the whole report, no printing, no exiting."""
    from . import provision

    vault = canonical(vault)
    Path(workspace).expanduser().mkdir(parents=True, exist_ok=True)
    workspace = canonical(workspace)
    # $BRAINIAC_HOME at CALL time: the workspaces module constants bind at
    # import and are inert to an env pin (provision.drain does the same).
    home = Path(os.environ.get("BRAINIAC_HOME", Path.home() / ".brainiac"))
    registry_path = registry_path or home / "workspaces.json"
    lock_path = lock_path or home / "workspaces.lock"

    if model_dir:
        model = str(canonical(model_dir))
    else:
        found = provision._find_model_source(
            vault, workspaces.list_entries(registry_path=registry_path))
        model = str(found) if found else None

    wires = wire_vault(vault, workspace, model_dir=model, snapshot_dir=snapshot_dir,
                       registry_path=registry_path, lock_path=lock_path,
                       desktop_config_path=desktop_config_path,
                       plist_dir=plist_dir, runner=runner, stager=stager)
    failed = [k for k in sorted(wires) if wires[k]["status"] == "failed"]
    report: dict[str, Any] = {
        "action": "provision-local", "vault": str(vault),
        "workspace": str(workspace), "registry": str(registry_path),
        "wires": wires, "failed_wires": failed, "ok": not failed,
    }
    if wires["6"].get("reload_required"):
        # install-brief-mac.sh reloads launchd itself whenever IT re-rendered
        # the plist; this line is only for the case where we merged the FILE
        # underneath a job that is already loaded.
        report["reload"] = wires["6"]["reload"]
    return report

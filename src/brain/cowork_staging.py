"""Keep note bodies out of the folder a Cowork sandbox attaches (CUT-01/criterion 7).

The property this plan buys is not "the vault was moved once", it is "the vault
is not on the mount". Nothing owned the second one: a second attached folder, a
re-run of ``tools/cowork_workspace_install.sh``, a ``/brainiac-cowork-setup``
restage, a ``/brainiac-update`` pass or a manual copy all put it back, every
skill keeps working, nothing fails, and the closure document goes on asserting a
property that stopped being true.

This module is that owner. The scanner it decides on ---
:func:`brain.cowork_leak_scan.recoverable_artifacts`, and the seven artefact
classes it proves --- lives next door; this module carries the two consumers
built on it:

* :func:`assert_workspace_carries_no_note_bodies`, the REFUSAL, called from every
  staging entry point (``tools/cowork_workspace_install.sh`` via
  ``python3 -m brain.cowork_staging``, and :func:`brain.update_channels
  .stage_engine_and_skills`, which ``/brainiac-update`` and provisioning reach
  directly without going through the shell script);
* :func:`brain.doctor_mount_leak.check_cowork_mount_leak`, the RECURRING check,
  which runs wherever ``brain doctor`` runs (``/brainiac-health`` included).

**On leases.** There is no lease mechanism to be active or abandoned. s05b was
retired by owner ruling on 2026-08-27 (criterion 4's per-session isolation
clause struck; the broker cannot distinguish two Cowork sessions), and s05
built the DECISION half only --- explicitly NOT leases, reapers, timers or
staging roots. So every staged original found inside a workspace is by
definition abandoned: nothing owns it and nothing will reclaim it. That is a
statement about what exists today, not a simplification --- if a lease
mechanism is ever built, this is where the active/abandoned split belongs.
"""
from __future__ import annotations

import os
from pathlib import Path

from .cowork_leak_scan import recoverable_artifacts

# NOT re-exported: `Artifact` and `recoverable_artifacts` have exactly one
# canonical import path, `brain.cowork_leak_scan`. A re-export here would give
# them two, and a test that patches one name would leave the other live --- the
# 2026-08-18 "monkeypatching an alias is not a guard" incident, verbatim.
__all__ = [
    "MountLeak",
    "assert_workspace_carries_no_note_bodies",
    "assert_relocated_vault_leaves_no_note_bodies",
    "vault_is_relocated",
    "staging_root",
]


def staging_root(
    vault: str | os.PathLike[str],
    workspace: str | os.PathLike[str] | None = None,
) -> Path:
    """The ``.brain`` staging directory the Cowork VM runs on.

    THE ONE relocation-aware resolver. Vault and workspace are SEPARATE
    arguments because after the cutover they are separate places: the notes
    live off the mount, and the engine staging the VM executes
    (``engine/``, ``vendor/``, ``bin/``, ``model/``, ``skills/``, ``routines/``,
    ``capture-inbox/`` --- s01 JOB 4's STAYS rows) has to stay ON the mount,
    where the sandbox can reach it. Deriving one from the other is what breaks:

    * ``tools/cowork_workspace_install.sh`` took the VAULT as ``$1`` and
      derived the workspace as ``$VAULT/..`` (line 359);
    * :func:`brain.update_channels.stage_engine_and_skills` named its parameter
      ``workspace_path`` and was CALLED with ``vault_path``.

    Both are correct only while the vault sits one level inside the workspace.

    An EMPTY workspace string is not a workspace. Callers pass ``None`` for
    "co-located"; the registry stores ``""`` for a malformed entry, and
    ``Path("").resolve()`` is the CURRENT DIRECTORY --- so a bare ``""`` here
    would stage the engine into ``<cwd>/vault/.brain`` and point the leak
    refusal at whatever directory the operator happened to be in.
    :func:`brain.update_channels._restage_cowork_workspace` passes
    ``workspace_path or None`` for exactly that reason.

    Co-located (today, and any vault that never moves) resolves to
    ``<vault>/.brain`` --- byte-identical to the previous behaviour, which is
    what ``tests/test_update_restage.py::
    test_restage_workspaces_cowork_vm_stages_engine_at_vault_path_not_workspace_path``
    pins. Relocated resolves to ``<workspace>/vault/.brain``.
    """
    vault_p = Path(vault).resolve()
    if workspace is None:
        return vault_p / ".brain"
    workspace_p = Path(workspace).resolve()
    if vault_p == workspace_p or workspace_p in vault_p.parents:
        return vault_p / ".brain"
    return workspace_p / "vault" / ".brain"


class MountLeak(RuntimeError):
    """A staging call would leave a note body inside an attached workspace."""


def assert_workspace_carries_no_note_bodies(
    workspace: str | os.PathLike[str], *, caller: str
) -> None:
    """Refuse to stage into ``workspace`` while it carries a recoverable note body.

    This is the refusal EVERY staging entry point makes, not just the shell
    script: ``src/brain/update_channels.py`` reaches the Python staging path
    directly, so ``/brainiac-update`` and provisioning would walk straight past
    a guard that lived only in ``tools/cowork_workspace_install.sh``.

    Raises :class:`MountLeak` naming every artefact found, so an operator does
    not have to re-derive the list from a bare refusal.
    """
    leaks = recoverable_artifacts(workspace)
    if not leaks:
        return
    lines = "\n".join(f"  - {a}" for a in leaks)
    raise MountLeak(
        f"{caller}: refusing to stage into {workspace} -- a Cowork sandbox attaches "
        f"this folder and {len(leaks)} artefact(s) inside it still yield note bodies "
        f"(VULN-3385):\n{lines}\n"
        "Move the vault off the mount, publish the snapshot to a host-only "
        "$BRAIN_SNAPSHOT_DIR and keep the derived index under $BRAIN_INDEX_DIR, "
        "then re-run."
    )


def vault_is_relocated(
    vault: str | os.PathLike[str], workspace: str | os.PathLike[str]
) -> bool:
    """True once ``vault`` lives OUTSIDE the folder a Cowork sandbox attaches.

    The whole point of the cutover, expressed as one predicate so the staging
    refusal and its tests read the same rule.
    """
    vault_p = Path(vault).resolve()
    workspace_p = Path(workspace).resolve()
    return not (vault_p == workspace_p or workspace_p in vault_p.parents)


def assert_relocated_vault_leaves_no_note_bodies(
    workspace: str | os.PathLike[str],
    vault: str | os.PathLike[str],
    *,
    caller: str,
) -> None:
    """Refuse to stage a RELOCATED vault into a workspace that still leaks bodies.

    THE REFUSAL every staging entry point makes -- not just the shell script.
    ``src/brain/update_channels.py`` reaches the Python staging path directly,
    so ``/brainiac-update`` and provisioning would walk straight past a guard
    that lived only in ``tools/cowork_workspace_install.sh``.

    Scoped to a RELOCATED vault, deliberately, and this is the load-bearing
    line: while the vault still sits inside the workspace, "a note body is
    inside the attached folder" is the vault ITSELF, and refusing there would
    break ``/brainiac-update`` on every vault that has not cut over yet --- a
    guard that blocks the ordinary case teaches operators to bypass it. That
    state is not unreported: :func:`brain.doctor_mount_leak
    .check_cowork_mount_leak` runs unconditionally and REPORTS it today as
    ``manual-required`` --- visible in ``brain doctor``, and deliberately NOT
    gating, because every vault is in that state until it cuts over and a gate
    that is red everywhere is a gate nobody reads. It becomes gating
    (``stale``) the moment the vault IS relocated and a body is still there.
    This sentence said "fails ``brain doctor`` on it today" until 2026-08-29,
    which described a guard the code does not have. This function owns the
    OTHER half --- once the vault has moved, staging may never put a corpus, a
    snapshot, an index or an abandoned original back on the mount.

    Raises :class:`MountLeak` naming every artefact found, so an operator does
    not have to re-derive the list from a bare refusal.
    """
    if not vault_is_relocated(vault, workspace):
        return
    assert_workspace_carries_no_note_bodies(workspace, caller=caller)


def _main(argv: list[str] | None = None) -> int:
    """``python3 -m brain.cowork_staging --vault V --workspace W``.

    The shell installer's ONE door to this module, doing both jobs in one
    call so bash never re-derives either rule:

    * prints the staging root (:func:`staging_root`) on stdout, so
      ``tools/cowork_workspace_install.sh`` stops computing ``$VAULT/.brain``
      itself and cannot drift from :func:`brain.update_channels
      .stage_engine_and_skills`;
    * exits **2**, printing the artefact list to stderr, when the vault is
      relocated and the workspace still yields note bodies.

    Stdlib-only on purpose: this runs BEFORE the installer has resolved a
    semantic-capable interpreter, on whatever bare ``python3`` is on PATH.
    """
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="brain.cowork_staging")
    ap.add_argument("--vault", required=True, help="the vault directory (holds brain/ raw/)")
    ap.add_argument("--workspace", required=True,
                    help="the folder a Cowork sandbox attaches")
    ap.add_argument("--caller", default="cowork_workspace_install.sh")
    args = ap.parse_args(argv)
    try:
        assert_relocated_vault_leaves_no_note_bodies(
            args.workspace, args.vault, caller=args.caller)
    except MountLeak as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(staging_root(args.vault, args.workspace))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(_main())

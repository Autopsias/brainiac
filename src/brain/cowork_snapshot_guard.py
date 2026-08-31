"""The publish-time containment rule for a workspace snapshot.

This lives in its own module because it is a CONTAINMENT rule, not an update
mechanic: the installer, the update lane and the staging scan all ask the same
question -- would this snapshot land inside the folder Cowork attaches? -- and
one answer in one place is what stops the three lanes drifting apart.
"""
from __future__ import annotations

import os


def snapshot_lands_on_the_mount(
    snapshot_dir: str, vault_path: str, workspace_path: str
) -> str | None:
    """The installer's containment rule, re-applied at PUBLISH time.

    The install-time guard protects the install. Nothing protected the refresh:
    `brain update` walks the registry and runs `sync --publish` with whatever
    `snapshot_dir` the entry holds, comparing it to nothing. A relative value
    (`snapshot`) resolves against whatever CWD the update runs in; an absolute
    one can simply point at the mount. Either way the snapshot -- 3105 note
    bodies on the reference vault, recoverable with `strings` alone -- lands
    inside the attached folder AFTER the staging scan has already reported the
    workspace clean. Raised blocking by the 2026-08-30 adversarial round.

    Returns the offending resolved path, or ``None`` when publishing is safe.

    CO-LOCATED IS EXEMPT, for the same reason the installer exempts it: the
    vault itself lives inside the workspace, so every note body is on the mount
    by construction and refusing the snapshot closes nothing.
    """
    if not workspace_path:
        return None
    ws = os.path.realpath(workspace_path)
    vault = os.path.realpath(vault_path)
    if vault == ws or vault.startswith(ws + os.sep):
        return None                      # co-located: nothing to protect
    if snapshot_dir:
        dest = snapshot_dir
    else:
        # ASK THE ENGINE, do not restate its answer. This line read
        # `os.path.join(vault, ".brain", "snapshot")` until 2026-08-30, which is
        # only the default when `$BRAIN_RUNTIME_DIR` is unset.
        # `config.snapshot_dir()` is `brain_runtime_dir(vault) / "snapshot"`, and
        # `brain_runtime_dir` honours that variable -- a layout `config.py`
        # documents as supported, precisely so a workspace install can point the
        # runtime at a workspace-root `.brain/`. `_workspace_sync` passes the
        # variable straight through to the child, so a guard that hard-coded the
        # path returned "safe" while the child published every note body to
        # `<workspace>/.brain/snapshot` -- on the mount, which is the one outcome
        # this function exists to prevent. Reading it from `config` makes the
        # guard answer the question the child will actually answer.
        from . import config as _config
        dest = str(_config.snapshot_dir(vault))
    dest = os.path.realpath(dest)
    if dest == ws or dest.startswith(ws + os.sep):
        return dest
    return None

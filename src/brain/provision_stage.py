"""Wire 2 — the Cowork runtime staging, and the receipt that certifies it.

Split out of ``provision_wire`` for exactly the reason wire 6 went to
``sweepdirs``: this is a coherent unit (one wire plus the on-disk evidence it
writes and reads), and ``provision_wire`` had reached the 500-line production
ceiling.

The whole module is one idea: **the stager's own word is not evidence.**
``tools/cowork_workspace_install.sh`` copies the engine stamp near its
beginning and the validated model cache hundreds of lines later, so model
validation, the snapshot publish, the verification pins, the skills and the
routines can each die with the stamp already on disk. Both paths through
:func:`_wire_stage` therefore check the ARTEFACTS: the re-use path before
skipping, and the fresh path before writing a receipt.

The receipt itself is HOST-PRIVATE (F102). It used to sit in the staging root,
on the mount a Cowork VM can write, so a VM could forge it and the three
artefact paths and make the drain register a ``cowork-vm`` row for a runtime
nobody staged. Only the host writes it now, right after the host-run stager
returned ``staged`` and its artefacts verified; a file in the staging root
certifies nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import config
from .config_hostpaths import HostPathUnsafe, host_private_base, proven_off_mount
from .pathkey import real_key

STAGE_MANIFEST = "provision-staged.json"


def _stage_receipt(vault: Path, root: Path, workspace: Path) -> Path:
    """Where the receipt for THIS staging root + workspace lives: off the mount.

    Same store shape and app-data fallback as the supersede journal and the
    golden-probes pin; refuses (raises) when even the fallback is VM-visible.
    Keyed by the real path of the root and the workspace, so two workspaces
    never share a receipt."""
    key = hashlib.sha256(
        f"{real_key(root)}\n{real_key(workspace)}".encode()).hexdigest()[:16]
    try:
        d = proven_off_mount(host_private_base() / "cowork-staged", vault,
                             what="cowork staging receipt store")
    except HostPathUnsafe:
        d = proven_off_mount(config._app_data_base() / "cowork-staged", vault,
                             what="cowork staging receipt store (app-data fallback)")
    return d / key / STAGE_MANIFEST


def _stage_artifacts(root: Path, workspace: Path) -> list[Path]:
    """What a COMPLETE staging leaves behind — the EXACT set, fixed here.

    The engine stamp, the validated model cache, and the generated workspace
    contract. ``tools/cowork_workspace_install.sh`` copies ``_version.py`` near
    its beginning and the model cache hundreds of lines later, so any SUBSET is
    a run that died in the middle.
    """
    return [root / "engine" / "brain" / "_version.py", root / "model",
            workspace / "CLAUDE.md"]


def _staging_complete(vault: Path, root: Path, workspace: Path) -> bool:
    """Did a PREVIOUS run finish staging, and is what it left still there?

    The word of record is the HOST-PRIVATE receipt (see the module docstring);
    the mount artefacts below are checked on top of it, because a deleted one
    means the runtime is gone. Never the mount alone: the VM can create every
    path in that list.

    The engine ``_version.py`` stamp alone was not evidence: the installer
    copies it before model validation, snapshot publication, the verification
    pins, the skills and the routines — so a failure in any later phase leaves
    the stamp behind, and skipping on it let wire 4 write a ``cowork-vm`` row
    for a runtime that was never finished.

    Neither is "every path the receipt happens to list". The receipt used to
    record ``[p for p in candidates if p.exists()]``, so a run that copied the
    stamp and then died wrote a receipt naming that ONE file — and this
    predicate then passed on it, certifying the partial run it exists to
    catch. The expected set is computed HERE, from the fixed list above, and
    every member of it must be present.
    """
    try:
        data = json.loads(_stage_receipt(vault, root, workspace)
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError, HostPathUnsafe):
        return False
    if not isinstance(data, dict) or data.get("version") != 1:
        return False
    return all(p.exists() for p in _stage_artifacts(root, workspace))


def _write_stage_receipt(vault: Path, root: Path, workspace: Path) -> None:
    """Write the receipt through an UNPREDICTABLE temp name, then rename.

    The receipt used to be ``<root>/provision-staged.tmp``, a fixed path in a
    directory a Cowork VM can write to, and ``write_text`` FOLLOWS a symlink: a
    link planted there ahead of time pointed the write at any host file the
    VM's user can write, and staging clobbered it. Same class as the plist temp
    file (R2-6), same fix — ``mkstemp`` creates with ``O_EXCL`` under a name
    nothing can predict, so there is no pre-existing path to follow or inherit
    a mode from. The store is host-private now, so this is defence in depth.
    """
    payload = {"version": 1,
               "staged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "staging_root": str(root),
               "artifacts": [str(p) for p in _stage_artifacts(root, workspace)]}
    receipt = _stage_receipt(vault, root, workspace)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    config.secure_file_permissions(receipt.parent, 0o700)
    fd, name = tempfile.mkstemp(dir=str(receipt.parent),
                                prefix=STAGE_MANIFEST + ".", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2) + "\n")
        os.replace(tmp, receipt)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _wire_stage(vault: Path, workspace: Path, *, model_dir: Optional[str],
                snapshot_dir: Optional[str], runner: Callable[..., Any],
                stager: Optional[Callable[..., Any]]) -> dict[str, Any]:
    from . import provision

    root = provision._staging_root(vault, workspace)
    if _staging_complete(vault, root, workspace):
        return {"status": "already", "detail": f"staging receipt intact ({root})",
                "cowork": {"status": "staged",
                           "note": f"{_stage_receipt(vault, root, workspace)} present"}}
    # Looked up at CALL time: a monkeypatch of
    # `provision._stage_cowork_runtime` must reach the CLI path too.
    run = stager or provision._stage_cowork_runtime
    raw = run(vault, Path(model_dir) if model_dir else None, workspace,
              runner=runner, snapshot_dir=snapshot_dir)
    if raw.get("status") == "staged":
        # ...and the fresh path certifies on the ARTEFACTS too, not on the
        # word `staged`. `_staging_complete` guarded only the RE-USE path, so a
        # staging run that reported success while producing nothing got a
        # receipt written for it — and wire 4 then registered a `cowork-vm` row
        # pointing at an empty runtime. No receipt is written for a run whose
        # output is not all there; the next run re-stages instead.
        missing = [str(p) for p in _stage_artifacts(root, workspace)
                   if not p.exists()]
        if missing:
            return {"status": "failed", "cowork": raw,
                    "detail": "the staging reported `staged` but did not "
                              f"produce: {', '.join(missing)}"}
        _write_stage_receipt(vault, root, workspace)
        return {"status": "done", "cowork": raw,
                "detail": f"cowork runtime staged into {workspace}"}
    detail = ("no model source — pass --model-dir <path>" if model_dir is None
              else str(raw.get("reason") or raw.get("stderr") or raw.get("status")))
    return {"status": "failed", "cowork": raw, "detail": detail}

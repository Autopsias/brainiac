"""VM-stage detection and the staged-copy surface checks."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

def looks_like_vm_stage(repo_root: Optional[Path] = None) -> bool:
    """True when this engine copy structurally lacks the host-only inputs
    (no ``tools/workspace_registry.py`` companion script, no ``pyproject.toml``
    SSOT) — i.e. it is a staged zero-install copy, even when role wasn't
    explicitly passed. The staged VM shim (``.brain/brain``) runs
    ``python3 -m brain.cli "$@"`` directly and does not set ``$BRAIN_ROLE``, so
    this structural fallback is what keeps a role-less VM invocation from
    hitting the host-only code path."""
    root = repo_root or _doctor_file().parent.parent.parent
    # POSITIVE signal first: the staged copy is written to
    # `<vault>/.brain/engine/brain/` by tools/cowork_workspace_install.sh, so
    # a `.brain` path component IS the stage -- unambiguous, and true of
    # nothing else.
    #
    # The absence-based test below cannot stand alone: an ORDINARY PyPI wheel
    # in site-packages also lacks tools/ and pyproject.toml, so every
    # pip/uv/pipx install was misdetected as a VM stage and `brain doctor`
    # ran the VM leg -- telling a Windows laptop user to "run brain doctor on
    # the host Mac" and diagnosing a Cowork workspace that did not exist
    # (enterprise pilot, 2026-07-29). Keep it only where it is safe: a copy that is
    # neither an installed package nor a checkout.
    if ".brain" in root.parts or ".brain" in _doctor_file().parts:
        return True
    if _in_site_packages(_doctor_file()):
        return False
    return not (root / "tools" / "workspace_registry.py").exists() and _doctor._ssot_version(root) is None


def _in_site_packages(path: Path) -> bool:
    """True when this module was imported from an installed package tree
    (POSIX ``lib/pythonX.Y/site-packages`` or Windows ``Lib\\site-packages``),
    rather than a source checkout or a staged copy."""
    parts = {p.lower() for p in path.parts}
    return bool(parts & {"site-packages", "dist-packages"})


def _read_version_stamp(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    m = re.search(r'(?m)^__version__ = "([^"]+)"$', text)
    return m.group(1) if m else None


def check_vm_engine_stamp(engine_version: str) -> dict:
    surface = "Engine version (this staged copy)"
    if engine_version.startswith("0.0.0"):
        return _row(surface, STALE, f"brain.__version__ reads {engine_version!r} — stale/pre-stamp stage",
                    remediation="re-stage from the host: tools/cowork_workspace_install.sh",
                    raw={"version": engine_version})
    return _row(surface, CURRENT, f"brain {engine_version}", raw={"version": engine_version})


def check_vm_snapshot(vault: Path) -> dict:
    from . import config
    from .snapshot import snapshot_status

    surface = "Snapshot (read-only, .brain/snapshot)"
    snap_dir = config.snapshot_dir(vault)
    st = snapshot_status(snap_dir)
    if st.get("snapshot") != "present":
        return _row(surface, NOT_DETECTABLE, f"no snapshot published at {snap_dir}",
                    remediation="publish a snapshot on the host (`brain snapshot`) and re-sync the VM mount")
    age_s = st.get("age_seconds") or 0.0
    detail = (f"gen {st.get('generation')} age {st.get('age_human')} "
              f"({st.get('notes')} notes / {st.get('chunks')} chunks)")
    if age_s > 48 * 3600:
        return _row(surface, STALE, f"{detail} — older than 48h",
                    remediation="publish a fresh snapshot on the host (`brain snapshot`) and re-sync the VM mount",
                    raw=st)
    return _row(surface, CURRENT, detail, raw=st)


def check_vm_model_cache(vault: Path) -> dict:
    from . import config

    surface = "Model cache (.brain/model)"
    model_dir = Path(os.environ.get("BRAIN_MODEL_CACHE") or (config.brain_runtime_dir(vault) / "model"))
    if not model_dir.is_dir() or not any(model_dir.iterdir()):
        return _row(surface, STALE,
                    f"{model_dir} missing/empty — the VM has no HF egress, so semantic search "
                    "silently falls back to hash embeddings without this",
                    remediation="re-stage from the host: tools/cowork_workspace_install.sh")
    # A dangling symlink is not is_file(): an HF-cache snapshot staged without
    # dereferencing (field finding 2026-07-20, F1) looks "present" while every
    # file points at a blobs/ dir that was never copied.
    dangling = [p for p in model_dir.rglob("*") if p.is_symlink() and not p.exists()]
    if dangling:
        return _row(surface, STALE,
                    f"{model_dir} has {len(dangling)} dangling symlink(s) (e.g. {dangling[0].name}) — "
                    "the HF-cache snapshot was staged without dereferencing its blobs/ links",
                    remediation="re-stage with resolved copies: cp -RL <hf-snapshot>/. into the model dir "
                                "(tools/cowork_workspace_install.sh now does this)")
    n_files = sum(1 for p in model_dir.rglob("*") if p.is_file())
    if not any(p.name.startswith("model") and p.suffix == ".onnx" and p.stat().st_size > 1_000_000
               for p in model_dir.rglob("*.onnx")):
        return _row(surface, STALE,
                    f"{model_dir} present ({n_files} file(s)) but no model*.onnx >1MB — cache is incomplete",
                    remediation="re-stage from the host: tools/cowork_workspace_install.sh")
    return _row(surface, CURRENT, f"{model_dir} present ({n_files} file(s))")


# The pinned Cowork-VM interpreter (field finding 2026-07-18: cp311 wheels
# staged for the 3.10-only VM caused a 10-run EmbedderUnavailable outage).
# Keep in lockstep with tools/vendor_semantic_deps.py's VM_PYTHON.
_VM_PYTHON = (3, 10)

# Vendored-deps ABI check lives in doctor_vendor.py since 2026-08-15 (size ratchet);
# re-exported here so every existing caller keeps importing it from doctor.

# Parent-namespace binds, deferred past this module's own defs. ``__file__``
# is read THROUGH the facade at call time so the tests that monkeypatch
# ``doctor.__file__`` keep governing the staged-copy detection.
from . import doctor as _doctor  # noqa: E402
from .doctor import (  # noqa: E402
    CURRENT as CURRENT,
    NOT_DETECTABLE as NOT_DETECTABLE,
    STALE as STALE,
    UNKNOWN as UNKNOWN,
    _row as _row,
    _ssot_version as _ssot_version,
)


def _doctor_file() -> Path:
    return Path(_doctor.__file__).resolve()

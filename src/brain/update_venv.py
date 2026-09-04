"""Engine source resolution and the engine venv refresh, split out of
``update.py`` at the 2026-09-04 size ratchet.

These five functions answer one question — WHICH checkout is this machine's
engine, and does its virtualenv match it — and they are the only part of the
update flow that touches a venv. ``update.py`` re-exports every name, so
``brain.update.<name>`` remains both the call site and the monkeypatch target
for existing callers and tests.
"""
from __future__ import annotations

import os
import shutil
import subprocess  # noqa: F401  (Runner's annotation, and the injected fake's type)
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from .doctor import (
    CHANNEL_EDITABLE,
    detect_install_channel,
    marketplace_install_location,
)
from .update_channels import refresh_engine_channel
from .update_plugins import _default_runner

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _packaged_script(name: str, engine_src: Optional[Path] = None) -> Optional[Path]:
    """Resolve a bundled script (``vm-selftest.sh``, ``brainiac-alerts.sh``)
    from either the packaged wheel mirror (``brain/_assets/scripts/``) or the
    checkout's own ``scripts/`` original — first hit wins. Returns None if
    neither exists."""
    candidates: list[Path] = []
    try:
        from importlib.resources import files
        candidates.append(Path(str(files("brain") / "_assets" / "scripts" / name)))
    except Exception:
        pass
    if engine_src is not None:
        candidates.append(engine_src / "src" / "brain" / "_assets" / "scripts" / name)
        candidates.append(engine_src / "scripts" / name)
    for c in candidates:
        if c.is_file():
            return c
    return None


# --------------------------------------------------------------------------
# Engine venv refresh — resolves the engine source path from the workspace
# registry / explicit override, NEVER a hardcoded ~/brainiac (HARDEN:codex-MEDIUM).
# --------------------------------------------------------------------------

def _owned_by_current_uid(path: Path) -> bool:
    """True iff ``path`` is owned by the uid this process runs as.

    Windows has no uid model (``os.getuid`` does not exist there) — the
    directory-existence check above is the whole guard on that platform.
    An unreadable/vanished path fails closed (never owned).
    """
    if not hasattr(os, "getuid"):
        return True
    try:
        return os.stat(path).st_uid == os.getuid()
    except OSError:
        return False


def resolve_engine_source(
    *, explicit: Optional[str] = None, repo_root: Optional[Path] = None,
    claude_home: Optional[Path] = None,
) -> Optional[Path]:
    """Resolve the engine checkout to build/refresh against, in order (RC1):

    1. an explicit override (config / CLI flag),
    2. ``$BRAINIAC_ENGINE_SRC`` — only when it resolves to an EXISTING
       directory owned by the current uid (LOW-01 item 3); anything else is
       ignored (falls through to step 3+) rather than trusted blind, since
       this env var otherwise points the rest of ``brain update`` (pip
       install, dist rebuild) straight at whatever directory it names,
    3. an explicit ``repo_root`` (caller-supplied),
    4. the repo root this module ships from — but ONLY when it actually carries
       a ``pyproject.toml`` (a wheel install resolves this inside site-packages,
       which has none — the exact RC1 mis-resolution),
    5. the marketplace's ``installLocation`` (known_marketplaces.json), again
       pyproject-guarded — the one already-persisted pointer to the real
       checkout on a directory-source install,
    6. ``None`` — no checkout resolvable. The downstream gate then honestly
       skips dist-rebuild / workspace re-stage instead of pointing pip at a
       nonexistent ``~/brainiac`` (the deleted last-resort fallback).
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("BRAINIAC_ENGINE_SRC")
    if env:
        candidate = Path(env).expanduser().resolve()
        if candidate.is_dir() and _owned_by_current_uid(candidate):
            print(f"BRAINIAC_ENGINE_SRC honoured: {candidate}", file=sys.stderr)
            return candidate
        # else: not a directory, or owned by someone else — ignore the
        # override rather than trust it, and fall through to the next
        # resolution step below instead of returning None outright.
    if repo_root is not None:
        return repo_root.resolve()
    inferred = Path(__file__).resolve().parent.parent.parent
    if (inferred / "pyproject.toml").exists():
        return inferred
    # Imported at call time, not module scope: `update` imports this module,
    # so a top-level import would close the cycle (2026-09-04 size ratchet).
    from .update import claude_home_default

    claude_home = claude_home or claude_home_default()
    loc = marketplace_install_location(claude_home)
    if loc and (loc / "pyproject.toml").exists():
        return loc
    return None


def _venv_bin(venv_dir: Path, name: str) -> Path:
    """Path to an executable inside a venv, cross-platform (Windows fix): POSIX
    venvs put executables in ``bin/`` bare; Windows in ``Scripts\\`` with a
    ``.exe`` suffix."""
    if os.name == "nt":
        return venv_dir / "Scripts" / f"{name}.exe"
    return venv_dir / "bin" / name


def refresh_engine_venv(
    engine_src: Optional[Path], brainiac_home: Path, run: Runner = _default_runner,
) -> dict:
    """Channel-aware refresh (PYP-04 / RC2): detect which channel the host is
    actually on — the legacy editable dev checkout, a plain wheel in
    ``~/.brainiac/venv`` (RC2: previously misdetected as editable), or one of
    the three PyPI channels (uv tool / pipx / pip --user) — and run THAT
    channel's own upgrade command. Detecting the live channel via the
    PATH-resolved `brain` binary is what makes `brain update` self-heal the
    right thing regardless of how the engine was installed.
    """
    return refresh_engine_channel(
        engine_src,
        brainiac_home,
        run,
        detect_channel=detect_install_channel,
        venv_bin=_venv_bin,
        which_brain=shutil.which("brain"),
        python_executable=sys.executable,
    )

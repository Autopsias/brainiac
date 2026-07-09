"""Stage the offline semantic-search deps the Cowork device VM can't pip-install
(DV-04, 2026-07-09).

The device VM has onnxruntime (base image) + the staged model, but NOT
``tokenizers`` (query tokenisation) or ``sqlite-vec`` (vec0 ANN), and it has no
outbound network. So the HOST (which does have network) downloads the per-arch
wheels and unpacks them into ``<brain_dir>/vendor/<arch>/``; the shim puts
``vendor/<arch>`` on ``PYTHONPATH`` ahead of the engine, and the VM imports them
locally — no pip, no egress.

This module is the SINGLE source of that logic, shared by
``tools/cowork_workspace_install.sh`` (fresh install) and ``brain update``'s
workspace re-stage (``src/brain/update.py``) so the two never drift — the exact
class of bug that left ``brain update`` shipping a stale artifact before.

``tokenizers`` ships an ``abi3`` wheel (one build works across py3.x); ``sqlite-vec``
ships a ``py3-none`` wheel. Both are pinned to the linux manylinux2014 target the
Cowork VM runs. Pure stdlib — safe to import from anywhere.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ARCHS = ("aarch64", "x86_64")

# The zero-install shim, with vendored deps ahead of the engine on PYTHONPATH.
# Kept here so the installer and the updater write the identical file.
SHIM_CONTENT = """#!/bin/sh
# zero-install shim: run the staged brain engine from source, with the vendored
# semantic deps (tokenizers/sqlite-vec) ahead of it on the path so real query
# embedding works offline in the VM (DV-04).
DIR="$(cd "$(dirname "$0")" && pwd)"
ARCH="$(uname -m)"
PYTHONPATH="$DIR/vendor/$ARCH:$DIR/engine${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m brain.cli "$@"
"""


def _has_deps(arch_dir: Path) -> bool:
    return (arch_dir / "tokenizers" / "tokenizers.abi3.so").exists() and (
        arch_dir / "sqlite_vec" / "vec0.so"
    ).exists()


def _download_and_unpack(arch: str, arch_dir: Path, python_exe: str) -> bool:
    plat = f"manylinux2014_{arch}"
    common = [python_exe, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
              "--platform", plat, "--python-version", "310"]
    jobs = (
        [*common, "--implementation", "cp", "--abi", "abi3", "tokenizers"],
        [*common, "sqlite-vec"],
    )
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for job in jobs:
            if subprocess.run([*job, "-d", str(tmp)], capture_output=True).returncode != 0:
                return False
        if arch_dir.exists():
            shutil.rmtree(arch_dir)
        arch_dir.mkdir(parents=True, exist_ok=True)
        for whl in tmp.glob(f"*{arch}*.whl"):
            with zipfile.ZipFile(whl) as zf:
                zf.extractall(arch_dir)
    return _has_deps(arch_dir)


def stage_vendor(brain_dir, *, archs=ARCHS, python_exe: str | None = None,
                 force: bool = False) -> dict:
    """Ensure ``<brain_dir>/vendor/<arch>/`` holds tokenizers + sqlite-vec for
    each arch. Skips an arch already staged (the deps are version-stable) unless
    ``force``. Returns ``{arch: 'present'|'staged'|'failed'}`` — a failed arch is
    advisory (the VM degrades to lexical), never an exception."""
    brain_dir = Path(brain_dir)
    python_exe = python_exe or sys.executable
    out: dict[str, str] = {}
    for arch in archs:
        arch_dir = brain_dir / "vendor" / arch
        if _has_deps(arch_dir) and not force:
            out[arch] = "present"
            continue
        out[arch] = "staged" if _download_and_unpack(arch, arch_dir, python_exe) else "failed"
    return out


def write_shim(brain_dir) -> Path:
    """Write the vendored-deps-aware zero-install shim to ``<brain_dir>/brain``."""
    p = Path(brain_dir) / "brain"
    p.write_text(SHIM_CONTENT, encoding="utf-8")
    p.chmod(0o755)
    return p


def _demo() -> None:
    """ponytail self-check: shim writer is deterministic + executable; the
    vendor staged-detector agrees with the on-disk .so files (no network)."""
    with tempfile.TemporaryDirectory() as td:
        bd = Path(td)
        p = write_shim(bd)
        assert p.read_text(encoding="utf-8") == SHIM_CONTENT
        assert p.stat().st_mode & 0o111  # executable
        assert not _has_deps(bd / "vendor" / "aarch64")  # nothing staged yet
        (bd / "vendor" / "aarch64" / "tokenizers").mkdir(parents=True)
        (bd / "vendor" / "aarch64" / "tokenizers" / "tokenizers.abi3.so").write_bytes(b"")
        (bd / "vendor" / "aarch64" / "sqlite_vec").mkdir(parents=True)
        (bd / "vendor" / "aarch64" / "sqlite_vec" / "vec0.so").write_bytes(b"")
        assert _has_deps(bd / "vendor" / "aarch64")
    print("OK: vendor_semantic_deps self-check passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--demo":
        _demo()
    else:
        target = sys.argv[1] if len(sys.argv) > 1 else "."
        print(stage_vendor(target))

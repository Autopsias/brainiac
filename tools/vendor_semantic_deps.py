"""Stage the offline semantic-search deps the Cowork device VM can't pip-install
(DV-04, 2026-07-09).

The device VM has onnxruntime (base image) + the staged model, but NOT
``tokenizers`` (query tokenisation) or ``sqlite-vec`` (vec0 ANN), and it has no
outbound network. So the HOST (which does have network) downloads the per-arch
wheels and unpacks them into ``<brain_dir>/vendor/<arch>/``; the shim puts the
engine FIRST on ``PYTHONPATH`` with ``vendor/<arch>`` after it, and the VM
imports the third-party deps locally — no pip, no egress.

Supply-chain hardening (codex 2026-07-19): a vendored wheel is untrusted input.
The engine is placed BEFORE the vendor dir on ``PYTHONPATH`` so a poisoned wheel
cannot shadow the ``brain`` package, and extraction refuses any member that
would drop a top-level shadowing/auto-exec module (``brain``, ``sitecustomize``,
``usercustomize``) or escape the vendor dir (zip-slip). Deps are pinned to exact
versions below.

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

# The zero-install shim, with the engine ahead of the vendored deps on
# PYTHONPATH (codex 2026-07-19 — an untrusted wheel must not shadow the engine).
# Kept here so the installer and the updater write the identical file.
SHIM_CONTENT = """#!/bin/sh
# zero-install shim: run the staged brain engine from source, with the ENGINE
# first on the path and the vendored semantic deps (tokenizers/sqlite-vec)
# AFTER it, so real query embedding works offline in the VM (DV-04) while an
# untrusted vendored wheel cannot shadow the brain engine (codex 2026-07-19).
DIR="$(cd "$(dirname "$0")" && pwd)"
ARCH="$(uname -m)"
PYTHONPATH="$DIR/engine:$DIR/vendor/$ARCH${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m brain.cli "$@"
"""


# Top-level names an untrusted wheel must never be allowed to drop into the
# vendor dir: the engine package itself, and the two modules CPython auto-imports
# at startup (a wheel placing `sitecustomize.py`/`usercustomize.py` on the path
# runs arbitrary code with no import statement). Engine-first PYTHONPATH already
# stops `brain` shadowing; this is belt-and-braces + covers the auto-exec pair.
_FORBIDDEN_TOPLEVEL = frozenset({
    "brain", "sitecustomize", "usercustomize", "sitecustomize.py", "usercustomize.py",
})


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract a wheel, refusing members that escape ``dest`` (zip-slip) or
    that would shadow the engine / auto-exec at a top-level name. A poisoned
    wheel is untrusted input (codex 2026-07-19)."""
    dest = dest.resolve()
    for member in zf.namelist():
        top = member.split("/", 1)[0]
        if top in _FORBIDDEN_TOPLEVEL:
            raise ValueError(f"vendored wheel member {member!r} would shadow a "
                             f"protected top-level name — refusing extraction")
        target = (dest / member).resolve()
        if target != dest and dest not in target.parents:
            raise ValueError(f"vendored wheel member {member!r} escapes {dest} "
                             f"(zip-slip) — refusing extraction")
    zf.extractall(dest)


def _has_deps(arch_dir: Path) -> bool:
    return (
        (arch_dir / "tokenizers" / "tokenizers.abi3.so").exists()
        and (arch_dir / "sqlite_vec" / "vec0.so").exists()
        and (arch_dir / "onnxruntime").is_dir()
        and (arch_dir / "numpy").is_dir()
    )


def _download_and_unpack(arch: str, arch_dir: Path, python_exe: str) -> bool:
    plat = f"manylinux2014_{arch}"
    common = [python_exe, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
              "--platform", plat, "--python-version", "310"]
    # onnxruntime ships no abi3 wheels, so it (and its closure: numpy etc.) must
    # match the VM python's exact minor — the Cowork base image is bookworm,
    # python 3.11. The base image does NOT ship onnxruntime (field finding
    # 2026-07-13: EmbedderUnavailable in-VM), so we vendor the full closure.
    onnx_plat = f"manylinux_2_28_{arch}"
    onnx = [python_exe, "-m", "pip", "download", "--only-binary=:all:",
            "--platform", onnx_plat, "--python-version", "311", "onnxruntime"]
    jobs = (
        [*common, "--implementation", "cp", "--abi", "abi3", "tokenizers"],
        [*common, "sqlite-vec"],
        onnx,
    )
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for job in jobs:
            if subprocess.run([*job, "-d", str(tmp)], capture_output=True).returncode != 0:
                return False
        if arch_dir.exists():
            shutil.rmtree(arch_dir)
        arch_dir.mkdir(parents=True, exist_ok=True)
        # extract every downloaded wheel — pure-python wheels (*-none-any) carry
        # no arch tag, so a *{arch}* glob would silently drop onnxruntime's deps.
        # _safe_extract refuses zip-slip and engine/auto-exec shadowing members.
        # ponytail: version+hash pinning is the deeper supply-chain hardening;
        #   deferred because pinning wheel versions we can't validate offline is
        #   the EmbedderUnavailable outage class — add a hash-locked requirements
        #   file when the threat model includes a fully compromised package index.
        for whl in tmp.glob("*.whl"):
            with zipfile.ZipFile(whl) as zf:
                _safe_extract(zf, arch_dir)
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

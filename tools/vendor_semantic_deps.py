"""Stage the offline semantic-search deps the Cowork device VM can't pip-install
(DV-04, 2026-07-09).

The device VM (pinned Python ``VM_PYTHON`` = 3.10, no outbound network, base
image ships NONE of the compiled deps) needs ``onnxruntime`` + ``numpy``
(inference), ``tokenizers`` (query tokenisation) and ``sqlite-vec`` (vec0 ANN).
So the HOST (which does have network) downloads the per-arch cp310/abi3 wheels
and unpacks them into ``<brain_dir>/vendor/<arch>/``; the shim puts the
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

import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ARCHS = ("aarch64", "x86_64")

# THE single pinned VM interpreter version. The Cowork Linux VM runs Python
# 3.10 ONLY (field finding 2026-07-18: cp311 wheels staged for a 3.10 VM caused
# a 10-run EmbedderUnavailable outage). Every pip-download job below derives
# its --python-version from this, and check_wheel_tag() refuses any wheel whose
# tag the pinned interpreter cannot import. Keep in lockstep with
# src/brain/doctor.py's _VM_PYTHON.
VM_PYTHON = "3.10"
_VM_PY_NODOT = VM_PYTHON.replace(".", "")  # "310"

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


def check_wheel_tag(wheel_name: str, vm_python: str = VM_PYTHON) -> bool:
    """True iff the wheel's filename tags are importable by the pinned VM
    interpreter (regression guard, 2026-07-18: cp311 wheels staged for the
    3.10-only VM caused the EmbedderUnavailable outage). Acceptable:
    ``cp310``, ``abi3`` built at or below 3.10 (e.g. cp39-abi3), and pure
    ``py3-none-any``. Refused: cp311/cp312/..., or any unparseable name."""
    major, minor = (int(x) for x in vm_python.split("."))
    stem = wheel_name[:-len(".whl")] if wheel_name.endswith(".whl") else wheel_name
    parts = stem.split("-")
    if len(parts) < 5:  # name-version[-build]-python-abi-platform
        return False
    py_tag, abi_tag = parts[-3], parts[-2]
    if abi_tag not in (f"cp{major}{minor}", "abi3", "none"):
        return False

    def _py_ok(t: str) -> bool:
        if t in ("py3", "py2.py3", f"py{major}{minor}"):
            return True
        m = re.fullmatch(r"cp(\d)(\d+)", t)
        if not m:
            return False
        maj, mnr = int(m.group(1)), int(m.group(2))
        # abi3 is forward-compatible: cp39-abi3 imports fine on 3.10.
        return maj == major and (mnr <= minor if abi_tag == "abi3" else mnr == minor)

    return any(_py_ok(t) for t in py_tag.split("."))


def _download_and_unpack(arch: str, arch_dir: Path, python_exe: str) -> bool:
    plat = f"manylinux2014_{arch}"
    common = [python_exe, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
              "--platform", plat, "--python-version", _VM_PY_NODOT]
    # onnxruntime ships no abi3 wheels, so it (and its closure: numpy etc.) must
    # match the pinned VM python's EXACT minor (VM_PYTHON = 3.10 — a 311 pin
    # here is precisely the 2026-07-18 EmbedderUnavailable outage). The base
    # image does NOT ship onnxruntime (field finding 2026-07-13), so we vendor
    # the full closure.
    onnx_plat = f"manylinux_2_28_{arch}"
    onnx = [python_exe, "-m", "pip", "download", "--only-binary=:all:",
            "--platform", onnx_plat, "--python-version", _VM_PY_NODOT, "onnxruntime"]
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
            # ABI regression guard: refuse (loudly) any wheel the pinned VM
            # interpreter cannot import, BEFORE it lands in vendor/.
            if not check_wheel_tag(whl.name):
                print(f"[vendor] REFUSING {whl.name}: wheel tag incompatible "
                      f"with the pinned VM interpreter (Python {VM_PYTHON}) — "
                      f"staging {arch} aborted", file=sys.stderr)
                shutil.rmtree(arch_dir, ignore_errors=True)
                return False
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
        (bd / "vendor" / "aarch64" / "onnxruntime").mkdir(parents=True)
        (bd / "vendor" / "aarch64" / "numpy").mkdir(parents=True)
        assert _has_deps(bd / "vendor" / "aarch64")
        # ABI pin guard (2026-07-18): cp310/abi3/pure pass, cp311 refused
        assert check_wheel_tag("numpy-1.26.4-cp310-cp310-manylinux_2_28_aarch64.whl")
        assert not check_wheel_tag("numpy-1.26.4-cp311-cp311-manylinux_2_28_aarch64.whl")
    print("OK: vendor_semantic_deps self-check passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--demo":
        _demo()
    else:
        target = sys.argv[1] if len(sys.argv) > 1 else "."
        print(stage_vendor(target))

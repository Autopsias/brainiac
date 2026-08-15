"""Vendored-deps ABI check, extracted from doctor.py (2026-08-15).

Lives apart so doctor.py — already at its size-ratchet baseline — could take
the arch-restriction fix without growing past it. doctor.py re-exports
``check_vendor_abi`` so every existing caller (``run_doctor``,
``run_doctor_vm``, ``update.py``, tests) keeps working unchanged.
"""

from __future__ import annotations

import os
import platform
import re
from pathlib import Path

_CPYTHON_SO_RE = re.compile(r"\.cpython-(\d)(\d+)-")


def _prune_retired_dirs(dirs: list[str]) -> list[str]:
    """Vendor-walk pruning: directories to NOT descend into. _retired-*/ holds
    deliberately quarantined old wheels (e.g. the cp311 set retired by the
    2026-07 ABI fix) — corpses, not the live vendor; counting them re-reports
    the exact outage they ended. Field finding 2026-08-15: on a Cowork
    VirtioFS mount _retired-cp311/ alone held 2,741 files (77% of the tree) at
    ~30 dir-entries/sec, so rglob-then-filter paid ~90s for files it discarded
    and `brain --role vm doctor` never completed inside any call budget. The
    exclusion must PRUNE the walk, not filter its results."""
    return [d for d in dirs if not d.startswith("_retired")]


def _running_vendor_arch() -> str | None:
    """Normalize platform.machine() to the vendor tree's arch-dir names
    (aarch64/x86_64), or None on an unrecognized machine."""
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    return None


def check_vendor_abi(vendor_dir: Path, interpreter: tuple[int, int],
                     arch: str | None = None) -> dict:
    """Report the vendored wheels' extension-module ABI tags against the
    interpreter that will import them, and NAME a mismatch explicitly
    ("vendor is cp311 but interpreter is 3.10") instead of leaving the VM to
    die later with a bare EmbedderUnavailable. Reads only filenames of
    extracted ``.so`` files (``*.cpython-3XX-*.so`` / ``*.abi3.so``).

    ``arch`` restricts the scan to ``vendor/<arch>/`` when that dir exists
    (falling back to the whole tree otherwise). Only the RUNNING machine's
    arch dir can ever be imported — the shim puts one
    ``vendor/$(uname -m)`` on PYTHONPATH — so a VM leg passes its own arch
    and never pays the VirtioFS walk for the other arch's ~400 files. A
    host-side call over registered workspaces omits ``arch`` and judges every
    staged arch against the pinned _VM_PYTHON, as before."""
    # Deferred: doctor.py imports this module at load time, so importing the
    # row builder back from it must happen at CALL time, not import time.
    from .doctor import NOT_DETECTABLE, CURRENT, STALE, _row

    surface = "Vendored deps ABI (.brain/vendor)"
    want = f"cp{interpreter[0]}{interpreter[1]}"
    if not vendor_dir.is_dir():
        return _row(surface, NOT_DETECTABLE, f"no vendor dir at {vendor_dir}")
    scan_root = vendor_dir
    if arch is not None:
        arch_dir = vendor_dir / arch
        if arch_dir.is_dir():
            scan_root = arch_dir
    tags: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(scan_root):
        dirnames[:] = _prune_retired_dirs(dirnames)
        for name in filenames:
            if not name.endswith(".so"):
                continue
            m = _CPYTHON_SO_RE.search(name)
            if m:
                tags.add(f"cp{m.group(1)}{m.group(2)}")
            elif name.endswith(".abi3.so"):
                tags.add("abi3")
    if not tags:
        return _row(surface, NOT_DETECTABLE,
                    f"no tagged extension modules under {vendor_dir} — vendor not staged "
                    "(VM runs lexical-only)")
    bad = sorted(t for t in tags if t not in ("abi3", want))
    if bad:
        return _row(surface, STALE,
                    f"vendor is {'/'.join(bad)} but interpreter is "
                    f"{interpreter[0]}.{interpreter[1]} — the vendored wheels cannot import "
                    "here, so semantic search dies with EmbedderUnavailable",
                    remediation="re-stage from the host: tools/cowork_workspace_install.sh "
                                f"(vendored wheels must be {want}/abi3 — "
                                "tools/vendor_semantic_deps.py now refuses mismatched tags)",
                    raw={"tags": sorted(tags), "interpreter": want})
    return _row(surface, CURRENT,
                f"vendored ABI tags {sorted(tags)} match interpreter "
                f"{interpreter[0]}.{interpreter[1]}",
                raw={"tags": sorted(tags), "interpreter": want})

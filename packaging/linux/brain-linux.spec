# -*- mode: python ; coding: utf-8 -*-
# brain-linux.spec — PyInstaller ONE-DIR spec for the `brain` CLI on Linux.
#
# Used for the Cowork VM surface (INT-02). Build once per arch (x86_64 + aarch64)
# — Cowork VMs run either. The per-session bootstrap symlinks `brain` -> the arch
# matching `uname -m` (see docs/cowork-windows-install.md).
#
# One-dir (not one-file): a one-file ELF self-extracts to /tmp at every launch,
# which (a) wastes the ephemeral VM disk and (b) breaks the "model lives in the
# workspace / mmap in place" invariant. One-dir keeps the payload stable on the
# VirtioFS mount. NO UPX (same AV-heuristic reasoning as Windows; also UPX-packed
# ELFs decompress to RAM, defeating mmap).
#
# Build:  pyinstaller --clean --noconfirm packaging/linux/brain-linux.spec
#         (drive from a 2-runner CI matrix or buildx for both arches)
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("brain")

a = Analysis(
    ["../brain_entry.py"],
    pathex=["../../src"],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PIL", "numpy.tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,            # one-dir
    name="brain",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="brain",
)

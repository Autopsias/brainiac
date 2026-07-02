# -*- mode: python ; coding: utf-8 -*-
# brain-macos.spec — PyInstaller ONE-DIR spec for the `brain` CLI on macOS.
#
# Why one-dir (not one-file): a one-file build self-extracts to a temp dir at
# launch, which (a) trips endpoint AV heuristics and (b) on macOS breaks the
# notarization staple (the staple must live next to the signed Mach-O). One-dir
# ships a stable, signable, notarizable bundle.
#
# Realism note: this spec BUILDS an unsigned bundle. Developer-ID signing +
# notarization are PENDING maintainer's Apple ID — see
# docs/operations/macos-build-notarization-runbook.md.
#
# Build:  pyinstaller --clean --noconfirm packaging/macos/brain-macos.spec
from PyInstaller.utils.hooks import collect_submodules

# The core is stdlib-only with graceful degradation; pull the brain package
# submodules explicitly so optional-dep guards resolve at runtime, not import.
hiddenimports = collect_submodules("brain")

a = Analysis(
    ["../brain_entry.py"],        # package-aware entry shim (NOT cli.py directly)
    pathex=["../../src"],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Keep the bundle lean + deterministic: exclude heavy optional accelerators.
    # They are shipped as a side-loaded model cache (BRAIN_MODEL_CACHE), never
    # frozen into the signed binary. See docs/cowork-windows-install.md rule 2.
    excludes=["tkinter", "matplotlib", "PIL", "numpy.tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,       # one-dir
    name="brain",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                   # NEVER UPX — packers are the #1 AV false-positive trigger
    console=True,
    target_arch=None,            # set "universal2" only with a universal2 Python
    # Signing + entitlements are applied POST-build in the notarization runbook
    # (codesign --options runtime --entitlements packaging/macos/entitlements.plist),
    # NOT at PyInstaller assembly time. Leaving these None lets the unsigned
    # bundle build cleanly on a machine with no Developer-ID identity.
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="brain",
)

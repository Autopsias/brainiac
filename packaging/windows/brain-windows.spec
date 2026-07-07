# -*- mode: python ; coding: utf-8 -*-
# brain-windows.spec — PyInstaller ONE-DIR spec for the `brain` CLI on Windows.
#
# This spec encodes the four Defender-survival decisions (design v5 §2):
#   1. ONE-DIR, not one-file. One-file self-extracts a PE to %TEMP% at every
#      launch — the single biggest Defender/ASR heuristic trigger. One-dir ships
#      a stable, signable folder; nothing is unpacked at runtime.
#   2. NO UPX. Executable packing is the #1 cause of AV false-positives; UPX is
#      explicitly disabled on the EXE and the COLLECT.
#   3. EMBED PE version metadata (version_info.txt) — an unversioned, company-less
#      PE reads as "anonymous dropper" to reputation engines. CompanyName /
#      ProductName / FileVersion give it an identity.
#   4. CUSTOM-COMPILED BOOTLOADER (recommended) — the stock PyInstaller bootloader
#      is in every malware sample's training set. Rebuilding it from source
#      (see build_windows.ps1 "bootloader" note) yields a unique byte signature.
#      This spec works with either; the runbook covers the rebuild.
#
# Realism note: this BUILDS an unsigned one-dir bundle. Authenticode signing via
# Azure Trusted Signing + RFC-3161 timestamp is PENDING (PW-2) — see
# packaging/windows/sign_windows.ps1 and docs/operations/packaging-windows-runbook.md.
#
# Build (on a Windows runner):
#   pyinstaller --clean --noconfirm packaging\windows\brain-windows.spec
from PyInstaller.utils.hooks import collect_submodules
import os

hiddenimports = collect_submodules("brain")

# DIST-02: bundle the e5-small ONNX model INLINE so the frozen binary is
# offline-first (no HF download at run time). Staged by build_windows.ps1.
MODEL_BUNDLE = os.environ.get(
    "BRAIN_MODEL_BUNDLE",
    os.path.abspath(os.path.join(SPECPATH, "..", "model_bundle", "e5-small")),
)
datas = []
if os.path.isdir(MODEL_BUNDLE):
    datas.append((MODEL_BUNDLE, "e5-small"))

a = Analysis(
    ["../brain_entry.py"],            # package-aware entry shim (NOT cli.py directly)
    pathex=["../../src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Heavy optional accelerators are NOT in the corporate minimal set.
    excludes=["tkinter", "matplotlib", "PIL", "numpy.tests",
              "torch", "transformers", "sentence_transformers", "fastembed",
              "qwen3_embed"],
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
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                        # decision #2 — NEVER pack
    console=True,
    disable_windowed_traceback=False,
    version="version_info.txt",       # decision #3 — embed PE version resource
    icon=None,                        # add a signed-product icon at release time
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,                        # decision #2 — applies to bundled DLLs too
    name="brain",
)

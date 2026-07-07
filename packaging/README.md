# packaging/ — build & deploy machinery (S07)

One-dir PyInstaller packaging for the `brain` CLI across the three surfaces, plus
the signing / Intune / WDAC config and runbooks. **Markdown stays the source of
truth; binaries are derived and never committed** (`dist/`, `build/`, root
`*.spec` are gitignored — the authored specs under `packaging/` are tracked).

```
packaging/
├── brain_entry.py              package-aware frozen entry shim (all OSes)
├── windows/
│   ├── brain-windows.spec      one-dir, no-UPX, PE version resource
│   ├── version_info.txt        PE version metadata
│   ├── build_windows.ps1       BUILD (unsigned)         [-RebuildBootloader]
│   ├── sign_windows.ps1        Azure Trusted Signing + RFC-3161  (PENDING PW-2)
│   ├── wdac-managed-installer-policy.md   WDAC policy   (PENDING PW-3)
│   └── intune/                 install/uninstall/detection + .intunewin packer
├── macos/
│   ├── brain-macos.spec        one-dir, no-UPX
│   ├── entitlements.plist      least-privilege hardened runtime
│   ├── build_macos.sh          BUILD (unsigned)  ✅ proven this session
│   └── sign_notarize_macos.sh  Developer-ID + notarize  (PENDING Apple ID)
└── linux/
    ├── brain-linux.spec        one-dir, no-UPX
    ├── Dockerfile.build        per-arch freeze container
    └── build_linux.sh          buildx x86_64 + aarch64  ✅ proven this session
```

## Build locally

```bash
# macOS one-dir (proven)
packaging/macos/build_macos.sh                 # → dist/brain/

# Linux both arches (proven; needs docker buildx)
packaging/linux/build_linux.sh                 # → dist/linux/brain-{x86_64,aarch64}/

# Windows one-dir (run on a Windows runner)
pwsh packaging/windows/build_windows.ps1       # → dist/brain/
```

## Production SBOM from a minimal venv (so SBOM == shipped components)

```bash
python -m venv .venv-release
.venv-release/bin/pip install -e ".[vec,audit,embed,yaml]"     # runtime extras only
.venv-release/bin/pip install cyclonedx-bom
.venv-release/bin/cyclonedx-py environment .venv-release \
  --of JSON --sv 1.5 --pyproject pyproject.toml -o dist/sbom.release.cdx.json
```

The tracked build-env SBOM (`packaging/sbom.cdx.json`) is the **build-env**
superset; a release freezes from the minimal venv above.

## Decisions baked in (design v5 §2)

- **No language rewrite** to Go/Rust — same Defender heuristics apply; the fix is
  packaging + Managed-Installer trust, not the language.
- **No EV cert** — EV no longer bypasses SmartScreen (2026); the trust anchor is
  WDAC Managed Installer on managed devices.
- **One-dir, no UPX, signed PEs, embedded version metadata** — the four
  Defender-survival decisions.

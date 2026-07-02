# macOS build + Developer-ID signing + notarization runbook (PKG-02)

**Session:** S07 · **Date:** 2026-06-27
**State:** the **build is DONE and proven** this session (a real unsigned one-dir
bundle was produced and runs against the live vault — see
`_evidence/s07/build-transcript-macos.txt`). **Signing + notarization are PENDING
maintainer's Apple Developer ID** (the build agent holds no Apple ID).

## Build (DONE — reproducible)

```bash
packaging/macos/build_macos.sh        # → dist/brain/ (one-dir, unsigned)
```

Proven this session on `Darwin arm64`, PyInstaller 6.21.0, Python 3.14:
- `dist/brain/brain` built, **44 MB** one-dir bundle, `upx=False`.
- Smoke: `dist/brain/brain --help` and `brain recent --json` return real results.
- SHA-256 of the unsigned artifact: `_evidence/s07/sha256-artifacts.txt`.

## Sign + notarize (PENDING — `packaging/macos/sign_notarize_macos.sh`)

Gatekeeper on a managed Mac requires a **Developer-ID Application** signature **+**
a **notarization ticket** stapled to the bundle.

Prerequisites (all need the Apple Developer account):
1. `Developer ID Application: Example Corp (<TEAMID>)` cert in the login keychain.
2. notarytool creds: `xcrun notarytool store-credentials example-notary --apple-id
   <id> --team-id <TEAMID> --password <app-specific-pw>`.

Run:
```bash
DEV_ID="Developer ID Application: Example Corp (TEAMID)" \
NOTARY_PROFILE=example-notary \
packaging/macos/sign_notarize_macos.sh
```
Steps the script performs: codesign nested Mach-O (deep) → codesign main binary
with **hardened runtime** (`--options runtime`) + least-privilege
`entitlements.plist` → `ditto` zip → `notarytool submit --wait` → `stapler staple`
→ `spctl --assess` → emit the **signed** SHA-256 list for the evidence pack.

## Entitlements posture (least privilege)

`packaging/macos/entitlements.plist` claims the **minimum** required for
notarization: hardened runtime with **no** JIT, **no** unsigned-memory, **no**
library-validation-disable, **no** dyld-env override, **no** debugger. `brain`
does its work in-process, so it needs none of the permissive entitlements.

## Why a Mac build at all (it is not the primary surface)

The **primary** surface is Cowork-Windows (Linux VM) → Windows endpoints. The Mac
build covers (a) maintainer's own host (the signing/host-broker machine) and (b) any
Mac user in the pilot. It is **not** the deployment driver.

## Distribution

A signed+notarized one-dir bundle distributes as a **zip** (or a signed `.pkg`/
`.dmg` if a GUI install is wanted later). For the host-broker role, a stapled zip
unpacked to `~/Applications` or `/usr/local` is sufficient — no MDM required for a
single-operator host.

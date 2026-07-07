# Packaging runbook — locked corporate Windows (PKG-01)

> **S12 update (2026-06-30):** the authoritative distribution-channel decision
> is now `docs/operations/s12-channel-decision.md` (DIST-00: `.intunewin` +
> Win32 app, NOT MSIX; verified 2026 facts on SmartScreen/CA-rotation/WDAC).
> The minimal-dep build (DIST-01: direct-ONNX e5-small, no fastembed) + the
> signing custody (DIST-03) + the clean-machine validation runbook (DIST-04)
> live alongside. This S07 runbook remains the operational Windows procedure;
> the S12 docs are the decisions of record.

**Date:** 2026-06-27 · **Repo:** `profile-a-brain/`
**Scope:** how a **locked, Defender-managed, Intune-enrolled the organization's Windows
endpoint** installs `brain` cleanly with **no admin** and IT approves it in **one
step** (Managed-Installer trust), without the binary getting quarantined.

> **The delivery channel is the Defender fix, not the language.** We do NOT
> rewrite to Go/Rust (same Defender heuristics apply to any unsigned, packed,
> unknown PE) and we do NOT buy an EV cert (EV no longer bypasses SmartScreen in
> 2026). The load-bearing control is **Intune Managed Installer → WDAC trust** on
> the managed device. Azure Trusted Signing supplies a valid Authenticode
> signature + RFC-3161 timestamp; it is necessary but not the trust anchor.

## Sequencing gate (HARDENED:codex / codex-verify-r1)

No **signed** artifact is produced until the upstream gates are green:

| Upstream | State | Meaning for S07 |
|---|---|---|
| S04 retrieval | ✅ green (committed) | ok to build |
| S05 eval harness | ✅ machinery green | **eval verdict is machinery-only**; the real frozen-baseline eval is S10/val-01 |
| S06 Cowork integration | ✅ green (committed) | ok to build |
| S08 security | ✅ green (committed) | ok to build |

**Therefore:** `build_windows.ps1` (BUILD only, unsigned) may run now.
`sign_windows.ps1` (SIGN) is held **PENDING** (a) Azure Trusted Signing
onboarding (PW-2) **and** (b) the **S10 real-eval gate** — treat any signed
artifact as gated on S10. Building unsigned is safe and is what S07 ships.

## Pipeline (5 stages)

```
build_windows.ps1   →  sign_windows.ps1     →  intune\package_intunewin.ps1  →  Intune portal       →  managed device
(one-dir, unsigned)    (Azure Trusted          (.intunewin Win32 app)           (Win32 app, USER       (IME Managed
                        Signing, PENDING PW-2)                                    context; PENDING       Installer ⇒
                                                                                  PW-3/PW-4)             WDAC-trusted)
```

### Stage 1 — BUILD (one-dir). `packaging/windows/build_windows.ps1`

Four Defender-survival decisions are encoded in `brain-windows.spec`:

1. **One-dir, not one-file.** One-file self-extracts a PE to `%TEMP%` on every
   launch — the single biggest ASR/Defender heuristic trigger. One-dir ships a
   stable, signable folder; nothing unpacks at runtime.
2. **No UPX.** Packing is the #1 AV false-positive cause. `upx=False` on the EXE
   **and** COLLECT.
3. **Embed PE version metadata** (`version_info.txt`): CompanyName / ProductName
   / FileVersion give the PE an identity instead of reading as an anonymous
   dropper. Values MUST match the signing-cert subject (set at PW-2).
4. **Custom-compiled bootloader** (`build_windows.ps1 -RebuildBootloader`): the
   stock PyInstaller bootloader is in every malware corpus; rebuilding from
   source (`pip install --no-binary pyinstaller`) yields a unique byte signature.

Output: `dist\brain\` (one-dir, **unsigned**). Verified analogously on macOS +
Linux this session — see `_evidence/s07/build-transcript-*.txt`.

### Stage 2 — SIGN. `packaging/windows/sign_windows.ps1` — **PENDING PW-2 + S10**

- Azure Trusted Signing (~$10/mo), `signtool sign /fd SHA256 /tr
  http://timestamp.acs.microsoft.com /td SHA256 /dlib Azure.CodeSigning.Dlib`.
- Signs **every PE** in the one-dir bundle (exe + bundled dll/pyd) so WDAC's
  per-file signature check passes on all of them.
- **RFC-3161 timestamp** so the signature outlives cert validity.
- **Region gating (verify FIRST):** Public-Trust identities issue only from
  US/CA/EU/UK billing regions — confirm the the organization's tenant is eligible at PW-2.
- **Mar-2026 intermediate-CA migration:** that CA rotation briefly raised
  SmartScreen warnings on otherwise-valid signatures. We do **not** depend on
  SmartScreen (we depend on Managed-Installer/WDAC), so managed-device install is
  unaffected; contingency = reputation wait-out or a second signing identity.

### Stage 3 — Win32 package. `packaging/windows/intune/package_intunewin.ps1`

Wraps the signed one-dir + `install.cmd` / `uninstall.cmd` / `detection.ps1`
into `install.intunewin` via the Microsoft Win32 Content Prep Tool.

### Stage 4 — Intune Win32 app (portal/Graph) — **PENDING PW-3 + PW-4**

| Setting | Value | Why |
|---|---|---|
| Install command | `install.cmd` | robocopy to `%LOCALAPPDATA%\Programs\brain` |
| Uninstall command | `uninstall.cmd` | clean per-user removal |
| **Install behavior** | **User** | USER context, `%LOCALAPPDATA%`, **no admin** |
| Detection | custom script `detection.ps1` | keys on per-user `.version` stamp |
| Return codes | `0 = success` | robocopy 0–7 normalized in `install.cmd` |
| **Managed Installer** | **enable IME as Managed Installer in the WDAC policy** | anything the Intune Management Extension installs becomes **WDAC-trusted automatically** — the one-step IT approval |

### Stage 5 — Managed device. **PENDING PW-4**

On a managed, WDAC-enforced endpoint: Intune pushes the app, IME (a Managed
Installer) installs it, WDAC trusts it by provenance. No SmartScreen prompt, no
admin, no quarantine. **val-01 acceptance = "clean install via Intune Managed
Installer on a managed device"** (NOT "good SmartScreen reputation").

## What is BUILT now vs PENDING external

| Artifact | State |
|---|---|
| `brain-windows.spec`, `version_info.txt`, `build_windows.ps1` | ✅ authored |
| One-dir build machinery proven (mac + linux real builds this session) | ✅ `_evidence/s07/build-transcript-*.txt` |
| `sign_windows.ps1` (Azure Trusted Signing) | ✅ authored · ⏳ run PENDING PW-2 + S10 |
| `intune/*` (install/uninstall/detect/package) | ✅ authored |
| Intune Win32 app + Managed-Installer enablement | ⏳ PENDING PW-3 + PW-4 |

External pre-work owners + dates + status: `external-prework-register.md`.

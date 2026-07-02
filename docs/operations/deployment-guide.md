# Deployment guide — `brain` on Example Corp endpoints (PKG-03)

**Session:** S07 · **Date:** 2026-06-27. Audience: the security team approving the
rollout, and the EUC/Intune operator deploying it. Pairs with the **approval
evidence pack** (`approval-evidence-pack.md`) and the **NIST CSF 2.0 profile**
(`nist-csf-2.0-profile.md`).

## Surfaces & channels (one table)

| Surface | Who | Channel | Trust anchor |
|---|---|---|---|
| **Cowork-Windows (PRIMARY)** | largest user pop. | Linux ELF (both arches) shipped into the workspace `.brain/` | runs in the Cowork Linux VM; read+draft only (`--role vm`), no signing key |
| **Managed Windows endpoint** | pilot users | Intune Win32 `.intunewin`, USER context → `%LOCALAPPDATA%` | **Intune Managed Installer ⇒ WDAC trust** (+ Authenticode sig) |
| **Host (maintainer's Mac)** | single operator | Developer-ID-signed + notarized one-dir | Gatekeeper (Developer-ID + notarization) |

## Install matrix

| OS / arch | Build | Sign | Deploy |
|---|---|---|---|
| Windows x64 | `build_windows.ps1` (one-dir) | `sign_windows.ps1` (Azure Trusted Signing — PENDING PW-2) | `intune/package_intunewin.ps1` → Intune (PENDING PW-3/PW-4) |
| macOS arm64/universal2 | `build_macos.sh` ✅ proven | `sign_notarize_macos.sh` (PENDING Apple ID) | stapled zip → `~/Applications` |
| Linux aarch64 | `build_linux.sh aarch64` ✅ proven | n/a | `tools/cowork_workspace_install.sh` |
| Linux x86_64 | `build_linux.sh x86_64` ✅ via buildx | n/a | `tools/cowork_workspace_install.sh` |

## One-step IT approval (the point of PKG-01)

On a managed, WDAC-enforced Windows device the operator does **one** thing:
deploy the Win32 app via Intune with the **Intune Management Extension enabled as
a Managed Installer** (PW-3). WDAC then trusts the install by provenance — no
per-hash allow-list, no per-version maintenance, no admin on the endpoint, no
SmartScreen prompt. This is why the trust anchor is the Managed Installer, **not**
SmartScreen reputation (EV certs no longer bypass SmartScreen in 2026).

## Rollout order (gated)

1. **Pre-work track (Track B)** complete — see `external-prework-register.md`
   (cyber review, Azure Trusted Signing, WDAC policy, test device).
2. **S10 real-eval gate** green (frozen-baseline non-inferiority) — no signed
   artifact ships before this.
3. **Single-device pilot** (val-01: clean install via IME on the PW-4 device).
4. **Fleet expansion** only after val-01 passes, with the rollback plan below.

## Rollback

- **Windows:** Intune "Uninstall" assignment runs `uninstall.cmd` (per-user
  removal); the app is also removable by retargeting the Intune assignment. WDAC
  Managed-Installer rule is reverted by Example Corp EUC if needed.
- **Substrate-level abort:** if cyber review rejects the egress posture, **HALT
  and stay on Obsidian + Smart Connections** — a first-class outcome
  (`external-prework-register.md` § Abort branch). Nothing is decommissioned.

## Operability (val-04 — see `raci-ownership-support.md`)

Deployment is not "done" until the **ownership + support model exists**: a second
operator beyond maintainer for every human checkpoint, a post-ship owner for the
brain/index/model lifecycle, and a user-support route. A fleet product with
bus-factor 1 is not operable.

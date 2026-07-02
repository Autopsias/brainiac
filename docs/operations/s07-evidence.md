# S07 evidence — Packaging & deployment (locked Windows) · PKG-01 / PKG-02 / PKG-03

**Session:** S07 · **Date:** 2026-06-27 · **Repo:** `/Users/user/DeveloperFolder/profile-a-brain/`
**Builds on:** S02 core · S04 retrieval · S05 eval harness · S06 Cowork integration · S08 security (all committed/green).
**Model note:** planned model unavailable → executed on **Opus**.

---

## Bottom line

`brain` is packaged for a **locked, Defender-managed, Intune-enrolled Windows
endpoint** so it installs with **no admin** and IT approves it in **one step**.
The delivery channel is the **Defender fix, not the language**: PyInstaller
**one-dir** (no UPX, embedded PE version metadata, custom-bootloader option),
**Azure Trusted Signing** for a valid Authenticode signature + RFC-3161 timestamp,
and a **Win32 `.intunewin` in USER context to `%LOCALAPPDATA%`** with the **Intune
Management Extension enabled as a Managed Installer ⇒ WDAC-trusted**. We did **not**
rewrite to Go/Rust (same heuristics) and did **not** buy an EV cert (pointless for
SmartScreen in 2026). The load-bearing trust control is the **Managed
Installer/WDAC trust on managed devices**, not SmartScreen reputation.

**Real builds this session (not just config):** a working **unsigned macOS one-dir**
bundle that runs against the live vault, plus **real Linux `aarch64` and `x86_64`
ELF** one-dir builds via `docker buildx`. A **real CycloneDX 1.5 SBOM** (77
components) was generated with `cyclonedx-py`. Everything that needs an external
identity (Azure signing, Apple notarization, WDAC policy, Intune device, cyber
review) is **honestly PENDING** in a two-track register with named owners + dates.

---

## PKG-01 — PyInstaller one-dir + Azure signing + Intune Managed-Installer ✅

- **One-dir spec** `packaging/windows/brain-windows.spec` encodes the four
  Defender-survival decisions: one-dir (no `%TEMP%` self-extract), `upx=False`,
  embedded `version_info.txt` PE metadata, custom-bootloader option
  (`build_windows.ps1 -RebuildBootloader`).
- **Package-aware entry shim** `packaging/brain_entry.py` — fixes the relative-import
  freeze failure (proven: the first build ImportErrored on `cli.py` directly; the
  shim build runs `brain --help` + `brain recent --json` cleanly).
- **Signing** `packaging/windows/sign_windows.ps1` — Azure Trusted Signing, signs
  **every PE** in the bundle, `/tr` RFC-3161 timestamp, region-gating + Mar-2026
  intermediate-CA-migration contingency documented. **PENDING PW-2 + S10 gate.**
- **Intune** `packaging/windows/intune/` — `install.cmd` (USER context →
  `%LOCALAPPDATA%\Programs\brain`, no admin), `uninstall.cmd`, `detection.ps1`,
  `package_intunewin.ps1`. **WDAC** `packaging/windows/wdac-managed-installer-policy.md`.
  **PENDING PW-3 + PW-4.**
- **Runbook:** `docs/operations/packaging-windows-runbook.md` (5-stage pipeline +
  sequencing gate).
- **Sequencing (HARDENED):** BUILD runs now (S04/S06/S08 green); **SIGN is held
  PENDING the S10 real-eval gate** — the S05 verdict is machinery-only.

## PKG-02 — ASR/CFA design rules + Linux & Mac builds ✅

- **ASR/CFA rules** `docs/operations/asr-cfa-design-rules.md` — five load-bearing
  rules: state in `%LOCALAPPDATA%` (never Documents/OneDrive/CFA), single signed
  binary launched directly, work in-process, no one-file self-extract, no LOLBin
  entry, explicit CFA allow if unavoidable.
- **In-process verification** `_evidence/s07/no-subprocess-spawn.txt` — the
  retrieval+capture hot path spawns **no** child process; the only `subprocess`
  use is host-side key custody (macOS `security`, opt-in `*_CMD`); **Windows
  custody uses the in-process `keyring` lib — no child**; never on the VM. (Found
  by verifying, not assuming — the doc states the honest exception.)
- **macOS build** `packaging/macos/{brain-macos.spec,build_macos.sh,entitlements.plist}` —
  **real unsigned one-dir built + smoke-tested** (`build-transcript-macos.txt`);
  notarization runbook `sign_notarize_macos.sh` + `macos-build-notarization-runbook.md`
  (**PENDING Apple ID**).
- **Linux builds** `packaging/linux/{brain-linux.spec,Dockerfile.build,build_linux.sh}` —
  **real `aarch64` + `x86_64` ELF** one-dir builds via buildx
  (`build-transcript-linux*.txt`; `file` confirms both arches).

## PKG-03 — Approval evidence pack ✅

- **Pack index** `docs/operations/approval-evidence-pack.md` (12 items, DONE/PENDING).
- **SBOM** `_evidence/s07/sbom.cdx.json` — real CycloneDX 1.5, 77 components, root
  `profile-a-brain 0.2.0` (build-env superset; minimal-venv release recipe in
  `packaging/README.md`).
- **SLSA provenance** `_evidence/s07/slsa-provenance.template.json` — valid in-toto
  v1 / SLSA v1, Build L2 target.
- **Malware scan** `_evidence/s07/defender-sandbox-report.md` — **Defender for
  Endpoint / internal sandbox by DEFAULT** (not public VirusTotal — would disclose
  the private binary + bundled model); public-scan policy gated if ever required.
- **SHA-256 list** `_evidence/s07/sha256-artifacts.txt` — real unsigned hashes
  (macOS + both Linux arches).
- **Deployment guide** `docs/operations/deployment-guide.md` + **NIST CSF map**
  (cross-refs `nist-csf-2.0-profile.md`, S08).

## Cross-cutting — pre-work register + RACI (the real critical path)

- **External pre-work register** `docs/operations/external-prework-register.md` —
  extended with the **two-track dashboard** (HARDENED:r2-codex): **Track A BUILD**
  (~9 days, mostly DONE) vs **Track B EXTERNAL** (PW-1..PW-4, each with a **named
  Example Corp owner** — `TBD-maintainer-to-assign` placeholders, not invented names — **lead
  time, start, target, status=PENDING, and the gate it unblocks**). Critical path +
  fleet-vs-single-device decision + abort branch documented. "~9 days" is the BUILD
  track only, never read as a ship date.
- **RACI / ownership / support** `docs/operations/raci-ownership-support.md`
  (HARDENED:r2-claude / val-04) — a **second operator** for every human checkpoint
  + the drain/brief floor, a **post-ship lifecycle owner** (re-index, model bumps,
  re-sign), and a **user-support route**. Fixes bus-factor 1.

---

## Realism ledger — DONE now vs PENDING external (no fabrication)

| Claim | State | Proof |
|---|---|---|
| macOS one-dir builds + runs | ✅ DONE | `_evidence/s07/build-transcript-macos.txt` |
| Linux aarch64 + x86_64 ELFs | ✅ DONE | `_evidence/s07/build-transcript-linux*.txt` |
| CycloneDX SBOM | ✅ DONE | `_evidence/s07/sbom.cdx.json` |
| One-dir/no-UPX/PE-metadata specs | ✅ DONE | `packaging/**/*.spec`, `version_info.txt` |
| Intune/WDAC/ASR/CFA config + runbooks | ✅ authored | `packaging/windows/**`, `asr-cfa-design-rules.md` |
| SLSA provenance template | ✅ template | `slsa-provenance.template.json` |
| Azure Trusted Signing run | ⏳ PENDING PW-2 + S10 | `sign_windows.ps1` (runbook) |
| Apple notarization run | ⏳ PENDING Apple ID | `sign_notarize_macos.sh` (runbook) |
| WDAC policy applied | ⏳ PENDING PW-3 | `wdac-managed-installer-policy.md` |
| Intune deploy + Defender/ASR evidence | ⏳ PENDING PW-4 | `defender-sandbox-report.md` (template) |
| Cyber-review sign-off | ⏳ PENDING PW-1 | register + `nist-csf-2.0-profile.md` |
| Signed-artifact production | ⏳ gated on S10 real-eval | sequencing gate |

## Reproduce

```bash
python3 _evidence/s07/generate_evidence.py     # grep-count + no-subprocess + validate specs/SBOM/SLSA
packaging/macos/build_macos.sh                 # real macOS one-dir
packaging/linux/build_linux.sh                 # real Linux x86_64 + aarch64
```

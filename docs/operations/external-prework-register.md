# External pre-work register (S08 → s07/pkg-03 dependency track)

**Session:** S08 · **Date:** 2026-06-27 · **Status of this register: confirmed at S08's before-dispatch human checkpoint.**

These items are **external dependencies that an automated build agent CANNOT
execute**. They require a named human owner inside Acme, have real lead times,
and are recorded here as `status: PENDING` — **not fabricated as done**. This
register is duplicated into the s07/pkg-03 evidence pack; because s07 *depends
on* s08, the s07 copy alone cannot pre-gate s08, so this copy is the authoritative
one for the s08 checkpoint (HARDENED:codex-verify-r2).

> **Owner convention:** "the maintainer" is the **accountable** owner (he
> books/sponsors); the bracketed team is the **responsible executor**. Names of
> individual Acme staff are intentionally left as role labels here per the vault
> comms-policy (anonymise external-facing docs); maintainer substitutes the real
> named individual at booking time.

| # | Item | Owner (accountable / executor) | Lead time | Status | Blocks | Notes |
|---|---|---|---|--:|---|---|
| PW-1 | **Corporate cyber-review of the NIST CSF 2.0 profile** (sign-off) | the maintainer / Acme Cyber (CISO team) | ~3–4 wks (booking + review cycle) | **PENDING** | pilot expansion; s07 checkpoint | The CSF profile (`nist-csf-2.0-profile.md`) is **cyber-REVIEWED, not self-attested**. Reviewer identity, dated outcome, open exceptions + risk owner, and pilot/fleet conditions are filled in by the reviewer — see the profile's "Sign-off" block. **Abort branch:** if the review REJECTS the egress posture → halt + stay on Obsidian+SC (first-class outcome, below). |
| PW-2 | **Azure Trusted Signing onboarding** (code-signing identity for the Windows/Mac `brain` binaries) | the maintainer / Acme EUC + Cyber | ~2–3 wks (tenant onboarding + identity validation) | **PENDING** | pkg-03 signed binaries; WDAC Managed-Installer trust | Without a signed binary the WDAC Managed-Installer policy (PW-3) has nothing to trust and the Keychain ACL "signed designated requirement" leg of SEC-02 is weaker. |
| PW-3 | **WDAC Managed-Installer policy** (allow the signed `brain` installer on the Acme Windows image) | the maintainer / Acme EUC (Intune/endpoint) | ~2–3 wks (policy authoring + ring test) | **PENDING** | application-control control (PR.PS); Cowork-Windows surface | Depends on PW-2 (a signing identity to anchor the rule). |
| PW-4 | **Intune test device** (a managed Windows endpoint to validate packaging + Defender + the Managed-Installer policy + Cowork-Windows) | the maintainer / Acme EUC (Intune) | ~1–2 wks (device provisioning) | **PENDING** | pkg-03 install validation; val-02 as-built | The AS-BUILT scheduled-task + install evidence is a later (s09 build → s10/val-02 acceptance) artifact; this device is the surface it runs on. |

---

## S07 extension — two-track dashboard (HARDENED:r2-codex) — added 2026-06-27

> **The pre-work register is NOT a documentation field — it is Track B, the real
> critical path.** "~9 days" is the **BUILD track (Track A) only**; it is NOT a
> ship date. The ship date is gated by Track B (external IT/Security procurement)
> **and** the S10 real-eval gate. Both tracks are shown so neither is read as a
> ship date.

### Track A — BUILD (agent-executable; mostly DONE this session)

| Item | Owner | Status | Evidence |
|---|---|---|---|
| PyInstaller one-dir specs (win/mac/linux), no-UPX, PE version metadata, entry shim | maintainer / build | ✅ DONE | `packaging/**`, `_evidence/s07/build-transcript-*.txt` |
| Real unsigned macOS one-dir build + smoke | maintainer / build | ✅ DONE | `build-transcript-macos.txt` |
| Real Linux aarch64 + x86_64 ELF builds (buildx) | maintainer / build | ✅ DONE | `build-transcript-linux*.txt` |
| ASR/CFA design rules + in-process verification | maintainer / build | ✅ DONE | `asr-cfa-design-rules.md`, `no-subprocess-spawn.txt` |
| Azure-signing / Intune / WDAC config + runbooks | maintainer / build | ✅ DONE (authored) | `packaging/windows/**`, `packaging-windows-runbook.md` |
| SBOM (CycloneDX) + SLSA template + Defender report template + CSF map | maintainer / build | ✅ DONE | `_evidence/s07/sbom.cdx.json`, `slsa-provenance.template.json`, `defender-sandbox-report.md` |

Track-A build effort estimate: **~9 days** of build work (NOT a ship date).

### Track B — EXTERNAL PRE-WORK (named owner · start · target · status · gate it unblocks)

These are **multi-week, multi-party IT/Security procurements** an agent cannot do.
Owner names are **`TBD — maintainer to assign`** placeholders where the real person is
not knowable here (clearly marked; not invented), with the responsible Acme team
named. Start/target dates are **planning placeholders** to be set at kickoff.

| # | Item | Accountable / Responsible owner | Lead time | Start (TBD) | Target (TBD) | Status | Gate it unblocks |
|---|---|---|---|---|---|--:|---|
| PW-1 | Corporate cyber-review of the NIST CSF 2.0 profile (sign-off) | maintainer / **Acme Cyber (CISO) — TBD named** | ~3–4 wks | TBD-kickoff | TBD-kickoff+4w | **PENDING** | pilot expansion; **abort-branch decision** |
| PW-2 | Azure Trusted Signing onboarding (+ **region-gating check**: US/CA/EU/UK only) | maintainer / **Acme EUC + Cyber — TBD named** | ~2–3 wks | TBD-kickoff | TBD-kickoff+3w | **PENDING** | signed binaries; WDAC trust anchor; SEC-02 signed-DR leg |
| PW-3 | WDAC Managed-Installer policy (trust IME on the Acme image) | maintainer / **Acme EUC (Intune/endpoint) — TBD named** | ~2–3 wks | TBD (after PW-2) | TBD+3w | **PENDING** | application-control (PR.PS); one-step IT approval |
| PW-4 | Intune test device (validate packaging + Defender + Managed-Installer + Cowork-Windows) | maintainer / **Acme EUC (Intune) — TBD named** | ~1–2 wks | TBD-kickoff | TBD-kickoff+2w | **PENDING** | **val-01** clean-install acceptance; Defender/ASR evidence |

**Critical path:** PW-2 → PW-3 (PW-3 needs a signing identity to anchor examples);
PW-1 + PW-4 run in parallel. Earliest signed-pilot start = max(PW-1, PW-3, PW-4)
**AND** S10 real-eval green. **No signed artifact before all three of: PW-2 done,
S10 eval green, S08 security green** (the last is already green).

### Fleet vs single-device decision (HARDENED:claude — confirm at kickoff)

**Default: single managed-device pilot first** (personal-tool scope), expand to
fleet only after val-01 passes. This bounds the WDAC policy blast radius and
scopes val-01 to one device. The fleet path additionally needs the
`raci-ownership-support.md` ownership model in place (val-04).

---

## Abort branch (first-class outcome — HARDENED:r2-claude)

If **PW-1 rejects the egress posture**, OR **PW-2/PW-3 cannot be obtained for the
Acme tenant** (no signing identity / WDAC cannot trust the binary), the defined
outcome is:

> **HALT the cutover. Stay on Obsidian + Smart Connections. Do NOT decommission
> the incumbent. Do NOT push the Profile-A substrate into operational use.**

This is a planned, acceptable end-state — not a failure to route around. The
substrate code remains built and reviewable; only the operational swap is held.
This mirrors the S05 eval gate's abort contract (fail → keep incumbent).

## Sequencing note (HARDENED:r2-verify-r1)

The **one sanctioned scheduled task** (ux-02 brief/digest, the guaranteed daily
drain floor) is **BUILT in s09**, which runs *after* s08. So s08 (this register)
+ the CSF profile carry the task **SPEC/design** (intended name, user context,
signed command, uninstall path — all knowable at design time). The **as-built
runtime evidence** (real registration, logs) is a **s10/val-02** acceptance
artifact. Nothing here claims as-built scheduled-task evidence before s09 runs.

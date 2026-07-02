# NIST CSF 2.0 Organizational Profile — Profile A `brain` substrate

**Document type:** Organizational Profile per **NIST SP 1301** (CSF 2.0
Organizational Profile guidance) · **CSF version:** 2.0 (Feb 2024).
**System:** Profile A local any-LLM second brain (`brain` engine: Markdown truth +
derived sqlite-vec/FTS5 index, multi-harness, Cowork-Windows-first).
**Scope:** single-user, local-first; no model API keys held; one egress (desktop
app model call). **Date:** 2026-06-27 · **Session:** S08.

**Target maturity (Implementation Tier):** **Tier 2 (Risk Informed)**, trending
**Tier 3 (Repeatable)** on **DETECT** and **GOVERN — Cybersecurity Supply Chain
(GV.SC)**. Rationale: a single-user local tool does not justify Tier 3/4
organisation-wide; but the audit/anchor (DETECT) and the per-vendor egress posture
register (GV.SC) are already repeatable/codified, so they trend higher.

> **This profile is cyber-REVIEWED, not self-attested.** The sign-off block at
> the end is **PENDING** an external Example Corp Cyber review (external pre-work PW-1).
> Nothing below claims a passed review. "Evidence" columns point to code/docs/tests
> that exist in this repo; the *adequacy* judgement is the reviewer's.

---

## How to read the tables

- **Current** = the as-built state in this repo today.
- **Target** = the Tier-2 (or trending-3) end state at operational cutover.
- **Evidence** = the artifact a reviewer inspects (path is repo-relative).
- Gaps and PENDING externals are stated, not hidden.

---

## GOVERN (GV)

| Subcategory | Current | Target | Evidence |
|---|---|---|---|
| **GV.OC** Organizational context | Single-user local tool superseding Obsidian+SC; substrate readiness ≠ operational cutover (a separate plan) | same, documented | `AGENTS.md` §7; `docs/substrate-spec.md` |
| **GV.RM** Risk management strategy | Risk-informed: security budget placed on **egress** (the real exposure), not at-rest; abort branch defined | Tier 2 | `_design_profile_a_architecture_v5` §2–3; abort branch below |
| **GV.SC-01/07/09** Cyber supply chain | **Trending Tier 3.** No API keys held; per-vendor no-train/ZDR posture is a codified, machine-readable register; MCP adapter optional/pinned | VERIFIED vendor postures + pinned deps | `docs/harness-allowlist.json`; `docs/operations/egress-provider-posture.md` §4; `tests/test_harness_allowlist.py` |
| **GV.PO** Policy | Conventions + security posture are a single contract file; classification required everywhere | same | `AGENTS.md` §5 (security posture); `docs/classification-scheme.md` |
| **GV.RR** Roles/authorities | Host=writer/owner; VM=read+draft; commit is human-gated host-broker | same | `AGENTS.md` §6; `src/brain/core.py` (`RoleError`) |
| **GV.OV** Oversight | Eval gate + cyber review are the oversight checkpoints | reviewed | `eval/gate.py` (S05); PW-1 review (below) |

## IDENTIFY (ID)

| Subcategory | Current | Target | Evidence |
|---|---|---|---|
| **ID.AM** Asset management | Markdown truth is the asset; index is derived/disposable (rebuildable); dependency inventory maintained | same | `docs/dependency-inventory.md`; `AGENTS.md` §1 |
| **ID.AM-08** Data classification | 5-tier scheme (`Public<Internal<Confidential<Restricted<Secret`), required on every note, deny-by-default | same | `docs/classification-scheme.md`; `src/brain/classification.py`; `tools/validate.py` |
| **ID.RA** Risk assessment | Lethal-trifecta threat model per execution path; importable-core bypass assessed + resolved | same | `egress-provider-posture.md` §1–2; `tests/test_direct_file_read.py` |
| **ID.RA (supply chain)** | Clean-room build (no AGPL fork); code-origin audit gate | same | `docs/clean-room-log.md`; `tools/code_origin_audit.py` |

## PROTECT (PR)

| Subcategory | Current | Target | Evidence |
|---|---|---|---|
| **PR.DS-01** Data at rest | **FDE (FileVault/BitLocker) + OS perms** baseline; dormant AES-256-GCM module (OFF by default; flip-list) | same; flip on a trigger | `at-rest-posture.md`; `src/brain/encryption.py`; `tests/test_encryption_module.py` |
| **PR.DS-02** Data in transit | TLS to the vendor model endpoint; **classification gate + projection** decide *what* leaves | VERIFIED vendor ZDR | `egress-provider-posture.md`; `src/brain/egress.py` |
| **PR.DS-10/11** Data confidentiality / backup | **Encrypted off-device backup** with a dated, successful **restore test**; index excluded (rebuildable) | scheduled encrypted backup | `src/brain/backup.py`; `_evidence/s08/restore-test-log.md`; `tests/test_backup_restore.py` |
| **PR.AA** Identity & access | Host/VM trust split; VM holds no signing key; commit is host-broker only | same | `AGENTS.md` §6; `src/brain/core.py` |
| **PR.PS** Platform security | App-control: **Azure Trusted Signing** + **WDAC Managed-Installer** (EXTERNAL, PENDING); Defender/Intune packaging | signed + WDAC-trusted | external pre-work PW-2/PW-3/PW-4 |
| **PR.PS (least privilege)** | Read+draft VM; egress chokepoint; `write_note` human-gated, fails closed | same | `src/brain/egress.py`; `src/brain/core.py` (`write_note`) |
| **PR.IR** Tech resilience | Markdown-truth + disposable index = always rebuildable; atomic snapshot publish | same | `src/brain/snapshot.py`; `src/brain/index.py` (`rebuild`) |

## DETECT (DE) — *trending Tier 3*

| Subcategory | Current | Target | Evidence |
|---|---|---|---|
| **DE.AE** Anomalies & events | Ed25519 **signed hash-chain** over every committed write; tamper breaks the chain | same | `src/brain/audit.py`; `tests/test_audit_chain.py` |
| **DE.AE (independent attestation)** | **Off-host anchor** of the signed chain head → a fully-re-signed silent rewrite by the key-holder is **detectable** (internal verify misses it; the anchor catches it) | scheduled daily anchor + optional RFC-3161 timestamp authority | `src/brain/anchor.py`; `_evidence/s08/anchor-demo.txt`; `tests/test_anchor.py` |
| **DE.CM** Continuous monitoring | `brain status` surfaces index/snapshot generation+age + pending-draft count; eval gate regression | scheduled health | `src/brain/core.py` (`status`); `eval/gate.py` |

## RESPOND (RS)

| Subcategory | Current | Target | Evidence |
|---|---|---|---|
| **RS.MA / RS.AN** | Audit `verify` + off-host `verify-anchor` localise tamper to an entry index; divergence verdict names the mismatch | runbook on divergence | `src/brain/audit.py` (`verify`); `src/brain/anchor.py` (`verify_against_anchor`) |
| **RS.MI** Mitigation | Fail-closed write path; compensating `write_failed` chain entry on partial write | same | `src/brain/core.py` (`write_note` F-06) |
| **RS.CO** Reporting | Cyber-review channel (PW-1); abort branch is a defined escalation outcome | reviewed | external-prework-register.md |

## RECOVER (RC)

| Subcategory | Current | Target | Evidence |
|---|---|---|---|
| **RC.RP** Recovery plan | Encrypted off-device backup + **tested restore** (byte-identity sha256 match) | scheduled + periodic restore drills | `_evidence/s08/restore-test-log.md`; `src/brain/backup.py` |
| **RC.RP (rebuild)** | Index is disposable — `brain rebuild` reconstructs from Markdown truth | same | `AGENTS.md` §1; `src/brain/index.py` |

---

## The ONE sanctioned scheduled task (threat-model entry — r2-codex)

A cyber reviewer will (correctly) reject "no scheduled task." There is exactly
**one**, and it doubles as the guaranteed daily capture-drain floor. **Design-time
SPEC** (the task is BUILT in s09; as-built runtime evidence is an s10/val-02
artifact — do NOT read as-built here, per r2-verify-r1):

| Attribute | Value (SPEC) |
|---|---|
| **Task name (intended)** | `profile-a-brain-brief` (ux-02 morning brief/digest) |
| **User context** | runs as the **host** user (EDR-visible), NOT the VM; never the EDR-blind Cowork sandbox |
| **Signed command** | the **signed** `brain` binary (PW-2 Azure Trusted Signing); invokes `brain sync --publish` (drain+sign+index+snapshot) then the digest — no network egress beyond the vendor model channel already governed by §egress |
| **Cadence** | daily (morning) |
| **Logs** | writes a dated run record; audit-chain entries are signed; `brain status` reflects the post-run generation/age |
| **Uninstall path** | remove the OS schedule entry (launchd plist / Task Scheduler / Intune policy); the binary + vault are unaffected (index is rebuildable) |
| **Why only one** | the host drains **on invoke** (first step of any `sync`); no capture daemon, no dedicated drain task — fewer unattended surfaces = smaller threat model |

This entry is duplicated into the s07/pkg-03 evidence pack.

---

## Abort branch (first-class outcome — r2-claude)

If the cyber review (PW-1) **REJECTS the egress posture**, OR the signing identity
/ WDAC trust (PW-2/PW-3) **cannot be obtained** for the Example Corp tenant:

> **HALT cutover. Stay on Obsidian + Smart Connections. Do NOT decommission the
> incumbent. Do NOT carry the substrate into operational use.**

A planned, acceptable end-state — recorded in `external-prework-register.md`.

---

## Sign-off (PENDING — external, do NOT self-attest)

| Field | Value |
|---|---|
| Reviewer (identity / team) | _PENDING_ — Example Corp Cyber (CISO team), named individual at booking |
| Review date | _PENDING_ |
| Outcome | _PENDING_ — {ACCEPT / ACCEPT-WITH-EXCEPTIONS / REJECT→abort branch} |
| Open exceptions | _PENDING_ (each with a named risk owner + remediation date) |
| Risk owner for residual risk | _PENDING_ |
| Conditions for **pilot** expansion | _PENDING_ (e.g. VERIFIED vendor posture for the pilot's harness; PW-2/PW-3 complete) |
| Conditions for **fleet** expansion | _PENDING_ (multi-user → flip at-rest encryption; EDR coverage of all surfaces) |

External pre-work that must complete before/at this review:
`docs/operations/external-prework-register.md` (PW-1…PW-4).

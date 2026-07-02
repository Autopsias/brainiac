# S08 evidence — Security & NIST (SEC-01 / SEC-02 / SEC-03 / SEC-04)

**Session:** S08 · **Date:** 2026-06-27 · **Repo:** `/Users/user/DeveloperFolder/profile-a-brain/`
**Builds on:** S02 core (classification filter, Ed25519 audit chain, key custody) · S04 agentic tools · S06 host/VM trust split + projection.
**Model note:** planned model unavailable → executed on **Opus**.
**Posture (design v5):** egress-first, not encrypt-everything. FDE is the at-rest baseline; the budget goes to the model-call egress.

---

## Bottom line

The security posture is built **egress-first**. The classification gate now funnels
through **one chokepoint** covering **every** content-returning subcommand
(SEC-01); the per-vendor no-train/ZDR posture is a codified register with all three
vendors honestly **PENDING** (we assert no coverage we cannot confirm). At-rest is
**FDE baseline** with a **dormant** AES-256-GCM module (OFF by default, flip-list
documented) and **no-file-fallback** key custody (SEC-02). The signed audit chain
is **anchored off-host** so a key-holder's silent rewrite is detectable, and an
**encrypted off-device backup has a dated, passing restore test** (SEC-03). The
**NIST CSF 2.0 Organizational Profile (SP 1301, Tier 2, trending Tier 3 on
DETECT/GV.SC)** with a per-control evidence table exists and is **cyber-review-
PENDING (not self-attested)** (SEC-04). The external pre-work (cyber review, Azure
Trusted Signing, WDAC, Intune device) is a register with named owners + lead times
+ status=PENDING; the **abort branch (cyber-review rejects → halt + stay on
Obsidian+SC)** is a first-class documented outcome.

**Tests:** 34 new S08 tests; 74 security-relevant tests pass; full suite 132
passed. The only failures (3) are **pre-existing, environmental** (ranx not
installed under py3.14; S05 scope) and fail identically on the committed HEAD.

---

## SEC-01 — Egress controls ✅

- **Single egress chokepoint** (`src/brain/egress.py::apply_gate`) shared by the
  CLI and the optional MCP adapter — no second egress path. CLI `_filter_dicts`
  and MCP `_filtered` both delegate to it.
- **Per-subcommand coverage (r2-codex):** the deny-by-default gate fires on
  EVERY content-returning subcommand — `search`/`hybrid-search` (incl. `--rerank`),
  `grep`, `bases-query`, `graph-expand`, `get`/`read`, `recent`. The canonical
  list (`egress.CONTENT_RETURNING_SUBCOMMANDS`) is **asserted exhaustive** by a
  test so a new content path can't slip an un-gated surface in. Real per-subcommand
  output: `_evidence/s08/egress-per-subcommand.txt` (every line `SENSITIVE_LEAKED=[]
  → PASS`; `get/read restricted-deal` → exit 2 withheld; `--max-tier Restricted`
  → human-gate elevation surfaces it).
- **Trifecta break per execution path** + **HITL** (write_note host-gated, fails
  closed) + **importable-core bypass resolved** (untrusted code contained by
  projection + VM split, not import-level filtering — C-3 consistent):
  `docs/operations/egress-provider-posture.md`.
- **Provider posture VERIFIED per vendor (HARDENED:claude):** all three vendors
  (Anthropic/Claude, OpenAI/Codex, Google/Gemini) are **PENDING** in
  `docs/harness-allowlist.json` with explicit verification step + named owner +
  lead time; default posture = allowlist/projection. **Openness ≠ "any app"** —
  a harness is allowed only when its vendor posture is VERIFIED; **val-03's
  cross-harness set must equal the VERIFIED subset** (r2-claude).
- Tests: `tests/test_egress_per_subcommand.py` (11), `tests/test_harness_allowlist.py` (6).

## SEC-02 — FDE baseline + dormant encryption + key custody ✅

- **FDE + OS perms** documented as the at-rest baseline (no over-engineering):
  `docs/operations/at-rest-posture.md`.
- **Dormant conditional encryption module** (`src/brain/encryption.py`): AES-256-GCM,
  **OFF by default** (`is_enabled()` False unless `BRAIN_ENCRYPTION=on`),
  **flip-list** codified (`FLIP_LIST`). `force=True` is the off-device-backup path.
- **Key custody — NO file fallback** (both signing + encryption keys): env PEM/key
  → env CMD → macOS Keychain → Windows Credential Manager → **fail closed**.
  Verified by `test_no_key_fails_closed` (audit + encryption).
- Tests: `tests/test_encryption_module.py` (9) — off-by-default, flip-on round-trip,
  tamper detection, wrong-key fail, fail-closed, bad-key-length reject.

## SEC-03 — Off-host anchor + encrypted backup + restore test ✅

- **Off-host audit anchor** (`src/brain/anchor.py`): publishes the signed chain
  head to an independent append-only store; `verify_against_anchor` recomputes the
  head as-of each anchored entry-count. **Demo (`_evidence/s08/anchor-demo.txt`):**
  a key-holder rewrites entry 1 and FULLY RE-SIGNS — internal `verify()` returns
  `ok` (re-signed), but the **off-host anchor returns `divergence` → DETECTED**.
  Also detects chain truncation.
- **Encrypted off-device backup + dated restore test**
  (`_evidence/s08/restore-test-log.md`): backup AES-256-GCM-encrypted, restore
  byte-identical (plaintext sha256 match), Restricted plaintext **absent** from the
  ciphertext, derived `.brain/` index excluded. Tamper → AES-GCM auth failure on
  restore.
- CLI: `brain anchor` / `brain verify-anchor` / `brain backup` / `brain restore`
  (host-broker; refused on the VM leg).
- Tests: `tests/test_anchor.py` (4), `tests/test_backup_restore.py` (4).

## SEC-04 — NIST CSF 2.0 Organizational Profile (SP 1301, Tier 2) ✅

- `docs/operations/nist-csf-2.0-profile.md`: all six functions (GOVERN, IDENTIFY,
  PROTECT, DETECT, RESPOND, RECOVER), **Current vs Target**, per-control **evidence
  table** pointing at real code/docs/tests, target **Tier 2 trending Tier 3 on
  DETECT + GV.SC**.
- **Cyber-REVIEWED, not self-attested** (r2-codex): the Sign-off block is PENDING
  with reviewer identity/team, dated outcome, open exceptions + risk owner, and
  pilot/fleet conditions left for the reviewer.
- **The ONE sanctioned scheduled task** (`profile-a-brain-brief` / ux-02) is named
  with its **SPEC** (user context, signed command, logs, uninstall path) — design-
  time only (built in s09; as-built is s10/val-02). "No scheduled task" would be
  false; this is the threat-model entry.
- **Abort branch** folded in as a first-class outcome.

## External pre-work register (required evidence)

`docs/operations/external-prework-register.md` — PW-1 corporate cyber-review,
PW-2 Azure Trusted Signing, PW-3 WDAC Managed-Installer, PW-4 Intune test device.
Each: named owner (accountable/executor) + lead time + `status: PENDING`, marked
clearly as an external dependency for maintainer/the org — **not fabricated as done**.
Confirmed at S08's before-dispatch human checkpoint (this copy is authoritative
since s07 depends on s08).

---

## Hardening compliance

| Item | Where addressed |
|---|---|
| claude — verify no-train/ZDR PER VENDOR; allowlist not "any app"; importable-core bypass below the import line / via gated boundary | `harness-allowlist.json` (3 vendors PENDING + verification steps); `egress-provider-posture.md` §2–4 |
| codex — cyber-review on the s07 pre-work track with owner + lead time | `external-prework-register.md` PW-1 |
| codex-verify-r2 — pre-work register is ALSO s08 evidence, confirmed at s08 checkpoint | `external-prework-register.md` (header) |
| r2-codex — egress tests cover EVERY content subcommand (incl. graph-expand/bases-query/rerank) | `test_egress_per_subcommand.py` + exhaustiveness assert; `_evidence/s08/egress-per-subcommand.txt` |
| r2-codex — name the ONE sanctioned scheduled task in the CSF profile + pre-work | CSF profile § "ONE sanctioned scheduled task"; register sequencing note |
| r2-claude — openness vs control = vendor-posture allowlist; val-03 set EQUALS it | `egress-provider-posture.md` §4; `harness-allowlist.json` policy |
| r2-codex — NIST cyber-REVIEWED, sign-off PENDING/external | CSF profile § "Sign-off (PENDING)" |
| r2-claude — abort branch = halt + keep incumbent | CSF profile + register § "Abort branch" |
| r2-verify-r1 — ux-02 SPEC at design time; as-built is s10/val-02 | CSF profile scheduled-task table; register sequencing note |

---

## Evidence artifacts (all under repo, non-empty)

- `docs/operations/s08-evidence.md` — this master
- `docs/operations/nist-csf-2.0-profile.md` — SEC-04 CSF 2.0 Organizational Profile
- `docs/operations/external-prework-register.md` — external dependency register
- `docs/operations/egress-provider-posture.md` — SEC-01 egress + per-vendor posture
- `docs/operations/at-rest-posture.md` — SEC-02 FDE baseline + flip-list + custody
- `docs/harness-allowlist.json` — trusted-harness allowlist register (val-03 source)
- `_evidence/s08/egress-per-subcommand.txt` — per-subcommand gate results
- `_evidence/s08/restore-test-log.md` — dated encrypted-backup restore test
- `_evidence/s08/anchor-demo.txt` — off-host anchor detects a re-signed rewrite
- `_evidence/s08/grep-count.txt` — engagement counts
- `_evidence/s08/pytest-summary.txt` — test results (132 passed; 3 pre-existing env failures)
- `_evidence/s08/generate_evidence.py` — reproducible evidence generator
- `src/brain/egress.py · encryption.py · anchor.py · backup.py` — new modules
- `tests/test_egress_per_subcommand.py · test_encryption_module.py · test_anchor.py · test_backup_restore.py · test_harness_allowlist.py` — 34 new tests

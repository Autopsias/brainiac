# S10 — Validation (FINAL) evidence

Session S10 of the Profile A second-brain framework build. Validates the
substrate built across S02–S09 and produces the **first REAL retrieval A/B
verdict on the real Example Corp corpus** (VAL-04), plus the in-sandbox legs of
VAL-01/02/03 and the operational-cutover hooks.

> **Substrate readiness ≠ operational cutover.** These 10 sessions migrate the
> CORPUS and build the substrate. They do NOT retire Smart Connections / Bases,
> do NOT repoint the live Example Corp control plane, and do NOT flip any scheduled
> task. That is the separate, maintainer-gated follow-on plan (hooks below).

> **Honesty rule.** A real Intune install on a managed Windows device (VAL-01),
> a real Claude-Desktop Cowork-Windows session (VAL-02 device leg), the actual
> Codex/Gemini/Code-tab desktop apps (VAL-03 app legs), and a real corporate
> cyber-review sign-off (VAL-04 second half) **cannot be performed from this
> sandbox and are NOT fabricated.** Each is run as far as genuinely possible
> locally; the device/app/sign-off parts are PENDING acceptance artifacts with
> named owners in `external-prework-register.md`.

---

## VAL-04 — Eval gate (real A/B) + CSF sign-off
**HEADLINE: GATE FAIL → ABORT BRANCH (result PARTIAL/BLOCKED).** On the REAL Example Corp
corpus, n=66/66, with the catalogued multilingual PROXY model
`paraphrase-multilingual-MiniLM-L12-v2` (Arctic-embed-m-v2.0 is not in the
fastembed catalog), the new `brain` retriever is **materially inferior** to Smart
Connections: overall Recall@10 0.427 vs SC 0.609 (Δ=−0.182, 95% CI lo −0.308 ≪
−2pp bound), driven by a **monolingual PT collapse** (0.083 vs 0.750) and ES
(0.333 vs 1.000). brain is competitive-or-better on EN (+0.022), EN→PT
cross-lingual (+0.25), temporal (+0.15), and ~tied on lexical identifiers
(0.958 vs 1.0) — the architecture is sound; the **embedding model is the gating
variable**. Reranking does not change Recall@10. **Recommendation: HALT, stay on
Obsidian + SC; re-run with the production Arctic checkpoint (or
`multilingual-e5-large`) and require a GREEN gate before any cutover.**
Full diagnosis: `s10-eval-verdict.md`. Artifacts:
`_evidence/s10/real-ab-scorecard.json` (+ `.md`), `real-ab-scorecard-rerank.json`,
`gate-hybrid-transcript.txt`, `eval/runs/current_sc.frozen.json`,
`eval/runs/new_brain_real.json`.

Method (real, not machinery-only):
- **Real corpus, real embeddings.** The real Example Corp vault
  (`/Users/user/Downloads/Example-Vault`, read-only source — never
  modified) was indexed into `brain` with a REAL multilingual embedding model.
  Arctic-embed-m-v2.0 (design of record) is NOT in the fastembed catalog
  (confirmed S03 + S10), so the **transparent proxy** is the catalogued,
  locally-cached **`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`**
  (384-d, real cross-lingual vectors) — disclosed here and in the verdict. Index:
  2250 frontmatter-bearing notes; app-data index at `_evidence/s10/brain-index/`
  (NOT in the vault). Wired via the new env-gated `CatalogEmbedder`
  (`src/brain/embed.py`); all sensitive content stayed local (no egress).
- **Frozen SC baseline (the incumbent "today").** Captured by driving the live
  Smart Connections MCP (`mcp__smart-connections__lookup`, note-level, k=20) over
  the golden set; SC index = `Xenova/multilingual-e5-small`, 3532 notes (real,
  current). Frozen into `eval/runs/current_sc.frozen.json`.
- **A/B** over the 66-query bilingual golden set qrels (`eval/qrels/qrels.json`;
  all 104 qrels doc-refs verified present on disk). Metrics via `eval/harness.py`
  (ranx; works on the 3.14 eval venv), gate via `eval/gate.py` (bootstrap 95% CI
  lower bound on per-query Recall@10 delta ≥ −2pp; p95 not worse).

CSF sign-off (second half): the NIST CSF 2.0 profile EXISTS and is structured as
**cyber-REVIEWED, not self-attested** (`nist-csf-2.0-profile.md`, Tier 2; Sign-off
block PENDING external review **PW-1**, owner maintainer / Example Corp Cyber). Bus-factor>1
ownership model EXISTS (`raci-ownership-support.md`, Primary + Backup per
checkpoint). **Cyber-review sign-off: PENDING (out-of-sandbox).**

---

## VAL-01 — Virgin locked-Windows install (Defender clean)
**Locally-doable leg = macOS packaged-binary smoke** (`_evidence/s10/val01-binary-smoke.txt`).

- The S07 macOS PyInstaller one-dir binary (`dist/brain/brain`, Mach-O arm64,
  **no PyTorch/ONNX in the bundle** — the footprint/Defender win) **launches and
  parses the CLI** (`brain --help` exit 0).
- **REAL FINDING (S07 packaging defect):** `brain rebuild` crashes — the native
  sqlite-vec extension `vec0.dylib` was NOT bundled into
  `_internal/sqlite_vec/`, so the vector backend `dlopen` fails. The Windows
  packaging spec (the actual VAL-01 target) must bundle the native `vec0` lib.
- **Root-caused + FIXED in source:** `get_backend("auto")` could not degrade
  because `SqliteVecBackend.available()` was import-only (the dlopen failure is
  deferred to `setup()`). Added `SqliteVecBackend.loadable()` (a real
  load-probe) and gated the `auto` selector on it
  (`src/brain/vectors.py`), so a build missing the native lib now degrades to the
  brute-force backend instead of crashing. **193 tests green** after the fix.
- The **source install works fully** with sqlite-vec (rebuild + search verified).
- **PENDING (out-of-sandbox, cannot run here):** real Intune IME install on a
  managed locked-Windows device + Microsoft Defender for Endpoint / ASR
  detonation. Owners + lead times: `external-prework-register.md` PW-2 (Azure
  Trusted Signing), PW-3 (WDAC Managed-Installer), PW-4 (Intune test device);
  Defender report template `_evidence/s07/defender-sandbox-report.md`.

---

## VAL-02 — Cowork-Windows smoke incl. the full capture loop
**Locally-doable leg = the role-split capture loop end-to-end**
(`_evidence/s10/val02-capture-loop.txt`). All steps PASS:

1. **VM leg** (`brain --role vm capture`) drops an **unsigned** draft into
   `capture-inbox/` (`signed:false, indexed:false`) — the one quasi-write a VM holds.
2. Draft staged, NOT indexed (host has not drained yet).
3. **HOST** (`brain --role host sync --publish`) drains → **signs + writes** to
   `brain/resources/` (Ed25519 audit chain) → **indexes** → **publishes a
   read-only snapshot** (generation 1, sha256, 1 note).
4. HOST index **retrieves** the captured note.
5. **VM reads the read-only snapshot and retrieves** the note — the role-split:
   the VM never touches the authoritative index, only the published snapshot.
6. `brain verify-audit` → chain OK (1 signed entry).
7. **Role guard:** `brain --role vm write …` is **REFUSED** (`role_forbidden`,
   exit 4) while the same write on the HOST succeeds (exit 0, signed via the
   injected key) — privilege separation holds.

- **PENDING (out-of-sandbox):** a real Claude-Desktop Cowork-Windows session on
  the PW-4 managed device (the as-built scheduled-task + install acceptance).
  Owner: `external-prework-register.md` PW-4.

---

## VAL-03 — Cross-harness smoke (set = the s08 vendor-cleared allowlist)
**The s08 allowlist** (`docs/harness-allowlist.json`) lists 3 harnesses
(claude-desktop, codex-cli, gemini-cli) **all PENDING** vendor verification →
the **VERIFIED subset is EMPTY**. Per the allowlist policy, *val-03's
cross-harness test set MUST equal the VERIFIED subset* → **no harness is cleared
to run against the full vault**; every PENDING harness runs ONLY against a
projected, sensitive-tier-free workspace. This is correctly an empty
full-vault set, not a failure.

**Locally-doable leg = the shared `brain` CLI primitive + the projection
posture** every harness shells into (`_evidence/s10/val03-cli-cross-harness.txt`):

- **A.** `brain search` (deny-by-default, max-tier Internal) surfaces only
  Public+Internal; Confidential + unlabelled withheld.
- **B.** `--max-tier Confidential` surfaces the Confidential note.
- **C.** Unlabelled is treated as **Secret** (default-deny) — only surfaces at
  `--max-tier Secret`. (Implication for the real corpus: most Example Corp notes carry no
  brain-native `classification:` and are therefore default-denied — the S03
  corpus-migration classification pass must assign tiers before full retrieval
  is useful; this is why the eval calls the core retriever directly, measuring
  the retrieval primitive, not the egress-gated surface.)
- **D.** `brain project --dest … --max-tier Internal` writes a projection whose
  on-disk files are **only** Public+Internal — Confidential + unlabelled are
  physically absent (real containment, the posture PENDING harnesses use).
- **E.** `brain grep` also gates (Confidential not surfaced at default).

- Harness wiring is in place (`docs/harness-wiring.md`; canonical `AGENTS.md`
  §5; `CLAUDE.md` `@AGENTS.md` import; `.gemini/settings.json`
  `contextFileName=AGENTS.md`).
- **PENDING (out-of-sandbox):** driving the actual Codex / Gemini / Claude-Desktop
  Code-tab apps, and the vendor no-train/ZDR/Secret verification that would move
  any harness from PENDING→VERIFIED (`external-prework-register.md`; the
  cyber-review PW-1 confirms the verifications).

---

## Operational-cutover hooks (emitted, NOT executed)
The corpus-only scope of these 10 sessions is restated; cutover is a follow-on
plan. Hooks emitted:
- **Dependency inventory** (populated from the S01 template, real paths/line
  refs): `docs/dependency-inventory.md`.
- **Old→new command map**: `docs/operations/cutover-command-map.md`.
- **Scheduled-task rewrite list**: `docs/operations/cutover-scheduled-tasks.md`.
- **SC/Bases retirement gates + dual-run & rollback criteria**:
  `docs/operations/cutover-retirement-and-dualrun.md`.

"**Substrate readiness is NOT operational cutover**" is stated in every hook.

---

## Test baseline
`193 passed` (full suite, `.venv` Python 3.14) after the S10 source changes
(env-gated `CatalogEmbedder`, real-corpus id-collision dedup in `rebuild`,
`SqliteVecBackend.loadable()` auto-degrade). See `_evidence/s10/pytest-summary.txt`.

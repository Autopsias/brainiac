# S05 evidence — A/B eval harness (EVAL-01 / EVAL-02 / EVAL-03)

**Session:** S05 · **Date:** 2026-06-27 · **Repo:** `/Users/user/DeveloperFolder/profile-a-brain/`
**Builds on:** S02 core · S03 chunked Arctic/ONNX embeddings + corpus-migration dry-run · S04 hybrid RRF retrieval.
**Model note:** planned model Fable unavailable → executed on **Opus**; the cross-model review gate (HARDENED:r2-claude) was satisfied by a **Sonnet** review (`_evidence/s05/second-model-review.md`).
**Harness:** ranx 0.3.21 (MIT). Every artifact listed exists on disk and is non-empty.

---

## Bottom line

The **harness + ship gate + golden set + CI script are real, runnable, and
regression-tested** — proven end-to-end on the committed dev vault producing a
real PASS (exit 0) and a real FAIL→abort (exit 1). The **current-system (Smart
Connections) baseline is FROZEN** into a committed ranx run with an SC
index-state hash. The **full-power current-vs-new A/B on the real Example Corp corpus is
scope-limited with disclosure** (below): it is **blocked on the S03 corpus
landing** (the migration is a dry-run today) plus pooled relevance judgments and
exact-pin Arctic activation. No real-corpus current-vs-new verdict is claimed or
fabricated this session — the machinery and the frozen "today" anchor are the
shippable deliverables, exactly as the session's scope exclusion permits.

---

## EVAL-01 — Bilingual EN/PT/ES golden set ✅

`eval/build_golden_set.py` → `eval/golden_set.json` (+ ranx `eval/qrels/qrels.json`).
**66 queries**, graded qrels 0–3 (104 qrel rows) keyed on the **canonical source
path** (real example-vault relative path). Builds + validates with one command;
6 validation classes enforced (unique ids, grade range, temporal-field presence,
cross-lingual token disjointness, path existence vs the S03 manifest).

| stratum | n | power | | language | n | power |
|---|--:|---|---|---|--:|---|
| cross_lingual_en_pt | 10 | marginal | | EN | 46 | gate |
| cross_lingual_en_es | 6 | **smoke** | | PT | 14 | gate |
| multi_hop | 10 | marginal | | ES | 6 | **smoke** |
| temporal | 10 | marginal | | | | |
| monolingual_pt | 12 | gate | | | | |
| monolingual_es | 6 | **smoke** | | | | |
| lexical_identifier | 12 | gate | | | | |

Hardening honoured:
- **Min-n per stratum (8) AND per language** + a power target (12) → segments
  below floor are emitted `power: smoke` so the scorecard/gate **downgrade them
  from ship-gate to smoke** (never silently green). ES is honestly smoke —
  native-ES content in the vault is sparse.
- **Explicit token-overlap rule** for the cross-lingual strata with an
  entity/acronym/system-name **whitelist** (Globex, NewERP, HRSystem… may overlap; other
  content tokens must be disjoint). Validated in code; all 16 cross-lingual
  queries pass.
- **Temporal qrels keyed on path + span + effective_date + version_state**;
  `make_qrels` stamps temporal doc ids as `path#version` so surfacing the **wrong
  version cannot score green**.
- **Blind grading + held-out stratum:** relevance graded from note identity/topic,
  independent of any retriever output; **33/66 queries `held_out=true`** (authored
  purely from domain knowledge, not from any SC-era log) — guards against
  measuring the incumbent's habits. Seed mix tagged per query
  (`past-query-log` / `ragas-verified` / `domain-knowledge`).
- **≥2 annotators + IAA:** Opus (author) + Sonnet (annotator-2) adjudicated a
  12-query sample; agreement reported in `second-model-review.md`.

**Known limitation (disclosed):** qrels are **single-primary-annotator and
sparse** (the definitive note(s) per query). Proper recall scoring on the real
corpus needs **TREC-style pooled judgments** over both systems' top-k — see
EVAL-02 pool-bias note. Until pooling, real-corpus per-class numbers are a
**lower bound**, not a verdict.

---

## EVAL-02 — ranx A/B harness + frozen SC baseline ✅ (real) / scope-limited (real-corpus verdict)

`eval/harness.py` — loads qrels + two run files, **normalises both systems to the
canonical source path** (`eval/path_normalize.py`; SC = identity, brain =
materialisation sidecar), scores **Recall@5/10/20, nDCG@10, MRR@10 + latency
p50/p95** OVERALL, PER-LANGUAGE, PER-CLASS. Comparison is **PAIRED** (only query
ids in BOTH runs) and the scored set is reported (`paired_scope`), so reduced
scope is explicit. Empty retrievals score 0 via a sentinel (ranx-safe).

**Frozen current-SC baseline (HARDENED:consensus).** `eval/capture_sc_baseline.py`
freezes the SC `lookup` ranking into a **version-committed** run
(`eval/runs/current_sc.frozen.json`) with an **SC index-state hash**
(`3d506ded…`, model `Xenova/multilingual-e5-small`, 42 702 searchable entries,
index mtime 2026-06-27) + capture date. The gate compares NEW against THIS file —
**never a live SC call**. A re-baseline **FAILS LOUD** (exit 2, writes nothing)
if SC results/stats are empty — verified.

**Scope of the captured baseline (smoke-grade, disclosed).** Driving the live SC
MCP across all 66 queries was out of scope for one session; the frozen baseline
covers a **representative 8-query subset** spanning all strata + EN/PT/ES.
SC's absolute quality on that real subset (`sc-baseline-scorecard.json`):

| segment | n | Recall@10 | nDCG@10 | MRR@10 |
|---|--:|--:|--:|--:|
| overall | 8 | 0.65 | 0.51 | 0.54 |
| lexical_identifier | 1 | 1.00 | 0.74 | 1.00 |
| temporal | 1 | 1.00 | 1.00 | 1.00 |
| monolingual_pt | 1 | 1.00 | 1.00 | 1.00 |
| multi_hop | 2 | 0.00 | 0.00 | 0.00 |
| cross_lingual_en_pt | 2 | 0.25 | 0.04 | 0.06 |

**Pool-bias finding (important, honest).** SC nails **crisp** queries at rank 1
(Three-Tier Adoption, SC-Pro-declined, M10 Alex Kim succession, LegacyERP all
rank-1). It scores 0 on **multi-hop / vague cross-lingual** queries **not because
it is bad** but because it surfaced OTHER also-relevant notes that are absent
from the sparse single-annotator qrels (classic pool bias). This is why the
real-corpus per-class numbers are a lower bound and require pooled judgments
before they gate anything.

**New-system real-corpus run — BLOCKED (disclosed), not fabricated.** A
comparable NEW run requires the real corpus to be **landed** into the new
substrate (S03 migration is a **dry-run** — 3 318 notes inventoried, 0
materialised) and the Arctic embedder activated (this venv falls back to the
deterministic HashEmbedder). Producing a NEW-vs-SC scorecard over a tiny
materialised subset would be apples-to-oranges (inflated brain recall vs
full-vault SC) — so it is **not** done. The harness is instead **proven
end-to-end** on the dev vault (next section).

**Machinery proof (dev vault, real brain retrieval).** Two real brain runs over
the same 9-note corpus → real scorecard → gate:
- `ci-scorecard.json` / `selftest-scorecard-PASS.json` — paired, segmented, with
  latency p50/p95.

---

## EVAL-03 — CI gate + Arctic-vs-e5 smoke ✅

`eval/gate.py` — **ONE primary gate** (HARDENED:r2-codex): Recall@10
**non-inferiority**, bootstrap 95% CI lower bound of the per-query (new−current)
delta **≥ −2pp**, applied OVERALL + per **gate-power** language **+ per
gate-power class/stratum** (marginal/smoke segments reported, not gating — added
after the S05 cross-model review's NO-STRATUM-GATE finding). Plus **latency
p95(new) ≤ p95(current)**. **Fisher
randomization** (sign-flip) superiority is reported as a **bonus signal only** —
"new ≥ current" is NOT a second, stricter gate.

**Exit contract (verified by tests + live runs):**
- **PASS → exit 0** (`selftest-gate-PASS.txt`): non-inferior + faster.
- **FAIL → exit 1 → ABORT BRANCH** (`selftest-gate-FAIL.txt`, HARDENED:r2-claude):
  prints "HALT. Stay on Obsidian + Smart Connections. Do NOT decommission. Do NOT
  carry the failing build forward." If the gate genuinely fails on real data the
  session result is PARTIAL/BLOCKED with the scorecard attached — never DONE.
- **ERROR → exit 2** (empty paired set): explicitly NOT a pass.

**CI wiring.** `scripts/ci/eval_gate.sh` — build+validate golden set → regen qrels
→ capture two real brain runs → score → gate; **exits with the gate's code**
(`gate-transcript.txt` shows a full green run, exit 0). The production swap (frozen
SC + real-corpus brain run once S03 lands) is documented in the script header.

**Arctic-vs-e5 in-vault smoke** (`arctic-vs-e5-smoke.md`, raw
`arctic-vs-e5-smoke-raw.json`): controlled cross-lingual fixture, both embedder
families run in-process via fastembed. **Both rank the PT/ES parallel above all
distractors** (e5-large xling cos 0.83–0.85; arctic-s 0.74–0.82) — Arctic is
cross-lingually viable. Smoke (family-level proxy: arctic-s / e5-large, not the
exact pins); exact-pin + landed-corpus head-to-head is the follow-up.

---

## System-level eval (separate from the retriever primitive) — HARDENED:claude

`eval/agentic_eval.py` (`agentic-eval.json`): a frozen ranx Run scores the
RETRIEVAL PRIMITIVE; both systems are LLM-orchestrated per query, so "beats
today" at the SYSTEM level needs a separate **task-success / answer-grounding**
check run through the full harness. Dev-vault run: **task_success 1.0**
(a sufficient note in top-k for every task), **answer_grounded 0.6** (top-1 is a
relevant source) — temporal grounding 0.0 exposes the HashEmbedder fallback's
weakness on "latest version" ranking (honest signal). Both this AND the retriever
gate must pass before a system-level "beats today" claim. Real-vault run shares
the S03-landing dependency.

---

## Cross-model review (HARDENED:r2-claude) — `second-model-review.md`

A **Sonnet** model independently reviewed harness/gate/golden-set and served as
**annotator-2**. **Verdict: methodologically sound enough to serve as a ship
gate**, with caveats. **No HIGH findings, no gate-logic bugs.** IAA on a 12-query
sample: **12/12 = 100%** (a spot-check, not a full kappa — disclosed). Four MED
findings, dispositioned:
- **NO-STRATUM-GATE → FIXED**: `gate.py` now applies per-class non-inferiority for
  gate-power strata; CI gate re-run green with `class:*` checks.
- **CI-MED (conservative 2.5th-pctile CI underdocumented) → FIXED**: documented in
  `bootstrap_ci_lower` (deliberately stricter than the one-sided NI bound).
- **SINGLE-ANNOTATOR → ACCEPTED/disclosed**: pooled multi-annotator judgments are
  the documented next step before real-corpus per-class numbers gate anything.
- **TEMPORAL-MED → ACCEPTED/disclosed**: `resolve_version` falls back path-date→
  "current" and records the method; tighten once the corpus carries version FM.

## Hardening compliance

| Item | Where addressed |
|---|---|
| consensus — FREEZE baseline, gate vs committed file, fail-loud | `capture_sc_baseline.py` + `current_sc.frozen.json` + index hash; fail-loud verified (exit 2) |
| claude — retriever primitive ≠ agentic system; separate task-success eval | gate renamed "retriever non-inferior"; `agentic_eval.py` |
| codex — min-n per stratum+lang, token-overlap whitelist, power target, temporal path+span+version | `build_golden_set.py` coverage + validators; `make_qrels` version-stamping |
| DECISION — "today" = Obsidian+SC frozen run; recall meaningful only post-landing | frozen SC baseline IS today; landing dependency disclosed |
| r2-claude — blind grading, ≥2 annotators + IAA, held-out stratum | annotation_protocol + 33 held-out + Sonnet annotator-2 (`second-model-review.md`) |
| r2-codex — ONE gate (−2pp non-inferiority), latency+per-language explicit | `gate.py` single primary gate; Fisher = bonus only |
| r2-claude — abort branch = halt + keep incumbent | `gate.py` exit-1 abort message; PARTIAL/BLOCKED contract |
| scope realism — honest disclosure of reduced baseline | this doc, throughout |

---

## Evidence artifacts (all under repo, non-empty)

- `eval/golden_set.json` · `eval/qrels/qrels.json` — EVAL-01 golden set + ranx qrels
- `eval/build_golden_set.py` · `make_qrels.py` · `path_normalize.py` — builders/normaliser
- `eval/harness.py` · `gate.py` · `capture_sc_baseline.py` · `capture_brain_run.py` · `agentic_eval.py` — harness/gate/capture
- `scripts/ci/eval_gate.sh` — CI ship gate (exit 0/1)
- `eval/runs/current_sc.frozen.json` (+ `_sc_lookup_raw.json` / `_sc_stats.json`) — frozen SC baseline
- `_evidence/s05/sc-baseline-scorecard.json|md` — SC absolute baseline (real subset)
- `_evidence/s05/ci-scorecard.json|md` · `selftest-scorecard-PASS.json` · `selftest-scorecard-FAIL.json` — dev A/B scorecards
- `_evidence/s05/gate-transcript.txt` · `selftest-gate-PASS.txt` · `selftest-gate-FAIL.txt` — gate exit-code proofs
- `_evidence/s05/agentic-eval.json` — system-level task-success/grounding
- `_evidence/s05/arctic-vs-e5-smoke.md|-raw.json` — embedder bake-off
- `_evidence/s05/second-model-review.md` — Sonnet cross-model review + IAA
- `_evidence/s05/grep-count.txt` — engagement counts
- `tests/test_eval_harness.py` — 6 regression tests (full suite 87 passed)

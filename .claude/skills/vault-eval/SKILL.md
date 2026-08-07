---
name: vault-eval
description: "Run the retrieval-quality eval against a brain-substrate vault — five dimensions: S (supersession-correct), X (retrieval-complete), T (temporal-correct), MH (multi-hop), CAL (calibration/refusal). Use monthly, and as the go-live gate after any structural change to the substrate (embedder swap, index rebuild, a change to which read tools the harness composes). Triggers: 'run the eval', 'vault eval', 'retrieval eval', 'monthly eval', 'go-live gate', 'score retrieval'. Also covers the quantitative Recall@k harness (eval/harness.py + eval/gate.py in this repo) for A/B non-inferiority checks against a frozen baseline run, AND the shared-corpus cross-engine A/B (eval/PORTABLE-CONTRACT.md): scoring a SECOND engine (e.g. Memento) that indexes the same corpus against the same golden set + qrels through the same gate — triggers: 'cross-engine eval', 'compare engines', 'A/B against memento', 'head-to-head retrieval', 'score memento on the golden set'. Outputs a dated baseline file with per-question scores and an aggregate. Do NOT use for ad-hoc retrieval probes — write those to a scratch note instead."
---

# vault-eval (brain-substrate kernel)

Three eval surfaces serving different questions:

| Surface | Question it answers | Tooling |
|---|---|---|
| **Standing graded gate (this skill, primary)** | "Did the current engine regress on the frozen 66-query golden set?" | `eval/harness_direct.py` + `eval/gate.py` in this repo |
| **Qualitative cascade eval** | "Does the model, using the brain's composable read tools, answer real questions correctly — supersession, completeness, timing, multi-hop, calibration?" | hand-written questions + `brain search/grep/bases-query/graph-expand/get` |
| **Quantitative Recall@k harness** | "Did a substrate/embedder change regress retrieval, measured against a frozen golden set?" | `eval/harness_direct.py` + `eval/gate.py` in this repo |
| **Cross-engine A/B (shared corpus)** | "Does a DIFFERENT engine indexing the same corpus rank better or worse than brain, on the same golden set + qrels?" | per-engine capture adapters + the same `harness_direct.py`/`gate.py` — contract: `eval/PORTABLE-CONTRACT.md` |

## When to run

- **Monthly and go-live / structural-change gate** — run the behavioural
  probes first as a smoke check, then run the standing 66-query graded gate
  below. A nonzero probe result is a hard stop; probe success does not replace
  graded scoring.
- **Structural changes** include an embedder swap, index rebuild, a change to
  which read tools the harness composes (e.g. adding `graph-rank`), a new CLI
  verb, an egress-filter change, or a vector-backend swap.

## Qualitative cascade eval — five dimensions

| Dim | Name | What it checks |
|---|---|---|
| **S** | Supersession-correct | Answer reflects the latest version of a note, not a stale or archived one |
| **X** | Retrieval-complete | Answer pulls from every relevant note/zone, not just the first hit |
| **T** | Temporal-correct | Answer is drawn from content current as of the question's implied date |
| **MH** | Multi-hop | A question naming ≥2 entities is correctly bridged via `graph-expand` + confirmation reads, not asserted from one note alone |
| **CAL** | Calibration / refusal | Confidence is stated honestly; a question with no answer in the vault is refused, not fabricated |

**Aggregate** = `(S + X + T + MH + CAL) / 5`. Pass bar: **≥80% per
dimension**, not just on the aggregate — an aggregate can mask a single
collapsed dimension (e.g. T=40% hidden behind S=100%/X=100%).

### How to run

1. **Write 10–25 questions** before running (anti-cherry-pick: questions are
   fixed before the run, never edited after seeing results). Cover all five
   dimensions; weight toward multi-hop and at least one deliberately
   vault-absent question (tests CAL refusal).

2. **For each question, let the model compose the brain's read tools** per
   AGENTS.md §5 — lexical-first, embed lazily:
   ```bash
   brain --vault "$BRAIN_VAULT" grep "<exact term>" --json          # cheap first probe, no embedding
   brain --vault "$BRAIN_VAULT" search "<query>" --rerank --json    # semantic escalation
   brain --vault "$BRAIN_VAULT" bases-query --where type=<t> --json # structured frontmatter view
   brain --vault "$BRAIN_VAULT" graph-expand <seed-id> --depth 2 --json  # multi-hop, DISCOVERY-ONLY
   brain --vault "$BRAIN_VAULT" get <id> --json                     # confirm before asserting
   ```
   Record which tools were actually invoked per question — that trace is
   what makes a score auditable, not just a final answer.

3. **Score** S/X/T/MH (where applicable)/CAL per question against the
   actual content in `vault/brain/` and `vault/raw/` — the ground truth is
   whatever the vault currently says, read directly if there's any doubt
   about what `search` returned.

4. **Record** a dated baseline — `_evidence/eval/baseline-YYYY-MM-DD.md` (or
   the deployment's equivalent eval-output location) with one row per
   question, per-dimension pass rate, aggregate, and a regression note
   against the last baseline.

5. **Act.** Any dimension `<80%` is a retrieval-integrity flag — surface
   immediately, don't wait for the next scheduled review.

### What changed from the Smart-Connections era

- The retrieval call is `brain search --rerank --json` (fused RRF(60) BM25 +
  dense, optional cross-encoder rerank), not `mcp__smart-connections__lookup`.
- There is no separate Step-1.5 rerank script to invoke — `--rerank` is a
  flag on `search` itself (Step 1+1.5 collapse into one verb).
- There is no fixed five-step "cascade" to walk in order. AGENTS.md §5
  explicitly replaces the stop-at-first-hit cascade with a **small
  composable tool set the model orchestrates** — score whether the model
  used the *right* tools for the question (lexical first, semantic when
  needed, graph-expand for multi-hop), not whether it walked steps 0→4 in a
  fixed sequence.
- The cross-client MCP-swap trigger ("smart-connections / obsidian-graph
  re-registered") becomes: **brain binary version or embed-model drift** —
  compare `brain status --json`'s `embed_model` / `schema_version` against
  the last baseline's recorded values before trusting a "no change" report.

## Standing monthly / go-live gate

The graded golden set is the standing quality instrument: capture the current
engine over all queries in `eval/golden_set.json`, score it with
`eval/harness_direct.py` against `eval/qrels/qrels.json`, and decide with
`eval/gate.py` against the standing baseline. The gate is the PRIMARY decision:
its Recall@10 non-inferiority, powered language segments, and within-engine p95
latency checks determine pass/fail.

```bash
python3 eval/capture_run.py --golden eval/golden_set.json \
  --vault "$BRAIN_VAULT" --system brain-current \
  --out eval/runs/<current-run>.json
python3 eval/harness_direct.py \
  --golden eval/golden_set.json --qrels eval/qrels/qrels.json \
  --current eval/runs/rebaseline-<version>-<date>-<standing>.json \
  --new eval/runs/<current-run>.json \
  --out _evidence/<session>/scorecard.json --md _evidence/<session>/scorecard.md
python3 eval/gate.py --scorecard _evidence/<session>/scorecard.json
```

The standing-baseline convention is
`eval/runs/rebaseline-<version>-<date>-*.json`. Select a full-golden capture
that carries `index_state.fingerprint`; for this rollout, the frozen reference
is `eval/runs/rebaseline-0.19.24-2026-08-04-bare.json` (66 queries). Do not
promote fixture-only or synthetic probe runs to a standing baseline. If
`gate.py` refuses because corpus-fingerprint drift exceeds 10%, do not pass
`--allow-drift`: capture a fresh baseline, date and fingerprint it, and use
that new `rebaseline-<version>-<date>-*.json` run as the standing baseline for
subsequent gates.

### Standing gate checklist

- [ ] Run `brain-golden-probe`; stop on any nonzero result.
- [ ] Capture all 66 golden queries and verify the run metadata/fingerprint.
- [ ] Score with `harness_direct.py` against the standing baseline.
- [ ] Decide with `gate.py`; re-baseline on >10% fingerprint drift, never with `--allow-drift`.

## Behavioural smoke check

Run the existing four behavioural probes before the graded gate. A probe failure
(any nonzero `brain-golden-probe` exit, including regression, invalid
configuration, or transient failure) is a hard stop requiring investigation;
probe success is only clearance to continue to the graded gate.

```bash
brain-golden-probe "$BRAIN_VAULT/eval/golden-probes.json" \
  --vault "$BRAIN_VAULT"
```

## Smoke test (sub-second mode)

```bash
brain --vault "$BRAIN_VAULT" status --json
```

Replaces the old `.smart-env/*.ajson` shape/source-count/embed-dim check —
`status` reports `index.notes`, `index.chunks`, `index.embed_model`,
`index.embed_dim`, `index.vector_backend` directly from the live index, plus
snapshot generation/age and pending-draft count. Run this on every routine
health pass; it's cheap (no model load). For a deeper probe that actually
exercises retrieval, `brain health --json` adds a one-query self-test
(`selftest.probe_ok`).

## Quantitative Recall@k harness (A/B non-inferiority)

For a structural/embedder change where "does retrieval still work at all"
isn't enough and you need a calibrated regression bound:

```bash
python3 eval/harness.py \
  --golden eval/golden_set.json --qrels eval/qrels/qrels.json \
  --current eval/runs/<frozen-baseline>.json --new eval/runs/<new-run>.json \
  --out _evidence/<session>/scorecard.json --md _evidence/<session>/scorecard.md

python3 eval/gate.py --scorecard _evidence/<session>/scorecard.json
```

`gate.py` is the **single primary gate**: a paired bootstrap 95% CI on the
per-query Recall@10 delta must clear `-2pp` non-inferiority OVERALL and
per-language where `power == "gate"`, plus `p95(new) <= p95(current)`
latency. Exit 0 = pass; exit 1 = **abort the change, stay on the prior
substrate**; exit 2 = the gate could not be decided (treat as not-a-pass).
This harness compares against a **frozen, committed baseline run file**,
never a live call to the system being replaced — see `eval/harness.py`'s
docstring for the full methodology.

## Cross-engine A/B (shared corpus)

When another engine (e.g. Memento) indexes **the same corpus** as the vault,
score both against the SAME golden set + qrels. `eval/PORTABLE-CONTRACT.md`
is the SSOT (schemas §1–2, shared-corpus conditions §6); this is the run
checklist:

1. **Preflight — ingest coverage.** Verify every qrels doc exists in BOTH
   engines (doc-inventory diff); exclude/annotate queries whose relevant
   docs one side lacks. Without this the A/B measures ingest coverage, not
   ranking. Also confirm the doc-key map (`{engine_source_key:
   canonical_vault_path}`, lives next to the qrels) is current.
2. **Capture both runs, same golden file:**
   ```bash
   # brain side (in-process)
   python3 eval/capture_run.py --golden eval/golden_set.json \
     --vault "$BRAIN_VAULT" --system brain-baseline \
     --out eval/runs/xengine-brain.json
   # foreign engine side (its adapter; Memento shown — API must be up)
   python3 <memento-repo>/apps/api/scripts/capture_ir_run.py \
     --golden eval/golden_set.json --system memento-baseline \
     --doc-key source --map eval/qrels/<engine>-map.json \
     --out eval/runs/xengine-memento.json
   ```
3. **A run with a non-trivial `scope.unmapped_keys` tail is not
   gate-worthy** — fix the map and re-capture before scoring.
4. **Score + gate** (same commands as the Recall@k harness above, using
   `harness_direct.py` with the two run files as `--current`/`--new`).
5. **Read the gate within its limits:** the p95 latency check binds within
   an engine, not across transports (in-process vs HTTP) — cross-engine
   latency is informational only.

## Pass-bar rationale

80% per dimension rather than on the aggregate, for the same reason a
single collapsed metric hides a real regression: a vault that's perfect on
four dimensions and broken on the fifth still has a broken dimension.

## When NOT to use

- Ad-hoc retrieval probing — just call `brain search` directly and look at
  the result; don't formalise a one-off check as an eval run.
- Single-question A/B testing — use the smoke test (`brain status`) instead.

## Cross-references

- `AGENTS.md` §5 (agentic tool surface, RET-04) — the composable-tools model this eval scores against
- `eval/harness.py`, `eval/gate.py` — the quantitative Recall@k A/B harness
- `eval/PORTABLE-CONTRACT.md` — run-file/qrels schemas, probe-class invariants, shared-corpus cross-engine rules
- `brain --help` — full verb + flag reference

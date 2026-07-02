---
name: vault-eval
description: "Run the retrieval-quality eval against a brain-substrate vault — five dimensions: S (supersession-correct), X (retrieval-complete), T (temporal-correct), MH (multi-hop), CAL (calibration/refusal). Use monthly, and as the go-live gate after any structural change to the substrate (embedder swap, index rebuild, a change to which read tools the harness composes). Triggers: 'run the eval', 'vault eval', 'retrieval eval', 'monthly eval', 'go-live gate', 'score retrieval'. Also covers the quantitative Recall@k harness (eval/harness.py + eval/gate.py in this repo) for A/B non-inferiority checks against a frozen baseline run. Outputs a dated baseline file with per-question scores and an aggregate. Do NOT use for ad-hoc retrieval probes — write those to a scratch note instead."
---

# vault-eval (brain-substrate kernel)

Two eval surfaces, both brain-backed, serving different questions:

| Surface | Question it answers | Tooling |
|---|---|---|
| **Qualitative cascade eval (this skill, primary)** | "Does the model, using the brain's composable read tools, answer real questions correctly — supersession, completeness, timing, multi-hop, calibration?" | hand-written questions + `brain search/grep/bases-query/graph-expand/get` |
| **Quantitative Recall@k harness** | "Did a substrate/embedder change regress retrieval, measured against a frozen golden set?" | `eval/harness.py` + `eval/gate.py` in this repo |

## When to run

- **Monthly cadence** — the qualitative eval below.
- **Go-live / structural-change gate** — after an embedder swap, index
  rebuild, or a change to which read tools the harness composes (e.g. adding
  `graph-rank`). `<80%` on any dimension reverts the change.
- **Post structural change to `brain` itself** — a new CLI verb, an egress
  filter change, a vector-backend swap.

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
  flag on `search` itself (`docs/cutover/repoint-map.md` row: "Step 1+1.5
  collapse into one verb").
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

## Pass-bar rationale

80% per dimension rather than on the aggregate, for the same reason a
single collapsed metric hides a real regression: a vault that's perfect on
four dimensions and broken on the fifth still has a broken dimension.

## When NOT to use

- Ad-hoc retrieval probing — just call `brain search` directly and look at
  the result; don't formalise a one-off check as an eval run.
- Single-question A/B testing — use the smoke test (`brain status`) instead.

## Cross-references

- `docs/cutover/repoint-map.md` §4 — the dependency table this skill implements
- `AGENTS.md` §5 (agentic tool surface, RET-04) — the composable-tools model this eval scores against
- `eval/harness.py`, `eval/gate.py` — the quantitative Recall@k A/B harness
- `docs/cutover/brain-cli-verbs.md` — full verb + flag reference

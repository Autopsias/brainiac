---
name: autoresearch
description: "Bounded self-tuning loop over the brain retriever's hybrid-search parameters (rrf-k, rerank on/off, rerank window, k): capture a baseline over eval/golden_set.json via eval/capture_run.py, propose ONE parameter change with a one-line rationale, re-measure with eval/harness_direct.py, keep the change only if eval/gate.py's non-inferiority gate passes AND the target metric actually improved, revert otherwise. Hard-bounded (max iterations, max consecutive no-improvement rounds) and on-invoke only — never a scheduled/maintain-fold task. Every iteration writes an evidence artifact under eval/runs/autoresearch-*.json with a top-level captured ISO timestamp (the brief's staleness line reads it). Triggers: 'run autoresearch', 'tune retrieval parameters', 'self-tune the retriever', 'autoresearch cascade', 'is retrieval quality stale', quarterly maintenance poke. This skill proposes and evidences changes — it never edits src/brain/ itself; applying a KEPT change to the shipped CLI/BrainCore defaults is a normal, human-reviewed code change informed by the evidence this skill produces."
---

# autoresearch (brain-substrate kernel)

<!-- BRAINIAC-DESK-BLOCK v1 -- BYTE-IDENTICAL in all 14 Cowork bundles.
     Canonical copy: docs/operations/cowork-desk-block.md. Pinned by
     tests/test_cowork_skill_bundles.py against the LIVE broker
     (brain.mcp_adapter.TOOLS). Edit the canonical copy and every bundle in one
     commit, never one bundle alone, and never to name a tool the broker does
     not serve. -->

## Cowork (VM leg) — the vault is served by the desk, not by this sandbox

Reach the vault ONLY through the **desk**: the host-run `brainiac` MCP server,
whose tools Claude Desktop announces into this session. Call the tool. Do not
shell out for note content, and do not open a note file.

| the CLI verb you know | the desk tool to call |
|---|---|
| `search`, `hybrid-search` | `search`, `hybrid_search` |
| `get`, `read` | `get`, `read` |
| `recent` | `recent` |
| `dossier` | `dossier` |
| `bases-query` | `bases_query` |
| `grep` | `grep` |
| `graph-expand` | `graph_expand` |
| `vault-languages` | `vault_languages` |
| `diagnose` | `diagnose` |
| `alerts`, `exceptions`, `inbox` | `alerts`, `exceptions`, `inbox` |
| `draft-capture` (stage an unsigned note) | `capture` |

Those fifteen names are the whole desk, and they are the only route to note
CONTENT this session may use. **"Not on the desk" is not the same as "refused
here"**, and conflating the two is how a session talks itself into a workaround:
measured 2026-08-28, nine of the CLI's 22 VM-allowed verbs have no desk tool
(`brief`, `cos-propose`, `digest`, `doctor`, `draft-capture`, `init`,
`mcp-config`, `provision-request`, `status`), and two desk tools (`inbox`,
`vault_languages`) are not VM-allowed on the CLI at all. Of those nine, only
`cos-propose` — the COS ingress nothing on the desk serves — and the staging
probes below are still invoked locally by a shipped bundle, both deliberately.
Every verb that COMMITS or rebuilds — `write`, `sync`, `maintain`, `rebuild`,
`ingest`, `curate`, `health`, `integrity`, `verify-audit`, `graph-report` — is a
HOST-broker privilege and is not available here at all. **Unless a line is
explicitly marked as a Cowork instruction, a shell command anywhere else in this
skill is a HOST-lane instruction**, correct on the owner's Mac and not this
session's route.

**Do NOT bootstrap a vault path, do NOT export one onto `PATH`, do NOT hunt for
a staged binary to retry with, and do NOT open a note under `brain/` or `raw/`
with `cat`, `ls`, `Read` or a glob.** That is a RULE, and until the vault
actually moves off the mount it is the only thing standing between this session
and the bypass this whole change exists to close. Stated exactly, because the
two halves are true at different times:

- **While a published snapshot is still on the mount** (every workspace
  installed before the cutover), a staged engine CAN answer a local retrieval
  verb from `<vault>/.brain/snapshot/` — measured 2026-08-28: a local `search`
  at role `vm` returned the note body and exit 0 with the host index deleted,
  because the
  snapshot path is independent of it. Content that arrives that way skipped the
  desk, and so skipped the classification egress record the desk writes. A read
  that works is not a read you were allowed to make.
- **Once the vault is off the mount**, the local retrieval verbs fail rather
  than returning less — measured 2026-08-28: `search`, `get`, `recent` and
  `grep` exit 3 with `OperationalError: unable to open database file`, and
  `status --json` carries that same error in its `index` block. That is the desk
  telling you to use it, not a broken install and not a host-only refusal.
  **Not every surface is that loud**, so never read a quiet answer as an
  answer: an EMPTY result and a `not-computed` status are also what you get from
  a vault you cannot see. Only a hit you obtained through the desk is evidence.

Either way the answer is the same: call the desk. And the desk itself has no
offline substitute — it speaks stdio to the host process Claude Desktop spawned,
so there is no endpoint in this sandbox to dial and nothing to fall back to.

**The one local exception, and it returns no note.** If this workspace still
carries a staged engine, its three staging probes — `brain --version`, `brain
--role vm doctor`, `brain --role vm status --json` — read `.brain/` staging
metadata: engine stamp, model cache, vendored-deps ABI, snapshot presence. Keep
`--role vm` on them; without it the shell runs at role `host`, whose egress cap
is the whole vault. Use them for staging questions only. Their absence is not a
fault to fix, and none of the three is a way to reach a note.

If you need something the desk does not serve, say so and stop. That is a
finding for the engine, not a gap to work around.

A bounded, on-invoke self-tuning loop: measure retrieval quality, try exactly
**one** parameter change, re-measure, keep it only if the numbers say so,
revert otherwise. The brain gets better at finding things without anyone
hand-tuning defaults blind — but every step is evidenced, gated, and bounded,
never silent and never unbounded.

**Why on-invoke, never a maintain fold** (ADR-0003 Ruling 7, "Rejected"):
self-tuning with a statistical gate is an analyst job that needs a human to
read the evidence before a kept change becomes the new shipped default — far
too heavy and judgment-laden for the nightly heartbeat. It respects THE LOCK
(`routines/manifest.json` `locked_counts`): zero new OS-scheduled entries.
Recommended cadence is **quarterly, by convention, as prose — never a
cron/schedule entry.**

**This skill never edits `src/brain/`.** Its output is evidence (scorecards +
a decision record per iteration). Turning a KEPT change into the actual
shipped default (e.g. `hybrid_search`'s `rrf_k=60` default in
`src/brain/core/_retrieval.py`, or `--rrf-k`'s default in `src/brain/cli.py`) is a
normal, reviewed code change a human makes afterward, citing the evidence
artifact — this skill stops at "here is what the data says," on purpose.

## The real interface (verified against this repo, not assumed)

- **`eval/golden_set.json`** — 66 graded queries (EN/PT/ES + 5 strata),
  qrels keyed on the **real, private owner vault's** relative paths (see its
  own docstring / `eval/build_golden_set.py`) — this is NOT the small
  dogfood `vault/` committed to this repo. Point `--vault` at whichever vault
  the golden set/qrels were built against; run only what's in
  `eval/qrels/qrels.json`.
- **`eval/capture_run.py`** (added by this session, AUT-04) — captures one
  `hybrid_search` run over the golden set with `--rrf-k`, `--rerank` /
  `--no-rerank`, `--rerank-top`, `-k` as first-class flags; writes the exact
  run-file schema `eval/harness_direct.py` / `eval/gate.py` already consume
  (`system`, `captured`, `index_state`, `k`, `runs`, `latency_ms`, `scope`).
  Pass `--rebuild` once per vault session if the index predates vector
  support (symptom: `no such table: vec_index`).
- **`eval/harness_direct.py`** — ranx-free (stdlib + math only, no numba/ranx
  install needed), validated bit-exact against the committed ranx scorecard.
  Scores a `--current`/`--new` pair paired-by-query-id, emits per-segment
  Recall@5/10/20, nDCG@10, MRR@10, latency p50/p95, and BOTH
  `per_query_recall@10` and `per_query_recall@20` (the gate needs whichever
  metric you pass it).
- **`eval/gate.py`** — the ship gate: non-inferiority (bootstrap 95% CI lower
  bound of the per-query delta `>= -0.02`) OVERALL + per-language/per-class
  for `power: "gate"` segments only, plus `p95(new) <= p95(current)`. Exit
  0 = PASS, 1 = FAIL (abort — do not carry forward), 2 = ERROR (no decision —
  never a pass). Use `--metric recall@20` — the **agentic-budget** metric
  (the agent consumes its whole returned working set, not just a human's top
  10) is the correct measure for a retriever feeding an LLM agent, and is
  what this skill's evidence contract uses by default.

If any of these paths or flags have drifted since this was written, trust
the actual `--help` output over this doc and note the drift in the run's
evidence record.

## The bounded loop

**Hard bounds (tune here, not sacred — document any change you make):**
`MAX_ITERATIONS = 5` per invocation, `MAX_CONSECUTIVE_NO_IMPROVEMENT = 2`
(two REVERTs in a row → stop early, further blind guessing isn't worth it).
One parameter changed per iteration — never bundle two knobs in one gate
read, or a pass/fail can't be attributed to a cause.

### Step 0 — establish the current baseline

If `eval/runs/autoresearch-current.json` exists and is recent, reuse it as
`--current` for iteration 1. Otherwise capture it fresh with the CLI's
present defaults (check `src/brain/cli.py`'s `--rrf-k`/`--rerank-top`
defaults first — don't assume 60/15 forever):

```bash
python3 eval/capture_run.py --golden eval/golden_set.json \
  --vault "$BRAIN_VAULT" --rrf-k 60 --rerank-top 15 -k 20 \
  --system current --out eval/runs/autoresearch-current.json --rebuild
```

### Step 1 — propose ONE change

Pick exactly one dimension and one direction, with a one-line rationale
(e.g. "rrf_k 60 -> 40: tighter fusion favors top-ranked BM25 hits on this
corpus's short lexical-identifier queries"). Candidates: `rrf-k`, `rerank`
on/off, `rerank-top`, `k`. Optionally corroborate the change is
vault-relevant by drawing a probe term from `overlay/keywords/*.md` (a
glossary/acronym this owner actually searches for) — if you do, the query
**string** the owner-initiated may appear in the evidence record, but never
any other overlay prose/people content (ADR-0003 Ruling e).

### Step 2 — capture the candidate

```bash
python3 eval/capture_run.py --golden eval/golden_set.json \
  --vault "$BRAIN_VAULT" --rrf-k 40 --rerank-top 15 -k 20 \
  --system "iter1-rrf40" --out eval/runs/autoresearch-2026-07-05-iter1-candidate.json
```

### Step 3 — re-measure and gate

```bash
python3 eval/harness_direct.py --golden eval/golden_set.json \
  --qrels eval/qrels/qrels.json \
  --current eval/runs/autoresearch-current.json \
  --new eval/runs/autoresearch-2026-07-05-iter1-candidate.json \
  --out eval/runs/autoresearch-2026-07-05-iter1-scorecard.json \
  --session autoresearch-iter1

python3 eval/gate.py --scorecard eval/runs/autoresearch-2026-07-05-iter1-scorecard.json \
  --metric recall@20
echo "exit=$?"   # 0=PASS 1=FAIL(revert) 2=ERROR(revert, and fix the setup)
```

### Step 4 — keep or revert

- **Gate PASS (exit 0) AND the overall `recall@20` delta in the scorecard is
  actually `> 0`** (non-inferiority alone is "didn't get worse" — an
  improvement is the bar to KEEP, per Ruling 7's "kept only if the numbers
  improve"): **KEEP**. Copy the candidate run over the rolling baseline
  (`cp eval/runs/autoresearch-2026-07-05-iter1-candidate.json
  eval/runs/autoresearch-current.json`) so the next iteration compares
  against it. Reset the no-improvement counter.
- **Gate FAIL, ERROR, or PASS-but-flat/negative delta**: **REVERT** —
  `eval/runs/autoresearch-current.json` is untouched, the candidate file
  stays only as an archived evidence artifact. Increment the no-improvement
  counter.
- Either way, write the decision record (below) for this iteration.

### Step 5 — stop conditions

Stop the loop (this invocation) when: `MAX_ITERATIONS` reached, OR
`MAX_CONSECUTIVE_NO_IMPROVEMENT` REVERTs in a row, OR the operator says stop.
Report a one-line summary: iterations run, how many kept, final params vs.
starting params.

## Evidence artifact contract (every iteration, no exceptions)

Every capture and every decision lands under `eval/runs/autoresearch-*.json`
— `src/brain/core/_briefing.py`'s `_autoresearch_status` globs exactly that pattern for
the newest top-level `captured` ISO field to drive the morning brief's
staleness line (`docs/adr/0003-parity-architecture.md` Amendment,
2026-07-05/s09). `eval/capture_run.py`'s own output already carries
`captured`, so a raw candidate capture alone satisfies the glob — but write a
**decision record** too, so a later read knows *what happened*, not just
*that something ran*:

```json
{
  "captured": "2026-07-05T14:32:00+00:00",
  "iteration": 1,
  "dimension_changed": "rrf_k",
  "from": 60,
  "to": 40,
  "rationale": "tighter fusion favors top BM25 hits on short lexical queries",
  "metric": "recall@20",
  "gate_exit_code": 0,
  "overall_delta": 0.015,
  "decision": "KEPT",
  "candidate_run": "eval/runs/autoresearch-2026-07-05-iter1-candidate.json",
  "scorecard": "eval/runs/autoresearch-2026-07-05-iter1-scorecard.json"
}
```

Name it `eval/runs/autoresearch-2026-07-05-iter1-decision.json` (date + iter
+ `-decision` suffix — anything matching `autoresearch-*.json` works for the
staleness glob; this naming keeps a quarter's run legible to a human
scanning the directory later).

**No overlay content beyond an owner-initiated query string ever lands in
these files** (ADR-0003 Ruling e — evidence files are repo-committed, hence
Public-equivalent).

## After the loop — a KEPT change is a recommendation, not a merge

Summarize for the operator: which dimension(s) moved, the evidence path(s),
the measured delta. Applying a KEPT change to the actual shipped default
(`src/brain/core/` / `src/brain/cli.py`) is a normal code edit + review +
test cycle the operator does next — this skill's job ends at "here's the
evidence a change helps."

## Hard guardrails

- **One parameter per iteration.** Never gate two simultaneous changes.
- **Never skip the gate.** A candidate that "looks better" without a PASS +
  positive-delta reading is REVERTED, full stop.
- **Never exceed the bounds.** `MAX_ITERATIONS` / `MAX_CONSECUTIVE_NO_IMPROVEMENT`
  are hard stops, not suggestions.
- **Never write to `src/brain/`.** Evidence only.
- **External web egress stays human-initiated** — this loop tunes retrieval
  parameters over the existing index; it does not fetch anything external.

## Cross-references

- `routines/manifest.json` `autoresearch-cascade` row — ON-INVOKE disposition,
  points at this skill; the quarterly cadence is documented there as prose,
  never a schedule (THE LOCK, `locked_counts`, unchanged by this skill)
- `docs/adr/0003-parity-architecture.md` Ruling 7 (this skill's contract),
  Ruling e (overlay-data egress tier), and the 2026-07-05/s09 Amendment
  (the `eval/runs/autoresearch-*.json` filename + `captured`-field contract
  the brief's staleness line depends on)
- `src/brain/core/_briefing.py` `_autoresearch_status` / `src/brain/maintenance.py`
  `autoresearch_staleness` — the reader this skill's evidence artifacts feed
- `eval/capture_run.py`, `eval/harness_direct.py`, `eval/gate.py` — the real,
  verified interface this loop drives
- `overlay/README.md` — `overlay/keywords/` (optional probe-query
  corroboration, §1) and Ruling e's egress posture for anything overlay-derived

## Retrieval contract

> **Multilingual vaults — the variant contract (AGENTS.md §5 rule 3).** Before
> the first vault search, read the derived census: `brain status --json` ->
> `index.languages`. When `multilingual` is true, issue every search as the
> question PLUS one `--variant "<the same question in that language>"` for each
> other entry in `vault_languages` — you write the translation, the engine
> fuses the result lists into one ranking. `multilingual: false`, one language,
> or no census: a single query is correct — do not invent a variant.

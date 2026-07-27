# Portable eval contract — engine-agnostic retrieval eval for second brains

**SSOT: this repo (`profile-a-brain/eval/`).** The scorer layer here is
engine-blind; any second-brain engine (Brainiac, Memento, …) plugs in by
writing ONE capture adapter that emits the run-file schema below. Golden
*data* (queries + qrels) never travels between corpora — only the schemas,
the scorer, the gate, and the probe-class invariants do.

```
┌─ per-engine, per-corpus ────────────┐   ┌─ shared, engine-blind (THIS repo) ─┐
│ golden set (queries + graded qrels) │   │ harness_direct.py  (recall/nDCG/MRR)│
│ capture adapter (engine → run file) │ → │ gate.py            (ship gate)      │
│ probe file (anchors per vault)      │   │ stats.py           (CI/perm/FDR)    │
└─────────────────────────────────────┘   └─────────────────────────────────────┘
```

Reference adapters: `eval/capture_run.py` (Brainiac, in-process
`BrainCore.hybrid_search`); `Memento/apps/api/scripts/capture_ir_run.py`
(Memento, HTTP `POST /api/mcp/search`).

---

## 1 · The run file (the ONE interchange format)

A capture adapter's entire job: for each golden query, ask the engine for
ranked results and emit:

```json
{
  "system": "memento-hybrid-default",
  "captured": "2026-07-23T12:00:00+00:00",
  "index_state": {"params": {"...engine-specific knobs..."}},
  "k": 20,
  "runs":       {"<query_id>": {"<doc_key>": 0.87, "<doc_key>": 0.41}},
  "latency_ms": {"<query_id>": 142.5},
  "scope": {"queries_captured": ["..."], "n": 66,
            "egress": "retrieval-primitive (no egress filter)"}
}
```

Rules:

- **`doc_key` is engine-defined but MUST be stable and MUST match the qrels
  keys exactly.** Brainiac: canonical vault-relative source path
  (`eval/path_normalize.py`). Memento: episode UUID. Never mix key styles
  within one corpus.
- **Scores are engine-native** (any monotone relevance score). The scorer
  only uses rank order (score-descending, insertion-order tiebreak).
- Duplicate hits collapsing to one `doc_key` keep the max score.
- An empty result set for a query is `{}` — never omit the qid (omission
  shrinks the paired scoring scope instead of scoring the miss as 0).
- `latency_ms` is wall-clock per query at the adapter boundary — it feeds
  the gate's p95 check, so capture it under realistic conditions.

## 2 · Golden set + qrels (per-corpus data, shared schema)

- Golden set: `{"queries": [{"id", "text", "lang", "stratum", "held_out",
  "provenance", "qrels": [{"path": <doc_key>, "grade": 0-3}]}]}` — see
  `eval/golden_set.json` for the full field set and
  `eval/build_golden_set.py` for validation.
- Qrels (scorer input): flattened `{query_id: {doc_key: grade}}`
  (`eval/qrels/qrels.json`).
- **Builder discipline that must transfer with the schema** (it is what
  makes the numbers trustworthy, not the schema itself):
  - graded relevance 0–3, graded from note IDENTITY/TOPIC, blind to any
    retriever's output;
  - strata with per-stratum AND per-language minimum-n; under-floor strata
    are marked `power: "smoke"` and never gate;
  - a `held_out: true` slice authored purely from domain knowledge (no
    query-log derivation) — the only slice a confirmatory significance
    claim may use;
  - queries frozen before any run; failures fix the ENGINE or the anchor,
    never the question (anti-cherry-pick);
  - a MAINTAINED artifact: re-review after corpus reorgs; a probe failing
    for a legitimate reason (the anchored fact itself superseded) means
    re-anchor + re-stamp the baseline.
- **Contamination rule:** golden data is corpus-derived and therefore
  private to its corpus. It never ships in a distribution artifact and
  never moves to another engine's repo. Examples in shared docs use
  placeholder entities (e.g. Contoso), never real corpus terms.

## 3 · Scorer + gate (shared; do not fork)

- `harness_direct.py` — recall@{5,10,20}, nDCG@10, MRR@10; PAIRED scoring
  over qids present in both runs and qrels; per-stratum/per-language
  segments; emits the scorecard `gate.py` consumes.
- `gate.py` — ONE primary gate: recall@10 non-inferiority, bootstrap 95% CI
  lower bound of per-query delta ≥ −2pp, per-language for `power=="gate"`
  segments, plus p95 latency ≤ current. Superiority is reported as bonus,
  never required. Extended stats (permutation test, BH-FDR, MDE/power) per
  the EF-04 block in `gate.py`'s docstring.
- Consuming repos call these scripts by path (they are stdlib-only /
  self-contained). If calling by path ever becomes untenable, extract to a
  shared package — do not copy-paste-fork the metric definitions.

## 4 · Behavioral probe classes (engine-neutral invariants)

The four golden-probe classes (`src/brain/golden_probe.py`,
`<vault-repo>/eval/golden-probes.json`) are statements about ANY second
brain that has decisions, versioning, ingestion, and an access-gated read
surface. Each engine satisfies them with its own verbs:

| Class | Invariant (engine-neutral) | Brainiac verbs | Memento analog |
|---|---|---|---|
| `decision_state` | The engine's DECISION layer surfaces the currently-decided claim for a known decided question — not a proposal, not a stale snapshot. | `brain dossier` | `POST /api/mcp/query` (synthesize) / `briefing_context` |
| `currency` | Following the version chain from a deliberately RETIRED anchor reaches the current HEAD, and HEAD is marked current. | supersession frontmatter chain-follow | bi-temporal edges (`invalid_at`/`expired_at`), `GET /api/temporal/supersessions` |
| `freshness` | Content ingested/updated within the last N days is reachable through the gated read surface. | `bases-query` + gated read | recency-scored search / episode timestamps |
| `tension` | A decision with known newer post-dating sources carries an explicit tension/conflict flag — newer material never silently overturns the decision layer. | `dossier.tensions` | supersession-candidate detection (no first-class flag yet — probe blocked until one exists) |

Probe files are per-vault data (anchors + claim substrings); runners are
per-engine. The Brainiac runner's non-negotiable behaviors transfer as
requirements for any future runner: chain-follow retired anchors before
alarming; claim-text fallback; missing anchor = loud INVALID, never a
silent pass; deterministic-invalid vs transient-CLI-failure distinguished
in exit codes (`action_required > regression > transient > ok`).

## 5 · Qualitative cascade eval (methodology, fully portable)

The five dimensions of `.claude/skills/vault-eval/` — **S** supersession-
correct, **X** retrieval-complete, **T** temporal-correct, **MH** multi-hop,
**CAL** calibrated refusal — with the ≥80% PER-DIMENSION bar (aggregates
mask collapsed dimensions). Questions are authored per-corpus before the
run; the tool-composition instructions are per-engine.

## 6 · Shared-corpus cross-engine A/B (the strongest comparison)

When two engines index **the same corpus** (e.g. one owner corpus in both the
brain vault and a second engine), the per-corpus golden set + qrels can score BOTH
engines — identical queries, identical relevance judgments, one gate. That
is a true cross-engine A/B, strictly stronger than two per-engine evals.

Requirements on top of §1–3:

- **One doc_key namespace.** Both adapters must emit the SAME key space —
  the canonical source path the qrels use. The foreign engine's adapter
  maps its native identity to it (Memento: `--doc-key source --map
  <json>`, filename/name → canonical vault path; chunks collapse to their
  parent document's key so scoring stays doc-level on both sides).
- **The map is an audited artifact.** Unmapped keys score as misses, so a
  bad or stale map reads as an engine regression. Adapters must report
  unmapped keys loudly (run-file `scope.unmapped_keys` + stderr warning);
  a run with a non-trivial unmapped tail is not gate-worthy.
- **Check ingest coverage before blaming ranking.** The scorer cannot
  distinguish "ranked it low" from "never ingested it". Before gating,
  verify every qrels doc exists in both engines (a one-off doc-inventory
  diff); exclude or annotate queries whose relevant docs one side lacks —
  otherwise the A/B measures ingest-pipeline coverage, not retrieval.
- **The map lives with the qrels** (same repo, same contamination rules) —
  it enumerates real corpus filenames and is corpus data, not shared code.
- Latency comparability: an in-process capture (brainiac) vs an HTTP
  capture (Memento) embeds transport overhead in `latency_ms`. The gate's
  p95 check only binds WITHIN an engine (candidate vs its own baseline);
  cross-engine latency comparison is informational unless both captures
  use equivalent transports.

## 7 · Adding an engine (checklist)

1. Capture adapter → run-file schema (§1); pick the corpus's stable
   `doc_key`; smoke-test with `--self-test` style offline check.
2. Author golden queries + graded qrels over THAT corpus under §2
   discipline (min ~20 queries before gating anything; mark thin strata
   `smoke`).
3. Baseline: capture a run, freeze it, and gate every subsequent change
   against it via `harness_direct.py` + `gate.py`.
4. (Optional) Map the four probe classes to the engine's verbs (§4 table);
   write a probe file with real anchors; port the runner behaviors.
5. (Optional) Run the S/X/T/MH/CAL qualitative eval monthly (§5).

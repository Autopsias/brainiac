# Eval follow-ups

These follow-ups preserve historical measurements and open investigations. They
do not replace the standing release gate: the 66-query graded run over
`eval/golden_set.json` is primary, and the behavioural probes are smoke-only.
Capture the current engine, score it with `eval/harness_direct.py` against
`eval/qrels/qrels.json`, and decide with `eval/gate.py` against the current
fingerprinted `eval/runs/rebaseline-<version>-<date>-*.json` baseline. If
corpus-fingerprint drift exceeds 10%, capture and date a fresh re-baseline
before scoring; never pass `--allow-drift`.

## 1. Spanish-query retrieval is genuinely weak (not just the fixed qrels bug)
> **Its candidate fix is FALSIFIED for Portuguese — see item 7 (2026-08-04).**
> The embedder is not what loses those queries, so "evaluate a stronger
> multilingual embedder" is not the fix. Spanish itself is still unexplained:
> `monolingual_es` stays at recall@10 0.000 under every remedy measured in
> item 7, so item 1 remains genuinely open — just not for the stated reason.

After correcting the stale ground truth (overall recall@10 0.350 → 0.471), the
Spanish segment is still low (recall@10 ≈ 0.17). It is a real
`multilingual-e5-small` weakness on Spanish-worded queries, independent of the
qrels fix: for the *same* target document, an English-worded query ranked it in
the top handful while its Spanish-worded twin ranked it far deeper. **Candidate
fix:** evaluate a stronger multilingual embedder (e.g. `multilingual-e5-base`,
or a reranker on the ES slice) and re-baseline; weigh the latency/size cost
against the gain. Highest-value of these three.

## 2. The `monolingual_es` stratum is mislabeled
Its target documents are English-language, so it actually tests cross-lingual
ES→EN retrieval, not monolingual Spanish. **Fix:** rename the stratum to reflect
that, or add genuinely Spanish-language target notes if a true monolingual-ES
test is wanted (none surfaced in the current corpus).

## 3. Make the eval self-healing against fixture/corpus drift
The committed eval fixtures (`golden_set.json`, `qrels/qrels.json` — both
gitignored/private) reference entity names by an anonymization scheme that
drifts from the private corpus over time; when it drifts, ground-truth paths
stop resolving and scores silently understate quality (this is what produced the
0.000 Spanish artifact). **Fix:** a gitignored codename map applied at score
time (same pattern as the release contamination denylist) so the public fixtures
and the private corpus stay reconcilable without either side leaking into the
other.

## 4. Rerank default-on: REJECTED 2026-08-04 (RK-01 / BR-03) — stays opt-in

**Decision:** reranking (`--rerank`) stays an opt-in flag. It is NOT made the
shipped default. This is a complete, deliberate outcome of applying the fixed
decision rule to real measurements — not a deferral.

**Measured** (engine 0.19.24, 66-query golden set, real corpus,
`eval/runs/rebaseline-0.19.24-2026-08-04-scored.json` — see also the readout
at `eval/runs/rebaseline-0.19.24-2026-08-04-readout.md`):

| | bare | rerank15 | rerank20 | rerank50 |
|---|--:|--:|--:|--:|
| recall@10 | 0.3725 | 0.4154 | 0.4230 | 0.5088 |
| recall@20 | 0.4230 | 0.4230 | 0.4230 | 0.5417 |
| mrr@10 | 0.2670 | 0.3982 | 0.4105 | 0.4989 |
| hit@1 | 0.2121 | 0.3333 | 0.3485 | 0.4394 |
| p50 warm (ms) | 276.6 | 3720.9 | 5515.4 | 67988.7 (contended)*, 5648.0 (clean 10-q sample) |
| p95 warm (ms) | 479.1 | 5100.0 | 8168.7 | 181833.4 (contended)*, 8844.2 (clean 10-q sample) |

\* the full-66 rerank50 capture ran while the test suite saturated the CPU;
those two numbers are a contended upper bound, kept for the record but not
used below. A clean, uncontended 10-query sample at `rerank_top=50` (this
session, same corpus/index/params, one throwaway warm-up call dropped) gives
p50 5648ms / p95 8844ms — close to rerank20's own contended p50, and roughly
12x lower than the contended full-66 rerank50 number, confirming that number
was contention noise, not the arm's real cost.

**Rule applied** (fixed by the plan, not renegotiated): default rerank on
IFF, on the best rerank arm, ALL of mrr@10 >= 0.32, hit@1 not worse than
bare, recall@20 delta >= -0.02, added p50 <= 200ms, AND p95 does not regress
beyond bare's p95.

- Quality conditions PASS on every rerank arm, resoundingly: mrr@10 clears
  0.32 by 0.08-0.23; hit@1 improves (never regresses); recall@20 only ever
  improves (0 at rr15/rr20, +0.12 at rr50).
- Latency conditions FAIL on every rerank arm, resoundingly, including the
  narrowest window: rerank15's added p50 is ~3.4s against a 200ms budget
  (~17x over), and its p95 (5.1s) is ~10.6x bare's p95 (479ms). rerank20 and
  rerank50 are worse on both counts. The rule requires ALL conditions on the
  BEST arm; one resounding latency failure is enough to reject, and here
  every arm fails it by more than an order of magnitude.
- Verdict: REJECT. The rule is a conjunction and the two latency clauses are
  not close on any arm — this is not a borderline call.

**Why this isn't just "latency vs quality, pick one":** BR-03's own
rerank-15/20 came from `src/brain/rerank.py`, which has no per-call timeout
and no fallback-to-pre-rerank-order on a slow (as opposed to erroring)
reranker call — it only falls back to `NoopReranker` on an *exception*, never
on a *slow* one. bl-03 (this plan's other rerank-adjacent session) is
separately chasing an unresolved p95 tail on this same engine. Flipping the
default here would hand every default search call an added multi-second p95
tail, unconditionally, with no circuit breaker if the model degrades further
under load — the wrong trade to make while another session is trying to
close a p95 gap, even setting the raw latency numbers aside.

**What a future session would need to make wide reranking (rerank50-class
quality) viable as a default** — the quality finding here is large and real
(hit@1 nearly doubles 0.212 -> 0.439, mrr@10 nearly doubles 0.267 -> 0.499 at
window 50) and should not be lost in this rejection:

1. **A timeout + fallback-to-pre-rerank-order in `src/brain/rerank.py`**,
   so a slow reranker degrades to the bare RRF order instead of stalling the
   caller — this is a prerequisite for ANY default-on rerank, independent of
   which window is chosen, and directly serves bl-03's p95 goal instead of
   fighting it.
2. **A materially faster reranker path**: batching multiple candidates per
   forward pass (current cost looks close to linear per-candidate — rough
   per-doc costs from the clean sample: rerank15 ~3.4s/15cand ≈ 230ms/cand,
   rerank50 ~5.6s/50cand ≈ 113ms/cand, so batching already helps some but not
   enough), a smaller/distilled cross-encoder, or GPU inference. Getting the
   per-call cost from seconds to under ~200ms added is the actual bar this
   rule sets, and nothing measured here is remotely close.
3. Once (1) and (2) land, re-run this exact rule against fresh measurements
   — the rule itself does not need to change, only the inputs.

No code changes this session (correctly — `src/brain/rerank.py`,
`src/brain/cli.py`, `src/brain/index.py`, and AGENTS.md's tool table are
unchanged; the shipped default stays `rerank=False`, `--rerank` unchanged).

## Owner ruling, 2026-08-04: the latency budget above was OVERRULED, not the analysis

The rule application above is correct and stands as written — every rerank
arm failed the plan's fixed latency budget (added p50 <= 200ms, p95 no worse
than bare) by more than 10x. What changed is that the **owner has since
overruled that budget for this vault**, not the measurement: he has
explicitly decided ~5-6s search latency is an acceptable price for the
quality gain shown above (hit@1 nearly doubling, mrr@10 nearly doubling at
window 50), and directed a follow-up session to ship it with a circuit
breaker rather than leave it opt-in indefinitely.

**Shipped as a result (same day, follow-up session):**
- `search`/`hybrid-search` rerank ON by default, candidate window **50** —
  the best-quality arm, chosen because it strictly dominates window 20 for
  essentially the same clean latency (p50 5.6s vs 5.5s measured on a clean
  10-query sample; window 20 is now dominated and not worth defaulting to).

  > **CORRECTION, same day — this bullet's latency claim is wrong and the
  > window it justified has been reverted (item 6).** "p50 5.6s vs 5.5s" is
  > the window-20 row quoted twice: window 50 actually measures p50 68.0s /
  > p95 188.4s on this set, and 85% of queries blew the 30s timeout below and
  > silently returned the bare ordering. `RERANK_TOP_DEFAULT` is **20**;
  > `RERANK_TOP_MAX` (the ceiling, last bullet) stays 50. The RANKING
  > comparison in this note is unaffected and still correct — only the
  > latency figure, and the window choice that rested on it, were wrong.
- Opt-out on both surfaces: `--no-rerank` (CLI, wins over everything) and
  `BRAIN_RERANK_DISABLED=1` (global kill switch, mirrors
  `BRAIN_EXACT_LEG_ENABLED`).
- The circuit breaker this note's residue section (item 1 above) called a
  prerequisite: `BRAIN_RERANK_TIMEOUT_S` (default 30s) bounds the CALLER's
  wait on a slow rerank call — the ONNX forward pass itself can't be
  interrupted, so a timed-out call keeps running in the background on a
  persistent single-worker executor and its result is discarded, falling
  back to the pre-rerank order. This directly serves bl-03's p95 goal
  instead of fighting it, per this note's own reasoning above.
- `RERANK_TOP_MAX` (the clamp ceiling) raised 20 → 50 to match;
  `BRAIN_RERANK_MAX`/`BRAIN_RERANK_TOP` still work exactly as before. (This
  bullet stands: the ceiling is still 50 after item 6's ruling — only the
  DEFAULT window came back down.)

**What's still open (residue item 2, unchanged by the ruling):** the
per-call cost is still seconds, not milliseconds — the timeout is a safety
valve for pathological cases, not a fix for the routine cost. A materially
faster reranker path (batching, a smaller/distilled cross-encoder, or GPU
inference) remains the real follow-up if this latency ever needs to come
down, and residue item 2's per-candidate cost estimates above are still the
best available data point for scoping that work.

**Caller impact flagged, not fixed this session:** default-on makes every
search several seconds slower everywhere `search`/`hybrid-search` is called
without `--no-rerank`. Two concrete callers this affects, named so they get
a deliberate decision rather than a surprise:
1. **The hourly `brain-nightly` maintenance umbrella and any other scheduled
   fold that calls `search`/`hybrid-search`** — each such call now costs
   seconds instead of milliseconds; if a fold issues many queries per run,
   this could push it toward its own timeout/budget.
2. **The golden-set eval harness** (`eval/capture_run.py`,
   `eval/rebaseline_rerank_capture.py`, `eval/harness_direct.py`) — a bare
   capture over the 66-query set now costs whatever a bare capture always
   cost; but any NEW capture script that shells out to `brain search`/
   `brain hybrid-search` (rather than calling `BrainIndex` directly with an
   explicit `rerank=` kwarg, which is what the existing capture scripts do)
   would now default to the ~5-6s-per-query cost without asking for it.

---

## 5. RK-02 — adaptive rerank gate: spend the cross-encoder where it pays (2026-08-04)

Reranking is now default-on and costs seconds per query (~5.5s p50 at the
shipped window 20 — item 6 below records how the window got there). The
per-stratum table in item 4 is the whole argument
for gating it: `temporal` mrr@10 goes 0.448 → 0.720 and `multi_hop`
0.226 → 0.467, while `lexical_identifier` is already at ceiling bare and
barely moves. Paying that cost on a query the cross-encoder cannot improve is
pure latency.

### The rule that shipped

**Skip the cross-encoder when ADR-0008 pinned a UNIQUE full alias/title
owner.** On such a query rank 1 is already decided and the reranker is
contractually forbidden from touching it, so the only thing the spend can
buy is a reshuffle below the pin.

Calibrated OFFLINE against the existing rank-preserving arms — no new corpus
run. `eval/rerank_gate_calibration.py` replays the gate over the captured
`bare` and `rerank50` orderings for all 66 queries: a query the rule SKIPS
takes its rows from the bare arm, one it reranks takes them from the arm for
the shipped window (`--rerank-window`, default 20), and the synthetic run is
scored with the same scorer (`eval/rebaseline_report.score_arm`). Artifacts:
`eval/runs/rerank-gate-signals-2026-08-04.json` (the per-query signals a
production gate can actually see BEFORE reranking),
`eval/runs/rerank-gate-calibration-2026-08-04.json` (every candidate rule,
scored against the shipped window 20) and
`...-window50.json` (the same rules against window 50).

**The table below is the window-50 scoring, kept as first captured.** The
gate was re-scored against the window-20 arm after the window ruling (item 6)
and `pinned_identity` is +0.0000 on all four metrics there too — the rule is
genuinely independent of the window, checked rather than assumed. What
changes with the window is only the SIZE of the saving: at window 20 the
seven skipped queries go from a 6.2s median to a 200ms median, ~31x, instead
of 76.3s → 200ms.

| rule | skip | Δrecall@10 | Δmrr@10 | Δhit@1 |
|---|--:|--:|--:|--:|
| `_control_always_on` | 0.0% | +0.0000 | +0.0000 | +0.0000 |
| `_control_always_off` | 100.0% | −0.1363 | −0.2319 | −0.2273 |
| **`pinned_identity` (SHIPPED)** | **10.6%** | **+0.0000** | **+0.0000** | **+0.0000** |
| `create_safety_exists` | 10.6% | +0.0000 | +0.0000 | +0.0000 |
| `margin_rel>=0.20` … `>=0.40` | 10.6% | +0.0000 | +0.0000 | +0.0000 |
| `identity_evidence` | 12.1% | −0.0075 | +0.0101 | +0.0151 |
| `identity_or_phrase` / `keyword_exact_too` | 12.1% | −0.0075 | +0.0101 | +0.0151 |
| `margin_rel>=0.15` | 13.6% | −0.0151 | −0.0152 | −0.0152 |
| `identity_or_margin>=0.15` | 15.2% | −0.0227 | −0.0051 | +0.0000 |
| `margin_rel>=0.10` | 19.7% | −0.0404 | −0.0509 | −0.0455 |
| `identity_or_margin>=0.10` | 21.2% | −0.0479 | −0.0408 | −0.0303 |

Always-on baseline: recall@10 0.5088, recall@20 0.5417, mrr@10 0.4989,
hit@1 0.4394.

Three things this table says, in order of how load-bearing they are:

1. **`pinned_identity` is free.** Zero delta on all four metrics, against
   the window-20 arm and the window-50 arm alike — the seven queries it fires
   on score identically while their measured latency drops from a 6.2s median
   to a 200ms median at the shipped window 20. Nothing else measured is both
   free and cheaper.
2. **The RRF-margin family buys nothing extra.** Any cutoff in 0.20–0.40
   selects the *same seven queries* (the highest non-pinned margin in the
   whole set is 0.168), and every cutoff loose enough to select more starts
   costing recall. The pin is the same rule without a tuned constant, so the
   constant is not worth carrying.
3. **Widening past a unique identity costs recall.** `identity_evidence`
   adds one query — `lex_01`, an alias hit on a COLLIDING owner, which
   ADR-0008 deliberately does not pin — and immediately gives back
   −0.0075 recall@10. The uniqueness is doing the work, not the alias.

### The self-check that makes the table trustworthy

The trap that already bit this plan once: the older capture recorded per-doc
SCORES and every scorer rebuilt rank by re-sorting them, so a rerank-only
reordering was invisible and two arms scored identically. The calibration
therefore always emits two control rows and asserts they differ —
`_control_always_on` must reproduce the rerank50 arm exactly,
`_control_always_off` must reproduce the bare arm, and
`self_check.controls_differ` must be true. All three hold in the committed
artifact. If they ever stop holding, every number above is noise.

### Overrides and observability

Both directions on both surfaces, mirroring BR-03's kill switch: an explicit
`--rerank-gate` / `--no-rerank-gate` always wins;
`BRAIN_RERANK_GATE_DISABLED=1` disables the gate globally (restoring
unconditional reranking); the default is ON. Turning the gate off never
disables reranking, it only stops the engine skipping it.
`search --explain --json` reports `ranking.rerank_gate` =
`{enabled, skipped, reason}` — read that rather than inferring a skip from
`rerank_requested` vs `rerank_applied`, which also covers an absent model and
a timeout fallback.

### Caller policy (item 4's flagged impact, now resolved)

Verified by reading each caller rather than assuming:

- **`brain maintain` and the scheduled folds do NOT rerank.** The nightly's
  retrieval latency self-test calls `core.hybrid_search("brain", k=1)`
  (`src/brain/maintenance.py`), and `BrainCore.hybrid_search` still defaults
  to `rerank=False` — the BR-03 flip changed the CLI default, not the
  library default. The weekly golden probe (`src/brain/golden_probe.py`)
  shells out to `dossier`/`get`/`recent`, never `search`, and
  `BrainCore.dossier` likewise calls `hybrid_search` with the library
  default. Nothing to change.
- **The MCP adapter does not rerank either** (`src/brain/mcp_adapter.py`
  calls `core.hybrid_search` with no `rerank=`), which is a pre-existing
  divergence from the CLI, not something this session introduced.
- **Two shell probes DID start paying it and were changed:**
  `scripts/vm-selftest.sh` and `scripts/vm-boundary-probe.sh` (and their
  `src/brain/_assets/scripts/` twins, which are the copies that actually
  ship into a staged Cowork workspace) now pass `--no-rerank`. Both are
  liveness checks — one discards stdout entirely — and RET-02 guarantees a
  broken reranker degrades to identity rather than failing search, so
  neither loses coverage.
- **Item 4's eval-harness note was half wrong and is corrected here:**
  `eval/capture_run.py` and `eval/rebaseline_rerank_capture.py` do call the
  engine directly with an explicit `rerank=` kwarg and are unaffected by the
  CLI default (verified). `eval/harness_direct.py` is a *scorer* — it never
  touches `BrainIndex` or runs a search at all, so it was never exposed.

### What this does not claim

10.6% is the skip rate on THIS golden set, whose stratification is a
deliberate research mix (12 of 66 queries are `lexical_identifier`). It is
not a prediction of the skip rate on live traffic — an owner looking up
notes by name will trip the gate far more often, one asking analytical
questions far less. The honest per-query claim is the one that generalises:
when the gate fires, that query returns in ~200ms instead of paying the
reranker, and loses nothing measurable.

The gate is also NOT a fix for item 6. It removes the cost from 10.6% of this
set's queries; the other 89.4% still pay whatever the reranker costs — which
is why the window itself had to be corrected too.

---

## 6. BR-03's window-50 latency figure was wrong by ~12x, and the default timed out on 85% of queries (found 2026-08-04 — RESOLVED same day, default window now 20)

Found while verifying RK-02 (item 5) against the real corpus in the shipping
context, not by reading anything. It was reported rather than fixed on the
spot, because BR-03's window was an explicit owner ruling made on a stated
latency and only the owner could revise it; he did, the same day — the
ruling is recorded at the end of this item, and the default is now 20.

### What BR-03 shipped on

> "Window 50 rather than 20 because it costs the same (p50 5.6s vs 5.5s)
> while delivering far more; window 20 is strictly dominated."
> — commit `51fbdee`, and item 4's ruling note above

The owner accepted "~5-6s search latency" for the quality gain, and window 50
was chosen over window 20 specifically because it was believed to be free.

### What the arms actually measured

Straight from the committed capture artifacts (first query dropped, as
`eval/rebaseline_report.py` does):

| arm | p50 | p95 | queries over the 30s default timeout |
|---|--:|--:|--:|
| `rerank15` | 3.7s | 5.2s | 0 / 65 |
| `rerank20` | 5.5s | 8.2s | 0 / 65 |
| `rerank50` | **68.0s** | **188.4s** | **55 / 65 (85%)** |

The "5.6s / 8.8s clean sample" BR-03 quotes for window 50 is, to the tenth of
a second, the **window-20** row. A window-20 sample was labelled as window 50.

### Reproduced clean, so contention is not the explanation

The `rerank50` artifact carries a contention caveat (it was captured
alongside the test suite), so the numbers above were re-measured on an idle
machine, in one warm process, same corpus, same query (`SAP ISU`), real ONNX
cross-encoder, best of 2 runs each:

| `rerank_top` | warm latency |
|---:|--:|
| 10 | 3.4s |
| 15 | 5.1s |
| 20 | 8.2s |
| 50 | **74.6s** |

Windows 15 and 20 land on the captured p50/p95 exactly; window 50 reproduces
the captured ~68s. Cost is strongly SUPER-linear in the window (2.5x the
window costs ~9x the time), so the premise that "the 50-candidate ONNX batch
amortizes almost as well as the 20-candidate one" is false on this corpus.
The same run measured bare search at 94-123ms against a captured 277ms p50,
i.e. this machine was FASTER, not slower, while producing the 74.6s figure.

### Why this matters more than a wrong number in a comment

BR-03 also shipped a 30s caller-side timeout that falls back to the
**pre-rerank (bare) order**. At the shipped default — window 50, timeout 30s
— 85% of golden-set queries blow that budget. Those queries therefore wait
30 seconds and then return exactly the ordering they would have returned with
`--no-rerank`. The quality BR-03 was shipped to deliver is, on most queries,
not being delivered; what is being delivered is a 30-second pause in front of
the bare result. (Directly observed: a warm repeat of `SAP ISU` at defaults
logs `reranker ... exceeded 30s; falling back to unreranked order` and
returns in ~30.2s. Worse, the abandoned call keeps the single rerank worker
busy, so the next query queues behind it and times out too.)

Note this does NOT invalidate item 4's quality table. Those rankings were
captured with a long-running capture script and `rerank_applied_verified =
66/66`; the reranker genuinely ran on every query there. The defect is in the
LATENCY figure the window choice was justified with, and in what the shipped
30s timeout does to that window in normal use.

### The decision as it was put to the owner

1. **Drop the default window to 20.** Gets the ~5-6s p50 / 8.2s p95 the owner
   actually accepted, zero timeouts, and the measured quality of the
   `rerank20` arm (mrr@10 0.411, hit@1 0.349) — below `rerank50`'s 0.499 /
   0.439 on paper, but `rerank50`'s numbers are only reachable if the query
   is allowed to take a minute or more.
2. **Keep window 50 and raise `BRAIN_RERANK_TIMEOUT_S` past ~200s.** Delivers
   the full measured quality and makes a normal search take about a minute.
3. **Keep window 50 and keep the 30s timeout.** Status quo: 85% of queries
   pay 30s for the bare ordering.

Recommendation: **(1)**. It is the only option whose latency matches what the
owner agreed to, and option (3) — the then-shipped state — is strictly worse
than `--no-rerank` for the queries it affects. Doing nothing means (3).

### RESOLVED — owner ruling, 2026-08-04: default candidate window back to 20

Independently re-measured by the coordinator before ruling (one warm process,
same query, the same read-only index): bare 0.14s, window 20 **11.9s**,
window 50 **28.1s**. Window 50 is ~2.4x window 20, not equal to it. Both
re-measurements — the coordinator's and this session's — agree on the shape
and on the conclusion; the absolute numbers differ with machine load, which
is exactly why the ~12x gap against BR-03's stated figure is not a contention
artifact.

**Ruling: `RERANK_TOP_DEFAULT` = 20.** The owner's reason, in his words: it
is the latency actually accepted, it reranks every query instead of timing
out on most of them, and *quality you receive beats quality that expires*.

Option (1) above, as recommended. Shipped with it:

- **The ceiling stays 50.** This is a change of DEFAULT, not a removal of
  capability — `BRAIN_RERANK_TOP` / `BRAIN_RERANK_MAX` still opt into the
  wide-candidate pass deliberately, which is what the cross-lingual recovery
  case (item 4's residue) will want.
- **The 30s timeout is unchanged**, and the window move is what makes it a
  safety valve again rather than the routine path. At window 20 the golden
  set measures p50 5.52s, p90 7.53s, **p95 8.17s**, p99 9.91s, **max 10.47s**
  (slowest: `lex_02`), with **zero of 65 queries past even 20s**. 30s is
  ~5.4x the median and ~2.9x the slowest query measured. Answering the
  question the coordinator asked explicitly: **no, the p95 does not credibly
  cross 30s at window 20** — it would take a ~3.7x slowdown of the worst
  query, and even scaled to the coordinator's slower machine (whose window-20
  reading ran ~1.45x this one's) the worst case lands near 15s. The timeout
  does need raising alongside any deliberate wide-candidate pass, and both
  `rerank_timeout_seconds`'s docstring and the CLI epilog now say so.
- **`brain diagnose --rerank-top` moved 15 → 20.** It matched `search` at 15
  before BR-03, which moved `search` to 50 and left `diagnose` behind; a
  diagnostic that reproduces a different window than production explains the
  wrong ranking. The two agree again.
- **RK-02 (item 5) is unaffected and was re-checked, not assumed.** Its
  calibration was re-scored with the window-20 arm as the always-on
  comparator: `pinned_identity` is still +0.0000 on recall@10, recall@20,
  mrr@10 and hit@1, with all three control self-checks green.

What item 4's quality analysis still says, unchanged: window 50 does rank
better on paper (mrr@10 0.499 vs 0.411, hit@1 0.439 vs 0.349, recall@20 0.542
vs 0.423). Those numbers stand and are correctly measured. They are simply
not reachable inside a latency anyone agreed to, and the shipped 30s timeout
converted them into the bare ordering on 85% of queries. If a faster reranker
path ever lands (item 4's residue item 2: batching, a distilled cross-encoder,
GPU inference), window 50 becomes worth revisiting — and this file has the
measurement to revisit it against.

---

## 7. BR-02's embedder swap: FALSIFIED at Gate 0, 2026-08-04 — the loss is in FUSION, not the embedder

BR-02 proposed rebuilding the index with `multilingual-e5-base` because
`monolingual_pt` measured recall@10 **0.000** (0/12) on the reference vault.
A pre-spend gate (s05) tested the premise before paying the 1-2h rebuild. It
failed, so no A/B was run. Full evidence and the decision card:
`eval/runs/embedder-ab-2026-08-04/GATE0-READOUT.md`.

**1. The shipped embedder does Portuguese.** A purpose-built fixture — 22
synthetic PT notes, 5 answering 5 PT questions, **17 plausible PT distractors**,
scored on **hit@1/MRR** (recall@10 over 22 notes cannot fail) — puts
`multilingual-e5-small` at **hit@1 1.000, MRR 1.000** monolingually, and
**hit@1 0.800** on PT questions against English notes. The same fixture on the
HashEmbedder scores 0.600 / 0.000: it discriminates.
Reproducers: `eval/pt_capacity_fixture.py`, `eval/pt_capacity_probe.py`.

**2. `monolingual_pt` is mislabeled, exactly like `monolingual_es` (item 2).**
All 20 gold documents behind its 12 questions are English prose. Neither
"monolingual" stratum tests monolingual retrieval; both are query→EN
cross-lingual. Same fix as item 2 — rename, or add real PT/ES target notes.

**3. Where the queries actually die.** On the real read-only index, per leg,
`monolingual_pt`, n=12 (`eval/pt_leg_attribution.py`):

| leg | recall@10 | median rank of the gold doc |
|---|--:|--:|
| dense (the embedder alone) | **0.500** | **2** |
| lexical (BM25) | 0.000 | not found in 100 |
| fused (production RRF) | **0.000** | **52** |

A PT query shares no tokens with EN note text, so the lexical leg contributes
nothing — but RRF sums `1/(60+rank)` per leg, so a mediocre document present in
*both* legs outscores an excellent document present in only one. The embedder
finds the answer at rank 2; fusion buries it at 52. Controls: every gold
document is retrieved at **rank 1 by its own title** (so corpus, qrels and path
map are sound), and English paraphrases of the same 12 questions score 0.417.

**4. The engine already ships the counterweight — dormant.** The RET-01/RET-01b
zone-authority prior in `_hybrid_search_impl` was written for exactly this
burial, and is a no-op twice over: `_DEFAULT_ZONE_WEIGHTS` is `{}`, and 0 of the
live vault's 2,570 INDEXED notes carry the `source_zone:` frontmatter
`_resolve_zone` keys on (the vault migrated again since that fix landed), so it
falls back to the flattened `brain`/`raw` column. Arming it via
`$BRAIN_ZONE_WEIGHTS` alone (`eval/pt_zone_prior_probe.py`, no default
changed), at k=20 — **the rows labelled `recall@10` here are `hit@10`**
(one gold document per query, best rank only), the same conflation item 9
found and fixed; item 9's per-stratum table supersedes this one:

| weight on `brain` | 1.0 (ships) | 2.0 | 3.0 | 5.0 | 8.0 |
|---|--:|--:|--:|--:|--:|
| monolingual_pt (n=12) | **0.000** | 0.417 | 0.583 | 0.667 | 0.667 |
| lexical_identifier (n=12) | 1.000 | 1.000 | 1.000 | 1.000 | 0.917 |
| overall (n=66) | 0.424 | 0.530 | 0.530 | 0.561 | 0.545 |
| overall mrr@10 | 0.244 | 0.328 | **0.358** | 0.331 | 0.327 |

In queries, at weight 2.0: `monolingual_pt` **+5** (pt_01, pt_03, pt_06, pt_11,
pt_12), `multi_hop` **+2**, `cross_lingual_en_es` **-1**, identifiers and
temporal unchanged. At 3.0: PT **+7**, but `temporal` **-1**. The curve turns
over past 5.0 — a real optimum, not a free lunch.

**Not shipped, and not a calibrated constant.** These 66 queries are the only
labelled data, so a weight picked from that table is fitted to its own test set.
The finding is "there is a large real effect of roughly this size, available
with no model change"; the weight itself needs a held-out split before it
becomes a default. `monolingual_es` stays 0.000 at every weight and is a
separate open cause (item 1).

**Consequence for the AGENTS.md rule.** §5 rule 3's advice — issue an English
paraphrase alongside the question as posed — is unchanged and still correct:
the paraphrase restores the lexical leg, which is what the fusion rewards. Its
stated *cause* ("the shipped embedder's cross-lingual alignment is measurably
weak") was falsified here and has been corrected in `AGENTS.md` and its
wheel-asset mirror `src/brain/_assets/AGENTS.md`. **The rule's SCOPE was
corrected too (2026-08-04, owner instruction):** it named Portuguese and
Spanish, which was an accident of what had been tested. The defect is
vocabulary overlap — it bites in either direction and within one language (an
English paraphrase of an English note's question scored 0.417 where that note's
own title wording scored 1.000) — and the rule is now a both-ways habit rather
than a two-language fallback.

---

## 8. BR-02 CLOSED — the embedder STAYS `multilingual-e5-small` (owner ruling, 2026-08-04)

**Decision: no model swap, no rebuild.** The owner read item 7's Gate 0 readout
and chose option **A** of its decision card — *"arm the dormant zone prior now,
and scope the fusion repair as its own next work"*. Option **C** (run the
e5-base A/B anyway) is closed as **answered**, not deferred.

**The owner's reason, in the readout's own terms:** a bigger embedder buys a
better *dense* rank, and the production ranking throws the dense rank away. The
dense leg already puts half the Portuguese gold documents in its own top 10 at
median rank **2**; fusion exits them at median rank **52**. Paying 1-2 hours of
re-embedding to improve a number the fused path does not use is spending on the
wrong stage.

**What this means operationally.** The shipped default in `src/brain/embed.py`
is unchanged, so nothing forces a clean rebuild: the index's `embed_model` /
`embed_dim` guard (`BrainIndex.model_matches`, written from `self.embedder`) trips only when
the EMBEDDER's `model_id`/`dim` change, and the remedy this session shipped
instead is a query-time multiplier applied to the fused score inside
`_hybrid_search_impl` — it reads no embedding and writes no index metadata. Verified rather than assumed:
`eval/runs/zone-prior-no-rebuild-verification.txt`.

**The model-selection hook stays** — `get_embedder("auto")` remains the single
call site, so a future swap is still a one-line change plus a rebuild. Nothing
about this ruling forecloses item 1's ES investigation reaching for a different
model on evidence.

## 9. RET-01 zone-authority prior: calibrated on the held-out split, shipped as a measured OPT-IN — not a default

Item 7 found the engine already carries the counterweight for fusion burial
(the RET-01/RET-01b zone-authority prior) and that it is dormant, and measured
`BRAIN_ZONE_WEIGHTS={"brain": W, "raw": 1.0}` taking `monolingual_pt` off
0.000. **That sweep was scored on the same 66 queries it was tuned
on**, so the owner accepted option A *including* its calibration condition.
This is that calibration.

> **The first cut of this calibration was re-run.** Adversarial review found
> three defects in the harness, each of which made every number below
> provisional, and all three are fixed and the whole sweep re-measured. They
> are recorded here because the defect classes recur: (1) four qrels named
> documents the vault carries under different filenames, and the harness
> DROPPED them silently — three `temporal` queries then scored as permanent
> misses at every weight, a fabricated retrieval failure over 30% of that
> stratum; (2) what was reported as `recall@10` was `hit@10`, computed from
> the best-ranked gold document only, while **30 of the 66 queries have more
> than one** — an aggregate gain could have hidden the loss of the rest;
> (3) `--from-ranks` defaulted to `held-out` and overwrote the analysis, so a
> replay could mint a second "primary" result just by changing `--target`
> (demonstrated: `target=recall@10` selected W=2.5 and labelled a different
> p-value primary). The harness now fails closed on all three, records the
> engine/corpus fingerprint, and records whether the reranker actually RAN.

**Method** (`eval/zone_prior_calibration.py`, engine 0.19.24, reference vault
2,570 indexed notes / 106,182 chunks, read-only, k=20, no reranker):
`eval/golden_set.json` already carries a pre-registered, stratum-balanced
`held_out` flag — 33 train / 33 held-out, every stratum split evenly, and
**all 66 are scorable** (104 positive qrels, 0 unmappable; the run aborts
otherwise). The weight was selected as `argmax` mean mrr@10 on the **train
half only** (ties to the smaller weight), then the held-out half was read with
`eval.stats.paired_permutation_test(fold_context="held-out")` — this eval's one
primary significance regime (H19) — plus a descriptive bootstrap CI, the
minimum detectable effect at n=33, and achieved power. Ranks are deterministic,
so one search pass per weight feeds every split computation offline.
**`mrr@10` is the pre-declared target**; every other metric below is a
secondary descriptive on the same data and is labelled as one in the artifact.

**Selected on train: W = 3.0.** The single held-out read:

| held-out (n=33) | W=1.0 (ships) | W=3.0 | delta | 95% CI | permutation p | MDE @80% power | achieved power |
|---|--:|--:|--:|:--:|--:|--:|--:|
| **mrr@10** (primary) | 0.1980 | 0.3859 | **+0.1879** | [+0.058, +0.334] | **0.011** | 0.199 | 0.75 |
| recall@10 | 0.3535 | 0.4697 | +0.1162 | [-0.020, +0.263] | 0.145 | 0.202 | 0.36 |
| hit@10 | 0.4242 | 0.5455 | +0.1212 | [-0.061, +0.303] | 0.347 | 0.266 | 0.25 |

9 held-out queries improve, 5 get worse, 19 are unchanged (mrr@10). `recall@10`
is real recall — relevant documents in the top 10 over relevant documents
known — and `hit@10` is the "did anything land" rate the earlier draft
mislabelled as recall.

**The direction transfers. The constant does not.** Three measurements say so,
and the third is the one that decides it:

1. Every weight >= 1.5 beats the shipped 1.0 on **both** halves and on mrr@10,
   recall@10 and hit@10 alike — the effect is not an artifact of the half it
   was fitted on.
2. The two halves disagree on **which** weight, and the corrected data widens
   the disagreement: train argmax **3.0**, held-out argmax **5.0**. The
   held-out curve is flat to within 0.011 across the whole 2.5-5.0 range
   (0.3902 / 0.3859 / 0.3850 / 0.3960 — a third of what one query contributes
   at n=33), so both argmaxes are picking noise.
3. The observed mrr@10 effect (0.188) still sits **below** the minimum effect
   this sample size could reliably resolve (MDE 0.199 at 80% power; achieved
   power 0.75). recall@10 is not resolvable at all here (power 0.36). 66
   labelled queries can establish that this helps; they cannot calibrate how
   much.

The full curve, by half (mrr@10):

| W | 1.0 | 1.25 | 1.5 | 2.0 | 2.5 | 3.0 | 4.0 | 5.0 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| train (n=33) | 0.3007 | 0.3007 | 0.3067 | 0.3488 | 0.3428 | **0.3512** | 0.3352 | 0.2886 |
| held-out (n=33) | 0.1980 | 0.1989 | 0.2146 | 0.3163 | 0.3902 | 0.3859 | 0.3850 | **0.3960** |

**So `BrainIndex._DEFAULT_ZONE_WEIGHTS` stays `{}`,** and
`$BRAIN_ZONE_WEIGHTS` is documented as a supported, measured opt-in with a
recommended range of **2.0-3.0** on a two-zone (`brain`/`raw`) vault. A fitted
constant shipped as a default is precisely what the calibration condition
existed to prevent.

**Why 2.0-3.0 and not the held-out argmax of 5.0.** The aggregate is flat from
2.5 upward, so nothing is bought past 3.0 — and the per-stratum table below
shows what is *paid*: at 4.0-5.0 `lexical_identifier` mrr@10 falls 0.750 →
0.665 → 0.631, `temporal` mrr@10 falls 0.431 → 0.317 → 0.179, and
`cross_lingual_en_es` recall@10 reaches 0.000 at 4.0. Choosing the argmax of a
flat curve at the price of two collapsing strata is not a calibration.

**Per-stratum, the whole sweep** (recall@10 / mrr@10, all 66 queries):

| stratum (n) | 1.0 | 2.0 | 2.5 | 3.0 | 4.0 | 5.0 |
|---|--:|--:|--:|--:|--:|--:|
| monolingual_pt (12) | 0.000 / 0.000 | 0.333 / 0.287 | 0.417 / 0.384 | 0.458 / 0.433 | 0.458 / 0.458 | 0.486 / 0.500 |
| monolingual_es (6) | 0.000 / 0.000 | 0.000 / 0.000 | 0.000 / 0.000 | 0.000 / 0.000 | 0.000 / 0.000 | 0.000 / 0.000 |
| cross_lingual_en_pt (10) | 0.200 / 0.061 | 0.250 / 0.071 | 0.250 / 0.160 | 0.150 / 0.150 | 0.150 / 0.120 | 0.150 / 0.120 |
| cross_lingual_en_es (6) | 0.250 / 0.046 | 0.167 / 0.024 | 0.167 / 0.021 | 0.167 / 0.019 | 0.000 / 0.000 | 0.083 / 0.019 |
| lexical_identifier (12) | 0.958 / 0.750 | 0.958 / 0.750 | 0.958 / 0.736 | 0.958 / 0.725 | 0.958 / 0.665 | 0.958 / 0.631 |
| multi_hop (10) | 0.258 / 0.226 | 0.392 / 0.439 | 0.442 / 0.547 | 0.525 / 0.537 | 0.558 / 0.592 | 0.592 / 0.592 |
| temporal (10) | 0.800 / 0.431 | 0.800 / 0.425 | 0.800 / 0.356 | 0.700 / 0.344 | 0.800 / 0.317 | 0.800 / 0.179 |

Read that table as the honest version of two claims the first draft got wrong.
`lexical_identifier` does **not** "hold 1.000" — its `hit@10` does; its real
recall@10 is 0.958 and flat, while its **mrr@10 degrades from W=2.5 onward**.
And `temporal` does not lose a whole query to the prior at 3.0 in the way the
first draft said: it keeps 0.800 recall at every weight except 3.0 (where one
gold document leaves the top 10), and what it really loses is **rank** —
mrr@10 0.431 → 0.344 at 3.0 and 0.179 at 5.0. The earlier "temporal loses 1
query and its mrr@10 drops 0.348 → 0.228" was computed over a denominator
30% of which was unresolvable and scored as a permanent miss.

**The costs are real and named** (held-out half, W=1.0 → 3.0, by query):

* *Gains* — `pt_01`, `pt_03`, `pt_11`, `xl_pt_03`, `mh_03` not-in-top-20 →
  rank 1; `pt_05` → 5; `mh_05` 9 → 1; `mh_07` 2 → 1; `mh_09` → 2. At the
  document level: `mh_09` 0/2 → 2/2 gold retrieved, `pt_11` 0/2 → 2/2,
  `mh_03` 0/3 → 2/3.
* *Losses* — `tmp_07` 5 → 11, `xl_es_01` 9 → 13 and `mh_01` 9 → 12 fall out
  of the top 10; `tmp_09` 2 → 3; `tmp_01` 14 → 15; `lex_06` 2 → 5 (still
  top-10, but identifier precision degrades before its stratum recall does —
  the recall stays 0.958 at every weight).

Anyone raising the weight past ~3.0 is trading temporal and identifier
precision for Portuguese and multi-hop recall. The ceiling is what this sweep
itself shows at 4.0-5.0 (above). The claim that the curve "turns over past
5.0" is NOT from this artifact — it comes from s05's separate high sweep
(`eval/runs/embedder-ab-2026-08-04/gate0-pt-zone-prior-sweep-high.json`,
W ∈ {3.0, 5.0, 8.0, 16.0}), where overall mrr@10 goes 0.358 → 0.331 → 0.327
and flatlines, `lexical_identifier` loses a query outright at 8.0
(hit@10 1.000 → 0.917), and `temporal` mrr@10 falls to 0.091. Cite that file
for the turnover; this one only reaches 5.0.

The two whole-set costs item 7 measured are the same trade, re-measured here
without the broken denominators (each versus the shipped W=1.0): at **W=2.0**
`cross_lingual_en_es` recall@10 goes 0.250 → 0.167; at **W=3.0**
`cross_lingual_en_pt` goes 0.200 → 0.150 and `temporal` gives up one gold
document (0.800 → 0.700). Those strata are n=6 and n=10, so one document is
most of the signal — the same power problem, seen per stratum.

**It survives the production path, and the contamination is now a number
instead of a narrative.** The primary read runs without the reranker.
Re-measured through the shipped ranking (rerank on, window 20, RK-02 gate on;
labelled `non-held-out` so it makes no second claim on the split), W=1.0 → 2.5,
where train and held-out argmax **agree at 2.5**:

| rerank arm (n=66) | 1.0 | 2.0 | 2.5 | 3.0 |
|---|--:|--:|--:|--:|
| overall recall@10 | 0.4306 | 0.5316 | 0.5669 | 0.5720 |
| overall mrr@10 | 0.4012 | 0.4962 | 0.5316 | 0.5316 |
| held-out mrr@10 (n=33) | 0.3503 | 0.4823 | **0.5227** | 0.5227 |
| held-out recall@10 (n=33) | 0.4141 | 0.5455 | 0.5859 | 0.5960 |

Held-out W=1.0 → 2.5: mrr@10 +0.1724 (p=0.015), recall@10 +0.1717 (p=0.008),
hit@10 +0.1818 (p=0.028). `monolingual_pt` goes 0.000 → 0.417 recall@10;
`lexical_identifier`, `temporal`, `cross_lingual_en_es` and `monolingual_es`
are all completely FLAT across every weight (the reranker re-decides their
head, so the prior's rank cost disappears); the movement is `monolingual_pt`
and `multi_hop` only.

**The arm is formally INVALID and by one query.** The capture now records, per
query, whether reranking actually RAN — so the first cut's caveat ("the
timeout fired and that fallback is sticky, so the W=1.0 baseline is partly
unreranked") is replaced by a count. Reranking applied to **57/66 at W=1.0 and
58/66 at 2.0, 2.5 and 3.0**, so `comparison_valid: false` — and the whole
inequality is `xl_pt_04`, which measures 26.6s uncontended against a 30s
timeout and lost the coin flip once. Decomposing the rest, via
`--explain --json`'s `ranking.rerank_gate` rather than by inference:

* **7 RK-02 gate skips** (`lex_02/04/07/08/09/11/12`, all
  `reason: pinned_unique_identity`) — by design, identical at every weight,
  not contamination.
* **1 timeout at every weight** (`xl_es_05`, 18.0s uncontended, over budget
  under load) — equal across arms, so it biases nothing.
* **1 timeout at W=1.0 only** (`xl_pt_04`) — the entire inequality.

So the direction is confirmed under production settings and the magnitudes are
near-clean rather than untrustworthy; the arm is still labelled invalid,
because "one query" is a judgement and `comparison_valid` is a rule. The old
"identical 2.5/3.0 rows are a contaminated pass" caveat is also withdrawn:
those rows are identical because both passes reranked the same 58 queries.

**One thing the re-run falsified in passing** (recorded, not fixed — it is the
reranker's, not this session's): the timeout warning says it falls back "for
this and all further queries this session", and the measurement says
otherwise — 57-58 of 66 queries were reranked in every pass that printed it.
The fallback is per call; only the WARNING is once-per-session. The wording
overstates the blast radius and led the first cut to discard a usable arm.


**Second reason the prior was dead, now recorded in the code.** `_resolve_zone`
keys on a `source_zone:` frontmatter field that **0 of the reference vault's
2,570 indexed notes carry** (it migrated again after that field was written),
so every lookup takes the flattened-column fallback. That count is the indexed
population itself — the scan covers exactly `brain/` + `raw/` minus
`raw/originals/`, which is what `BrainIndex.rebuild` walks (2,571 markdown
files, 2,570 indexed notes). An earlier "3,589" in this document and in the
shipped docstring named no population and is withdrawn. The method is kept —
the fallback is correct, and vaults migrated only once still have the field —
but the consequence for tuning is now in its docstring: on a vault without
`source_zone`, `BRAIN_ZONE_WEIGHTS` must be keyed on `brain`/`raw`, not on
Johnny-Decimal zone names, or it silently does nothing.

**A misconfigured opt-in now says so.** Invalid JSON, a non-object document, a
non-numeric value, or a factor outside [1e-6, 1e6] is dropped with one stderr
warning naming the offending key (5e-324 parses as a positive finite float and
multiplies every RRF contribution to zero — indistinguishable from the feature
being off). An unrecognised `BRAIN_ZONE_SCOPE` fails safe to `semantic_only`
rather than falling through to `all`, which would apply weights to lexical and
exact candidates and change what the reranker is handed. Confirm what actually
applied with `search --explain --json` — each hit carries `zone.applied` and
`zone.factor`.

**What a real calibration would need** (none of it available today): a second
labelled corpus, or a golden set several times larger — at n=33 the smallest
resolvable mrr@10 effect is 0.199, and distinguishing W=2.5 from W=3.0 means
resolving a difference of ~0.004. Per-stratum calibration is further out of
reach still: `monolingual_es` and `cross_lingual_en_es` are n=6, where one
query is 16.7 percentage points.

**Proven through the shipped CLI, not only the eval harness.**
`brain --vault <ref> search "<pt_01>" -k 20 --no-rerank --json` puts the gold
document nowhere in the top 20 by default and at **rank 1** with
`BRAIN_ZONE_WEIGHTS='{"brain": 2.5, "raw": 1.0}'` set
(`eval/runs/zone-prior-cli-end-to-end.txt`). Worth recording how that check was
first WRONG: the committed qrel path is pre-migration, so a plain `endswith()`
on it matched nothing and reported "not found" for BOTH arms — a false negative
that looked like the feature failing. It has to be mapped through the same
slug→note map `eval/pt_stratum_diagnosis.py` uses. **That lesson is now a guard
on the harness, not just a note**: the identical false-negative class was
sitting unnoticed in the primary capture (three `temporal` queries), and the
capture now aborts on it.

Artifacts (gitignored, re-runnable):
`eval/runs/zone-prior-calibration-2026-08-04-bare-v2.json` (primary read),
`eval/runs/zone-prior-calibration-2026-08-04-production-rerank-v2.json`
(confirmation through the shipped reranker),
`eval/runs/zone-prior-qrel-overrides.json` (the 4 canonical-path overrides,
owner-private, same shape as `eval/s02_established_path_map.py`'s),
`eval/runs/zone-prior-calibration-2026-08-04-readout.md`. The superseded
first-cut artifacts (`…-bare.json`, `…-production-rerank.json`) are kept
beside them; nothing in this document is computed from them.

## 10. The fusion defect itself — RRF rewards leg BREADTH over leg STRENGTH (scoped, not started)

Item 7 named the cause; item 9 counterweights it. **Neither fixes it.** This is
the properly-scoped follow-on, deliberately NOT attempted inside s06 — it
redesigns the ranking every query passes through, which is not a tail-end task.

**The problem.** `_hybrid_search_impl` fuses BM25, dense and the ADR-0008 exact
leg with Reciprocal Rank Fusion at `k=60`: each leg contributes `1/(60+rank)`
and the contributions are summed. A document a leg never returned contributes
nothing — so a mediocre document present in *two* legs outscores an excellent
document present in *one*. There is no notion of a leg being INAPPLICABLE to a
query as opposed to merely unconvinced by a document.

**The measurement that isolates it** (`eval/pt_leg_attribution.py`,
`monolingual_pt`, n=12): dense alone recall@10 **0.500** at median rank **2**;
BM25 alone **0.000** (0 of 12 found within 100 — a Portuguese query shares no
tokens with English note text, which is arithmetic, not a model weakness);
fused **0.000** at median rank **52**. Fusion is strictly worse than its better
leg, by construction.

**What a fix has to do.** Detect that a leg has no purchase on this query
(rather than that it disagrees), and stop that leg diluting the others —
without breaking the case RRF is good at, where two independent weak signals
agreeing is genuine evidence. Candidate directions: per-query leg weighting
from leg-internal score distributions; score-based fusion for the dense leg
where its similarities are calibrated; treating an empty/uninformative leg as
absent rather than as a zero vote.

**What it must re-measure before shipping.** All seven strata, both halves of
the pre-registered split, with `lexical_identifier` (n=12, currently 1.000) as
the non-inferiority gate — a fusion change that helps Portuguese by weakening
exact-identifier retrieval is a regression, and the same held-out
discipline as item 9 applies (`eval/zone_prior_calibration.py` is the harness
shape to reuse). This is also the item that would make item 9's opt-in
unnecessary rather than permanent.

## 11. `monolingual_es` is 0.000 at EVERY zone weight — a separate, still-unexplained cause

Recorded as its own item at the owner's instruction, because item 9's remedy
demonstrably does not touch it and item 7's falsification removed its
previously-assumed cause.

**What is known.** Across the full sweep — W = 1.0, 1.25, 1.5, 2.0, 2.5, 3.0,
4.0, 5.0, 8.0, 16.0 — `monolingual_es` (n=6) stays at recall@10 **0.000**. Not
degraded, not moved: identical at every setting, while `monolingual_pt` on the
same corpus and the same mechanism goes 0.000 -> 0.667. So whatever loses the
Spanish queries is not the burial that the zone prior counterweights.

**One new data point, from item 9's production-path arm:** with the RERANKER
on, `monolingual_es` reads 0.1667 — 1 of 6 — at every weight from 1.0 to 3.0.
So the cross-encoder finds something the fused ranking alone never surfaces,
and the zone weight still moves nothing. That is a second signal that the
Spanish failure is upstream of fusion, not inside it. At n=6 it is one query
and proves nothing on its own; it is recorded as a lead, not a finding.

**What is known to be WRONG about the earlier diagnosis.** Item 1 attributed it
to embedder weakness and proposed a bigger multilingual model; item 7 falsified
that reasoning for Portuguese and closed the swap (item 8). The Spanish
attribution was never independently tested — no ES equivalent of item 7's
22-note capacity fixture was built.

**What is known to be wrong about the FIXTURE.** Item 2: every gold document
behind those 6 questions is English prose, so the stratum tests cross-lingual
ES→EN retrieval, not monolingual Spanish, and its name misleads every reader of
every table it appears in.

**Next step, in order:** (a) run item 7's capacity fixture in Spanish — it is
parameterised prose, so this is a fixture edit, not new machinery — to
establish whether `multilingual-e5-small` can do ES at all before anything else
is theorised; (b) fix the stratum label (item 2); (c) only then re-open a cause.
At n=6 nothing measured on this stratum can be significant — one query is 16.7
percentage points — so any finding needs more labelled ES queries first.

## 12. BR-07 p95 tail: shape is stable, but the absolute interactive budget FAILS (S09, 2026-08-05)

**Verdict: NOT resolved as a latency acceptance.** The previous 5.8-second
worst-case report is obsolete, but the current post-S12 configuration does not
meet the owner's accepted **~5–6 s p95** budget. Its tail is no longer a 14x
outlier — both arms pass the `<3x` shape rule — yet their warm p95 values are
about 15–16 seconds. This is a named-cause, bounded S09 acceptance record, not
a claim that BR-07 is closed-by-performance.

### Measurement

Engine **0.19.27**, post-S12 `BAAI/bge-m3-int8` live index (2,571 notes / 106,183
chunks), shipped rerank window 20. The durable plan log shows S12's re-embed,
test gate, and shipping completed before S09 was dispatched; no other plan
session was in flight during the capture. The sandbox denies process-table
inspection and normal SQLite's WAL shared-memory sidecar, so the harness opened
the already-settled live index through `mode=ro&immutable=1`; it verified the
post-S12 index metadata first and made no vault or index writes.

The harness ran two interleaved passes over all 66 golden queries. It forced one
unmeasured cold real-rerank call first (**10.86 s**) and discarded the first
timed call of each arm; p50/p95 below are linear percentiles over warm per-query
medians. Cold loading is therefore reported, never averaged into the result.

| arm | p50 warm | p95 warm | p95/p50 | absolute ≤6 s? |
|---|---:|---:|---:|---|
| Shipped gated path (S11 on) | 7.30 s | **16.00 s** | 2.19x | **FAIL** |
| Always rerank (`--no-rerank-gate`) | 7.31 s | **14.97 s** | 2.05x | **FAIL** |

The S11 gate keeps **7/66 (10.6%)** queries on the fast path and sends
**59/66 (89.4%)** through the cross-encoder. That is a real saving for exact
identity queries, but it cannot make the corpus-wide distribution interactive:
the gated arm had 116 real reranks and 14 gate skips in its 131 retained calls;
always-rerank had 130 real reranks in 131. One retained timeout fallback occurred
in each arm.

Artifacts (gitignored, no note bodies or query text):
`eval/runs/s09-latency-2026-08-05/latency.json` and
`eval/runs/s09-latency-2026-08-05/S09-ONE-PAGER.html`.

### One profile pass — named cause

S02's five slowest rerank-20 IDs (`lex_02`, `xl_pt_01`, `tmp_02`, `xl_es_05`,
`lex_10`) were profiled through the production in-process candidate/rerank path,
with cProfile attached inside the rerank worker (profiling the caller alone
would only report `Future.result()` waiting). The dominant frame in every case
is `onnxruntime.capi.onnxruntime_pybind11_state.run`, consuming **5.65–18.56 s**
inside `OnnxReranker.rerank`; tokenizer batching is at most 53 ms. The tail is
therefore real CPU cross-encoder inference over the 20-candidate window, not a
cold model load, fusion/chunk scan, or the S11 gate.

The one small reversible mitigation available on this host was also tested:
`CoreMLExecutionProvider,CPUExecutionProvider`. ONNX Runtime reports the
provider as available, but model compilation fails in this sandbox even with
`TMPDIR=/private/tmp`, and every result falls back to the unreranked order. It
is not a valid speed comparison and is not shipped as a default. Evidence:
`s02-slowest-profile.json`, `provider-probe.json`, and
`coreml-tmpdir-probe.json` beside the latency artifact.

### Bounded acceptance / upgrade path

No safe cache, candidate-limit, or model-load move fixes the warm tail: the
model is already warm, the candidate window is the owner-selected 20, and ONNX
execution itself dominates. Do not change ranking policy in S09. If the owner
wants this p95 target met, the next work must be a separately scoped performance
decision: validate a functioning accelerated provider on the deployed host, or
evaluate a materially faster cross-encoder / richer adaptive gate against the
same quality and warm-latency suite. Until then, S10 should report BR-07 as a
**GAP**, not as tail-resolved.

## 13. BGE English non-regression is met by the existing window-20 quality path (Q01, 2026-08-06)

**Verdict: resolved for the declared gate, with two query-level gaps left
explicit.** The post-S12 BGE artifact is the expected 66-query comparison and
its English R@10 losses are exactly `mh_06`, `tmp_07`, `xl_pt_02`, and
`xl_pt_09`. The saved BGE fingerprint is `BAAI/bge-m3-int8`, dim 1024, 2,571
notes / 106,183 chunks. The fresh read-only published snapshot remains BGE
(generation 674, 2,572 / 106,184, vault fingerprint
`dee4bcdfa10400e3d842053b62b856a6f33cbff0ad6bebef916f61c5a76eb90d`).
No live vault/index write and no qrel or threshold change was made.
An earlier ignored Q01 GAP artifact captured generation 672 at 08:20 local
while its snapshot was e5-small; generation 674 was published later
(`2026-08-06T19:38:33Z`) with BGE and remained unchanged through this capture,
so that older observation is chronological history, not the current baseline.

Production traces classify all four as **rank losses after candidate entry**,
not candidate absence and not rerank demotion: the best relevant targets enter
the 160-candidate pool at fused ranks 19, 119, 151, and 15 respectively.
Window 20 recovers `mh_06` and `xl_pt_09` to rank 1. Window 50 recovers the same
two and cannot reach ranks 119/151, so widening it is not a correction for the
remaining cases. The two unresolved queries stay named: `tmp_07` is the sole
temporal English loss and `xl_pt_02` the sole cross-lingual EN→PT loss.

The production defect was a surface mismatch. CLI `search` already followed
BR-03's default-on window-20 path; MCP called `BrainCore.hybrid_search` without
ranking arguments and therefore selected bare retrieval. MCP now resolves the
same shared default/`BRAIN_RERANK_DISABLED` rollback, passes
`rerank=True, rerank_top=20`, and records the actual choice in host query
capture. The focused regression was recorded RED before the change and GREEN
after it.

Fresh rank-correct 66-query scorecard versus the frozen e5 baseline:

| metric | e5 | BGE + production r20 | result |
|---|---:|---:|---|
| overall recall@10 | 0.3725 | 0.5795 | +0.2070 |
| English recall@10 | 0.5127 | 0.5489 | +0.0362 |
| monolingual_pt recall@10 | 0.0000 | **0.5417** | PASS (≥0.20) |
| max lost queries / English stratum | — | **1** | PASS (≤1) |
| lexical_identifier recall@10 | 0.9583 | 0.9583 | non-regression |

Reranking genuinely applied on 59/66 queries; the other seven are the expected
RK-02 unique-identity skips, not timeouts. Latency is context, never a Q01 gate:
p50 5.98 s / p95 11.72 s. Both exploratory probes set
`BRAIN_EMBED_THREADS=8` and `BRAIN_RERANK_THREADS=8`.

Artifacts (gitignored, re-runnable):
`eval/runs/q01-loss-traces-2026-08-06.json`,
`eval/runs/q01-bge-rerank20-2026-08-06.json`,
`eval/runs/q01-scorecard-e5-vs-bge-rerank20-2026-08-06.{json,md}`,
`eval/runs/q01-mcp-rerank-{red,green}.xml`, and the rendered readout +
three-option decision card under
`eval/runs/q01-bge-english-non-regression-2026-08-06/`.

## 14. Actual-vault currency coverage measured read-only (Q02, 2026-08-06)

**Verdict: DONE; the reference deployment's index has 2,572 notes, 445 linked,
for 17.30% curated supersession coverage.** The pure
`brain.versionlink.generate()` pass
used the production 14-day cutoff (`2026-07-23`), examined 75 pairs, and found
one proposal-only Internal name family: the deployment's cloud/AI
supplier-acceptance
policy v2 (2026-07-21) → v3 (2026-07-31), grounded by matching archived
filenames and an advancing numeric marker. It also returned eight ambiguities,
all involving one untrusted migration placeholder whose `captured: unknown`
and generic origin produce a misleading shared stem; none became a proposal.
The recent window was non-empty, so the historical diagnostic was not run.
Before/after manifests for all 3,593 canonical Markdown files and hashes,
sizes, mtimes, and ctimes for the SQLite main/WAL/SHM files are identical;
index metadata and counts are unchanged (`mutation=false`). No `maintain`,
broker, sync, rebuild, supersede, accept, or write command ran, and no owner
acceptance was simulated. Evidence:
`eval/runs/actual-vault-supersession-coverage-2026-08-06.{json,md}`.

## 15. Plan close-out: before-vs-after measured end to end (2026-08-06)

The plan's own before/after, measured rather than assembled from per-session
artifacts. Three arms over the frozen 66-query golden set
(`aa4a2d61…`), live published snapshot generation 674 (2,572 notes /
106,184 chunks), read-only, no vault or index writes.

| arm | recall@10 | nDCG@10 | mrr@10 | p50 | p95 |
|---|--:|--:|--:|--:|--:|
| before (e5-small, no rerank, engine 0.16.0) | 0.3725 | 0.2817 | 0.2670 | 277 ms | 497 ms |
| now, rerank off (bge-m3-int8, 0.19.27) | 0.5202 | 0.3734 | 0.3329 | 297 ms | 425 ms |
| now, shipped default (+ rerank 20 + RK-02 gate) | 0.5795 | 0.5370 | 0.5660 | 6,980 ms | 11,009 ms |

Overall recall@10 +0.2071 (bootstrap 95% CI +0.1010/+0.3157, permutation
p=0.0004, achieved power 0.965). `lang:PT` +0.500 and `class:monolingual_pt`
+0.5417 both survive BH-FDR; nothing else does. **The embedder swap alone is
free** — +0.1477 recall@10 at a p95 slightly BELOW the pre-plan engine; the
reranker buys the remaining +0.0593 recall and the whole ranking gain
(mrr 0.333 → 0.566) for a 26x p95.

`eval/gate.py` returns **FAIL** on both "now" arms, on two criteria:

* `lang:EN` non-inferiority — mean Δ +0.0362 (shipped) and −0.0054 (rerank
  off), but the 95% CI lower bounds are −0.0580 and −0.1087 against a −0.02
  bound. This is a POWER limit at n=46, not a measured English regression:
  closing it needs more English golden queries, not an engine change.
* p95 latency ≤ before — 11,009 ms vs 497 ms for the shipped default (the
  rerank-off arm PASSES this). BR-07 stays a GAP (see #12).

The gate's FAIL text ("abort branch, stay on Obsidian + Smart Connections") is
the S05 cutover decision it was written for, and is not the action here.

### Two measurement defects found and fixed while doing this

1. **`eval/capture_run.py` silently discarded rerank ordering.** It records
   `hit.score`; reranking reorders hits WITHOUT changing that score, so any
   scorer re-sorting on score measures the un-reranked order.
   `eval/rebaseline_rerank_capture.py` already existed for exactly this reason
   and its docstring names the trap — but nothing STOPPED the wrong adapter
   being used. Measured cost when it happened here: mrr@10 0.300 against 0.566
   on identical inputs. `capture_run.py` now refuses `--rerank` and names the
   rank-correct script.
2. **Two capture-time settings were never in the run file.** Neither the RK-02
   rerank-gate state nor WHICH path map was applied (only `mapped: true`) was
   recorded, and both change the numbers: swapping only the map — the 72-entry
   golden-scoped `ne-upgrade-established-path-map.json` for the 3,354-entry
   `_evidence/cutover-s10/path-map.json` — moved overall recall@10 from 0.5795
   to 0.5227 with engine, index and queries identical, because the doc_key a
   hit is emitted under decides whether it matches a qrel at all. Both capture
   scripts now stamp `params.rerank_gate`, and the rank-correct script stamps
   `map: {path, sha256, entries}`.

Reproducibility: this capture reproduces the independent 20:06 capture of the
same configuration EXACTLY on every quality metric, and the RK-02 gate took its
fast path on the same 7 of 66 queries in both. Evidence, one-pager and decision
card: `eval/runs/plan-close-2026-08-06/`.

### Owner ruling, 2026-08-07: KEEP the shipped default (option 1)

Reranking stays ON by default (window 20, RK-02 gate), at the measured p50
7.0 s / p95 11.0 s. This re-affirms the 2026-08-04 ruling on a larger measured
gain; no engine or config change follows from this close-out. BR-07's p95 tail
remains a standing GAP (#12) and the accelerated-provider path (option 3) stays
open engineering work, not a scheduled commitment. The `lang:EN`
non-inferiority check remains unproven at n=46 — it needs more English golden
queries, not an engine change.

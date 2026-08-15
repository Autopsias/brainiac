# Context

Domain language for the `brain` engine. Terms here are the ones that were ambiguous enough
to cause a real design error — not a glossary of every noun in the codebase.

## Index lifecycle

**Rebuild** — a full re-index of the vault from scratch: every note chunked, embedded and
written. Builds into a temp DB and atomically swaps it into the live path on success. Can
run 90+ minutes on the real vault.

**Sync** — an incremental reconcile by path + content-hash; only changed/new notes are
re-indexed. **Sync is not a separate operation from rebuild**: `sync()` self-delegates to
`rebuild()` whenever the schema is missing or the embed model changed
(`index.py:558-565`, modes `rebuild(no-schema)` / `rebuild(model-change)`). Any reasoning
that treats them as two independent actors — two lock holders, two durations — is wrong.

**Complete temp DB** — a rebuild staging DB carrying a `meta`-table row marking completion,
written and committed **in the same transaction as its final batch**. Only a complete temp
DB may be swapped into the live path. Introduced because per-batch commits made "the
function returned normally" an insufficient completeness signal. The marker lives *in the
DB*, never in the JSON manifest — a filesystem write cannot join a SQLite transaction, and
the two files have independent durability across a power loss. See
[ADR 0007](docs/adr/0007-resumable-rebuild-staging-artifact.md).

**Staging artifact** — a surviving partial rebuild, deliberately preserved for resume. It
is **DB + WAL + SHM as a unit**, never the DB alone: with WAL enabled, committed batches
may live only in `<temp>-wal` until a checkpoint. Distinct from a *leak*, which is an
orphaned temp file failing validation and is still deleted on sight.

**Manifest** — advisory pre-flight metadata beside a staging artifact (model id, embedding
dimension, schema version, backend, index-format version, vault fingerprint), used to
reject an unusable partial cheaply before opening the DB. It is **not** the completion
authority.

## Concurrency

**Single-writer discipline** — at most one process writes the index at a time, enforced by
an advisory `flock` held for the writer's process lifetime. The lock is taken at the
**outermost write verb** and is re-entrant within a process (because sync delegates to
rebuild). Read paths never take it, and the read-only VM posture (`mode=ro` +
`PRAGMA query_only=ON`) must create no lock file at all.

**Writer-busy skip** — the outcome when a scheduled run cannot acquire the writer lock
because a long rebuild holds it. It is **not a failure**: it exits 0, refreshes
`last_attempt`, and does not increment `consecutive_failures`. Conflating the two would
let one 90-minute rebuild manufacture an hour of "failures" and fire a spurious
escalation.

## Retrieval ranking

**Fusion leg** — one ranked candidate list contributing `w / (rrf_k + rank)` into the RRF
sum. There are two organic legs (lexical/BM25 and dense/vector); ADR 0008 adds a third,
`exact`, for alias and title matches. A leg contributes in **rank space** — it never reasons
about score magnitude, which is the whole point of RRF.

The rank-space property is **load-bearing, not incidental**: it is what gives the fused
score a known RRF ceiling, and that ceiling is what makes every post-fusion *prior* a
damping-only factor (see *prior* vs *boost* below). So a proposal to fuse in **score
space** — a convex combination of normalized leg scores, however well evidenced in the
literature — is not a drop-in tuning change: it moves the ceiling, silently redefines what
the zone-authority and staleness priors mean, and strands the `exact` leg's weights, which
are calibrated in rank space. Such a change requires an ADR superseding this entry, never
a flag alone.

**Fusing constant** (`RRF_K_FUSE`) vs **calibration key** (`RRF_K_EXACT`) — two different
60-vs-3 numbers that are easy to conflate. `RRF_K_EXACT` (60) is ADR 0008's calibration
key: it gates whether the exact leg may participate at all, and is what a stored query-log
record replays against. `RRF_K_FUSE` (3, since RET-11) is the denominator the legs are
actually fused at. Consequence that has already misled one experiment design:
`_exact_leg_enabled(rrf_k)` returns False whenever `rrf_k != RRF_K_EXACT`, so **an eval arm
that varies the fusing constant through the ARGUMENT path silently drops the exact leg
entirely** — which reads as "the candidate broke identifier retrieval" when it was the
coupling. Measured 2026-08-09 (`_evidence/eval-power/s06-attempt-1/exact-leg-participation-probe.txt`):
`eval/capture_run.py --rrf-k` and `eval/rebaseline_rerank_capture.py --rrf-k` take
`exact_leg_len` 2 → 0; the environment variable **`BRAIN_RRF_K` does not** — it moves the
fusing constant with the exact leg still ON. Sweep with the env var, never the flag.

**Prior** vs **boost** — a *prior* is a post-fusion multiplicative factor **≤1.0** (the
zone-authority prior, the staleness penalty). It can only ever damp. The fusion-scale
invariant is that the fused score never exceeds the RRF ceiling, so a **boost** (>1.0) is
not a smaller version of a prior — it is a different mechanism that breaks the invariant.
Wanting a note to rank *higher* is therefore an argument for a new fusion leg, never for a
post-fusion multiplier. (This distinction is exactly the design error `/plan-harden` caught
in the 2026-07-28 retrieval plan, which specified a "bounded post-RRF multiplier".)

**Alias** — an *alternate label*: a string that denotes the same entity as the note's title
with 100% equivalence (an acronym, a spelling variant, a former name). It is NOT a synonym;
context-dependent near-meanings do not earn exact-match treatment. An alias may be owned by
more than one note (a `supersede` leaves the retired note holding its aliases), so
"the note for this alias" is a **tiebreak**, not a lookup.

**evidence** — why a hit matched (which layer fired). **create_safety** — how far an agent
may trust that the note already exists: `exists` requires an *unambiguous* match (uniquely
owned alias, exact title, exact lexical); an ambiguous match is at most `probable`. The
distinction matters because `create_safety` is the field a capture agent reads to decide
*not* to write a duplicate note.

**The fusion defect** vs **the fusion residual** — the *defect* is the RRF breadth-over-strength
failure (a note two legs found mediocrely outranking a note one leg found first). It is
**FIXED**: RET-11 (commit `d5b2c58`, 2026-08-05) lowered the fusing constant to 3, taking
golden hit@10 from 30 to 36 of 66. The *residual* is what that fix did not reach —
`cross_lingual_pt_en` at 6 of 12 rather than 12 (the stratum was called
`monolingual_pt` until PT-01 renamed it on 2026-08-09 — all 22 of its gold documents
are English prose behind Portuguese questions), and one `cross_lingual_en_es` regression.
Conflating the two is not hypothetical: on 2026-08-09 an entire 9-session plan was built to
"fix the fusion defect" because `eval/FOLLOWUPS.md` item 10 still read "(scoped, not
started)" four days after the fix shipped. Work in this area names which of the two it
targets, and cites the shipped baseline rather than the pre-fix numbers.
**The residual is no longer a fusion question.** **56 candidate arms plus the shipped
baseline (57 measured in total)**, across all three successor mechanisms, were measured on
2026-08-09; not one passed both pre-registered eligibility criteria (the
`lexical_identifier` kill floor and the +0.0200 minimum effect), so no candidate earned a
held-out test and the owner closed `eval/FOLLOWUPS.md` item 10 the same day. What remains
is **consistent with a retrieval-recall floor; root cause unconfirmed** — 7 of 10 new
`cross_lingual_en_pt` queries have an unreachable grade-3 gold document, while the same
s09 review also records duplicate families splitting the dense signal between
near-identical vectors, and join defects, as contributors. So reach for retrieval recall,
not for a fusion dial — and do not name the embedder as the cause until something
measures it.

**Held-out claim** — the act of scoring the held-out half of the pre-registered golden split.
It may happen **exactly once per split generation**, because a split consulted while
choosing among candidates is no longer an unbiased test of them. A claim is therefore a
consumable resource, not a repeatable measurement: `eval/capture_run.py`'s guard is
positional (only a fresh bare capture may claim held-out; a `--from-ranks` replay or a
rerank arm is refused outright), and a spent split can only be replaced by registering a
new one over a changed golden set. The 66-query split (`held_out_v1`) is **spent** — RET-11's
constant sweep and the zone-prior calibration both claimed it. Its successor over the
114-query set, `s05-2026-08-09-expanded114`, is **also spent as of 2026-08-10**. It was
registered on 2026-08-09 and survived one no-finalist pass — s06 measured 57 fusion arms on
the train half, found no finalist, and declined to open the seal; a ledger row reading NOT
MADE is a no-finalist branch, not a blocked measurement. The claim that spent it was
RET-05 query-variant fan-out on 2026-08-10 (+0.0380 recall@10, p = 0.2509 — a null), one
row in `_evidence/eval-power/HELDOUT-CLAIM-LEDGER.md`. **Both registered splits are now
consumed**: the next held-out read needs a new split over a changed golden set, not another
pass at these.

**Query-variant fan-out (RET-05 / the CON-01 variant contract)** — the CALLER translates
the question into each vault language the derived census lists and passes them as repeatable
`--variant` arguments; the engine pools the per-variant rankings in rank space before fusion.
It is **caller-opt-in and will not become an engine default** (owner ruling 2026-08-10,
`_decisions/anylang-s05-ship-ruling.md`), because it missed its own pre-registered held-out
bar: +0.0380 recall@10 at p = 0.2509 against +0.0890 and p < 0.05. Read that null with the
two facts beside it, or it reads as "the mechanism does nothing": on the target case
(`cross_lingual_en_pt`) the held-out stratum moved off an absolute 0.0000 → 0.2000, and
recall@20 rose +0.0980 against recall@10's +0.0380 — fan-out fills the pool and about a third
of the extra gold reaches the top 10 unaided. **The famous +0.1667 for
fan-out + `rerank_fused` is MIS-ATTRIBUTED and must not be cited as a fan-out
number** (corrected 2026-08-11, `_evidence/invariants/s10-claim-readout.md`): it compared a
RERANKED arm against a NON-reranked baseline. Re-measured it reproduces at +0.1491, of which
+0.0848 (57 %) is the reranker the vault already ships; the residual over the SHIPPED
configuration is **+0.0643** (p = 0.0284, 6 wins / 1 loss / 50 ties), **train-half only, never
confirmed** — the held-out half of `s08-2026-08-11-expanded` was never opened and the ledger's
terminal state is CLAIM NOT MADE. On that train-half evidence the owner ruled (2026-08-12,
`_decisions/invariants-s11-ship-ruling.md`) that **`rerank_fused` auto-enables on 2+ variants
and never on a single query**; kill switch `BRAIN_RERANK_FUSED_DISABLED=1`, per-call opt-out
`--no-rerank-fused`. The `--variant` fan-out itself remains caller-opt-in.
Spanish is unreachable here by census rule, not by defect (`es` at 0.65 % is below
`min_share`, so it is not a vault language and no ES variant may be invented). The
correlated-vote guard was measured **harmful** (361 demotions) and stays off.

**Gate** vs **signal** — the golden set + `eval/gate.py` non-inferiority test is the **gate**:
it blocks a ranking change. Real-traffic replay is a **signal**: it runs against a corpus
that has moved since capture, so it can report risk but must not block. A stability number
over a mutated vault cannot separate ranking drift from vault drift without a per-query
vault fingerprint.

## Failure surfacing

**Pull surface** — a surface the operator has to go and look at: `brain doctor`,
`brain status`. Cheap to be noisy, so it escalates at `consecutive_failures >= 2`.

**Push surface** — a surface that interrupts the owner unprompted: the brief/digest banner
and the desktop notification. Expensive to be noisy, so it escalates at `>= 3`. The two
thresholds differ **deliberately**; unifying them would either spam the owner or silence
doctor.

**Failure escalation** vs **liveness escalation** — a failure escalation counts branches
that ran and failed (`consecutive_failures`). A liveness escalation flags branches that
stopped running *at all*, keyed on `last_attempt` age. Only the latter can detect the mode
that went unnoticed for 32 nights: when the process never runs, no handler fires, no
counter increments, and a failure count stays at zero forever.

## Capture provenance & ingestion learning

**`provenance.*` keys** — flat dotted frontmatter keys, written and read as literal
strings (`provenance.trust` at `capture.py:72`, `core.py:61`, `maintenance.py:1736`).
There is **no `provenance:` mapping**: nesting these keys breaks the drain's
untrusted-input detection (it looks up the literal dotted string) and the validator's
allowlist. New provenance facts (sender, sent date, conversation id, subject) are added
as **sibling flat dotted keys**, never as a nested block. This distinction caused a real
plan-design error on 2026-07-30 (a session premised on "extend the mapping").

**Pattern vs category** — two independent graduation keys in the auto-capture gate.
*Pattern* is the existing opaque per-extraction-pattern string (`pattern_stats`,
`autocap-config.json` `patterns`); *category* is the owner-facing ingest taxonomy label
from `overlay/cos/ingest.md`. The never-graduate sentinel set `_UNPATTERNED`
(`cos.py:1461`) contains the string `"unclassified"` — an unknown/missing value on
either key must map to **that existing sentinel**, not a new spelling ("uncategorized"
is graduable, which is the bug).

## COS run evidence

**Ledger** vs **counter** — a *ledger* is the per-row append-only record of what the run
did (`_cos_verdict_ledger_*.jsonl`, `_cos_ingestion_ledger_*.jsonl`,
`_chip_reeval_*.jsonl`); a *counter* is the aggregate written to the metrics row. They are
**independent writers and either one can lie about the other**: run 64 rebuilt run 63's
ledger while its counter stayed truthful, and a pre-flight metrics row once reported six
empty nights while 181 real archives went unledgered. A diagnosis is only sound once the
two are **joined** — a count agreeing with itself proves nothing.

**Verdict ledger** vs **ingestion ledger** — the *verdict* ledger is one row per
classified thread (`act` / `read` / `noise` + priority); the *ingestion* ledger is one row
per thread that Phase 1.6 actually processed. The verdict ledger is therefore the
**denominator** and the ingestion ledger the **numerator** — E29's whole content is that
the second covers the in-scope subset of the first (`act`, plus `read` at P0/P1). Reading
either alone cannot see a silent shortfall, which is the failure E29 exists for.

**Producer self-report** vs **host control** — the 30 E-checks are written by the model
running the nightly and are *self-reported*; `cos_runverify.check_self_eval` binds only
that a result line exists for **every id in the frozen bundle's set** (the set, never the
count). Seven checks have some clause re-executed host-side and **E28 alone is fully
re-derived**. So "E<n> PASS" in a run report is a claim, and "scored from the artifacts"
means re-deriving it from the ledger files — never from the report's own PASS line.
`docs/cos-instrument-inventory.md` §7 is the authority on which is which.

**Vacuous pass** — a conditional check that reports PASS because its precondition never
fired (no rows, no actions, nothing in scope). The doctrine repeatedly had to add an
explicit non-vacuity floor for exactly this (E22(a2), E26's non-zero evaluated count,
E29's run-obligation). **A check scored on a run that did nothing is evidence of nothing**
— any acceptance criterion naming an E-check must also name the denominator that makes it
non-vacuous.

**Hold** vs **chip** — a *hold* is a label parked on a conversation (`Held · uncertain`
being the generic one); a *chip* is the tracked commitment the re-evaluation phase
disposes. Chip re-evaluation is the **only** mechanism that removes a hold, so "drain the
holds" and "re-evaluate the chips" are the same operation named from two ends.

**Lane** vs **toolset** — a *lane* is the mutation TRANSPORT the run elected (`rest` |
`native-ui` | `none`); a *toolset* is the browser SURFACE that proved it (`iab` |
`chrome-plugin`). They are recorded as two separate metrics fields (`mutation_lane`,
`mutation_toolset`) because **a lane is only available on some toolsets**: doctrine v5.13
records two shadow lanes UNRUNNABLE for six days because their screens were specified
"REST-shaped" on a surface that "exposes no in-page REST at all", and v5.7 records that
Codex's unattended browser "cannot capture bearers or execute in-page fetch". So "the REST
lane works" is never a complete claim — it is only true *of a named toolset*, and a design
that names a lane without naming its toolset has not said whether it can run unattended.

**Undo canary** vs **unread canary** — same word, unrelated concepts, and they appear in
the same session. The *undo* canary (`cos-ops/_cos_undo_canary.json`) is the periodic drill
proving a mutation can be reversed; E17 gates auto-archive on one that is **lane-matched**,
≤30 days old, and carries **per-step receipts** — "a written file is not a run drill". An
*unread* canary is an ad-hoc read-only probe: one unread message named at run start,
never touched, re-checked at the end to prove the run did not change read state. Qualify
the word every time; an unqualified "the canary passed" is ambiguous about which guarantee
was actually established.

**Outcome contract** vs **host validator** — two separate verdicts on one run. The
*outcome contract* (`tools/cos_contract.py`) is the deterministic pass/fail computed from
the run's own three inputs; the *host validator* (`brain.cos_runverify`, INS-01) scores
the run VALID/DEGRADED/INCONCLUSIVE from host-derived stamps. A run needs **both** — the
contract can pass on a run the validator cannot vouch for at all.

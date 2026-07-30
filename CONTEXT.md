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

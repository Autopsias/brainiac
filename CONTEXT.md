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

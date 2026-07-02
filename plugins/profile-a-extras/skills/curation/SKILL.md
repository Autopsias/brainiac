---
name: curation
description: "On-invoke curation pass over a brain-substrate vault — stale wikilink targets (a note links to something that's drifted or been archived since), and a staleness revisit sample (notes overdue for a re-read, ranked by age and, where available, link centrality). Triggers: 'run curation', 'find stale links', 'what needs a re-read', 'revisit queue', 'curation pass'. Read-only by default — surfaces findings, never edits a note's content. Not a scheduled task in this kernel: a deployment that wants a cron cadence wraps this skill with its own scheduler; the kernel itself is maintainer/analyst-invoked."
---

# curation (brain-substrate kernel)

Two independent checks. A failure in one never suppresses the other — they
read disjoint inputs (the wikilink graph vs. note age) and produce disjoint
outputs (a stale-link report vs. a revisit queue).

This kernel is **on-invoke**, matching `docs/cutover/task-disposition.md`
task #4's disposition: orphan/stale-link/centrality lint is vault-structure
work with no `brain`-retrieval verb behind it (it stays substrate-overlay,
not a CLI fold), so it runs when a maintainer or analyst calls it — never as
a host-write OS-scheduled task in the kernel itself.

## §C — stale wikilink targets

**Why this matters.** `graph-expand`'s wikilink-BFS is DISCOVERY-ONLY by
design (AGENTS.md §5: "never authoritative... confirm candidates with
get/read") — but that warning is about treating graph hops as ground truth,
not about whether the *targets themselves* are still good. A wikilink
pointing at a note that's since moved to `archive/`, been superseded, or
simply gone stale is a different problem: the graph edge still resolves, but
following it leads somewhere outdated.

```bash
# 1. Find recently-touched notes (the sources worth checking)
brain --vault "$BRAIN_VAULT" recent -n 20 --json

# 2. For each, extract its outbound [[wikilinks]] and resolve each target
brain --vault "$BRAIN_VAULT" get <target-id> --json
```

For each recently-updated note's outbound links, fetch the target via `get`
and check its `updated:` date and whether it's parked in `archive/`. Flag a
target as stale if: (a) it lives in `archive/` but is still linked from an
active `projects/`/`areas/`/`resources/` note, or (b) its `updated:` date is
older than a configurable threshold (default 180 days) relative to the
linking note's `updated:` date.

**Report, don't auto-fix.** A stale target is a human merge/keep/repair
decision — surface `<source> → [[target]] (target last updated: YYYY-MM-DD,
zone: archive/)` and propose: update the target, repoint the link, or accept
it as an intentional historical reference. The one case safe to auto-fix is
an **unambiguous rename** (the target id changed but is otherwise identical
content) — everything else is `action_required`.

## §E — staleness revisit sample

**Why this matters.** Not every note that's gone stale is *linked from*
something recent (§C only catches stale targets reachable from active
notes) — some notes are simply overdue for review regardless of inbound
links. The legacy version of this check used a precomputed PageRank ×
age scorer; under the brain substrate, **global graph centrality is gap G3**
(`docs/cutover/brain-cli-gaps.md`) — not yet a CLI verb. This kernel uses the
documented fallback: **age alone**, with `graph-expand`'s per-seed PPR score
as an optional, partial centrality signal when a seed set is already known
(it is NOT a substitute for whole-corpus ranking — don't claim it is).

```bash
# Primary signal: every note's age (no centrality term — degraded but functional, per G3's documented fallback)
brain --vault "$BRAIN_VAULT" bases-query --where type=note --json   # enumerate notes + their `updated:` dates

# Optional secondary signal, only if you already have candidate seeds:
brain --vault "$BRAIN_VAULT" graph-expand <seed-id-1> <seed-id-2> --depth 2 --json
# read each result's "ppr" field as a rough centrality proxy for THAT seed set only
```

Build a staleness score from `(today − updated)` alone, sort descending,
sample the top 3–5 into a revisit list. If a `graph-expand` PPR score is
available for a candidate (because it surfaced as a neighbour of a known
seed), let it nudge the ranking — but the age-only ranking is the
substrate-accurate baseline; treat any PPR contribution as a bonus signal,
not the scorer's foundation, until `brain graph-rank` (G3) ships.

**Output.** Write the sample as a plain list (not auto-committed to the
index — this is a triage artefact, not a `brain/` note):

```markdown
## Revisit queue — generated YYYY-MM-DD

| Note | Last updated | Days stale | PPR (if seeded) |
|---|---|---|---|
| `<id>` | YYYY-MM-DD | N | 0.0N or — |
```

## Backpressure

If a deployment tracks an open-findings queue and it's already over
capacity, suppress new §C/§E *emissions* (don't surface more action-required
rows) but **still run both checks and still write the revisit list** — the
list itself is useful triage input regardless of whether new findings are
being surfaced this run. This mirrors the legacy task's merge decision: the
queue file is worth producing even under backpressure; only its emission
into a downstream action queue is what gets held.

## Hard guardrails

- **No content edits.** §C never rewrites a note body — even an "unambiguous
  rename" auto-fix only repoints the wikilink syntax, never touches prose.
- **No deletes.** A note past its staleness threshold is surfaced for
  review, never moved to `archive/` automatically.
- **§E never asserts a corpus-wide ranking it can't back up.** Without
  `graph-rank` (G3), say "age-ranked, PPR-augmented where seeded" — not
  "PageRank-ranked" — in any report this produces. Overclaiming a centrality
  method that isn't actually running is the kind of drift this kernel exists
  to prevent in *other* notes; it shouldn't introduce its own.

## Cross-references

- `docs/cutover/repoint-map.md` §5 — the dependency table this skill implements
- `docs/cutover/brain-cli-gaps.md` G3 — global graph-rank (the gap §E's fallback works around)
- `docs/cutover/task-disposition.md` task #4 — OVERLAY-ONLY / ON-INVOKE disposition
- `AGENTS.md` §5 — `graph-expand` is DISCOVERY-ONLY; never treat its output as authoritative
- `.claude/skills/kb-curator/SKILL.md` — `audit-orphans` (zero-backlink notes) is the companion check this skill doesn't duplicate

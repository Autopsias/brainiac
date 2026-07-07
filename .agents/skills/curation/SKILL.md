---
name: curation
description: "Curation pass over a brain-substrate vault — stale wikilink targets (a note links to something that's drifted or been archived since), and a staleness revisit sample (notes overdue for a re-read, ranked by age x whole-corpus link centrality). The engine (`brain curate`) now runs both checks itself and folds them into the Sunday brain-nightly branch (AUT-02) — this skill is the on-invoke deep-dive surface: read the engine's findings, and run the two checks (orphans, contradictions, callouts) that still have no brain-retrieval equivalent. Triggers: 'run curation', 'find stale links', 'what needs a re-read', 'revisit queue', 'curation pass'. Read-only by default — surfaces findings, never edits a note's content."
---

# curation (brain-substrate kernel)

Two independent checks — stale wikilink targets (§C) and a staleness revisit
sample (§E) — plus two orphan/structure checks (§F) that stay this skill's
own job. A failure in one never suppresses another.

**As of AUT-02 (session s08), §C and §E are ENGINE checks, not overlay-only
lint:** `brain curate --json` runs both directly against the index
(`stale_links` + `revisit_sample` fields) and, per `routines/manifest.json`'s
`curation` row, the Sunday branch of `brain maintain` (the single sanctioned
`brain-nightly` task) runs them automatically every week, queuing findings
into `.brain/memory/hot.md` — nothing to remember to run. This skill is now
the **on-invoke surface**: run it to inspect the engine's findings in depth,
run `brain curate` standalone between Sundays, or run §F's orphan/
contradiction/callout checks, which still have no brain-retrieval equivalent
(vault-structure lint, `routines/manifest.json` disposition: OVERLAY-ONLY).

## §C — stale wikilink targets

**Why this matters.** `graph-expand`'s wikilink-BFS is DISCOVERY-ONLY by
design (AGENTS.md §5: "never authoritative... confirm candidates with
get/read") — but that warning is about treating graph hops as ground truth,
not about whether the *targets themselves* are still good. A wikilink
pointing at a note that's since moved to `archive/`, or vanished entirely, is
a different problem: the graph edge no longer leads somewhere current.

```bash
# The engine check — every outbound wikilink whose target vanished or moved
# to archive/, egress-gated on BOTH the source and (if resolved) target note:
brain --vault "$BRAIN_VAULT" curate --json --max-tier Internal
# -> res["stale_links"]: [{"from": {...}, "target": {...}|null,
#                          "target_text": "...", "reason": "vanished"|"archived"}]
```

For a deeper look at one hit, `brain get <from-id> --json` /
`brain get <target-id> --json` pull the full notes.

**Report, don't auto-fix.** A stale target is a human merge/keep/repair
decision — the CLI's `action_required` outcome already frames it as: repoint
the link, update the target, or accept it as an intentional historical
reference. §C never rewrites a note body.

## §E — staleness revisit sample

**Why this matters.** Not every note that's gone stale is *linked from*
something recent (§C only catches stale targets reachable from active
notes) — some notes are simply overdue for review regardless of inbound
links.

**Centrality is now real, whole-corpus, and reused — not a gap.** The
previous version of this skill documented "global graph centrality is gap
G3, not yet a CLI verb" and fell back to age-only ranking. AUT-02 (s08)
closes that gap by REUSING the existing `src/brain/graph.py` wikilink-BFS+PPR
module (built for `graph-expand`, RET-03): `revisit_sample` runs Personalized
PageRank seeded with EVERY note (uniform restart across the whole corpus —
i.e. standard PageRank), not a new ranking module. The score is
`age_days x (centrality + 1)` — the `+1` means an isolated/orphan note
(centrality 0) still ranks by age alone rather than scoring zero and never
surfacing.

```bash
brain --vault "$BRAIN_VAULT" curate --json --max-tier Internal
# -> res["revisit_sample"]: [{"id", "title", "path", "updated", "age_days",
#                             "centrality", "score"}, ...]  sorted descending
```

**Output.** The engine's list IS the revisit queue — nothing to hand-build;
render it as a table for a human read if useful:

```markdown
## Revisit queue — generated YYYY-MM-DD

| Note | Last updated | Days stale | Centrality | Score |
|---|---|---|---|---|
| `<id>` | YYYY-MM-DD | N | 0.0N | S |
```

**Upgrade path (s10, grf-01):** once `graphify` builds INFERRED
(embedding-neighbour) edges, they feed into the SAME `revisit_sample`
function for a richer centrality signal — the ranking formula itself does
not need to change; only the graph it's centrality is computed over grows.

## §F — orphans, contradictions, callouts (still vault-overlay, no brain equivalent)

Zero-backlink notes, contradiction pairs, and stale callout blocks are
vault-structure lint with no `brain`-retrieval verb behind them (RETIRE per
G4) — these stay this skill's own job, on-invoke, never a scheduled fold.
`audit-orphans` (zero-backlink notes) is the companion check in
`.claude/skills/kb-curator/SKILL.md` — don't duplicate it here.

## Backpressure

If a deployment tracks an open-findings queue and it's already over
capacity, suppress new §C/§E/§F *emissions* (don't surface more
action-required rows) but **still run every check and still write the
revisit list** — the list itself is useful triage input regardless of
whether new findings are being surfaced this run.

## Hard guardrails

- **No content edits.** §C never rewrites a note body — even an "unambiguous
  rename" auto-fix only repoints the wikilink syntax, never touches prose.
- **No deletes.** A note past its staleness threshold is surfaced for
  review, never moved to `archive/` automatically.
- **§E's centrality claim must stay accurate.** It IS whole-corpus PageRank
  now (not an age-only fallback) — say so plainly, and note the s10 upgrade
  path (INFERRED edges) as a future ENHANCEMENT, not a missing baseline.

## Cross-references

- `routines/manifest.json` `curation` row — FOLD (Sunday brain-nightly branch, §C/§E) + this skill (on-invoke, §F and deep-dives)
- `docs/adr/0003-parity-architecture.md` Ruling 5 + the 2026-07-05/s08 amendment — the Sunday fold + the G3-closure rationale
- `AGENTS.md` §5 — `graph-expand` is DISCOVERY-ONLY; never treat its output as authoritative
- `.claude/skills/kb-curator/SKILL.md` — `audit-orphans` (zero-backlink notes) is the companion check this skill doesn't duplicate

---
name: kb-curator
description: "Maintain a brain-substrate second-brain (plain Markdown + YAML under vault/brain/ + vault/raw/, indexed by the `brain` CLI — see AGENTS.md). Modes: audit (status + curate + integrity + check + health folds), audit-near-dup (corpus-wide near-duplicate scan via `brain integrity`), audit-orphans (zero-backlink notes via tools/validate.py --backlinks), lint-stale (frontmatter/classification conformance via `brain bases-query` + tools/validate.py), refresh-index (`brain sync` / `brain rebuild`), propose-cleanup, rotate-logs (N/A under brain — see notes), audit-writes (verify-audit), promote-lesson. Triggers: audit kb, kb health check, refresh the index, propose cleanup, rotate logs, lint orphan notes, lint stale notes, find near-duplicate notes, find orphan notes, check the audit chain, promote a lesson, or any phrase about checking, pruning, or maintaining the brain knowledge base. Not for Excel/Word/PDF audits, contract review, or filesystem cleanup unrelated to the brain substrate."
---

# kb-curator (brain-substrate kernel)

**This is the generic, brain-backed kernel version of kb-curator** — the
maintenance brain for a `brain`-substrate second-brain (`AGENTS.md` /
`docs/substrate-spec.md`). It works standalone against any vault that follows
the brain conventions (`vault/raw/`, `vault/brain/{projects,areas,resources,
archive}`, frontmatter `classification`). It carries **no project-specific
content** — a deployment that needs extra checks layers them in an overlay
SKILL.md at the same path, per the override pattern below.

This kernel **supersedes** the Smart-Connections/Obsidian-Bases version of
kb-curator. Every check that used to read `.smart-env/*.ajson` vectors or
`90 System/Bases/*.base` files now goes through the `brain` CLI instead — see
`docs/cutover/repoint-map.md` §1 in this repo for the full per-row mapping
this skill implements.

## Phase 0 — locate the vault and confirm it's brain-shaped

```bash
export BRAIN_VAULT="${BRAIN_VAULT:-./vault}"
ls "$BRAIN_VAULT/brain" "$BRAIN_VAULT/raw" >/dev/null 2>&1 \
  || { echo "Not a brain-substrate vault: $BRAIN_VAULT (expected brain/ and raw/)"; exit 1; }
```

If the fingerprint doesn't match, this skill refuses — it does not guess at a
different substrate. (A project running FLAT or Obsidian/Smart-Connections
keeps the legacy kb-curator base for now; this kernel is brain-only.)

## When to invoke

**Manual triggers:** "audit the kb", "kb health check", "weekly maintenance",
"refresh the index", "propose cleanup", "lint orphans", "lint stale", "find
near-duplicates", "find contradictions", "check the audit chain".

**Auto-suggest at session start when:**
- `brain status --json` shows `pending_drafts > 0` for more than a day (a
  capture is sitting undrained).
- The snapshot generation is stale relative to the index (VM clients are
  reading old data).
- A prior `audit-near-dup` or `audit-orphans` run reported findings above a
  configurable threshold (defaults: near-dup `score ≥ 0.95` any pair; orphans
  `≥ 30` zero-backlink notes).

When a threshold trips, propose the corresponding mode — never auto-execute.

## Pick a mode

| Mode | What it does | brain command(s) |
|---|---|---|
| `audit` | Cheap, frequent health check — index/snapshot/draft state + audit-chain integrity + a quick retrieval self-test | `brain health --json` (folds `status` + `verify-audit` + a probe search) |
| `audit-near-dup` | Corpus-wide cosine near-duplicate scan over the real vector backend — **REPOINT of the old `.smart-env`-cosine contradiction lint (G1, already shipped)** | `brain integrity --json` |
| `audit-orphans` | Notes with zero inbound wikilinks — the brain-substrate equivalent of "no Base type-binding and no link-in" | `python3 tools/validate.py "$BRAIN_VAULT" --backlinks`, then read `vault/brain/backlinks.md` and diff against every note id; any id absent from the backlinks targets is an orphan |
| `lint-stale` | Frontmatter/classification conformance — every note has the required keys (`id, title, type, classification, created, updated` for `brain/`; `id, type, classification, captured, origin, immutable` for `raw/`) | `brain bases-query --where type=note --json` (enumerate, then validate locally) cross-checked with `python3 tools/validate.py "$BRAIN_VAULT"` |
| `refresh-index` | After a batch of writes — incremental reindex | `brain sync` (fast path) or `brain rebuild` (full rebuild, always safe) |
| `propose-cleanup` | Surface candidate moves/merges without executing them | composes `audit-near-dup` + `audit-orphans` + `lint-stale` into one proposal; never writes |
| `rotate-logs` | **N/A under brain by default** — the audit chain is itself an append-only, anchor-checkpointed log (`brain anchor` / `brain verify-anchor`), not a flat file that grows unbounded. If a deployment maintains its own operational log file outside the brain index, rotate that file with ordinary log-rotation tooling; this mode is a no-op here and says so explicitly. | `brain verify-anchor` (confirms the chain hasn't been silently rewritten — the closest brain-side analogue) |
| `audit-writes` | Reconcile what the audit chain says was written against what's actually on disk | `brain verify-audit` |
| `promote-lesson` | Promote a twice-applied lesson from session reflections into a durable note | writes a `brain/resources/` note via `brain capture` (host) or `brain draft-capture` (VM); no retrieval call |

If ambiguous, default to `audit` — it's read-only and surfaces what other
modes are needed. Run one mode per invocation.

## `audit` — the default fold

```bash
brain --vault "$BRAIN_VAULT" health --json
```

`health` already folds `status` (index/snapshot/draft counts) + an
audit-chain verify + a retrieval self-test (`selftest.probe_ok`) into one
call (`src/brain/maintenance.py`, shipped C-s03). Read the `outcomes` block —
it is shape-stable across every maintenance verb in this CLI:

```json
"outcomes": {
  "auto_fixed": [],
  "action_required": [],
  "blocked": [{"finding": "...", "blocking_on": "...", "retry_when": "..."}],
  "counts": {"auto_fixed": 0, "action_required": 0, "blocked": 0}
}
```

Report `auto_fixed` and `action_required` verbatim; a non-empty `blocked`
list (e.g. "no audit signing key resolved") is not a kb-curator failure — it
is a host-environment gap (see `docs/cutover/client-access-model.md` for key
custody). Surface it as-is, do not silently retry.

## `audit-near-dup` — the Bases-existence check's REPOINT, not RETIRE

The old `.base`-file-existence checks (kb-curator's `audit_obsidian.py` check
1) **RETIRE outright** — there are no `.base` files under the brain
substrate, and there is nothing to replace them with (`docs/cutover/
brain-cli-gaps.md` G4). Do not invent a stand-in check for a file format that
no longer exists.

What does carry forward is contradiction/near-dup detection, because that was
never really about `.base` files — it was about finding notes that say
almost the same thing. That REPOINTs cleanly onto the vector backend the
brain already indexes with:

```bash
brain --vault "$BRAIN_VAULT" integrity --json
```

This runs a corpus-wide pairwise cosine scan directly over the brain's own
vectors (no MCP round-trip, no `.ajson` parsing) and returns `near_dup_pairs`
above `--min-score` (default 0.95). Each pair is a **human merge/keep
judgment** — never auto-merge. Report each pair as `action_required` with
both note paths so the next operator can inspect and decide.

## `audit-orphans`

```bash
python3 tools/validate.py "$BRAIN_VAULT" --backlinks
```

This regenerates `vault/brain/backlinks.md` — the reverse-link map. A note
whose id never appears as a target in `backlinks.md` (and is not `index.md`
or `backlinks.md` itself) is an orphan: nothing links to it, so graph-expand/
PPR multi-hop traversal can never reach it from another note. Report the list
as `action_required` — link it from somewhere relevant, fold it into an
existing note, or move it to `archive/`.

## `lint-stale`

Two passes, both read-only:

1. **Frontmatter conformance** — `python3 tools/validate.py "$BRAIN_VAULT"`
   exits non-zero on any missing required key or an unrecognised
   `classification`. Per AGENTS.md §5, a note with a missing/unrecognised
   `classification` is already treated as MNPI and withheld at the egress
   gate — this check surfaces *why*, so it gets fixed rather than silently
   staying invisible.
2. **Source freshness** — `brain bases-query --where type=source --json`
   enumerates `raw/` sources; if a deployment tracks an external manifest of
   upstream document hashes, diff against it here. The brain CLI itself has
   no "stale citation" concept (that lived in an earlier deployment's own
   ingestion manifest, before this kernel was generalised) — this pass is a
   placeholder hook for a deployment that wants it, not a built-in brain verb.

## `propose-cleanup`

Run `audit-near-dup`, `audit-orphans`, and `lint-stale` in sequence and
combine their findings into a single proposal. **No edits, no merges, no
deletes** — this mode only writes a report. The human decides what to do
with each finding.

## Hard guardrails (every mode)

- **No content edits.** Never summarise, condense, or rewrite a note's body.
- **No deletes, ever.** A finding proposes a move/merge; execution is a
  separate, explicitly-approved step.
- **Near-dup pairs are never auto-merged** — `brain integrity` itself refuses
  to resolve them; this skill does the same.
- **`brain write` / `brain capture` (the host-broker commit path) is only
  invoked for `promote-lesson`**, and only after the lesson has cleared the
  same two-strike bar the legacy skill used (bitten/applied twice before
  graduating from a session reflection into a durable note).
- **Classification is never inferred or auto-assigned.** A note that fails
  `lint-stale`'s frontmatter check gets reported, not silently patched with a
  guessed `classification:`.

## Why the Bases checks retire instead of repointing

The legacy OBSIDIAN audit chain spent four scripts partly on structural
introspection — "do the 11 canonical `.base` files exist, and is their
schema intact?" Under brain there is no separate structured-view file format
to go stale: `bases-query` reads the SAME index `brain status` already
reports on. So the genuinely new failure mode (a missing/corrupt structured
view) cannot occur independently of the index itself — `brain health`
already covers it. Inventing a parallel check would be insurance against a
risk the architecture removed, not a real gap.

## Override pattern

A deployment that needs checks beyond this generic kernel (a project-specific
lint, a tuned near-dup threshold, an extra frontmatter key) layers them in by
extending the `--min-score` / `--where` arguments shown above or by adding a
thin project-local SKILL.md that calls this one for everything it doesn't
change. Keep this file substrate-generic; project-specific content belongs in
the overlay, not here.

## Cross-references

- `docs/cutover/repoint-map.md` §1 — the full per-dependency disposition table this skill implements
- `docs/cutover/brain-cli-gaps.md` — G1 (near-dup, shipped), G4 (Bases introspection, retired)
- `docs/cutover/brain-cli-verbs.md` — canonical verb reference
- `AGENTS.md` §5 — classification egress gate; §8 — `tools/validate.py`
- `src/brain/maintenance.py` — `check` / `health` / `curate` / `integrity` / `promote-scan` / `maintain` implementations

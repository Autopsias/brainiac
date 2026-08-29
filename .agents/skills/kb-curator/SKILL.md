---
name: kb-curator
description: "Maintain a brain-substrate second-brain (plain Markdown + YAML under vault/brain/ + vault/raw/, indexed by the `brain` CLI — see AGENTS.md). Modes: audit (status + curate + integrity + check + health folds), audit-near-dup (corpus-wide near-duplicate scan via `brain integrity`), audit-orphans (zero-backlink notes via tools/validate.py --backlinks), lint-stale (frontmatter/classification conformance via `brain bases-query` + tools/validate.py), refresh-index (`brain sync` / `brain rebuild`), propose-cleanup, rotate-logs (N/A under brain — see notes), audit-writes (verify-audit), promote-lesson. Triggers: audit kb, kb health check, refresh the index, propose cleanup, rotate logs, lint orphan notes, lint stale notes, find near-duplicate notes, find orphan notes, check the audit chain, promote a lesson, or any phrase about checking, pruning, or maintaining the brain knowledge base. Not for Excel/Word/PDF audits, contract review, or filesystem cleanup unrelated to the brain substrate."
---

# kb-curator (brain-substrate kernel)

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

**This is the generic, brain-backed kernel version of kb-curator** — the
maintenance brain for a `brain`-substrate second-brain (`AGENTS.md` /
`docs/substrate-spec.md`). It works standalone against any vault that follows
the brain conventions (`vault/raw/`, `vault/brain/{projects,areas,resources,
archive}`, frontmatter `classification`). It carries **no project-specific
content** — a deployment that needs extra checks layers them in an overlay
SKILL.md at the same path, per the override pattern below.

This kernel **supersedes** the Smart-Connections/Obsidian-Bases version of
kb-curator. Every check that used to read `.smart-env/*.ajson` vectors or the legacy
Obsidian Bases (`*.base`) files now goes through the `brain` CLI instead — see
AGENTS.md §5/§6 in this repo for the retrieval-tool + trust-split mapping
this skill implements.

## Phase 0 — locate the vault and confirm it's brain-shaped

**HOST LANE.** The fingerprint below lists two vault directories, so it only
runs where the vault is on disk:

```bash
export BRAIN_VAULT="${BRAIN_VAULT:-./vault}"
test -d "$BRAIN_VAULT/brain" && test -d "$BRAIN_VAULT/raw" \
  || { echo "Not a brain-substrate vault: $BRAIN_VAULT (expected brain/ and raw/)"; exit 1; }
```

**Cowork lane:** never run that. Confirm the substrate through the desk instead
— one `recent` call returning notes is the fingerprint, and a desk that answers
at all is by definition pointed at a brain-substrate vault. If `recent` errors, report the
desk as unreachable; do not go looking for the directories.

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
| `audit-orphans` | Notes with zero inbound wikilinks — the brain-substrate equivalent of "no Base type-binding and no link-in" | HOST ONLY — `python3 tools/validate.py "$BRAIN_VAULT" --backlinks` regenerates `vault/brain/backlinks.md`, then diff its link targets against every note id; any id absent is an orphan. No desk equivalent (see `audit-orphans` below) |
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
is a host-environment gap (see `AGENTS.md` §6 for key custody). Surface it as-is, do not silently retry.

## `audit-near-dup` — the Bases-existence check's REPOINT, not RETIRE

The old `.base`-file-existence checks (kb-curator's `audit_obsidian.py` check
1) **RETIRE outright** — there are no `.base` files under the brain
substrate, and there is nothing to replace them with (Bases introspection
was retired, never shipped as a CLI verb — G4). Do not invent a stand-in
check for a file format that no longer exists.

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

**HOST LANE ONLY, and there is no Cowork substitute.** `tools/validate.py` is a
repo tool and `backlinks.md` is a vault file; a Cowork session has neither, and
the desk serves notes one at a time rather than a whole-corpus backlink map. In
a Cowork session, report that this mode needs the host and stop — do not
approximate it by pulling notes one by one, and do not go looking for the file.

```bash
python3 tools/validate.py "$BRAIN_VAULT" --backlinks
```

This regenerates `vault/brain/backlinks.md` — the reverse-link map. A note
whose id never appears as a target in `backlinks.md` (and is not `index.md`
or `backlinks.md` itself) is an orphan: nothing links to it, so graph-expand/
PPR multi-hop traversal can never reach it from another note. Report the list
as `action_required` — link it from somewhere relevant, fold it into an
existing note, or move it to `archive/`. If `.brain/curation/link-candidates-*.json`
(if present) exists, read it first — it's a pre-computed, noise-filtered list of
cosine-similar unlinked knowledge-layer pairs (ranked, with a rationale per
pair) that turns "link it from somewhere relevant" into a ranked shortlist
instead of a cold search.

## `lint-stale`

Two passes, both read-only:

1. **Frontmatter conformance (HOST ONLY)** — `python3 tools/validate.py
   "$BRAIN_VAULT"` exits non-zero on any missing required key or an unrecognised
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
- **Every note this skill writes carries `provenance.produced_by: kb-curator`,
  and `promote-lesson` asks the deliverable question once** (DLV-01,
  AGENTS.md §4): if the note anchors a *finished output produced for an
  audience* rather than a working note, add `deliverable: true` and
  `project: <project-id>`. Never change `type:` to mark one — `type` is
  single-valued and load-bearing (`type: decision` IS the decision layer), so a
  produced decision keeps its type and gains the marker. The stamp is
  automatic, the marker is the judgment; the gap between them is what makes
  adoption measurable. `lint-stale` REPORTS an unmarked produced note like any
  other finding — it never stamps one.

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

- G1 (near-dup, shipped), G4 (Bases introspection, retired) — no standalone gaps doc, tracked here
- `brain --help` — canonical verb reference
- `AGENTS.md` §5 — classification egress gate; §8 — `tools/validate.py`
- `src/brain/maintenance.py` — `check` / `health` / `curate` / `integrity` / `promote-scan` / `maintain` implementations

## Retrieval contract

> **Multilingual vaults — the variant contract (AGENTS.md §5 rule 3).** Before
> the first vault search, read the derived census: `brain status --json` ->
> `index.languages`. When `multilingual` is true, issue every search as the
> question PLUS one `--variant "<the same question in that language>"` for each
> other entry in `vault_languages` — you write the translation, the engine
> fuses the result lists into one ranking. `multilingual: false`, one language,
> or no census: a single query is correct — do not invent a variant.

---
name: vault-ingestion
description: "Capture a new source into the brain substrate's vault/raw/. Routes by shape: a BINARY or semi-structured file (PDF/DOCX/PPTX/XLSX/image/.eml/.html/.zip) goes through the kernel's own `brain ingest` drop-zone pipeline (ADR-0003, ING-01/03) — extraction, quarantine, and bounded zip/eml recursion all happen in-repo now, no overlay extractor needed. A TRANSCRIPT (already Markdown, produced by an external transcriber) goes through `brain ingest-transcript` (ING-04) for origin/language provenance stamping. A pasted document / forwarded note / URL clip with no file at all still uses the manual `brain write` recipe below (dedup-checked against the live index via `brain search`, or staged via `brain draft-capture` on a no-write VM leg), then `brain sync` reindexes in every case. Every route ends the same way: the session that captures a source also writes the `brain/` note that cites it (Phase 4), because a source nobody cites is reachable only by its own text. Triggers: 'ingest this', 'capture this source', 'add this to the brain', 'drop this in raw', 'this should be a source note', 'ingest this transcript'."
---

# vault-ingestion (brain-substrate kernel)

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

Three capture paths, chosen by what you're handed, and **one shared ending**.
`brain sync` reindexes after any of them — a written/promoted source is not
retrievable until sync runs. Phase 4 then links it, and an ingest is not
finished until that is done.

| You have | Route |
|---|---|
| A binary or semi-structured FILE (PDF, DOCX, PPTX, XLSX, image, `.eml`, `.html`, `.zip`) | Drop it in `<vault>/inbox/` and run `brain ingest` (or just `brain sync` — the drop-zone drains on every host sync). See "Path A" below. |
| A TRANSCRIPT `.md` file from the transcriber (audio/video already transcribed) | `brain ingest-transcript <path> --origin <recording-path\|verbal>`. See "Path B" below. |
| Pasted text / a forwarded note / a URL clip with no file | The manual dedup + `brain write` recipe, Phase 1-3 below (unchanged). |

## Path A — binary/semi-structured files: `brain ingest`

```bash
cp /path/to/the/file "$BRAIN_VAULT/inbox/"
brain --vault "$BRAIN_VAULT" ingest --json     # or: brain sync (drains inbox/ first)
```

The kernel's own `src/brain/ingest/` pipeline (ADR-0003 Ruling 1, ING-01
through ING-03) handles extraction, an extraction-quality gate, immutable
original archival, create-exclusive no-clobber writes, and audited
`write_note` commits — **no overlay extractor needed**, this is no longer
"out of scope for the generic kernel." Handlers: PDF, DOCX, PPTX, XLSX,
plain text/Markdown/CSV, images (metadata + local-only OCR when a tesseract
binary is present — never cloud), `.eml` (headers + body + attachment
manifest — attachments re-enter the pipeline as their own sources), `.html`
(readable-text conversion), and `.zip` (bounded, Zip-Slip-hardened member
expansion — members re-enter the pipeline too). An unhandled extension
quarantines to `inbox/_quarantine/<reason>/` with a report entry — it is
never silently dropped. Check `brain ingest --dry-run --json` first if you
want a preview with zero writes.

## Path B — transcripts: `brain ingest-transcript`

Meeting transcripts are already Markdown — there's no extraction step, only
provenance. `origin` is the one fact the generic pipeline above cannot
supply on its own (it can only point at an archived COPY of whatever file
was dropped, never at the real-world recording a transcript came from):

```bash
brain --vault "$BRAIN_VAULT" ingest-transcript /path/to/transcript.md \
  --origin "/path/to/the/original/recording.m4a"   # or: --origin verbal
```

`--language` is optional (auto-detected from the filename, e.g.
`standup_2026-07-05_en.md` -> `en`; omit if the filename carries no
recognised code — never guessed from the transcript's prose).
`--document-date YYYY-MM-DD` is optional too, for the meeting/recording date
if it differs from the ingest date. Dedup is by content sha256, sharing the
SAME manifest as Path A — re-ingesting a byte-identical transcript is a
no-op, not a duplicate note. See `docs/ingestion.md` for the full contract.

## Path C — no file at all: manual capture

For a pasted document, a forwarded note, or a URL clip that never existed as
a file, use the recipe below directly.

## Phase 1 — dedup check (REPOINT of the old SC-cosine soft-warning)

The legacy pipeline read `.smart-env/*.ajson` to compute a cosine similarity
between the new content and existing notes and soft-warn below an
auto-block threshold. That capability is **gap G1** — note-to-note similarity
isn't a standalone verb yet, but the same effect is achievable today via a
query-against-corpus search using the new content's own gist as the query:

```bash
brain --vault "$BRAIN_VAULT" search "<first ~150 chars of the new source>" --rerank --json
```

> **Multilingual vaults — the variant contract (AGENTS.md §5 rule 3).** Before
> the first vault search, read the derived census: `brain status --json` ->
> `index.languages`. When `multilingual` is true, issue every search as the
> question PLUS one `--variant "<the same question in that language>"` for each
> other entry in `vault_languages` — you write the translation, the engine
> fuses the result lists into one ranking. `multilingual: false`, one language,
> or no census: a single query is correct — do not invent a variant.

Read the top result's score. There is no calibrated "auto-block" threshold
shipped yet (the legacy threshold was tuned for a different embedder and
scoring scale) — treat any high-scoring hit as a **soft warning**, never an
auto-block: surface "similar to `[[existing-id]]` (score N.NN)" and ask
whether to ingest anyway, merge, or skip. This is honestly a degraded
substitute for true note-to-note similarity (G1's `brain similar` /
`brain near-dup` proposal) — once those land, swap this query-based proxy
for the real verb.

## Phase 2 — write the source (immutable, host-broker)

```bash
brain --vault "$BRAIN_VAULT" write "raw/<id>.md" \
  --content "$(cat <<'EOF'
---
id: <id>
type: source
classification: Internal
captured: YYYY-MM-DD
origin: "<url | path | person | verbal>"
sha256: "<hex of body>"
immutable: true
---

<extracted body>
EOF
)" --reason "ingest: <one-line description of the source>"
```

Compute `sha256` of the body before writing — it's the integrity anchor
AGENTS.md §4 requires and the cheapest exact-duplicate guard available (a
second source with an identical body hash is a hard duplicate, not a soft
one — skip the write and tell the user the source is already captured under
`<existing-id>`).

In a Cowork session, call the desk's **`capture`** tool instead, with
`type: "source"` — that is the desk's spelling of `--source`, and it is what
routes the content to `raw/<id>.md` rather than `brain/resources/`
(`core.capture`'s host branch). Pass `content` = the same full Markdown and
`id` = the source id. The broker runs capture in the HOST process, so this is
signed and indexed immediately, not a candidate awaiting a drain.

## Phase 3 — reindex

```bash
brain --vault "$BRAIN_VAULT" sync
```

`write` signs and commits the Markdown file but does **not** touch the
search index — the source is not retrievable until `sync` runs, so treat
this step as **mandatory after every `write`**, not optional. `sync` does
incremental upsert-by-content-hash plus delete-propagation and drains any
pending `capture-inbox/` drafts first (the VM's unsigned captures become
durable here too) — it is also the right call after a batch of writes.
`--publish` republishes the read-only snapshot under `<vault>/.brain/snapshot/`.
That snapshot exists for workspaces installed BEFORE the Closed Stacks cutover;
it is the on-mount copy the cutover removes. A Cowork session reading through
the desk does not need it — the desk queries the host's live index — so do not
add `--publish` on the VM's behalf. Add it only when the owner still runs a
pre-cutover workspace and has asked for it.

## Phase 4 — link the source (MANDATORY, all three paths)

A `raw/` source that no `brain/` note cites is reachable only by its own text.
It has no place in the graph, no backlink, and nothing states why it matters.
The engine counts these as `unlinked_sources` and ratchets on them, so leaving
one behind degrades the vault's health until somebody else clears it.

**The session that captures a source writes the note that cites it.** Not the
weekly synthesis session, not the owner, not "later" — this session, before it
reports the ingest as done.

```bash
brain --vault "$BRAIN_VAULT" write "brain/resources/<note-id>.md" \
  --content "..." --reason "link: derived note for <source-id>"
brain --vault "$BRAIN_VAULT" sync --publish
```

Four rules the note has to satisfy (AGENTS.md §4 rule 4, the BAK-04 lane):

1. **Cite as `[[<bare-id>]]` in the BODY.** The `[[raw/<id>]]` form belongs in
   `source:` frontmatter and creates **no graph edge** — used in a body it
   inflates the metric while linking nothing.
2. **Say something true the source's own title does not.** A title-restating
   stub is worse than no note. If you cannot say anything yet, you have not
   read the source.
3. **Group freely.** One note may cite several sources that genuinely share a
   subject, and one source may earn several notes. Match the material, not a
   one-note-per-file rule.
4. **Read the whole source before describing it.** A long document's later
   pages routinely carry schedules, annexes and personal data the first page
   does not hint at — describe what is in the file, not what the first page
   suggests.

In a Cowork session, call the desk's **`capture`** tool instead — `content` =
the same full Markdown, `id` = the note id, and no `type` (the default lands it
under `brain/resources/`). It runs host-side, so it comes back signed and
indexed rather than pending.

**Verify before reporting done** — the citation, not your intent. A lexical
`grep` is the check: it needs no embedding, and both lanes have one.

**HOST LANE.** The CLI form, which pipes to `jq`:

```bash
brain --vault "$BRAIN_VAULT" grep --regex "\[\[<source-id>\]\]" --json \
  | jq '[.results[] | select(.zone=="brain")
         | select(.id | test("^catalog-|^backlinks")|not) | .id]'
```

**COWORK LANE.** Call the desk's `grep` tool with
`pattern: "\\[\\[<source-id>\\]\\]"` and `regex: true` — the brackets MUST be
escaped, exactly as the host form above escapes them. Unescaped, `[[<id>]]` is
not a literal: a dated id fails to compile and only works through the engine's
silent fallback to an escaped literal, and an id whose letters happen to form a
valid character range (measured: `[[brain-swap]]`) compiles as a character class
and matches unrelated wikilinks, so the citation check below can pass on a note
that cites a DIFFERENT source. Then apply the same two
filters to its `results` yourself: keep `zone == "brain"`, drop ids matching
`^catalog-|^backlinks`. Do NOT run the CLI form here and do NOT read the note
file — the desk is the only route, and its `grep` is the same lexical scan over
the same bodies.

A non-empty list means a real note cites the source. An empty list means Phase 4
is not done.

**`--regex` with the brackets is load-bearing — do not simplify it to a plain
`grep "<source-id>"`.** Plain grep tokenizes, so it matches notes that merely
share a token such as the date prefix: a completely invented id returns hits and
the check passes for a source nobody cites. Verified 2026-08-17 — the bare form
reported LINKED for `2026-08-17-a-source-that-does-not-exist`.

Two more properties, both verified on the same date:

- **It confirms the bare-id form for free.** `brain grep` scans note bodies
  only, so `source: "[[raw/<id>]]"` in frontmatter never matches. A hit is a
  real body citation, which is the thing that makes a graph edge.
- **It does not see unsynced content.** Run Phase 3 first.

The filter on `catalog-`/`backlinks` is required: those are generated maps that
wikilink every note in their zone by design, and they would make any source look
linked.

`brain curate` does not report this — it has no `unlinked_sources` field, and
it is host-broker only. The nightly's own count lands in
`<vault>/.brain/curation/unlinked-sources.json` (`total_unlinked`), but that
file is only as fresh as the last `corpus_invariants` fold, so it confirms
yesterday, never your edit.

On the VM the note is a draft until the host signs it. Say so in the handoff:
the count clears on the host's next sync, not when you finish.

## Checklist

- [ ] Route chosen (Path A binary / B transcript / C manual)
- [ ] Phase 1 dedup check run, high-scoring hit surfaced as a soft warning
- [ ] Source captured (`ingest` / `ingest-transcript` / `write`)
- [ ] Quarantine report read — nothing refused in silence
- [ ] Phase 3 `sync` run
- [ ] **Phase 4 note written, citing `[[<bare-id>]]` in the body**
- [ ] Citation verified with the `grep --regex "\[\[<id>\]\]"` check, per source
- [ ] On the VM: draft staged, and the handoff says the count clears on host sync

## What this skill is NOT responsible for

- **Extraction implementation.** PDF/DOCX/PPTX/XLSX/image/email/HTML parsing
  and ZIP expansion now live IN the kernel (`src/brain/ingest/`, Path A
  above) — this skill does not reimplement or duplicate that logic, it only
  tells you which path (A/B/C) to route a given input through.
- **The full promotion WORKFLOW.** Deciding a source deserves a typed entity
  note, choosing its type, and shaping it to the template is the `promote`
  skill's job. **This skill still owns the minimum link** (Phase 4) — one
  `brain/` note citing the source — because that is what keeps the source
  reachable. Reach for `promote` when the source deserves more than that;
  never treat it as a reason to leave Phase 4 undone.
- **Cloud OCR / external egress decisions.** Out of scope for the generic
  kernel; a deployment's egress policy governs this, not this skill.

## Hard constraints

- Never write a `raw/` note without `sha256` and `immutable: true` —
  AGENTS.md §4 makes immutability the whole point of the zone.
- Never edit an existing `raw/` file. A correction is a new source plus a
  `brain/` note that supersedes the old reading, never an in-place rewrite.
- Never skip the dedup check to save a round-trip — the soft-warning is
  cheap (one `search` call) and catches the case that actually costs time
  later (a duplicated source diluting retrieval).
- **Never report an ingest as done with a source nobody cites.** Phase 4 is
  part of the ingest, not follow-up work for someone else.

## Gotchas (from what actually went wrong)

- **2026-08-17 — five contracts ingested, zero notes written.** An OCR ingest
  put five scanned contracts into a vault's `raw/`. Nothing cited them, so
  `unlinked_sources` went 0 → 5 and the vault sat DEGRADED. The cause was in
  this file: it listed promotion under "NOT responsible for" and handed it to
  the `promote` skill, which nothing required anyone to run. Phase 4 exists
  because of that. Fixed by writing the five notes by hand afterwards.
- **2026-08-17 — the first page lied about the document.** One of those
  sources looked like a fixed-asset register for its first 940 lines. It was
  Schedules 1–4, and Schedule 3 was a list of named employees with dates of
  birth and salaries — a personal-data holding that had entered at the
  ingest-default `Internal` with nothing assessing it. The derived note was
  written wrong and had to be corrected. Read to the end before you describe
  a source, and say so when a tier looks wrong for what you found.
- **2026-08-17 — a signed note moved and broke its own audit entry.** A
  drained draft with `type: project` was signed into `brain/resources/`, then
  the PARA fold filed it to `brain/projects/`. `verify-audit` now reports it
  `missing` as unexplained content drift. Do not re-sign or delete a drifted
  note to clear the count (AGENTS.md forbids it) — report it instead.

## Cross-references

- G1 — note-to-note similarity (the gap Phase 1 works around; no standalone gaps doc, tracked here)
- `AGENTS.md` §4 (capture rules), §6 (host/VM write split)
- `docs/ingestion.md` — the full `brain ingest` / `brain ingest-transcript` contract (handlers, caps, provenance fields)
- `docs/adr/0003-parity-architecture.md` — Ruling 1 (drop-zone placement, trust split)
- `.claude/skills/promote/SKILL.md` — turning a captured `raw/` source into a `brain/` note

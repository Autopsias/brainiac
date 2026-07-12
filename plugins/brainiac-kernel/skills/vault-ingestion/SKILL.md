---
name: vault-ingestion
description: "Capture a new source into the brain substrate's vault/raw/. Routes by shape: a BINARY or semi-structured file (PDF/DOCX/PPTX/XLSX/image/.eml/.html/.zip) goes through the kernel's own `brain ingest` drop-zone pipeline (ADR-0003, ING-01/03) — extraction, quarantine, and bounded zip/eml recursion all happen in-repo now, no overlay extractor needed. A TRANSCRIPT (already Markdown, produced by an external transcriber) goes through `brain ingest-transcript` (ING-04) for origin/language provenance stamping. A pasted document / forwarded note / URL clip with no file at all still uses the manual `brain write` recipe below (dedup-checked against the live index via `brain search`, or staged via `brain draft-capture` on a no-write VM leg), then `brain sync` reindexes in every case. Triggers: 'ingest this', 'capture this source', 'add this to the brain', 'drop this in raw', 'this should be a source note', 'ingest this transcript'."
---

# vault-ingestion (brain-substrate kernel)

Three capture paths, chosen by what you're handed. `brain sync` reindexes
after any of them — a written/promoted source is not retrievable until sync
runs.

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
auto-block threshold. That capability is **gap G1** in
note-to-note similarity isn't a
standalone verb yet, but the same effect is achievable today via a
query-against-corpus search using the new content's own gist as the query:

```bash
brain --vault "$BRAIN_VAULT" search "<first ~150 chars of the new source>" --rerank --json
```

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

On the Cowork VM (`--role vm`), `write` is refused — stage instead:

```bash
brain --vault "$BRAIN_VAULT" draft-capture --id <id> --source --content "<same full markdown>"
```

`--source` stages it as a `raw/` candidate (vs a `brain/` note) once the
host drains it.

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
Add `--publish` if the Cowork VM's read-only snapshot needs to see the new
content this run.

## What this skill is NOT responsible for

- **Extraction implementation.** PDF/DOCX/PPTX/XLSX/image/email/HTML parsing
  and ZIP expansion now live IN the kernel (`src/brain/ingest/`, Path A
  above) — this skill does not reimplement or duplicate that logic, it only
  tells you which path (A/B/C) to route a given input through.
- **Promotion to a typed `brain/` note.** A `raw/` source is immutable
  capture, not insight — turning it into an atomic, wikilinked `brain/` note
  is the `promote` skill's job (AGENTS.md §4: "Insight lives in `brain/`.
  When a source matters, write an atomic `brain/` note that links back via
  `source:`").
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

## Cross-references

- G1 — note-to-note similarity (the gap Phase 1 works around; no standalone gaps doc, tracked here)
- `AGENTS.md` §4 (capture rules), §6 (host/VM write split)
- `docs/ingestion.md` — the full `brain ingest` / `brain ingest-transcript` contract (handlers, caps, provenance fields)
- `docs/adr/0003-parity-architecture.md` — Ruling 1 (drop-zone placement, trust split)
- `.claude/skills/promote/SKILL.md` — turning a captured `raw/` source into a `brain/` note

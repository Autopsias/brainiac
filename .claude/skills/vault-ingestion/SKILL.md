---
name: vault-ingestion
description: "Capture a new source (a pasted document, a forwarded note, a URL clip, a call transcript) into the brain substrate's vault/raw/ — dedup-checked against the live index via `brain search`, written via the host-broker `brain write` (or staged via `brain draft-capture` on a no-write VM leg), then reindexed via `brain sync`. Triggers: 'ingest this', 'capture this source', 'add this to the brain', 'drop this in raw', 'this should be a source note'. Binary extraction (PDF/DOCX/PPTX/XLSX parsing, OCR, ZIP expansion) is explicitly OUT of scope for this generic kernel — that is deployment-specific overlay tooling that produces plain text/Markdown BEFORE this skill's capture step runs; this skill owns only the substrate-side capture, dedup, and reindex."
---

# vault-ingestion (brain-substrate kernel)

This is the generic kernel of the capture pipeline. It deliberately does
**not** reimplement binary extraction (PDF/DOCX/PPTX/XLSX/email/HTML
parsing, OCR). Per `docs/cutover/repoint-map.md` §3, that extraction layer
is vault-side overlay tooling — every concrete deployment needs its own
extractor stack, and the brain repo's job is the substrate-side half: take
already-extracted plain text, dedup it against the live index, write it into
`vault/raw/` (immutably), and reindex.

If your deployment has a binary-extraction pipeline, run it first; this
skill picks up from "I have a clean Markdown/plain-text body and a source
description" onward.

## Phase 1 — dedup check (REPOINT of the old SC-cosine soft-warning)

The legacy pipeline read `.smart-env/*.ajson` to compute a cosine similarity
between the new content and existing notes and soft-warn below an
auto-block threshold. That capability is **gap G1** in
`docs/cutover/brain-cli-gaps.md` — note-to-note similarity isn't a
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

- **Extraction.** PDF/DOCX/PPTX/XLSX parsing, OCR, email/HTML parsing, ZIP
  expansion — all deployment-specific overlay tooling that runs *before*
  this skill and hands it clean text. Do not write extraction logic here.
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

- `docs/cutover/repoint-map.md` §3 — the dependency table this skill implements
- `docs/cutover/brain-cli-gaps.md` G1 — note-to-note similarity (the gap Phase 1 works around)
- `AGENTS.md` §4 (capture rules), §6 (host/VM write split)
- `.claude/skills/promote/SKILL.md` — turning a captured `raw/` source into a `brain/` note

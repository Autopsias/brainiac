# Ingestion — `brain ingest` and `brain ingest-transcript`

Design of record: `docs/adr/0003-parity-architecture.md` Ruling 1 (placement,
trust split, cadence). This doc is the user-facing contract: what goes in,
what comes out, and the bounds each handler enforces. Both verbs are
**host-broker only** — refused on the Cowork VM leg before any filesystem
side effect (AGENTS.md §6).

## `brain ingest` — the drop-zone pipeline (ING-01/02/03)

Drop a file into `<vault>/inbox/` and run `brain ingest` (or just
`brain sync`, which drains the drop zone on every host run). Each file is
claimed, dispatched by extension to a handler, and — on success — the
extracted Markdown is committed through the audited `write_note` path while
the untouched original is archived immutably under
`raw/originals/<date>-<slug>/`. A handler that can't make sense of a file
returns a quarantine reason instead of raising; nothing is ever silently
dropped — check `inbox/_quarantine/<reason>/` and the `quarantined` list in
the JSON report.

### Handlers

| Extension(s) | Handler | Notes |
|---|---|---|
| `.pdf` | pypdf | text extraction; encrypted/no-text-layer PDFs quarantine |
| `.docx` | python-docx | paragraphs + Markdown tables |
| `.pptx` | python-pptx | slide text |
| `.xlsx` | openpyxl | one Markdown table per sheet; cached formula values preferred |
| `.txt` `.md` `.markdown` `.csv` | stdlib | pass-through |
| `.png` `.jpg` `.jpeg` `.webp` `.gif` `.bmp` `.tiff` | Pillow (+ optional pytesseract) | metadata always; OCR text ONLY when a local `pytesseract` + tesseract binary are both present — **never cloud OCR**, no cloud code path exists in this kernel at all. Missing OCR degrades to a metadata-only note, never a quarantine. |
| `.eml` | stdlib `email` | headers + body (`text/plain`, falling back to a stripped `text/html`) + an attachment manifest. Each attachment **re-enters this same dispatcher** as its own ingest candidate (bounded — see Recursion below). |
| `.html` `.htm` | stdlib `html.parser` (+ optional `lxml` fast path) | readable-text conversion; `<title>` becomes the note's `# ` heading when present |
| `.zip` | stdlib `zipfile` | bounded, Zip-Slip-hardened member expansion (below); each member **re-enters this same dispatcher** |

Any other extension quarantines with reason `no_handler_for_extension`.
`brain ingest --dry-run --json` (or the drop-zone `capability_report()`)
shows which handlers are currently available given installed dependencies.

### Extraction-quality gate

Every handler routes its Markdown through the same density gate
(`handlers.base.density_gate`, ≥40 non-heading, non-fence characters by
default) before it is ever signed — a scanned PDF with no text layer, an
empty HTML page, or a docx with one word all quarantine as
`empty_or_low_text_density` rather than polluting the corpus with
near-nothing. A `.zip`'s own "archive contents" listing uses a lower
threshold (a listing's job is enumerating members, not prose density) —
still routed through the same shared gate function, just tuned to that
content's shape.

### ZIP bounds (Zip-Slip-hardened, bomb-guarded)

- **Zip-Slip:** every member's path is checked BEFORE any decompression —
  absolute paths, `..` traversal, a Windows-drive-rooted name, and
  symlink/hardlink/non-regular members all quarantine the **whole archive**
  (`zip_unsafe_member`), never just the offending entry. A member's name
  never becomes a filesystem path directly: only a sanitized basename feeds
  a wholly synthetic, generated temp filename.
- **Bomb guard, before AND during decompression:** each member's *declared*
  uncompressed size (from the central directory) is checked against a
  per-member cap and a running total-declared-size cap BEFORE any member is
  opened; each member is then decompressed in bounded chunks, counting REAL
  output bytes as they arrive, so a member whose actual output would exceed
  the cap is aborted mid-stream rather than ever being fully materialized.
- **Caps:** ≤500 members, ≤200 MB per member, ≤500 MB declared total per
  archive (`src/brain/ingest/handlers/zip.py`).

### Recursion (zip members / eml attachments re-entering the dispatcher)

A zip member or an eml attachment is handed back to the SAME dispatcher as
its own ingest candidate — a PDF attachment gets its own `raw/` source, an
unrecognised one quarantines on its own without aborting its siblings or the
container's already-completed promotion. Bounded on two axes:

- **Depth** (`MAX_NESTED_DEPTH = 3`): a zip-in-zip-in-zip (or an eml whose
  attachment is itself a zip) stops re-entering after 3 levels; items beyond
  that report `nested_depth_exceeded`.
- **A shared budget per top-level candidate** (`≤500 MB` / `≤1000 items`,
  summed across the WHOLE recursion tree, not just one level) — defends
  against a "zip bomb via nesting" pattern that each handler's own
  single-level caps alone wouldn't catch (N archives each individually
  under-cap, nested, would otherwise multiply past any single-level limit).

A duplicate top-level archive (identical bytes to something already
ingested) is never re-expanded — its members were already processed the
first time, by construction of the content-hash dedup manifest.

## `brain ingest-transcript` — transcript provenance (ING-04)

Meeting transcripts are produced externally (the transcriber MCP/CLI — never
in-kernel) and are already Markdown, so there is no extraction step: the
transcript file's own text becomes the note body. The one thing the generic
drop-zone pipeline above cannot express is real-world provenance — its
`origin` always points at an archived COPY of whatever file was dropped,
never at the real-world recording the content came from. This verb fills
exactly that gap:

```bash
brain --vault "$BRAIN_VAULT" ingest-transcript /path/to/transcript.md \
  --origin "/path/to/the/recording.m4a"          # or --origin verbal
```

- **`--origin`** (required): a source audio/video file path, or the literal
  string `verbal` for a no-recording capture. Free text, but every string
  value written to frontmatter — including this one — is control-char
  stripped in the ONE shared frontmatter-builder before it's ever signed, so
  a hostile/careless value can't forge a new YAML key.
- **`--language`** (optional): an ISO 639-1 code. If omitted, detected from
  the filename ONLY when a recognised code (`en`/`pt`/`es`/`fr`/`de`/`it`/`nl`)
  appears as its own segment (e.g. `standup_2026-07-05_en.md` -> `en`) —
  never guessed from the transcript's prose; a wrong guess is worse than an
  absent field.
- **`--document-date`** (optional, `YYYY-MM-DD`): the ADR-0003 Ruling 2
  `document_date` bitemporal key, for when the meeting/recording happened if
  it differs from the ingest date.
- **`--classification`** (optional, default `Internal`).

Same underlying machinery as the drop-zone (create-exclusive original
archival, the shared density gate, the SAME content-hash dedup manifest —
one dedup universe across both ingest surfaces) minus the drop-zone
claim/quarantine dance, since there's no `inbox/` involved: a bad input
returns `{"ok": false, "reason": ...}` rather than raising or leaving
anything half-written.

## Egress

Both verbs' JSON reports carry real note ids + classifications for freshly
promoted content, so they join the content-returning surface: `brain.cli`
routes `processed`/`duplicates` (and `ingest-transcript`'s single-result
equivalent) through the same deny-by-default classification gate as
`curate`/`integrity`, before stdout — never raw (ADR-0003 Ruling 8).

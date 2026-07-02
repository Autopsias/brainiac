# Corpus migration + bulk classification (design)

**Why this is a prerequisite, not an afterthought (claude hardening):** the
substrate is **default-deny on unlabelled** (`classification-scheme.md`). An
un-migrated or unclassified import is **invisible** to retrieval. So importing
the existing Obsidian/Johnny-Decimal vault into the flat/PARA + `classification:`
substrate, *and assigning classification at scale*, is a lifecycle prerequisite
for "beats today." This document is the design **shape**; the execution is
SESSION S03.

## Source → target

| | Source (today) | Target (Profile A) |
|---|---|---|
| Substrate | Obsidian vault, Johnny-Decimal zones (`10 People/` … `90 System/`) | `vault/raw/` + `vault/brain/{projects,areas,resources,archive}/` |
| Organisation | numbered folders + manual tags + Bases | flat, wikilink-first, light PARA |
| Retrieval | Smart Connections (`.smart-env/*.ajson`) | `brain` (sqlite-vec + FTS5 + Arctic-embed) |
| Sensitivity | implicit (git-crypt zones, comms policy) | explicit `classification:` on every note |

## Migration phases

### Phase 0 — inventory & dry-run
- Walk the source vault; enumerate every `.md` with its zone, frontmatter,
  wikilinks, and git-crypt status.
- Produce a **migration manifest** (one row per file: source path → proposed
  target path → proposed PARA bucket → proposed classification → confidence).
- **Dry-run only.** No writes. Human reviews the manifest.

### Phase 1 — raw vs brain split
- **Sources** (extractions, transcripts, ingested PDFs, `50 Sources/`,
  `00 Inbox/_drop` outputs) → `vault/raw/` with `immutable: true` + `sha256`.
- **Synthesised notes** (People, Companies, Concepts, Decisions, MOCs) →
  `vault/brain/` as atomic notes, linking back to their raw source where one
  exists.

### Phase 2 — PARA bucketing (folder mapping, deterministic seed)
Light, reversible mapping from Johnny-Decimal zones to PARA — a *seed*, refined
by review, not a law:

| Source zone | PARA bucket (seed) |
|---|---|
| `30 Projects/` | `projects/` |
| `10 People/`, `20 Companies/`, `40 Meetings/` | `areas/` |
| `50 Sources/`, `60 Concepts/`, `70 Decisions/` | `resources/` |
| `90 System/` | `resources/` (or repo `docs/`, not vault content) |
| superseded / archived blocks | `archive/` |

Johnny-Decimal numbering is **stripped** from filenames (`60.03 CRM.md` →
`crm.md`). Wikilinks are rewritten to the new `id`s.

### Phase 3 — bulk classification (the load-bearing step)
Assign `classification:` to every note **at scale**, fail-closed:

1. **Rule-based first pass** (deterministic, auditable):
   - git-crypt-designated zones (`10 People/`, `20 Companies/`, `70 Decisions/`,
     `30 Projects/Meridian.md`, transcripts, voice) → `Confidential` or higher.
   - Notes mentioning Meridian counterparties / deal terms / financials →
     `Restricted`; flagged-regime MNPI/PII → `MNPI`.
   - `90 System/` operational docs, public concepts → `Internal`.
   - Genuinely public/published material → `Public`.
2. **LLM-assisted second pass** for the residual — propose a tier + reason per
   note; **never auto-apply above `Internal`** without human confirmation.
3. **Default-deny safety net:** anything the passes can't classify stays
   **unlabelled ⇒ MNPI-at-gate** — invisible, not leaked. The migration is not
   "done" until the unlabelled count is driven to zero (or explicitly accepted).
4. **Human review** of every `Restricted`/`MNPI` assignment and a sample of the
   rest.

### Phase 4 — index build & parity check
- Build the `brain` index over the migrated corpus.
- **Parity gate (val-04 input):** run the retrieval eval against the
  Obsidian + SC baseline; Profile A must **beat today** before any operational
  cutover is contemplated.

## What migration does NOT do
Per substrate-spec §7, corpus migration is **not** operational cutover. It loads
the data and proves parity; it does **not** repoint CLAUDE.md, the P-rules, the
Bases, or the scheduled tasks. Those surfaces are catalogued in
`dependency-inventory.md` and handled by a separate follow-on plan.

## Reversibility
The source vault is left **untouched** (read-only during migration). The target
is a new repo. Cutover is a later, deliberate, reversible step — not a side
effect of migration.

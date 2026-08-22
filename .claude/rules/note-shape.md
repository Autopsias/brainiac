---
paths:
  - "vault/brain/**"
  - "vault/raw/**"
  - "templates/**"
---

# Note shape and link style

> Moved verbatim from `AGENTS.md` during the S05 context diet (2026-08-22).
> `AGENTS.md` remains canonical for Codex/Gemini; this rule is the Claude Code
> path-scoped copy, loaded only when a file under its `paths:` glob is touched.

## 2 · Note shape (frontmatter schema)

Every file under `vault/brain/` and `vault/raw/` carries YAML frontmatter.

### `brain/` note

```yaml
---
id: arctic-embed-choice          # stable slug, lowercase-hyphen, unique
title: "Why Arctic-embed over e5"
type: note                       # note | index | moc | source-derived
classification: Internal         # Public|Internal|Confidential|Restricted|MNPI (Material Non-Public Info, most restrictive — see docs/glossary.md)
created: 2026-06-27
updated: 2026-06-27
source: "[[raw/2026-06-27-arctic-benchmark]]"   # provenance link if derived; omit if original
tags: []                         # OPTIONAL, emergent only — NOT a taxonomy
aliases:                          # OPTIONAL brain-zone identity names (not a tag taxonomy)
  - "Hall of Light"
# --- bitemporal (ALL OPTIONAL — ADR-0003 ruling 2; omit entirely on ordinary notes) ---
document_date: 2026-06-27        # when the underlying document was produced
effective_date: 2026-06-27       # when the content takes effect (valid time)
superseded_date: 2026-07-01      # when this note lost its claim to currency
is_latest_version: true          # false ⇒ a successor exists (then superseded_by is required)
superseded_by: "[[e5-small-choice]]"     # the successor note, if any
previous_version: "[[arctic-embed-choice]]"  # the predecessor note, if any
replaces: "[[arctic-embed-choice]]"      # alias of previous_version, capture-time ergonomics
# --- email provenance (ALL OPTIONAL — PRV-01/PRV-02; FLAT DOTTED keys) ---
provenance.trust: untrusted              # capture stamp (drain reads this literally)
provenance.sender: "Alice <alice@example.com>"   # who sent it
provenance.sent: 2026-06-27              # ISO date (or full ISO datetime)
provenance.conversation_id: "<thread-root@example.com>"  # the thread it came from
provenance.subject: "Q3 pricing"         # the subject line
provenance.verified: true                # HOST parsed these from the archived original
---
```

**Bitemporal keys are optional** — a note with none of them validates exactly
as before. When present, `tools/validate.py` type-checks them (ISO dates, real
booleans, resolvable ids) and enforces supersession-chain invariants: no
self-supersession, no cycles, no forks (two successors claiming one
predecessor), at most one `is_latest_version: true` per chain, and an
**explicit `classification` on both sides of every supersession link**. See
`docs/substrate-spec.md` §8.1 for the full validator contract.

**Provenance keys are optional, FLAT, and dotted — never a nested mapping.**
`provenance.trust` is written and read as a literal key across the capture and
drain paths, so its email siblings (`provenance.sender`, `.sent`,
`.conversation_id`, `.subject`, `.verified`) take the same shape. They record
where an ingested source came from, so retrieval and version deduction can
reason from people and dates. **`provenance.verified: true` is a HOST
assertion** — set only when the ingest pipeline parsed the values out of the
archived original itself (e.g. `.eml` headers). Anything a VM supplies
(a `cos-propose` candidate's frontmatter, an ingest-manifest line) is a CLAIM:
it rides along unverified and never carries `verified`. `tools/validate.py`
type-checks `provenance.sent` (ISO date/datetime) and `provenance.verified`
(boolean), and WARNS — never errors — on an unrecognised `provenance.*` subkey.
Email-derived sources default to `classification: MNPI`; only an explicit
`overlay/keywords/` tier mapping lowers that (see §5's retrieval discipline and
`docs/cos-ingest-taxonomy.md` for the category `min_tier` floor that can only
raise it).

**Aliases are optional and brain-zone-only.** An alias is an owner-curated
identity claim, not an automatically derived tag: it is a list of 0–128
non-empty strings (each at most 256 Unicode scalar values), unique after NFC +
casefold + whitespace-collapse normalization within one note. Different notes
may share an alias; the validator warns rather than rejecting historical or
supersession collisions. Automatic alias derivation is a future maintenance
fold, not current behavior. The retrieval index projects aliases into a
normalized identity table beside normalized titles. Alias/title ownership is
computed over the complete pre-egress index, so a hidden owner can never make a
visible collision look uniquely safe.

**Edit vs. supersede** — the identity test: if the claim is the same and you
are only improving how it is stated, **edit** (same `id`, bump `updated:`). If
the world changed and the old claim was true-then but not true-now, and that
history matters, **supersede**: write a new note with `previous_version`
(or `replaces`) pointing at the old one; retire the old note with
`superseded_by`, `superseded_date`, and `is_latest_version: false`. Both notes
remain retrievable — supersession never deletes or edits the retired note's
text.

### `raw/` source (immutable)

```yaml
---
id: 2026-06-27-arctic-benchmark
type: source
classification: Internal
captured: 2026-06-27
origin: "https://example.com/arctic-bench"   # url | path | person | "verbal"
sha256: "<hex of body at capture>"            # integrity anchor
immutable: true
---
```

**Required keys** — `brain`: `id, title, type, classification, created,
updated`. `raw`: `id, type, classification, captured, origin, immutable`.
`classification` is required **everywhere** — a note without it is denied at the
surfacing boundary (see §5).

**Type vocabulary (ADR-0003 ruling 3, TMP-04)** — `type:` in `brain/` accepts
the core four (`note | index | moc | source-derived`, the default — nothing
forces the rest on a vault) plus the **typed entity vocabulary**: `person |
company | project | meeting | decision | concept | daily`. `source` remains
the `raw/`-zone-only type and never joins the brain/ entity vocabulary.
`tools/validate.py` warns (never errors — legacy notes stay valid) on an
unrecognized `type:` for its zone. Each entity type has a ready-made template
at `templates/<type>.md` (kernel, generic/placeholder-only); an owner's
house-style version at `<vault>/overlay/templates/<type>.md` wins when
present (`overlay/README.md`).

**Type-specific lint (ADR-0003 ruling 3, TMP-05, warn-only):** `concept`
notes without a "Counter-Arguments" heading, and `decision` notes with no
source anchor (`source:` key or a wikilink resolving to a `raw/` note), warn
in `tools/validate.py`. Never a hard failure — these are quality nudges, not
gates.

---

## 3 · Link style — flat and link-first

- **Folders carry almost no meaning.** The only directory taxonomy is **light
  PARA** (Projects/Areas/Resources/Archive — `docs/glossary.md`) at the top of
  `brain/` (`projects/ areas/ resources/ archive/`). Within
  a PARA folder, notes are **flat** — no nesting, no numbering.
- **NO Johnny-Decimal.** Filenames are `kebab-slug.md`, never `60.03 Foo.md`.
  The validator flags any `^\d\d[. ]` filename.
- **NO manual tag taxonomy.** `tags:` may exist but is emergent and optional;
  organisation comes from **wikilinks**, not tags or folders.
- **Wikilinks are the primary structure**: `[[note-id]]` or
  `[[note-id|display]]`. Link densely — every note should connect to ≥1 other.
  `index.md` is the human entry map; `backlinks.md` is generated.
- **Zone catalogs (HYG-03, generated).** `tools/validate.py --catalogs`
  regenerates one `catalog.md` per PARA zone (`brain/{projects,areas,
  resources,archive}/catalog.md`) listing every note's id/title/type/
  updated/classification — same "generated, do not hand-edit" posture as
  `backlinks.md`, derived purely from frontmatter so re-running it on an
  unchanged vault is a no-op diff.
- **State-MOC pattern (HYG-03).** A vault/project MAY keep one live
  `type: moc` "state of play" note (template: `templates/state-moc.md`) whose
  body is a set of anchored `## Section: <name>` headings, each carrying its
  own `Updated: YYYY-MM-DD` stamp on the very next line — the freshness of
  *each section* is visible independently of the note's own top-level
  `updated:`. `index.md`'s own zone headings (`## Projects` etc.) use the same
  `Updated:` stamp convention. `tools/validate.py` warns (never errors) on any
  stamped section older than `STATE_MOC_STALE_DAYS` (90 days, reusing the
  ADR-0003 autoresearch-staleness convention) — a quality nudge, not a gate.


# The personalization overlay (PER-01 / PER-02)

The substrate (`vault/brain/`, `vault/raw/`) is generic — it carries **no
hard-coded owner identity**. Brand, voice, recurring-keyword, and people
content is a **data-driven slot** any new owner fills with their own, without
touching the kernel (`src/brain/`) or the kernel skills. That slot is the
**overlay**.

## Where the active overlay lives

The overlay travels with the user's vault, as a sibling of `raw/` and
`brain/`:

```
<vault>/
├── raw/
├── brain/
├── .brain/        ← runtime (gitignored)
└── overlay/        ← THIS layer
```

Resolution order (`brain.overlay.overlay_dir`, same precedence pattern as
`config.vault_root`):

1. `--overlay-dir DIR` (explicit CLI flag)
2. `$BRAIN_OVERLAY_DIR` (env override)
3. `<vault>/overlay` (default — `$BRAIN_VAULT` or `--vault`)

## Shape

```
overlay/
├── voice/      *.md   — durable writing voice (tone, register, sign-offs)
├── brand/      *.md   — naming / anonymisation / title conventions
├── keywords/   *.md   — glossary / acronym / codename decoder ring
└── people/     *.md   — the always-on people this owner's notes reference
```

Each category is a directory of one-or-more Markdown files. Every file
carries a small frontmatter block so a validator can check shape without
guessing from the folder name alone:

```yaml
---
overlay_type: voice        # must equal the containing directory name
title: "..."
updated: 2026-07-01
---

<body — never empty>
```

`overlay_type` is the one required key; everything else in the body is free
text the kernel skills + drafting-facing rituals read as plain prose (no
further schema is imposed — this is content, not config).

## Starting point

- **`template/`** — an empty/placeholder scaffold for a brand-new owner: one
  file per category with the frontmatter filled in and the body holding
  `<!-- fill this in -->`-style prompts. Copy this to `<vault>/overlay/` and
  fill it in to onboard a new owner.

## Validating an overlay

```bash
# Validate the template scaffold (no index needed — filesystem-only check):
PYTHONPATH=src python3 -c "from brain.cli import main; main()" \
    init --validate-overlay --overlay-dir overlay/template --json

# Validate the active overlay for a real vault:
BRAIN_VAULT=/path/to/vault dist/brain/brain init --validate-overlay
```

`brain init --validate-overlay` is the **minimal slice** implemented in this
session (PER-02): it detects and validates the overlay and reports problems
per category. It does NOT yet drive client detection or task registration —
that is the full `brain init` orchestration, extended in a later session
(s09) on top of this validate-only slice.

## HTML brief/digest branding (AUT-01/AUT-03, ADR-0003 Ruling c)

`brain brief --html` / `brain digest --html` (host-only) render a
self-contained, branded HTML file. Branding is resolved from the FIRST
`overlay/brand/*.md` file (sorted) via two OPTIONAL frontmatter keys — no new
overlay category, no schema ceremony:

```yaml
---
overlay_type: brand
title: "Acme Ops Brief"        # existing key — reused as the HTML page title
owner_name: "Jordan Rivers"    # OPTIONAL — shown in the brief/digest subtitle
accent_color: "#2563eb"        # OPTIONAL — must be a #rgb/#rrggbb hex string
updated: 2026-07-05
---
```

Either key absent, or the whole `brand/` category absent, falls back to a
NEUTRAL brief ("Brain Brief" / "Brain Digest" title, no owner subtitle, a
default accent) — the kernel/overlay contract: the generated HTML never
depends on an owner overlay existing. Per ADR-0003 Ruling (e), this is the
one place `owner_name` may appear in a generated FILE artifact — and only in
the local, gitignored `.brain/brief/` output, never in anything
repo-committed.

**Outlook / calendar integration is explicitly OUT of kernel scope.** The
brief/digest sections are generic (captures, notes, revisit sample,
recommendations, index health) — surfacing calendar events is owner-specific
scheduling infrastructure, not substrate. An owner who wants that stitches it
in as an overlay-side extension (e.g. a personal script that appends a
calendar section to the generated HTML, or a separate personal automation
reading `.brain/brief/brief-latest.html`) rather than the kernel growing a
calendar dependency.

## Note templates (TMP-04, ADR-0003 ruling 3)

Kernel note templates for the typed entity vocabulary ship generic and
placeholder-only at `templates/<type>.md` (repo root, one file per type:
`person, company, project, meeting, decision, concept, daily`). An owner who
wants house-style templates (fixed sign-offs, extra required sections, a
house voice) overrides one at a time by dropping a same-named file at
`<vault>/overlay/templates/<type>.md` — it wins over the kernel default
whenever present, same precedence as the four categories above. This is a
content override, not a new overlay category with its own frontmatter
schema — a template file is a note scaffold, not `voice/brand/keywords/people`
prose, so it is not covered by `brain init --validate-overlay`.

## Who reads the overlay

- The seven repointed kernel skills (`kb-curator`, `promote`, `vault-ingestion`,
  `vault-eval`, `curation`, `save-conversation`, `improve`) read `voice/` +
  `brand/` wherever they draft audience-facing prose, and `people/` +
  `keywords/` wherever they need a decoder ring for a name/acronym a query or
  a note references. None of those skills hard-code an owner's identity —
  the overlay is the only place that identity lives.
- **`voice`** (ADR-0003 Ruling 7, HYG-01) reads all four categories directly
  as its entire source of owner signal — DRAFT/REWRITE/CHECK modes degrade to
  a neutral register when a category is empty, never inventing owner content.
- **`autoresearch`** (ADR-0003 Ruling 7, AUT-04) optionally reads
  `keywords/` to draw a vault-relevant probe query when spot-checking a kept
  parameter change; it never reads `voice/`, `brand/`, or `people/`, and never
  commits overlay prose into its `eval/runs/` evidence artifacts.
- `brain` itself never reads the overlay for retrieval (search/get/recent are
  generic over whatever is in `vault/brain/` + `vault/raw/`); only `brain init
  --validate-overlay` touches it directly, as a setup-time shape check.

## Cross-references

- `src/brain/overlay.py` — the validator implementation + `resolve_brand()`
  (AUT-01/AUT-03 HTML brief/digest branding)
- `src/brain/brief.py` — the pure HTML brief/digest renderers
- `src/brain/cli.py` — `brain init --validate-overlay`, `brain brief --html`,
  `brain digest --html`
- `AGENTS.md` §1 — the substrate tree (overlay is documented there too)
- `docs/adr/0003-parity-architecture.md` — Ruling c (HTML egress) / Ruling e
  (overlay-data egress tier)

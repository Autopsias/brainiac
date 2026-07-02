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

## Who reads the overlay

- The seven repointed kernel skills (`kb-curator`, `promote`, `vault-ingestion`,
  `vault-eval`, `curation`, `save-conversation`, `improve`) read `voice/` +
  `brand/` wherever they draft audience-facing prose, and `people/` +
  `keywords/` wherever they need a decoder ring for a name/acronym a query or
  a note references. None of those skills hard-code an owner's identity —
  the overlay is the only place that identity lives.
- `brain` itself never reads the overlay for retrieval (search/get/recent are
  generic over whatever is in `vault/brain/` + `vault/raw/`); only `brain init
  --validate-overlay` touches it directly, as a setup-time shape check.

## Cross-references

- `src/brain/overlay.py` — the validator implementation
- `src/brain/cli.py` — `brain init --validate-overlay`
- `AGENTS.md` §1 — the substrate tree (overlay is documented there too)

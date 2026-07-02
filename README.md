# profile-a-brain

A local, any-LLM **second brain** whose substrate is plain **Markdown + YAML**.
Built to **supersede Obsidian + Smart Connections** as the retrieval substrate
(decision 2026-06-27). This repo holds the substrate spec, conventions, a tiny
sample vault, and a conventions validator. It is **separate from the Acme vault**
by design — the vault is migrated *into* this substrate (see `docs/`), not built
inside.

## Read first
- **`AGENTS.md`** — the conventions/schema the assistant reads at startup (note
  shape, link style, capture rules, the four interactions, security posture).
- **`docs/install/`** — 3-tier onboarding (Claude Code / Codex / Cowork) +
  `docs/install/new-owner.md` for the five-minute "what runs where" mental
  model. **`docs/cutover/runbook.md`** — the operational cutover runbook:
  retiring the old Smart-Connections-wired scheduled tasks safely (dual-run →
  disable, never delete → rollback).

## Layout
```
AGENTS.md            conventions + frontmatter schema
docs/
  substrate-spec.md       full substrate spec (host/VM split, capture protocol)
  classification-scheme.md  5 tiers + default-deny rule
  corpus-migration.md       import the existing vault + bulk classification
  dependency-inventory.md   checklist SHAPE of control-plane surfaces (S10 fills)
  okf-lint-profile.md       OKF = optional lint profile, not the substrate
tools/validate.py    conventions validator (stdlib-only; PyYAML optional)
vault/               the tiny sample vault
  raw/   immutable captured sources
  brain/ agent-owned atomic notes + index.md + generated backlinks.md
```

## Validate
```bash
python3 tools/validate.py vault              # exit 0 = conventions clean
python3 tools/validate.py vault --backlinks  # regenerate brain/backlinks.md
python3 tools/validate.py vault --okf        # + optional OKF lint
```

## Scope note
Substrate readiness ≠ operational cutover. This repo makes Profile A *ready* to
replace Obsidian + SC and emits the cutover hooks (corpus migration +
dependency-inventory shape); the live operating-model swap is a separate
follow-on plan. See `docs/substrate-spec.md` §7.

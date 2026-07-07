---
name: voice
description: "Draft, rewrite, and check text against the owner's writing voice — three modes: DRAFT (compose new text from rough input), REWRITE (transform an existing draft into the owner's voice + an annotated diff), CHECK (lint a draft against the voice, report violations, never rewrite). ALL owner signal comes from vault/overlay/{voice,brand,keywords,people}/ — this skill carries zero hard-coded owner identity and degrades to a neutral professional register (with a pointer to overlay/README.md) when the overlay is empty or missing. Triggers: 'draft an email/memo/message', 'write this up in my voice', 'in my voice', 'stakeholder update', 'briefing for', 'respond to' (DRAFT); 'rewrite this in my voice', 'voicify', 'make this sound like me', 'fix this draft's tone', 'clean this up' (REWRITE); 'check this draft', 'is this in my voice', 'voice check', 'does this sound like me' (CHECK). Do NOT use for vault-internal artefacts (audit reports, tech specs), verbatim quotes, legal text, or machine-readable content — and never bake overlay content into anything repo-committed or publish-bound (ADR-0003 Ruling e)."
---

# voice (brain-substrate kernel)

**One skill, three modes, zero hard-coded owner content.** Every signal about
*how this owner writes* — tone, register, sign-offs, banned phrases, the
people they reference, the terms they use, naming/anonymisation conventions —
comes from `<vault>/overlay/{voice,brand,keywords,people}/` at invocation
time, never from this file. A different owner never edits this skill; they
fill in their own overlay (`overlay/README.md`, starter scaffold at
`overlay/template/`).

This is the kernel/overlay contract (ADR-0003 Ruling 7 / `overlay/README.md`):
the substrate is generic, identity is data. Adapted from the reference
vault's `voice.skill` for **mode structure only** (DRAFT/REWRITE/CHECK, the
pre-ship-checklist idea, the self-eval pass) — none of that skill's embedded
profile, banned-word list, or samples travel here; they are reference-owner
content and belong (if ported at all) in *that* owner's own overlay files,
never in this kernel skill (ADR-0003 Appendix B).

## Phase 0 — load the overlay (every mode, every invocation)

```bash
export BRAIN_VAULT="${BRAIN_VAULT:-./vault}"
OVERLAY="${BRAIN_OVERLAY_DIR:-$BRAIN_VAULT/overlay}"
ls "$OVERLAY/voice" "$OVERLAY/brand" "$OVERLAY/keywords" "$OVERLAY/people" 2>/dev/null
```

Read whichever of the four categories exist (`overlay/README.md`'s shape:
one-or-more `*.md` files per category, each with an `overlay_type:` frontmatter
key). For each present category:

- **`voice/`** — durable writing voice: tone, register, sentence rhythm,
  sign-off repertoire, phrases to use/avoid. This is the Voice DNA layer (§2).
- **`brand/`** — naming/anonymisation/title conventions (e.g. role-title vs.
  real name in external drafts).
- **`keywords/`** — glossary / acronym / codename decoder ring — expand or
  preserve these terms correctly instead of guessing.
- **`people/`** — the people this owner's notes reference — get names,
  titles, and register-per-person right instead of generic.

**Graceful degradation — never error.** A missing category, a missing
`overlay/` directory entirely, or a template-only scaffold (still full of
`<!-- fill this in -->` placeholders) all mean the same thing: run in a
**neutral, professional register** using only the universal craft baseline
(§1) and say so plainly in the delivered output — *"No `overlay/voice/`
found (or it's still the placeholder template) — drafting in a neutral
register. Fill in `<vault>/overlay/voice/` to personalize (see
`overlay/README.md`)."* Never block, never invent owner content to fill the
gap.

## §1 — Craft baseline (universal — always applies, overlay or not)

These are generic writing-craft rules, not owner identity, so they apply
regardless of what the overlay contains (same posture as the reference
organization's `_writing_craft.md` "Layer B — universal, cross-language" — Pyramid
Principle / BLUF, not any one person's voice):

1. First sentence states the answer / recommendation / news — no
   throat-clearing, no context-only opener.
2. Each paragraph's first sentence carries its point (topic sentence).
3. The ask is explicit and concrete — action, decision, or by-when.
4. Active voice unless passive is genuinely clearer.
5. No hidden verbs (nominalisations) where a plain verb works
   ("implementation of" → "implement").
6. Cut clutter ("utilize" → "use", "in order to" → "to", "due to the fact
   that" → "because").
7. Every sentence passes the "so what" test.
8. Common generic AI-writing tells, flagged whether or not the overlay's own
   `voice/` file names its own list: "delve", "foster", "multifaceted",
   "plays a crucial role", symmetric N/N bullet pairs, Title Case Headers,
   5+ em-dashes in one paragraph, "Warm regards," as a default close.

## §2 — Voice DNA (100% overlay-sourced — only applies once `overlay/voice/` exists)

Read every file in `overlay/voice/` as free-text prose (no schema beyond the
`overlay_type: voice` frontmatter key — `overlay/README.md`) and apply it:
tone, sentence rhythm, sign-off repertoire, phrases the owner actually uses,
anything the owner has written down about their own voice. If
`overlay/brand/` also exists, apply its naming/anonymisation convention on
top. If `overlay/people/` or `overlay/keywords/` exist, use them to get names
and domain terms right in the draft.

**Egress (ADR-0003 Ruling e — overlay content is Internal-tier owner
identity):** overlay content may inform this skill's interactive drafting
output and any local artefact the owner keeps for themselves. It must
**never** be baked into anything repo-committed or publish-bound — a
committed fixture, test, doc, template default, or `dist/` package. If asked
to draft content that itself will be committed to this repo (e.g. a docs
page), use placeholder-only phrasing and say why, the same posture
`overlay/template/` uses for a brand-new owner.

## Three modes

### DRAFT — compose from rough input

**Fires when** the input is rough notes, bullets, or an instruction with no
existing draft to transform.

**Process:** Phase 0 (load overlay) → draft, applying §1 always and §2
wherever the overlay has signal → run the checklist (§3) → deliver (§4).

### REWRITE — transform an existing draft

**Fires when** the input is an existing draft (the owner's, a colleague's,
an AI's) that needs to read as the owner's voice.

**Process:** Phase 0 → diagnose the input against §1/§2 (AI tells, wrong
register, missed voice-DNA patterns) → rewrite → run the checklist (§3) →
deliver rewritten draft **+ an annotated diff table**:
`| Change | Original | Rewritten | Rule cited |`.

### CHECK — scan without rewriting

**Fires when** the input is a draft and the ask is "what's wrong", not "fix
it".

**Process:** Phase 0 → scan against §1/§2 → report violations with
line/phrase references and a suggested fix each, **without rewriting**. The
report IS the deliverable — no separate self-eval pass.

## §3 — Pre-ship checklist (DRAFT + REWRITE modes)

Run every §1 rule plus every rule the overlay's `voice/` files stated
explicitly (there is no fixed item count — it depends on what the overlay
says). Report as `[N/M pass]` where `M` is however many rules actually
applied this run (§1's fixed set, plus overlay rules if present). Any
overlay-specific rule that failed and was fixed is worth a two-line note back
to the owner (that's this skill's only "memory" — there is no committed
corpus/log to append to, since a per-owner miss/hit log is itself owner data
and belongs in the owner's own vault, not this kernel skill).

**Self-eval (DRAFT + REWRITE, optional but recommended):** if a fresh
subagent is available, spawn one with ONLY the finished draft plus the §1
list (no reasoning, no input notes, no conversation) and have it grade
independently. No subagent available → re-read the draft cold and grade it
yourself. Any failed check → repair → re-grade the full set (max 2 rounds);
still failing → ship anyway but flag it plainly, never silently.

## §4 — Delivery shapes

**DRAFT:**
```
[The draft, ready to send]

---
Checklist: [N/M pass]
[Overlay status: personalized from overlay/voice/... | neutral register — overlay/voice/ empty, see overlay/README.md]
```

**REWRITE:**
```
## Rewritten draft
[...]

---
## What changed and why
| Change | Original | Rewritten | Rule cited |
|---|---|---|---|

---
Checklist: [N/M pass]
```

**CHECK:**
```
## Voice + craft check report

Verdict: [N/M pass]

## Violations
1. [Line/phrase]: [what's wrong] -> [suggested fix] (rule)

## What's working
- [pattern correctly applied]
```

## Hard guardrails

- **Zero hard-coded owner content.** No embedded profile, no embedded sample
  corpus, no fixed banned-word list beyond §1's generic, non-owner-specific
  baseline. A future owner's personalization is 100% `overlay/` data, never a
  SKILL.md edit.
- **Never fabricate overlay content.** A missing category is degraded
  register + a pointer, never an invented voice.
- **Overlay egress stays Internal-tier** (ADR-0003 Ruling e) — interactive
  output and local artefacts only, never a committed or publish-bound file.

## Cross-references

- `overlay/README.md` — the four-category schema, resolution order, starter
  scaffold (`overlay/template/`)
- `docs/adr/0003-parity-architecture.md` Ruling 7 (this skill's contract) and
  Ruling e (overlay-data egress tier)
- The reference vault's `99 Workspace/_skill_packages/voice/SKILL.md` —
  mode-structure reference only (DRAFT/REWRITE/CHECK, the checklist +
  self-eval idea); not itself pinned by ADR-0003 Appendix B (that pins the
  voice-profile/craft *data* files under `90 System/`, not the skill
  package) — its embedded profile, banned-word list, and samples are
  reference-owner content and were never ported here
- `src/brain/overlay.py` — `overlay_dir()` resolution, `brain init
  --validate-overlay`

---
overlay_type: cos
title: "Ingest/no-ingest categories — starter template"
setting: ingest
updated: 2026-07-30
---

# Ingestion categories

OPTIONAL file (TAX-01/TAX-02). It tells the nightly chief-of-staff's Phase 1.6
what KIND of thing a candidate is, and therefore whether it is kept
automatically, proposed for your review, or never turned into a candidate at
all. Full spec: `docs/cos-ingest-taxonomy.md`.

**This file is a gate — read §Absent semantics before deleting or editing it.**

## Rule syntax

One list line per category, in this order:

```
- <category-id>: always|propose|never | lane=text|attachment|both | min_tier=<Tier>
```

- `<category-id>` — lowercase-hyphen, **stable** (see §Lifecycle).
- disposition — REQUIRED, one of `always` / `propose` / `never`.
  - `always` = auto-ELIGIBLE. It is **NOT** evidence-exempt: no source quote
    still means no candidate.
  - `propose` = staged for your batch answer. The default for anything
    unrecognised.
  - `never` = ZERO candidates. Not staged, not proposed, not deferred.
- `lane=` — OPTIONAL, default `both`. Which evidence lane this applies to:
  message body (`text`), ingested attachment (`attachment`), or both.
- `min_tier=` — OPTIONAL classification FLOOR
  (`Public|Internal|Confidential|Restricted|MNPI`). It can only RAISE the tier
  the `overlay/keywords/` mapping resolves; a category **never** lowers a tier.

<!-- examples (replace with your own categories — these are placeholders):
- contract-version: always | lane=both | min_tier=Confidential
- decision-record: always | lane=text
- commitment: always | lane=text
- key-number: always | lane=both
- counterparty-position: propose | lane=text
- working-draft: propose | lane=both
- market-digest: never | lane=both
- scheduling-logistics: never | lane=text
-->

## Absent semantics (STRICT — deliberately NOT auto-archive.md's convention)

| State of this file | Behaviour |
|---|---|
| **ABSENT** | The whole category feature is **OFF** — no category stamping, no engine refusal. Phase 1.6 runs exactly as it did before this feature existed. |
| **UNPARSEABLE** | **Fail CLOSED to `propose` for everything**, plus a logged defect. NEVER silent-off. NEVER `always`. NEVER `never`. |
| **One rule invalid** (unknown disposition, malformed line) | That rule resolves to `propose` with a warning; the remaining rules still apply. |

This deliberately diverges from the sibling `auto-archive.md`, whose
`aged_read_lane` key defaults ABSENT ⇒ **true** (the owner ruled that narrower
lane ON) while its `any_sender_lane` defaults ABSENT ⇒ OFF. Each key's default
is argued from its own blast radius — there is no overlay-wide convention to
inherit. The divergence pattern is documented in
`.claude/skills/chief-of-staff/SKILL.md` (Phase 1.5b, *"Explicit opt-in key,
ABSENT ⇒ OFF (deliberate reversal of the roster lane's convention)"*).

Rationale for strict-here: this file can SUPPRESS candidates. A silent-off on a
typo would look identical to a healthy run while quietly discarding substance —
the exact "the instrument cannot fail" shape the 2026-07-30 field audit found in
Phase 1.6. Failing closed to `propose` keeps every candidate visible to you.

## Lifecycle

- **Category ids are STABLE identifiers.** Auto-capture graduation (ING-04)
  accumulates acceptance evidence keyed on them.
- **A rename is a NEW id, and its evidence RESETS by design** — the renamed
  category starts with zero acceptance history, exactly as a fresh
  `kernel_version` does. If you want the old track record, keep the old id.
- **Removing a category, or flipping it to `never`, demotes any graduation
  immediately at the next engine load** — no grace period, no drain of
  in-flight auto-capture for that category.

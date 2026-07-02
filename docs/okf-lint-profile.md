# OKF — optional lint profile (NOT the substrate)

**Position:** Google's Open Knowledge Format (OKF) is **the vault's own
Markdown+YAML pattern, minimized**. Per the prior assessment (user memory
`reference_okf_assessment.md`), adopt it as a **gated boundary adapter / optional
lint profile**, *not* as the substrate. Revisit at the 2026-11-05 substrate
review.

## What this means concretely

- The substrate is **plain Markdown + YAML** as defined in `substrate-spec.md`.
  Reading or writing a note **never** requires OKF.
- OKF is available as an **opt-in lint pass**: `tools/validate.py vault --okf`
  applies a stricter profile (a documented subset of frontmatter keys /
  link-shape conventions that align with OKF) and reports deviations as
  **warnings**, never hard failures.
- OKF can also act as a **boundary adapter** for export/interchange with other
  OKF-speaking tools — at the boundary, gated, not in the core store.

## Why optional, not load-bearing
OKF buys interchange and a schema vocabulary the substrate already has in a
lighter form. Making it mandatory would re-introduce schema rigidity the flat,
link-first design deliberately avoids. Keep it as a profile you can turn on for
export or stricter hygiene, off by default.

## The `--okf` lint (what it checks, as warnings)
- Frontmatter keys conform to the OKF-aligned subset (no unknown keys outside an
  allowlist).
- Links use the canonical `[[id]]` / `[[id|display]]` shape.
- `classification`, `id`, `type` present (these overlap the hard checks; under
  `--okf` additional optional keys are also linted).

The profile is documentation + a validator flag. It is decision-reversible and
does not alter how notes are stored or retrieved.

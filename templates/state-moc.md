---
id: "{{id}}"
title: "{{title}}"
type: moc
classification: Internal
created: "{{date}}"
updated: "{{date}}"
tags: []
related: []
---

# {{title}}

One live "state of play" note — the navigable spine for this vault/project
(AGENTS.md §3, HYG-03). Each `## Section:` heading below carries its own
`Updated: YYYY-MM-DD` stamp on the very next line, so a reader (or
`tools/validate.py`'s warn-only staleness lint) can see which parts are
current without touching this note's own top-level `updated:`. A section
whose stamp is older than the staleness threshold is warn-flagged, never
blocked — a quality nudge, not a gate.

## Section: Current Priorities
Updated: {{date}}

- What matters right now.

## Section: Open Threads
Updated: {{date}}

- Unresolved questions, in-flight decisions.

## Section: Key Decisions
Updated: {{date}}

- Link to `[[decision-notes]]` rather than re-stating them here.

## Section: Watch List
Updated: {{date}}

- Things to revisit; stale items surface via the lint above.

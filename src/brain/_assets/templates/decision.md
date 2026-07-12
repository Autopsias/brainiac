---
id: "{{id}}"
title: "Decision - {{title}}"
type: decision
classification: Internal
created: "{{date}}"
updated: "{{date}}"
tags: []
# Bitemporal frontmatter (ADR-0003 ruling 2, optional but recommended for
# type:decision): two clocks — document_date is when the decision was
# recorded, effective_date is when it applies in the world.
document_date: "{{date}}"
effective_date: "{{date}}"
context: ""
project: ""
stakeholders: []
# Lint (TMP-05, warn-only): a decision note must anchor its claim to a
# source — set `source:` to a raw/ wikilink, or cite one inline below.
source: ""
related: []
---

# Decision - {{title}}

## Context

Why this decision was needed.

## Decision

What was decided.

## Rationale

Why this option was chosen over alternatives. Anchor claims to sources, e.g.
`[[raw/2026-07-05-example-source]]`.

## Consequences

What follows from this decision.

## Alternatives Considered

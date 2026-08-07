---
overlay_type: keywords
title: "Glossary & keyword decoder ring — starter template"
updated: 2026-07-01
---

# Glossary

List acronyms, codenames, and recurring keywords this owner's notes use, so
retrieval (`grep`/`search`) and a new reader have a decoder ring.

The third column is OPTIONAL. When it names a classification tier
(`Public|Internal|Confidential|Restricted|MNPI`), it says "material mentioning
this term is that tier" — and it is the ONLY thing that lowers an
email-derived ingest off its `MNPI` default (PRV-02). A row without it maps no
tier. A category's `min_tier` in `cos/ingest.md` can only RAISE what this
resolves, never lower it.

| Term | Expansion / meaning | Classification |
|---|---|---|
| `<ACRONYM>` | `<what it means>` | |
| `<codename>` | `<what it refers to>` | `<Tier, or leave blank>` |

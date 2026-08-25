---
id: "{{id}}"
title: "{{title}}"
# `deliverable` is ORTHOGONAL to `type` — never a member of its vocabulary.
# `type` is single-valued and load-bearing for retrieval (`type: decision` IS
# the decision layer; `type: project` drives PARA filing), so keep whatever
# type this output really is: note, decision, meeting, project, …
type: note
classification: Internal
created: "{{date}}"
updated: "{{date}}"
# The marker: this is something we PRODUCED, not something we ingested.
deliverable: true
# The project this output belongs to — a bare note id or a [[wikilink]]. It is
# what groups the shelf, so a deliverable without one lands ungrouped.
project: "{{project}}"
# Lint (DLV-01, warn-only): a deliverable anchors the output it marks — set
# `source:` to the raw/ payload, or cite it inline below.
source: "[[raw/{{source-id}}]]"
related: []
---

# {{title}}

What this output is, who it was produced for, and what it concludes. One
paragraph — the payload itself lives in `raw/`, immutably; this note is the
marker, the grouping, and whatever commentary the output needs.

Project: [[{{project}}]]
Source: [[{{source-id}}]]

## What changed since the last version

A new version of a deliverable is a **supersede**, never an edit: write the new
note, then `brain supersede <old-id> <new-id>`. The shelf shows the latest
member of the chain, so editing in place makes the old version vanish from the
record instead of retiring it.

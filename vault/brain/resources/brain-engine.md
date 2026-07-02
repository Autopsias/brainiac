---
id: brain-engine
title: "The brain engine — four interactions over Markdown truth"
type: note
classification: Internal
created: 2026-06-27
updated: 2026-06-27
tags: [retrieval, architecture]
---

# The brain engine

`brain` indexes `vault/brain/` with sqlite-vec (dense, via
[[arctic-embed-choice]]) + FTS5 (lexical). Markdown is the truth; the sqlite
index is a derived cache rebuildable from `vault/`.

It exposes exactly four agent-facing verbs: **search**, **get**, **recent**,
**draft_capture**. The real write (`write_note` — sign + index + WAL) is a
host-broker privilege, never an agent verb — see [[host-vm-trust-split]].

Every retrieval verb honours the [[classification-gate]] (default-deny). This
engine is the replacement for Obsidian + Smart Connections as the retrieval
substrate.

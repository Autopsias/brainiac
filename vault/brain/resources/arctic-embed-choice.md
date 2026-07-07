---
id: arctic-embed-choice
title: "Why Arctic-embed over e5 for the brain engine"
type: note
classification: Internal
created: 2026-06-27
updated: 2026-06-27
source: "[[2026-06-27-arctic-vs-e5-benchmark]]"
tags: [retrieval]
---

# Why Arctic-embed over e5

The `brain` engine uses Snowflake **Arctic-embed** (ONNX, mmap'd from the mount)
as the dense embedder, per the [[2026-06-27-arctic-vs-e5-benchmark]] capture. It
feeds the [[brain-engine]] alongside FTS5 lexical search.

Cross-lingual EN↔PT retrieval is the acceptance bar — the same criterion that
made the old Smart Connections `multilingual-e5-small` workable. See
[[host-vm-trust-split]] for where embedding actually runs (host commits; the VM
only reads).

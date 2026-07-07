---
id: host-vm-trust-split
title: "Host / VM trust split + capture protocol"
type: note
classification: Confidential
created: 2026-06-27
updated: 2026-06-27
tags: [security, architecture]
---

# Host / VM trust split

The Cowork Linux VM is EDR-blind and ephemeral, so it may only **search / get /
recent / draft_capture** — it never signs the audit chain, writes the WAL, or
commits the index. The **host broker** is the sole writer ([[brain-engine]]).

## Capture protocol (VM draft → host commit)

1. VM `draft_capture` → `vault/.brain/drafts/<id>.md` (`status: draft`,
   `provenance.trust: untrusted`), no index/WAL/signature.
2. Draft sits on the shared mount (host-visible).
3. Host `write_note`: validate frontmatter + [[classification-gate]], compute
   sha256, promote to `raw/` or `brain/`, Ed25519-sign the audit entry, write
   WAL, commit to index, regenerate snapshot, delete draft.

A draft is never surfaced by `search` until the host canonises it. This protects
the audit chain from the untrusted VM.

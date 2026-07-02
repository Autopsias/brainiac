---
id: classification-gate
title: "Classification egress gate (default-deny)"
type: note
classification: Confidential
created: 2026-06-27
updated: 2026-06-27
tags: [security]
---

# Classification egress gate

Every note carries `classification:` ∈
`Public < Internal < Confidential < Restricted < MNPI`. The [[brain-engine]]
filters retrieval results by tier before surfacing anything to a model.

**Default-deny:** a note with a missing or unrecognised `classification` is
treated as **MNPI** (most-restrictive) and withheld unless a human gate opens
it. Fail-closed — so an un-migrated import is invisible, never leaked. This is
why bulk classification is a migration prerequisite, and it is the input to the
egress work in [[host-vm-trust-split]].

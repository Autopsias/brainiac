---
overlay_type: cos
title: "COS priority overrides — starter template"
updated: 2026-07-13
---

# COS priority overrides

OPTIONAL category (CUT-01E). `brain cos-priority-map` generates the
VM-readable priority map from your `type: person` / `type: company` notes;
list ids below to override the computed placement. One list line per note:

`- <note-id>: high|normal|low|exclude`

<!-- examples (replace with your own note ids):
- contoso-acquisition-lead: high
- northwind-vendor-contact: low
- retired-supplier-x: exclude
-->

## Priority-chip taxonomy (chief-of-staff kernel v4.6 — companion block)

The nightly run projects the act queue onto exactly three Outlook
categories (recorded verbatim — category names are IMMUTABLE once created;
a rename means delete + recreate + re-chip every tagged message):

- `P0 · Now` — red
- `P1 · Today` — orange
- `P2 · This week` — blue

`chips_confirmed` is the RUNTIME chip gate the nightly reads: chips are
withheld (the legacy flat Action mark continues) until the owner's recorded
YES to the queued name/color confirmation question, at which point the
answering session sets:

```
chips_confirmed: false
chips_confirmed_date:
```

(The answering session flips the value to `true` and dates it. Never
pre-fill `true` — an uncommented `true` line IS the gate opening.)

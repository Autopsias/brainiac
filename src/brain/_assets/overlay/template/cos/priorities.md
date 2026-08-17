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

The nightly run projects its verdicts onto exactly FOUR Outlook
categories (recorded verbatim — category names are IMMUTABLE once created;
a rename means delete + recreate + re-chip every tagged message):

- `P0 · Now` — red
- `P1 · Today` — orange
- `P2 · This week` — blue
- `P3 · Read` — grey

The chip is chosen on TWO axes (kernel v7 DOCTRINE §4.1), never on tier alone:

| bucket | P0 | P1 | P2 | P3 |
|---|---|---|---|---|
| `act` | `P0 · Now` | `P1 · Today` | `P2 · This week` | `P3 · Read` |
| `read` | `P0 · Now` | `P1 · Today` | `P3 · Read` | `P3 · Read` |
| `noise` | no chip | no chip | no chip | no chip |

`read`/P2 and `act`/P2 share a TIER and take a different CHIP, which is the
reason a tier-only lookup cannot express this policy. `noise` takes no chip at
any tier: it is the archive-eligible bucket.

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

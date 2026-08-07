---
overlay_type: cos
title: "Auto-archive controls — starter template"
setting: auto-archive
updated: 2026-07-30
---

# Auto-archive controls

OPTIONAL file. It is the kill switch and the scope lever for every
chief-of-staff lane that may move mail out of your Inbox on its own. The skill
reads the keys below as plain `key: value` body lines (Phase 1.5 rule 7,
Phase 1.5b, Phase 1.5e, RTG-01).

**Every default here is argued from its own blast radius — there is no
overlay-wide "absent means X" convention.** The absent-value column is the
contract; read it before deleting a line.

## Keys

| Key | Values | ABSENT ⇒ | What it does |
|---|---|---|---|
| `enabled` | `true` \| `false` | `true` | Master kill switch. `false` — or this file present but unparseable — disables auto-archive for the run entirely (verdicts still computed, recorded as shadow). |
| `cap` | `<int>` | skill default | Maximum rows any lane may auto-archive in one run. All lanes share this one cap. |
| `scope` | `p3-only` \| `all-noise` | `p3-only` | Which `noise` verdicts the noise lane may archive. Any unrecognised value also reads `p3-only` — widening to full-NOISE is an explicit one-line opt-in, never a default. |
| `aged_read_lane` | `true` \| `false` | **`true`** | The roster-scoped aged-read lane: priority-list senders, already READ on the server, no open action. Absent means ON because the owner explicitly ruled this narrower lane on. |
| `aged_read_min_days` | `<int>` | `7` | Minimum age (server `receivedDateTime`) for the aged-read lane. **`0` is VALID and means NO age gate — read alone qualifies.** A `0` here is a deliberate setting, never a missing one; it must not be coerced back to the default by a falsy check. |
| `any_sender_lane` | `shadow` \| `live` | **OFF** | The any-sender widening of the aged-read lane. Absent means the lane does not run at all — not even in shadow. Materially larger blast radius, so it does NOT inherit the roster lane's absent-means-on convention. Any unrecognised value also reads OFF. |
| `recurring_digest_supersession` | `true` \| `false` | `true` | Keep-latest disposition for recurring automated digests (same normalized subject, same sender, ≥2 Inbox instances). The latest instance is never archived. |
| `chip_reeval` | `shadow` \| `live` | **OFF** | Full-Inbox chip re-evaluation. Absent or unrecognised produces ZERO mutations (verdicts computed, bookkeeping only). |

<!-- starter body (uncomment and edit — these are the shipped defaults spelled out):
enabled: true
cap: 20
scope: p3-only
aged_read_lane: true
aged_read_min_days: 7
recurring_digest_supersession: true
-->

## Sender scoping is NOT set here

Which senders count as priority ("roster-`high`") comes from
`overlay/people/` — with per-note overrides in `overlay/cos/priorities.md`.
This file controls *which lanes may act and how far*; it never carries a
sender allow/deny list of its own. Keeping the two apart means widening a lane
and widening a roster stay separate decisions.

## Guards this file cannot switch off

The keys above tune scope. They never disable the structural guards: P0/P1 is
excluded from auto-archive under every lane and every scope, a low-confidence
verdict is held rather than archived, an uncertain row is held, a
draft-protected thread is kept, every archived row carries a full
undo-capable ledger entry written BEFORE the move, and a stale or absent
undo canary holds the whole feature in shadow.

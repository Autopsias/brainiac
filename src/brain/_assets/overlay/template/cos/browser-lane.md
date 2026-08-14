---
overlay_type: cos
title: "Elected browser lane — starter template"
setting: browser-lane
updated: 2026-08-09
---

# Elected browser lane

OPTIONAL file. It pins WHICH browser surface the nightly run drives, so a fix
proven on one lane does not have to be proven again on the other. The skill
reads the key below as a plain `key: value` body line, exactly like
`auto-archive.md`.

## Keys

| Key | Values | ABSENT ⇒ | What it does |
|---|---|---|---|
| `pin` | `chrome-plugin` \| `iab` \| `none` | **no pin** | The toolset the run MUST elect. With a pin in force the ordinary IAB-first preference order (SKILL.md, browser-toolset preference order) is bypassed and the pinned lane is attempted first. `none`, an unrecognised value, or an absent file all read as NO pin and the preference order stands. |

<!-- uncomment and edit:
pin: chrome-plugin
-->

## Why a pin, and how it is lifted

The run elects between browser surfaces on capability, which is right for
availability and wrong for evidence: a lane mechanic proven on one surface has
to be re-proven on the other, and a night can silently land on the unproven one.
Pinning makes the evidence lane the run lane.

A pin is a CAMPAIGN setting, not a permanent one. Lift it by setting `pin: none`
or deleting this file — no version bump, no code change, effective on the next
run.

## When the pin moves — the promotion bar

A pin should point at the lane with the best evidence, and "evidence" here is a
measured number, not a good night. **The pin moves to a lane only after that
lane passes ONE read-only rehearsal of 20 already-read rows with
`first_attempt_ok: 20`, `mismatches: 0` and `contract_problems: []`.** That is
what the Chrome Plugin measured on runs 101 and 102 (20/20 first-attempt opens,
twice), so it is the bar for replacing it.

`LANDS, WITH RETRIES` is not a pass. The mechanic being promoted is the FIRST
click; a re-target that rescues a row in a daylight rehearsal is a re-target the
night will spend on a row that matters.

Run the rehearsal in daylight — it costs about five minutes and mutates
nothing, where discovering the same fact on a live night costs the night:

| Lane | Rehearsal |
|---|---|
| `chrome-plugin` | `python3 <repo>/tools/cos_lane_rehearsal.py --deep-link --rows 20` |
| `iab` | point Codex at `<repo>/tools/cos_lane_rehearsal_iab.md` — the in-app browser lives inside the Codex app, so no host process can drive it |

**Rehearse the primitive the night will use.** Since v5.55 the body pass opens a
thread by NAVIGATING to its own URL, not by clicking its row, so `--deep-link`
is the one to prove; drop the flag to rehearse the CLICK path, which remains the
documented fallback for a lane that cannot navigate and is the one bounded
re-target on either. A deep-link rehearsal reports four extra
numbers: `unconfirmed` (the URL agreed and OWA's list never named that row
selected — a verdict of `UNCORROBORATED`, which does not promote),
`full_reloads` (how often navigating threw the whole list away), `open_wait_s`
(what an open actually cost, since v5.56 waits for the page instead of sleeping
a constant) and `bodies_rendered` (how many landed opens had any body text at
all).

**Read `rows_attempted` before you read the verdict.** OWA's list is virtualized
and renders about a dozen rows at a time, so a rehearsal used to measure 12 when
asked for 20 and still print CLEAN. Since v5.56 it scrolls for its sample and,
when it still falls short, says `SHORT SAMPLE` and exits 2 — a pass measured
over fewer rows than requested is a false all-clear, not a smaller pass.

Both write one standalone report outside `cos-ops/` and take no run id. The
rehearsal recommends; editing `pin:` below is the owner's call.

## What a pin cannot do

- **It cannot make an unavailable lane work.** A pinned lane that cannot be
  elected is a NAMED failure — the outcome contract renders
  `OC-lane-pin-not-honoured` and the run reports the lane it actually ran. It is
  never a silent fallback to the other lane.
- **It cannot lower any guard.** Enumeration completeness, the zero-send proof,
  identity assertion and every mutation authority are unchanged on either lane.
- **It is read from THIS file, never from the run.** The outcome-contract
  checker reads it out of the vault beside the ops dir, so a run cannot drop the
  pin by omitting it.

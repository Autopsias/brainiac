---
name: cos-nightly
description: Controls and monitors the unattended chief-of-staff mailbox run — status readout, HTML status page, start a run now, dry run, emergency stop, resume, undo a run's archives, and the install/uninstall commands for the 06:30 schedule. Use when the user says "cos status", "what did the nightly do", "did COS run last night", "stop the nightly", "undo last night's archives", "pause/resume the schedule", or "/cos-nightly".
---

# /cos-nightly

Everything routes through ONE script — never a hand-typed `launchctl` or a
remembered flag. The script lives in the plan worktree; set it once, then use
the short form:

```bash
# `$COS_REPO` overrides the location — same contract as cos_ctl.sh's own default,
# so the two can never disagree about where the tooling lives.
COS="${COS_REPO:-$HOME/DeveloperFolder/profile-a-brain/.claude/worktrees/cos-workflow-rebuild}/tools/cos_ctl.sh"

$COS status      # schedule state, kill switch, recent runs, log tail
$COS page        # rebuild the HTML status page and open it
$COS dry         # full night that STOPS before applying (~13 min)
$COS run         # full night, uncapped, last ~2 weeks (add `--all` for historic)
$COS stop        # halt the run in flight + pause the schedule
$COS resume      # lift the stop + re-arm the schedule
$COS undo <run>  # put that run's archives back, verified per thread
$COS unchip <run># take that run's priority chips back off, verified
$COS install     # PRINTS the two install commands (owner runs them)
$COS uninstall   # PRINTS the two removal commands
```

The script sets `BRAIN_VAULT` and `PYTHONPATH` itself and runs from any
directory. The status page lands at `<vault>/cos-ops/_cos_nightly_status.html`
— machine output, never indexed. (When the plan branch merges to `main`, this
skill ships from the repo and the absolute path becomes `tools/cos_ctl.sh`.)

## What to run for which ask

| The user asks | Do |
|---|---|
| "what happened last night" / "cos status" | `status`, then answer from its output — or `page` if they want to look at it |
| "run it now" / "do a pass" | `run`. If they have not seen tonight's plan before, prefer `dry` first and show them the plan |
| "stop it" / "kill it" | `stop` immediately, no questions first |
| "undo last night" | `status` to get the run id, then `undo <run-id>` |
| "take those chips off" | `status` to get the run id, then `unchip <run-id>` |
| "turn the schedule on/off" | print what `install`/`uninstall` outputs and let the owner paste it |

The doctrine the nightly runs under is
`.claude/skills/chief-of-staff/DOCTRINE.md` (chief-of-staff v6.0) — 517
lines. The 6,339-line `SKILL.md` beside it is SUPERSEDED and binds nothing;
do not answer a question about what the nightly does from that file.

## Hard rules

- NEVER execute the `install`/`uninstall` commands yourself — loading or
  removing persistent automation is the owner's action. The script only
  prints them; keep it that way.
- `run`, `undo` and `unchip` mutate the live mailbox: invoke them only on the owner's
  explicit request in this session, never as a side effect of a status
  question.
- `stop` is always safe and needs no confirmation — it halts AFTER the
  mutation in flight and pauses the schedule. The persistent lever is the
  kill switch (`<vault>/overlay/cos/auto-archive.md`, `enabled: false`),
  which `stop` names in its output.
- A `status` row showing `UNFINISHED` mutations means a pass died mid-flight:
  the next `run`/`dry` reconciles them from the ledger before doing anything
  new — do not "clean up" the ledger by hand.

## Failure you will actually see

Exit 4 from `run`/`dry`/`undo` = the mailbox session lapsed. The fix is human:
open the Chrome-COS window, sign in once (expect an MFA prompt), re-run. Do
not diagnose the lane before checking this — measured twice, a stale bearer
looks exactly like a broken lane (401 on every mutation).

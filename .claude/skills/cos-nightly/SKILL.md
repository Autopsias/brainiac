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

## A run whose point is GROUNDING — check BEFORE, and read the numbers AFTER

Grounding can come out empty in three different ways, and the preflight sees
only the first. **Both halves below are required; the check before the run is
necessary and is not sufficient.**

### 1 · Before the run — can senders even be classed internal?

```bash
python3 tools/cos_ground.py --vault "$BRAIN_VAULT" --preflight
```

**Exit 0 (`senders-classifiable`) means exactly one thing: the tenant-domains
overlay exists and declares at least one usable domain, so a sender can class
`internal`.** It is the FIRST of grounding's preconditions and the only one this
command checks — it makes no `brain` call at all, so it cannot know whether a
lookup will find anything. It is safe to run at any time (no engine call, no
write, no mailbox).

**Exit 1 means every sender will class `external`, no vault lookup is reachable,
and the run will declare `ungrounded` — a clean, passing, entirely uninformative
night.** `ungrounded` is designed behaviour and E10 PASSES it, so a run built to
exercise the grounded path can execute the whole lane, prove only the failure
path, and look identical to a success. The cause is almost always one missing
file: `<vault>/overlay/cos/tenant-domains.md`, whose frontmatter must declare
`setting: tenant-domains` and whose body lists the owner's own domains as
`- example.com` lines. **Only the owner can write it** — those domains identify
them — so surface the exit-1 reason and ask; never invent domains, and never
treat the resulting `ungrounded` night as a grounding test.

### 2 · After the run — did any vault content actually reach the model?

**A night can declare `grounded` and still have handed the model nothing.**
`lookup-failed` and `no-vault-content` are downstream of the preflight and it
never sees them; E10 passes such a night too, and this time without even the
word `ungrounded` to warn you. So read E10's substance sentence in the run
report — it is the numbers, not the state word:

```
N of M delivered id(s) carried vault content; per leg with_content:
triage 12/50 (used at least 4), staging 3/9 (used at least 0), …
```

Treat it as a **FAILED grounding exercise, not a pass**, when either:

- **`with_content` is 0 on every leg.** The lane ran, the map was delivered, and
  no block carried any vault text. Nothing was grounded; the run proves the
  plumbing and nothing about the judgment.
- **`used at least 0` on a leg whose `with_content` is NOT 0.** Content was
  handed over and no verdict on that leg reused a distinctive phrase from it.
  Read it as the dead-subsystem signal it is — but read the qualification with
  it: the number is a LOWER BOUND (see `refused_grounding_overlap` on the same
  sentence, which counts rows refused precisely BECAUSE they quoted their
  block). A leg that paraphrased everything scores 0 honestly.

Report both numbers to the owner rather than the state word. "E10 PASS" on its
own does not mean the night was grounded.

The doctrine the nightly runs under is
`.claude/skills/chief-of-staff/DOCTRINE.md` (chief-of-staff v7) — 1,127
lines. The 6,394-line `SKILL.md` beside it is SUPERSEDED and binds nothing;
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

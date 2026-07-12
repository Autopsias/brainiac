# WD-01 — off-host watchdog of last resort: spec

Session s07 of `_plans/brainiac-self-maintenance-hardening-2026-07-11/`.

## Problem

`brain-nightly` (launchd) runs the maintain umbrella hourly, which includes
the WATCHDOG-01 synthesis-heartbeat check (commit d28c0ce) — the two
scheduled tasks watch each other. But if **launchd itself** dies, or its
plists get wiped, both on-host watchdogs die with it: nothing left running
can report that. WD-01 is a check that lives on a **different scheduling
substrate** than launchd, so it survives launchd's own death.

## What ships now (owner decision 2026-07-12)

The **LOCAL** check-logic + a macOS-push failsafe. The off-host **cloud**
leg (a Claude `/schedule` routine reading this data remotely) is
**DEFERRED** — see the capability gap below.

| Piece | Where |
|---|---|
| Check logic | `brain.maintenance.offhost_watchdog_findings(vault, now)` — STALENESS-SCOPED (rework fix [4]): reads the latest health-history record's `ts` (an unparseable newest `ts` is itself a breach — fix [6]) and delegates to the UNCHANGED `synthesis_heartbeat_finding` (WATCHDOG-01). Transient content findings (`blocked>0`, `health_trend` regressions) are deliberately NOT folded — that is OBS-02's deduped on-host job. Returns a list of breach strings (empty = healthy/silent) |
| Runnable script | `scripts/offhost_watchdog_check.py` — iterates every HOST vault in `~/.brainiac/workspaces.json`, calls the check logic per vault, fires ONE `fire_notification` (the existing OBS-02 osascript channel) on any breach, silent otherwise |
| Tests | `tests/test_offhost_watchdog.py` — forced-stale fixture proves the breach path fires; a fresh heartbeat, and no-history-at-all, both prove silence |

### Why freshness is keyed on the health-history record's `ts`, not `maintain-state.json`

`maintain-state.json`'s `last_run` is an **ISO DATE** (`"2026-07-12"`), not a
timestamp — it can only express staleness in whole-day (24h) steps. A >26h
threshold is meaningless against day-granularity data (the first value it
could ever cross is 48h, silently degrading a 26h intent into a 48h one).
The health-history record's `ts` is a precise ISO datetime written every
maintain run, so it is the only field that can honor an hourly-cadence
threshold like 26h. This is the one thing WD-01 adds beyond WATCHDOG-01 and
`brain status`'s own `maintain_heartbeat` summary (which also uses
day-granularity `age_hours = date_diff.days * 24` — fine for its own 48h
threshold, not fine for 26h).

## Verified capability gap: `/schedule` cloud routines cannot read local files

Before designing the cloud leg, this session probed the `schedule` skill's
own documentation (per the standing rule: verify a substrate's behavior
before depending on it). Its own words:

> These are CLOUD agents — they run in Anthropic's cloud, not on the user's
> machine. They cannot access local files, local services, or local
> environment variables.

A `/schedule` routine's `job_config.ccr` gets a **fresh git-repo checkout**
in Anthropic's sandbox, not the user's Mac filesystem — so even with a
`profile-a-brain` repo attached as a source, it has zero visibility into
`~/.brain/health-history.jsonl`, `~/.brain/synthesis-state.json`, or
`~/.brainiac/workspaces.json` on the actual host. There is currently no
remote-export transport that would put this vault's local state somewhere
a cloud routine could read it — that transport is exactly what corrections
3/7 (the remote-store leg) describe, and it is **deferred** per the owner's
2026-07-11 checkpoint decision.

**Consequence:** a `/schedule` routine cannot, today, be the thing that
actually performs WD-01's check. This is not a new deferral beyond what was
already decided — the checkpoint instruction already anticipated "no
remote-read integration test needed since there's no remote leg." This
document makes the reason explicit and verified, rather than leaving the
cloud-routine idea looking shippable when it is not.

## The routine PROMPT (for whenever a remote-read channel exists)

Kept here so the wiring is one paste away once the remote-export leg (or
an attached MCP connector exposing the host's `.brain/` state) exists:

```
Weekly Brainiac off-host watchdog. For every host vault this account
manages, check whether brain-nightly (the hourly maintenance task) or the
weekly synthesis task have GONE QUIET (staleness only — transient content
findings are the on-host alarm's job). If ANY vault has a staleness finding,
send me a push notification summarizing it (vault path + the finding text).
If everything is healthy, do nothing — no notification, no message. Do not
modify anything; this is a read-only check.
```

Cadence: weekly (the plan's original cadence choice — this is a slow-moving
signal; `brain-nightly` already alarms hourly for anything it can still
detect from inside itself). Minimum `/schedule` interval is 1 hour, so a
weekly cron (e.g. `0 8 * * 1`, Monday 08:00 UTC) fits easily once wired.

## Registration today (per the plan checkpoint: do not block on live routine creation)

1. Run `scripts/offhost_watchdog_check.py` manually, or fold it into
   whatever periodic habit already runs `/brainiac-health` — both read the
   same host vault registry and health-history files.
2. Once the remote-export leg lands (a future session, not deferred here
   silently — see `docs/operations/wd01-deferred-cloud-leg.md`), register
   the routine above via `/schedule` for real.

## routines/manifest.json entry

Documented as a **NOTE**, `os_scheduled: false` — THE LOCK
(`routines/manifest.json`'s `locked_counts`) counts OS-level schedulers
(launchd/Task Scheduler) on the HOST; a Claude `/schedule` cloud routine is
a different scheduling substrate entirely (Anthropic's cron infrastructure,
not an entry in the host's own scheduler), which is exactly why it can
survive launchd's death and exactly why it does not count against the
budget THE LOCK exists to bound.

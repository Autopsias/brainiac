# WD-01 follow-up — the off-host CLOUD leg is deferred

Status as of session s07 (`_plans/brainiac-self-maintenance-hardening-2026-07-11/`):
**INCOMPLETE by design, not an oversight.** Owner decision at the WD-01
checkpoint (2026-07-11) chose to ship the LOCAL heartbeat check +
macOS-push failsafe now (`brain.maintenance.offhost_watchdog_findings` +
`scripts/offhost_watchdog_check.py`, see
`docs/operations/wd01-offhost-watchdog-spec.md`) and defer the off-host
**cloud** export/transport (corrections 3/7 of the original plan).

## What's missing

A way for a `/schedule` cloud routine (Anthropic's cloud infrastructure) to
read this vault's local state — `.brain/health-history.jsonl`,
`.brain/synthesis-state.json`, `~/.brainiac/workspaces.json` — none of
which are visible to a cloud sandbox. Session s07 verified this directly
against the `schedule` skill's own documentation rather than assuming it:
cloud routines "cannot access local files, local services, or local
environment variables."

## What a real fix needs (not built here — no new egress introduced this session)

One of:
- A **remote-store transport**: the host periodically pushes a small,
  scrubbed summary (heartbeat ages + regression findings, never vault
  content) to some remote store a cloud routine can read. This is a NEW
  outbound egress channel and needs its own classification/scrub review
  before it exists — explicitly out of scope for this session per the
  checkpoint decision ("do NOT introduce any new outbound egress or
  credentials").
- An **MCP connector** exposing the host's `.brain/` runtime state to a
  connected cloud routine (the `schedule` skill already supports attaching
  MCP connectors — none are currently connected for this account).
- A platform capability change letting a scheduled routine run against a
  local execution context instead of a cloud sandbox.

## Revisit trigger

Pick this back up once the owner names a transport (or a platform capability
appears that removes the need for one). Until then, the LOCAL script is the
real, working watchdog — a human (or any local scheduling habit) runs it
periodically; nothing about the LOCAL leg is blocked or partial.

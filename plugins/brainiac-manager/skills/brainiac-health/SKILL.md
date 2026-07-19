---
name: brainiac-health
description: One-command health readout for every registered Brainiac host vault — runs `brain doctor` + `brain status`, reads the health-history trend (week-over-week deltas for notes/quarantine/latency/golden-score/synthesis-cost), both scheduled-task heartbeats (maintain + synthesis), and the hot-queue tail, then renders one VERDICT (HEALTHY/DEGRADED/BROKEN) with the exact next command for anything wrong. Use when the user says "is brainiac healthy", "brainiac health check", "/brainiac-health", "check vault health", or asks for a health/status readout instead of running doctor/status/trend/heartbeat by hand.
---

# /brainiac-health

Read-only. Answers "is Brainiac healthy?" in one shot instead of four
separate commands (`brain doctor`, `brain status`, a manual read of
`health-history.jsonl`, a manual read of `synthesis-state.json`). Safe to
run any time, safe for the model to invoke on its own — it never writes to
the vault, the index, or any state file.

## Run it

```bash
python3 .claude/skills/brainiac-health/scripts/health_report.py
```

With no arguments it iterates every `target: "host"` entry in
`~/.brainiac/workspaces.json` (deduplicated by `vault_path`). Pass one or
more vault paths to check specific vaults instead (useful for a vault that
isn't registered yet, or to check just one out of several):

```bash
python3 .claude/skills/brainiac-health/scripts/health_report.py /path/to/vault
```

**Relay the script's own output.** It already renders the VERDICT line,
the deltas table, both heartbeats, the open-items list (each with its
remediation command), and the hot-queue tail — do not re-derive or
re-summarize the numbers by hand, and do not re-run `brain doctor`/
`brain status` separately unless the user asks to dig into one finding
further (`brain get <id> --json`, `brain maintain --json`, etc., per
AGENTS.md §5).

## What the verdict means

- **BROKEN** — `brain` isn't on PATH, no vault is registered/reachable,
  `brain doctor`/`brain status` produced no parseable JSON at all, the
  maintain heartbeat shows `repeated_failures` on a branch, or the latest
  health-history record has `blocked > 0`. Something is actively failing,
  not just aging.
- **DEGRADED** — one or more `brain doctor` rows are `stale`/`unmanaged`,
  the maintain heartbeat is `stale` on a branch, a week-over-week trend
  regression fired (latency, quarantine growth, or golden-score drop —
  `brain.maintenance.health_trend`'s own thresholds, never re-derived
  here), or the synthesis watchdog says the weekly synthesis run is overdue
  or failing. Nothing is on fire, but something needs attention.
- **HEALTHY** — none of the above. Across multiple vaults the overall
  verdict is the worst individual vault's verdict.

Every open item in the report carries the exact next command to run —
relay it verbatim rather than inventing a different fix.

## Trend deltas — reading the table

Each row is "current value" vs "the value from the closest record at/before
~7 days ago" (falls back to the oldest available record + the real day gap
when history is younger than a week — the table says so explicitly rather
than faking a 7-day comparison). `golden_score` and `synthesis_$` are
sparse (weekly/quarterly cadence): their "baseline" is the previous non-null
observation, whenever that was, matching how `health_trend` itself treats
`golden_score`.

The table is descriptive only — it never labels a direction "good" or
"bad" on its own (`notes` growing is fine, `quarantine` growing is not).
The judgment call lives in the Open Items list, sourced from
`health_trend`'s own regression findings; read the table alongside it, not
instead of it.

## Design notes (why it's built this way)

- **Never reimplements the regression algorithm.** All trend math (daily
  bucketing, baseline medians, per-metric thresholds, the sparse
  golden-score union) stays in `src/brain/maintenance.py`
  (`read_health_history`, `read_sparse_history`, `health_trend`,
  `synthesis_heartbeat_finding`, `latest_synthesis_cost`) — this skill only
  calls those functions and renders their output, per the OBS-01/02/04
  contract those helpers ship under.
- **A discoverable repo checkout wins over the installed package.** The
  helpers above are pure stdlib (no `BrainCore` import, no third-party
  deps), so a bare `python3` can import them from either place. The script
  prefers a repo checkout it can find relative to its own path, and falls
  back to whatever `brain` is actually installed otherwise — this keeps the
  readout correct on a dev checkout that is ahead of the installed package,
  while working unchanged on a plain end-user install with no checkout at
  all. See `references/design-notes.md` for the exact resolution order and
  known environmental caveats (e.g. `brain doctor`'s own role-detection
  quirk on some installed-package layouts).
- **`brain doctor` exits non-zero on findings, by design** (same convention
  as a linter) — the script always tries to parse stdout as JSON first and
  only treats a truly unparseable result as a hard failure.
- **Open items reuse the engine's own remediation strings** wherever the
  engine already ships one (`doctor` row `remediation`, the synthesis
  watchdog's `proposed_action`); only the four trend-metric remediations
  (`blocked`/`selftest_ms`/`quarantine`/`golden_score`) are composed here,
  since `health_trend` findings don't carry one.

Full field reference (health-history schema, verdict severity mapping,
troubleshooting a `BROKEN` result) is in `references/design-notes.md` —
only open it when a finding needs deeper investigation than the report
already gives.

<!-- SKILL_VERSION: 0.19.1 (generated by tools/package_clients.py — do not hand-edit) -->

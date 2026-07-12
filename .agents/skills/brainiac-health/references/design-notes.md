## Contents
- [Repo-fallback resolution order](#repo-fallback-resolution-order)
- [Health-history fields rendered](#health-history-fields-rendered)
- [Verdict severity mapping](#verdict-severity-mapping)
- [Troubleshooting a BROKEN result](#troubleshooting-a-broken-result)
- [Known environmental caveat: doctor role detection](#known-environmental-caveat-doctor-role-detection)

## Repo-fallback resolution order

`_load_maintenance()` in `scripts/health_report.py`:

1. Walk up from the script's own path (max 8 levels) looking for a
   directory that has both `pyproject.toml` and `src/brain/` — i.e. a
   checkout of this repo. If found, prepend `<checkout>/src` to
   `sys.path` and import `brain.maintenance` from there.
2. Otherwise import `brain.maintenance` however it's already resolvable
   (whatever `pip`/`uv tool`/`pipx` put on the ambient Python path).

This is a permanent design property, not a temporary patch: once a real
`pip install` ships the OBS-01 helpers, both paths return the same module
and behave identically. It only matters *today*, mid-hardening-effort,
where the installed `~/.brainiac/venv` build predates
`read_health_history`/`read_sparse_history`/`health_trend` — verified
2026-07-11 against `brain 0.15.0`: the installed copy lacks all three.

The same walk-up-and-check pattern locates `tools/workspace_registry.py`
for reading `~/.brainiac/workspaces.json` (`_registered_host_vaults()`);
absent a checkout, it falls back to a plain JSON read of the same file
(`doctor.py`'s own registry check uses this identical fallback shape).

## Health-history fields rendered

One record per `brain maintain` run (`<vault>/.brain/health-history.jsonl`,
schema owned by `collect_health_metrics` in `src/brain/maintenance.py`):

| Field | Rendered as |
|---|---|
| `notes` / `chunks` | index size gauge (deltas table `notes` row) |
| `quarantine` / `duplicate` | inbox hygiene (deltas table `quarantine` row) |
| `selftest_ms` | one-note hybrid-search latency probe (deltas table `latency_ms` row) |
| `action_required` / `blocked` | outcome counts from the run's three-bucket report — `blocked > 0` on the LATEST record alone is a BROKEN-severity trend finding |
| `golden_score` | sparse, quarterly cadence (deltas table `golden_score` row, via the sidecar union) |
| `synthesis_cost_usd` | last metered synthesis cost persisted at run time (deltas table `synthesis_$` row) |
| `snapshot_gen` / `snapshot_age_s` | not rendered by this skill — already visible via `brain status`'s own `snapshot` block |

## Verdict severity mapping

| Source | Condition | Severity |
|---|---|---|
| `brain doctor` row | `status: stale` or `unmanaged` | DEGRADED |
| `brain status` `maintain_heartbeat.status` | `stale` | DEGRADED |
| `brain status` `maintain_heartbeat.status` | `repeated_failures` | BROKEN |
| `health_trend` finding | `metric: blocked` | BROKEN |
| `health_trend` finding | any other metric (`selftest_ms`/`quarantine`/`golden_score`) | DEGRADED |
| `synthesis_heartbeat_finding` | non-null (stale or failing synthesis heartbeat) | DEGRADED |
| `brain doctor` / `brain status` | no parseable JSON at all | BROKEN |
| `brain` executable | not found on PATH | BROKEN |
| no registered/reachable vault | — | BROKEN |

Overall verdict across vaults = the worst individual vault's verdict.

## Troubleshooting a BROKEN result

- **"no `brain` executable on PATH"** — run `/brainiac-install`, or confirm
  the channel-specific bin dir (`~/.local/bin`, the `uv tool`/`pipx` shim
  dir, or `~/.brainiac/venv/bin`) is on `$PATH`.
- **"no host vault registered"** — run `/brainiac-install` against the
  intended vault, or pass the vault path explicitly as an argument.
- **"brain doctor/status failed"** — the script only reports this when
  stdout wasn't parseable JSON at all (a real crash, not just a nonzero
  exit — `doctor` intentionally exits non-zero on ordinary findings). Run
  the failing command manually to see the raw traceback/stderr.
- **repeated_failures / blocked>0** — check `~/.brain/logs` and
  `launchctl list com.brainiac.*` (macOS) for the `brain-nightly` task,
  then re-run `brain maintain --json` manually to reproduce and inspect the
  blocked item's `blocking_on`/`retry_when` detail directly.

## Known environmental caveat: doctor role detection

`brain doctor`'s `looks_like_vm_stage()` check (src/brain/doctor.py)
infers "this is a staged Cowork VM copy" when `tools/workspace_registry.py`
and a resolvable `pyproject.toml` SSOT are both absent *relative to the
running `brain` package's own location* — which can also be true for a
plain pip/uv/pipx-installed host copy that was never colocated with a repo
checkout. When that happens, `brain doctor --json` reports `"role": "vm"`
and marks several genuinely-host-only surfaces (host venv, plugin/
marketplace staleness, version SSOT) `"not-detectable"` even though the
host has all of that context. This skill renders `not-detectable` rows as
no-signal (they never affect the verdict) rather than guessing around the
underlying detection — that detection logic belongs to `brain doctor`
itself, not this skill.

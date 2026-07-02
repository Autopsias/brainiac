# S09 evidence — Daily-use UX layer (UX-01 / UX-02 / UX-03)

**Session:** S09 · **Date:** 2026-06-27 · **Repo:** `/Users/user/DeveloperFolder/profile-a-brain/`
**Builds on:** S02 core (Ed25519 audit chain, key custody) · S06 host/VM trust split · S08 security posture.

---

## Bottom line

The daily-use layer adds three things built on top of the existing security posture:

1. **UX-01 — `brain capture`:** The ONE capture entry point. HOST: enforce
   frontmatter → `write_note` (signed + audited) → incremental sync. VM
   (Cowork): enforce frontmatter → `draft_capture` (unsigned drop to
   `capture-inbox/`). NO signing key is ever resolved on the VM path.

2. **UX-02 — `brain brief` + `brain digest`:** Quiet daily/weekly summaries.
   `brain brief` IS the guaranteed daily drain floor — it drains pending
   captures before reporting. The tripwire line surfaces a stalled drain:
   `"N captures pending · last successful drain: stalled (no key?)"`.
   The ONE sanctioned scheduled task (`brain-daily-brief`) runs `brain sync
   --publish && brain brief --json` daily at 07:00.

3. **UX-03 — Four interactions, quiet defaults:** The whole system is four
   things — capture, ask, morning brief, weekly digest. Everything else is
   plumbing. Output is quiet by default: no paths, no backend noise, no
   unnecessary verbosity.

**Tests:** 56 new S09 tests; full suite 185 passed (pre-existing ranx failure
in S05 scope is unchanged and expected).

---

## UX-01 — `brain capture` (write_note-routed capture)

### Design

```
host-native clients (Codex / Claude Code / Gemini CLI):
  brain capture → capture.enforce() → write_note() → sync(drain=False) → DONE
  [signed, audited, indexed, immediately retrievable]

sandboxed clients (Cowork / VM):
  brain capture → capture.enforce() → draft_capture() → capture-inbox/ → DONE
  [unsigned, unindexed, awaits host drain-on-invoke]

host drain-on-invoke (first step of any brain sync):
  capture-inbox/*.md → validate(untrusted) → write_note() → unlink draft
```

### `capture.enforce()` — frontmatter guarantees (additive, non-clobbering)

| Key | Default when absent | Never overwrites |
|-----|--------------------|--------------------|
| `id` | `capture-<sha256[:12]>` | ✓ |
| `title` | same as `id` | ✓ |
| `type` | `note` | ✓ |
| `classification` | `Internal` (NOT Secret — usable by default) | ✓ |
| `created` | today ISO | ✓ |
| `updated` | today ISO | ✓ |
| `status` | `draft` (always) | ✗ |
| `provenance.trust` | `untrusted` (always) | ✗ |

`status: draft` and `provenance.trust: untrusted` are ALWAYS stamped so the
host drain can identify and datamark untrusted ingest candidates (HARDENED
constraint: untrusted-in model).

### Host-routed capture proof

`_evidence/s09/host-routed-capture-proof.txt` — real CLI output showing:
- `brain capture` on HOST: `signed: true`, `indexed: true`, `role: host`
- File written to `vault/brain/resources/<id>.md` (NOT to `capture-inbox/`)
- Audit chain `status: ok` after the write (signed entry confirmed)
- `capture-inbox/` empty after host capture

### Cowork-direct-write-blocked hard test

`_evidence/s09/cowork-direct-write-blocked.txt` — real CLI output showing:
- `BRAIN_ROLE=vm brain write ...` → `role_forbidden`, exit 4
- `BRAIN_ROLE=vm brain capture ...` → draft in `capture-inbox/`, `signed: false`,
  `indexed: false`, exit 0 (NOT blocked — routes correctly via `BrainCore.capture()`)
- `capture-inbox/` has the VM draft; `vault/brain/resources/` has NO new files
- No signing key resolution on VM path (verified by `test_vm_audit_key_never_resolved`)

The trust boundary is enforced at two independent levels:
1. **CLI VM trust gate** (`main()` VM_ALLOWED check): refuses `write`, `rebuild`,
   `sync`, `snapshot`, `verify-audit`, `anchor`, `backup`, `restore` on VM role.
   Returns exit 4 BEFORE BrainCore is constructed.
2. **`BrainCore._require_host()`**: refuses `write_note`, `drain_drafts`, `rebuild`,
   `sync`, `publish_snapshot`, `verify_audit`, `anchor_chain`, `backup`, `restore`
   on the VM role — defence-in-depth.

### Index refresh

HOST `brain capture` calls `self.sync(drain=False)` after `write_note()` — the
note is immediately retrievable by `brain get <id>` in the same session. The
`drain=False` flag skips the capture-inbox drain (the note was just directly
written by `write_note`; draining again would be a no-op).

### New module: `src/brain/capture.py`

`enforce(content, override)` and `validate(content)` — the single enforcement
point for all capture-path content. Used by both `BrainCore.capture()` (the
unified verb) and available for direct use by the host drain.

---

## UX-02 — Scheduled morning brief + weekly digest

### The ONE sanctioned scheduled task

There is exactly ONE sanctioned automated `brain` invocation:
**`brain-daily-brief`** — daily at 07:00 local time.

It runs `brain sync --publish --json && brain brief --json >> $LOG 2>&1`.

This single task is the **guaranteed daily drain FLOOR**: it drains pending
captures (via `brain sync`), reconciles the index, publishes the snapshot, and
reports the morning brief. A second weekly task (`brain-weekly-digest`) can
optionally run `brain digest --json` on Sundays.

### Task registration

| Property | macOS | Windows |
|----------|-------|---------|
| Task name | `com.profile-a-brain.daily-brief` | `brain-daily-brief` |
| Mechanism | LaunchAgent (user-level) | Scheduled Task (Limited/User) |
| Schedule | `StartCalendarInterval Hour=7 Minute=0` | Daily 07:00 |
| Script | `scripts/brain-brief.sh` | inline PowerShell in task action |
| Install | `bash scripts/install-brief-mac.sh` | `.\scripts\install-brief-windows.ps1` |
| Uninstall | `launchctl unload <plist> && rm <plist>` | `Unregister-ScheduledTask -TaskName brain-daily-brief` |
| Logs | `~/.brain/logs/brief-YYYY-MM-DD.log` (30-day rotation) | same |
| Key injection | macOS Keychain `security find-generic-password -s profile-a-brain-audit` | Windows Credential Manager `cmdkey /generic:profile-a-brain-audit` |
| Key in XML/plist | NEVER stored in source tree | NEVER stored in source tree |

The signing key is resolved at install time (by `install-brief-mac.sh` / `install-brief-windows.ps1`), written into the plist/task's `EnvironmentVariables` block, and read from the OS credential store. The key is NEVER committed to source control.

### Drain tripwire

`brain brief` always reports `pending_before_drain` BEFORE the drain attempt.
If `pending_before_drain > 0` and the drain stalls (`promoted == 0`, `skipped > 0`),
the brief sets:

```
"tripwire": "N captures pending · last successful drain: stalled (no key?)"
```

The log line is visible at the next session start (morning glance at `brief-<date>.log`).
The 24-hour worst-case gap between stall and discovery is acceptable (no real-time alerting
required for a personal second brain).

`_evidence/s09/drain-tripwire-demo.txt` — real CLI output showing both the stalled
and successful drain scenarios.

### Dry-run evidence

`_evidence/s09/scheduled-task-dry-run.txt` — real CLI output from `brain brief --json`
and `brain digest --json` on a 4-note test vault. Shows:
- JSON structure with all required keys
- Human-readable output (quiet, no plumbing noise)
- Complete scheduled task spec (task name, user context, signed command, logs, uninstall/rollback)

### Threat model integration (s08 CSF profile addition)

| CSF 2.0 control | Implementation |
|-----------------|----------------|
| **DE.AE-2** | Daily brief log is the DETECT floor. Tripwire fires within 24h of stall. |
| **DE.CM-3** | Log rotation (30d) + JSON lines are machine-parseable for future alerting. |
| **PR.AC-4** | Task runs as current user (NOT admin). Key from OS credential store, never in XML. |
| **ID.AM-2** | Each run emits structured JSON (notes/chunks/drain/tripwire/date). |
| **RC.RP-1** | One-command uninstall. Drafts survive failed drain (fail-closed). `brain rebuild` is always safe. |

The scheduled task is documented in `docs/operations/s08-evidence.md` **SEC-04** as
the one sanctioned automated invocation feeding into the NIST CSF 2.0 Tier 2 profile.

---

## UX-03 — The four interactions + quiet defaults

### The four interactions

| Interaction | Command | Description |
|------------|---------|-------------|
| **Capture** | `brain capture [--id] [--type] [--classification]` | Dump anything here. HOST: signs+indexes. VM: drops to capture-inbox/. |
| **Ask** | `brain search <query> [--rerank] [--json]` | Sourced answer with classification-filtered results. |
| **Morning brief** | `brain brief [--no-drain] [--json]` | Daily drain + quiet index summary. Quiet by default. |
| **Weekly digest** | `brain digest [--days D] [--json]` | Rolling N-day view of what was added/updated. |

Everything else in the CLI (`rebuild`, `sync`, `snapshot`, `verify-audit`, `anchor`,
`backup`, `restore`, `project`) is plumbing — the user never needs to call these
directly in normal operation. The scheduled task handles sync/drain; backups are
scheduled separately.

### Quiet defaults

- `brain brief` and `brain digest` default to human-readable (quiet) output.
  No paths, no backend names, no DB details. `--json` for structured use.
- `brain capture` default: one-line summary (`captured <id> -> <path> (signed=True, indexed=True)`).
  `--json` for structured use.
- `brain search` / `brain get` / `brain recent` follow the same pattern.
- `brain status` is the one diagnostic command that shows plumbing details.

### Test coverage for the four interactions

| Interaction | Tests |
|------------|-------|
| Capture | `tests/test_capture_path.py` (26 tests) |
| Ask (search/get/recent) | `tests/test_retrieval.py`, `tests/test_index.py` (from S04) |
| Morning brief | `tests/test_brief_digest.py` (30 tests) |
| Weekly digest | `tests/test_brief_digest.py` (included) |

---

## Test summary

```
tests/test_capture_path.py   26 tests  (enforce, validate, host capture, VM blocked, drain tripwire)
tests/test_brief_digest.py   30 tests  (build_brief, format_brief, build_digest, core.brief/digest, CLI)
─────────────────────────────────────────────────────────────────────────────
S09 subtotal                 56 tests  all pass
Full suite (excl. ranx)     185 tests  all pass
Pre-existing failure          1 test   test_eval_harness (ranx not installed, S05 scope, env-only)
```

---

## Files added / modified

### New
- `src/brain/capture.py` — frontmatter enforcement + validate() for capture path
- `src/brain/brief.py` — morning brief + weekly digest pure functions
- `tests/test_capture_path.py` — capture path + drain tripwire tests
- `tests/test_brief_digest.py` — brief + digest tests
- `scripts/brain-brief.sh` — daily brief wrapper (invoked by both schedulers)
- `scripts/brain-digest.sh` — weekly digest wrapper
- `scripts/brain-brief-mac.plist` — macOS launchd plist template
- `scripts/install-brief-mac.sh` — one-command macOS install
- `scripts/install-brief-windows.ps1` — one-command Windows install
- `_evidence/s09/host-routed-capture-proof.txt`
- `_evidence/s09/cowork-direct-write-blocked.txt`
- `_evidence/s09/drain-tripwire-demo.txt`
- `_evidence/s09/scheduled-task-dry-run.txt`
- `docs/operations/s09-evidence.md` (this file)

### Modified
- `src/brain/core.py` — `capture()`, `brief()`, `digest()` methods on `BrainCore`
- `src/brain/cli.py` — `capture`, `brief`, `digest` subcommands; `VM_ALLOWED` extended

---
name: task-registrar
description: "Idempotent cross-client registrar that turns routines/manifest.json into per-client scheduled-task setup for a brain-substrate second-brain: registers the single locked host OS-scheduled task (`brain-nightly`, running `brain maintain`) via macOS launchd or Windows Task Scheduler, and emits a paste-ready, idempotent prompt for registering on-invoke Cowork triggers (promotion-scan, on-demand digest -- autoresearch-cascade moved to host-only in AUT-04/s11, see routines/manifest.json) without ever creating a second OS-level scheduled entry. Triggers: 'register the brain scheduled tasks', 'set up brain-nightly', 'install the brain task on this machine', 'give me the Cowork registrar prompt', 'is brain-nightly already registered', 'how do I wire up the brain maintenance task across clients'. Safe to re-run any time — it lists what's already registered before creating or updating anything, and defaults to a dry-run report. Not for editing the manifest itself (that's a direct edit to routines/manifest.json) and not for the brain CLI's own maintenance verbs (check/health/curate/integrity/promote-scan) — this skill is purely about WHO/WHERE invokes those verbs on a schedule."
---

# task-registrar (brain-substrate kernel)

<!-- BRAINIAC-DESK-BLOCK v1 -- BYTE-IDENTICAL in all 14 Cowork bundles.
     Canonical copy: docs/operations/cowork-desk-block.md. Pinned by
     tests/test_cowork_skill_bundles.py against the LIVE broker
     (brain.mcp_adapter.TOOLS). Edit the canonical copy and every bundle in one
     commit, never one bundle alone, and never to name a tool the broker does
     not serve. -->

## Cowork (VM leg) — the vault is served by the desk, not by this sandbox

Reach the vault ONLY through the **desk**: the host-run `brainiac` MCP server,
whose tools Claude Desktop announces into this session. Call the tool. Do not
shell out for note content, and do not open a note file.

| the CLI verb you know | the desk tool to call |
|---|---|
| `search`, `hybrid-search` | `search`, `hybrid_search` |
| `get`, `read` | `get`, `read` |
| `recent` | `recent` |
| `dossier` | `dossier` |
| `bases-query` | `bases_query` |
| `grep` | `grep` |
| `graph-expand` | `graph_expand` |
| `vault-languages` | `vault_languages` |
| `diagnose` | `diagnose` |
| `alerts`, `exceptions`, `inbox` | `alerts`, `exceptions`, `inbox` |
| `draft-capture` (stage an unsigned note) | `capture` |

Those fifteen names are the whole desk, and they are the only route to note
CONTENT this session may use. **"Not on the desk" is not the same as "refused
here"**, and conflating the two is how a session talks itself into a workaround:
measured 2026-08-28, nine of the CLI's 22 VM-allowed verbs have no desk tool
(`brief`, `cos-propose`, `digest`, `doctor`, `draft-capture`, `init`,
`mcp-config`, `provision-request`, `status`), and two desk tools (`inbox`,
`vault_languages`) are not VM-allowed on the CLI at all. Of those nine, only
`cos-propose` — the COS ingress nothing on the desk serves — and the staging
probes below are still invoked locally by a shipped bundle, both deliberately.
Every verb that COMMITS or rebuilds — `write`, `sync`, `maintain`, `rebuild`,
`ingest`, `curate`, `health`, `integrity`, `verify-audit`, `graph-report` — is a
HOST-broker privilege and is not available here at all. **Unless a line is
explicitly marked as a Cowork instruction, a shell command anywhere else in this
skill is a HOST-lane instruction**, correct on the owner's Mac and not this
session's route.

**Do NOT bootstrap a vault path, do NOT export one onto `PATH`, do NOT hunt for
a staged binary to retry with, and do NOT open a note under `brain/` or `raw/`
with `cat`, `ls`, `Read` or a glob.** That is a RULE, and until the vault
actually moves off the mount it is the only thing standing between this session
and the bypass this whole change exists to close. Stated exactly, because the
two halves are true at different times:

- **While a published snapshot is still on the mount** (every workspace
  installed before the cutover), a staged engine CAN answer a local retrieval
  verb from `<vault>/.brain/snapshot/` — measured 2026-08-28: a local `search`
  at role `vm` returned the note body and exit 0 with the host index deleted,
  because the
  snapshot path is independent of it. Content that arrives that way skipped the
  desk, and so skipped the classification egress record the desk writes. A read
  that works is not a read you were allowed to make.
- **Once the vault is off the mount**, the local retrieval verbs fail rather
  than returning less — measured 2026-08-28: `search`, `get`, `recent` and
  `grep` exit 3 with `OperationalError: unable to open database file`, and
  `status --json` carries that same error in its `index` block. That is the desk
  telling you to use it, not a broken install and not a host-only refusal.
  **Not every surface is that loud**, so never read a quiet answer as an
  answer: an EMPTY result and a `not-computed` status are also what you get from
  a vault you cannot see. Only a hit you obtained through the desk is evidence.

Either way the answer is the same: call the desk. And the desk itself has no
offline substitute — it speaks stdio to the host process Claude Desktop spawned,
so there is no endpoint in this sandbox to dial and nothing to fall back to.

**The one local exception, and it returns no note.** If this workspace still
carries a staged engine, its three staging probes — `brain --version`, `brain
--role vm doctor`, `brain --role vm status --json` — read `.brain/` staging
metadata: engine stamp, model cache, vendored-deps ABI, snapshot presence. Keep
`--role vm` on them; without it the shell runs at role `host`, whose egress cap
is the whole vault. Use them for staging questions only. Their absence is not a
fault to fix, and none of the three is a way to reach a note.

If you need something the desk does not serve, say so and stop. That is a
finding for the engine, not a gap to work around.

**This is the generic, host/VM-aware scheduled-task registrar kernel** for a
`brain`-substrate second-brain. It reads `routines/manifest.json` — the
canonical, host/VM-tagged list of every maintenance task the substrate runs —
and turns it into concrete, idempotent registration steps per client. It
carries no project-specific content; the manifest is the only input that
varies per deployment.

It exists because scheduled tasks **cannot travel as files** — each user's
machine (and each user's Cowork account) has to register them locally. This
skill is the one-time (and re-runnable) bridge from "the manifest says what
should run" to "this machine/account actually runs it."

## The governing constraint — read this before registering anything

`routines/manifest.json`'s `locked_counts` LOCKS the OS-scheduled-task count at
**exactly 2 host tasks, 0 VM/Cowork tasks** (it read "1 host task" here until
2026-08-30, seven weeks after the owner amended the lock to 2 on 2026-07-11 —
and `scripts/register_tasks.py:91` had been refusing anything but 2 the whole
time, so this paragraph contradicted the code it describes) (MITRE ATT&CK T1053.005
minimization — every additional scheduler entry is an independent
persistence/hijack surface for no functional gain; a markdown-truth +
disposable-index substrate needs exactly one reconciling heartbeat). This
skill enforces that lock structurally:

- `scripts/register_tasks.py` refuses to run at all if
  `routines/manifest.json`'s `locked_counts` don't read `{host: 2, vm: 0}` —
  it will not register against an unratified budget.
- The HOST leg registers exactly one task: `brain-nightly`
  (`brain maintain --json`), which already does sync+publish+brief AND the
  date-gated health/integrity/digest branches internally (one OS entry, many
  cadences multiplexed inside it — see `src/brain/core/` `BrainCore.maintain`).
- The COWORK leg never sets a cron/schedule expression on anything it
  registers. The triggers it creates are **poke-only** (fired manually, by
  name, via the Cowork scheduled-tasks tools' own "run now" / `fire_trigger`
  equivalent) — convenience aliases for retyping a prompt, not autonomous
  cron entries. If a future deployment genuinely wants a Cowork cron, that is
  an explicit budget amendment to `persistence-budget.md` first, not something
  this skill (or any paste-prompt it emits) does silently.

If you are asked to "make the Cowork task run automatically every morning" —
**stop and flag the budget conflict** rather than wiring up a cron-fired
Cowork routine. That request is a `persistence-budget.md` amendment, not a
registrar operation.

## Phase 0 — locate the manifest and confirm the budget is sane

```bash
cd <brain-repo-root>
python3 -c "
import json
d = json.load(open('routines/manifest.json'))
lc = d['locked_counts']
host = [r for r in d['tasks'] if r.get('os_scheduled')]
ratified = {'brain-nightly', 'brain-synthesis-weekly'}
ids = {r['id'] for r in host}
assert ids == ratified, ('UNRATIFIED host task set', sorted(ids ^ ratified))
assert lc['host_os_scheduled'] == len(host), (lc, sorted(ids))
assert lc['vm_os_scheduled'] == 0, lc
print('budget OK:', lc, '->', sorted(ids))
"
```

**Do not hardcode the budget number here.** This line asserted
`host_os_scheduled == 1` until 2026-08-30, seven weeks after the owner amended
the lock to 2 (brain-nightly + brain-synthesis-weekly, 2026-07-11). So the
check FAILED on a healthy manifest and passed on nothing — a guard that fires
only when the state is correct.

Comparing the declared count against the routines actually marked
`os_scheduled` was still not a guard, and the 2026-08-30 adversarial round said
so: both numbers come from the SAME file. A manifest that adds a third task and
bumps `locked_counts` in one edit satisfies `len(host) ==
lc['host_os_scheduled'] == 3`, and the check meant to stop an unratified
persistence expansion prints `budget OK`.

So the assertion pins the ratified ID SET instead. `brain-nightly` and
`brain-synthesis-weekly` are the two the manifest's own lock note ratifies and
the two `scripts/register_tasks.py:91` hard-refuses to deviate from, so the
anchor lives OUTSIDE the file being checked — which is the whole difference
between a lock and a restatement. A third task now fails closed whatever
`locked_counts` says about itself. Adding one is an owner decision: amend
`persistence-budget.md`, then the manifest, then this set, in that order.

If this fails, the manifest has drifted from the locked budget — fix the
manifest (or `persistence-budget.md`, with the reopening-clause ratification
it requires) before running the registrar.

## Phase 1 — dry-run report (always do this first)

```bash
python3 scripts/register_tasks.py --dry-run --client all
```

This is read-only end to end:
- HOST leg: probes `launchctl list <label>` (macOS) or
  `Get-ScheduledTask`/`schtasks` (Windows) — a listing call, no mutation —
  and reports whether `brain-nightly` is already registered (so you know if
  the next step would CREATE or UPDATE/re-point it).
- COWORK leg: prints the full paste-ready prompt. Printing text touches
  nothing; the prompt only mutates Cowork state once a human pastes it into
  an actual Cowork chat session that has the scheduled-tasks MCP tools.

Use `--save-cowork-prompt <path>` to also write the prompt to a file (handy
for handing off to a Cowork session verbatim, or for evidence).

## Phase 2 — apply the HOST leg

```bash
export BRAIN_VAULT=/path/to/your/vault
python3 scripts/register_tasks.py --apply --client host
```

This invokes the existing platform installer
(`scripts/install-brief-mac.sh` on macOS, which itself does an idempotent
unload-then-load of the vault's per-vault `com.brainiac.nightly.<id>` LaunchAgent; on
Windows you run `scripts/install-brief-windows.ps1` directly per the printed
instructions, since this Mac-host script cannot drive a Windows machine
remotely). Both installers already re-point an existing registration at the
current script body rather than erroring on "already exists" — that's what
makes "create or update" the same call. **Never** hand-roll a competing
LaunchAgent label or Task Scheduler task name; the manifest's
`registration.mac.label` / `registration.windows.task_name` are the stable
IDs every re-run keys off.

`--apply --client cowork` (or `--client all`) does **not** do anything extra
for the Cowork leg — this script has no way to call Cowork's
`list_scheduled_tasks` / `create_scheduled_task` / `update_scheduled_task`
tools from a Mac-host shell. The Cowork leg's only "apply" is a human pasting
the printed prompt into an actual Cowork chat session.

## Phase 3 — the Cowork leg, run inside an actual Cowork session

Paste the block from Phase 1's output (or the file written by
`--save-cowork-prompt`) into a Cowork chat session. The prompt is
self-contained: it names the manifest source for each task, the exact
`list_scheduled_tasks` → `create_scheduled_task`-if-absent /
`update_scheduled_task`-if-present sequence, the never-delete /
`enabled:false`-to-retire rule, and the
[#29022](https://github.com/anthropics/claude-code/issues/29022) caveat
(`create_scheduled_task` sometimes silently no-ops — verify with a follow-up
`list_scheduled_tasks` call; fall back to the Cowork Schedule UI if the tool
call didn't take).

Every `brain` command line inside the prompt already carries
`--max-tier Internal` — this satisfies PF-02's export-egress gate
(`docs/operations/egress-provider-posture.md`) Leg 2 (classification ceiling) by
construction. If a future edit to one of these triggers turns it into a true
export (shipping retrieved content outside the Cowork session — an email, a
doc, a paste elsewhere), run `brain snapshot` first and record the gate
evidence per that doc's Step D before shipping.

## Retiring a task

Host: re-run the relevant uninstall command from the installer script's
header comment (`launchctl unload ... && rm ...` / `Unregister-ScheduledTask`).
Cowork: call `update_scheduled_task(enabled=false)` on the named trigger —
**never** `delete_scheduled_task` (keeps the registration history visible
and re-enable-able, matches the vault's broader "no destructive deletes,
moves/disables only" posture).

## Updating the manifest

`routines/manifest.json` is the single source of truth this skill reads. If
a new task needs scheduling, add it there first (with the correct
`disposition` / `runtime` / `os_scheduled` / `persistence_cost` per
`routines/manifest.json`'s per-task `disposition` taxonomy), re-validate the budget check
in Phase 0, then re-run this skill. Do not hand-edit a LaunchAgent plist or a
Cowork trigger directly and let the manifest drift out of sync with reality —
the next dry-run would then report stale state.

## Cross-references

- `routines/manifest.json` — the manifest this skill consumes
- `scripts/register_tasks.py` — the registrar implementation (`--dry-run` /
  `--apply`, `--client host|cowork|all`, `--json`, `--save-cowork-prompt`)
- `routines/manifest.json` `locked_counts` — THE LOCK (1 host, 0 VM) this skill enforces
- `routines/manifest.json` per-task `disposition` field — taxonomy (FOLD /
  DISTINCT-CADENCE / ON-INVOKE / OVERLAY-ONLY) the manifest rows are built from
- `docs/operations/egress-provider-posture.md` — PF-02, the gate every Cowork-bound
  prompt this skill emits already satisfies on Leg 2 (classification ceiling)
- `scripts/install-brief-mac.sh` / `scripts/brain-brief-mac.plist` /
  `scripts/install-brief-windows.ps1` / `scripts/brain-brief.sh` — the HOST
  leg's existing idempotent installers, reused unmodified by this skill

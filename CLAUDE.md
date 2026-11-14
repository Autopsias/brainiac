# CLAUDE.md — Profile A `brain` (Claude Code + Claude Desktop Code tab)

@AGENTS-core.md

> **AGENTS.md is canonical; AGENTS-core.md is its ≤200-line Claude Code core.**
> The rest of AGENTS.md loads from `.claude/rules/*.md` when a matching file is
> touched. Do not duplicate content across the three: edit `AGENTS.md` first,
> then mirror the change into the split. How each harness (Codex, Gemini CLI,
> the Desktop Code tab) reads it: `docs/adr/0012-agents-md-fits-every-harness.md`.

## Brain usage (one paragraph)

Retrieval, capture, and indexing are owned by the **`brain` CLI** — call it from
your native shell, **never via MCP**. Read tools: `brain search "<q>" --json`,
`brain get <id> --json`, `brain recent --json`, plus `grep` / `bases-query` /
`graph-expand` (compose them; lexical-first, embed lazily). Every read applies the
**classification filter** before stdout (unlabelled ⇒ ranked MNPI; host
default cap = full vault, `--role vm` default = Internal; narrow with
`--max-tier` / `$BRAIN_DEFAULT_MAX_TIER`). Capture with
`brain draft-capture` (stages a draft; the host signs + indexes it later). Run
`brain --help` for the always-current contract. On the **Cowork Linux VM** add
`--role vm` (or `export BRAIN_ROLE=vm`): a read + draft surface that reads only
the published read-only snapshot and never signs — see
`docs/cowork-windows-install.md`.


## Standing rulings and traps from closed plans

The history of each closed plan lives in `docs/closed-plans-history.md`; only the rulings and traps a later session must not miss stay here.

**The historical backfill is FINISHED — do NOT re-run it, and do not re-run `~/Desktop/run-s02-backfill.command`.** **Owner ruling 2026-09-16: a dated `attachment-gone` and a live-probed `item-attachment` are TERMINAL** — neither is a pending state and both are re-decided every run — and building an item-attachment fetch path was declined (at most 3 files, only if Outlook exposed bytes it has already refused four times). Owner choice 2026-09-14 still stands: released body-error threads are cleared from the Inbox BY HAND; do not re-raise.

**Both live indexes read format 5 since 2026-09-11 17:50 — the rebuild window is
DONE, do not redo it.** Read it back from `brain --vault <v> status --json`
(`index.schema_version` = `5` on both vaults), never from here.
**`sync` upgrades a format-4 index in place** (`b11124c2`); no rebuild is needed.

One Email, One Note duplicate trap: If a later night files a copy for an unchanged thread,
`tools/cos_bridge_retire_duplicates.py --dry-run` then `--apply` resets the
count; it is reversible with `--undo`.

Nightly diagnostic: If a night reports
zero threads, read `~/.brain/logs/cos-nightly-launchd.out` for that line before
looking anywhere else.

**The read-back job is LOADED and armed — 2026-09-06, do not redo this.** Read
it back from `launchctl print gui/<uid>/com.brainiac.cos-readback`, never from
the file. The two checks: `tools/cos_nightly_schedule_check.py --label com.brainiac.cos-readback --hour 7
--minute 0` and the same for `cos-nightly --hour 2 --minute 0`.

Install trap: with `BRAIN_VAULT` unset the installer REFUSES (exit 2) and
leaves nothing behind. **If you ever render these jobs, name the vault:**

    BRAIN_VAULT=$HOME/DeveloperFolder/<name>/vault \
    COS_PYTHON=$HOME/.brainiac/venv/bin/python \
    bash tools/launchd/install-cos-jobs.sh

It renders THREE jobs (nightly, read-back, remind) — read all three back from
`launchctl print`. It is NOT `tools/launchd/reload-cos-nightly.sh` (below).

**Owner ruling 2026-09-04: the P0 exemption is retired.** A thread with no action
for him is archived whatever its priority, P0 included. Read it back, not from here:
`git show master:tools/cos_judge_rules.py | grep -c "NEVER auto-archived"`
returns **0**. `triage.p0_never_noise` stays.

**The nightly job already runs `tools/cos_nightly.sh` at 02:00 (done 2026-09-05)
— do not redo this.** Read
back from `launchctl print`, never from the file: program
`.../profile-a-brain/tools/cos_nightly.sh`, schedule `{Hour 2, Minute 0}`,
state `not running` (correct — `RunAtLoad` is false).
**`tools/launchd/reload-cos-nightly.sh` reloads the job only; `--kickstart` also
fires a real night against the live mailbox.** To swap a path without running
anything, run it with no argument or use `bootout` then `bootstrap` by hand.

**Required status checks on the public `main` are CLOSED: the owner declined them on 2026-09-05. Do not re-raise them.**

*Why:* nothing merges into `Autopsias/brainiac`'s `main`. Every commit in its history is a release squash that `tools/publish_public_uploads.py:259` pushes directly (`git push origin main`), and every pull request against `main` — ten of them, all Dependabot — was closed unmerged. Required status checks would therefore gate a path nobody uses and **block the only path anybody uses**. **If you ever want the guarantee, the fix is to move releases to a pull request — not to switch `enforce_admins` off.**

**The public `audit` check is red, and that is also closed — 2026-09-05, owner's call.** `pypdf==6.15.0` in the mirror's `requirements.lock` carries CVE-2026-84309/84310/84311, all denial-of-service, fixed in 6.16.1. **No user is exposed**: `pyproject.toml` declares `pypdf>=6.15.0`, a FLOOR. Cutting a release to clear a lockfile was rejected as disproportionate.

**M-2, the `.claude/settings.local.json` hand-edit, is DONE as of 2026-09-05 — do not re-raise it.** **A worktree reads `unmanaged`, not `warn`**, because it carries no `.claude/settings.local.json` — check the row from the main checkout or you will read a clean row from a file that is not there. And the file is **gitignored**, so this edit is invisible to every test, CI leg and fresh clone, and reverts on a new machine; the non-gating doctor row is the only surface that can report it.

**The commitment-judging revision loop is PAUSED: the meeting corpus cannot support another revision.** **The 8 meetings named in the report below cannot be repaired** —
none of their source audio is on this machine and none has a matching embedding sidecar. `96eef39` stands
and nothing is deployed. Report: `docs/operations/commitment-judging-2026-09.md`.

See `_plans_index.md` for all other plans (complete, halted-pending-closure, or
awaiting owner review — the index carries per-plan status).
Closed-plan history lives in `docs/closed-plans-history.md`; for plan status, read `_plans_index.md`.

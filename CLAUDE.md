# CLAUDE.md — Profile A `brain` (Claude Code + Claude Desktop Code tab)

@AGENTS-core.md

> **AGENTS.md is canonical; AGENTS-core.md is its ≤200-line Claude Code core.**
> Codex, Gemini CLI, and the Desktop Code tab all still read the full,
> unmodified `AGENTS.md` — that stays ONE source of truth for the note shape,
> link style, capture rules, the four interactions, and the security posture.
> Claude Code alone imports the smaller `AGENTS-core.md` instead, to keep base
> context down; the rest of AGENTS.md's content loads CONDITIONALLY from
> `.claude/rules/*.md` (path-scoped — see AGENTS-core.md's pointer table) only
> when a matching file is touched. Do not duplicate content across
> `AGENTS.md`/`AGENTS-core.md`/`.claude/rules/`; edit `AGENTS.md` for the
> canonical text, then mirror any structural change into the split (context
> diet, s05, 2026-08-22).

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

**One Email, One Note closed and LANDED 2026-09-11** as `518bc8c1` — 7 of 7
sessions DONE, 11 of 11 items DONE. The acceptance review scores **4 ACHIEVED,
0 GAP, 3 NOT-YET-MEASURABLE**:
`_plans/one-email-one-note-2026-09-10/_evidence/one-email-one-note/acceptance-review.md`.
The three unmeasured criteria (a scheduled night that settles `already-ingested`
and files no copy, the email header on the LIVE index, and the body budget on the
newest corpus) wait for two things only: the owner's rebuild window, and a night
that actually judges threads — the last two nights judged 0 (session died, then
not signed in). The land also carries `fix/grep-retired-and-fold-reindex`, which
closes the review's finding 1: `grep` now hides retired versions unless
`--include-retired`, and every row carries `is_latest_version`. The owner skipped
the review's recommended `ultrareview` before the merge. Suite on the merged tree:
**7046 passed, 5 skipped, 0 failed**; ratchets 0 BLOCKING on all three; ruff
clean. `run.py land` parked `default-unresolved` as always; the hand merge is in
the plan's `LAND_NOTICE.txt`.

**The index format on `master` is now 5, and both live indexes stay 4 until the
owner rebuilds them.** Nothing rebuilds by itself yet: the hourly jobs run the
engine installed in `~/.brainiac/venv`, which stays the old one until
`pip install --force-reinstall --no-deps` of this checkout (the version number did
not change, so a plain upgrade skips it). The installed engine's `sync` rebuilds a
format-4 index the moment it is the new code, so the install IS the start of the
rebuild — do it only in the window, with the two hourly nightlies and
`com.brainiac.cos-nightly` booted out. The COS night runs this checkout's code
directly; the new write path degrades on a format-4 index rather than failing, and
it is NOT measured whether that night reaches `sync`, so keep it booted out until
`brain --vault <v> status --json` reads `index.schema_version` = `5` on both
vaults. **Re-dispatch s07 after the rebuild and the first judged night on the
merged code (earliest 2026-09-12).**

**The Porter Finishes closed and LANDED 2026-09-06** as `5ddf5480` — 12 of 12
sessions terminal, 16 of 16 items DONE. The acceptance review scores its seven
criteria: **1 ACHIEVED** (the nightly armed at 02:00, its literal "deploy
worktree" wording superseded by the owner's 2026-09-05 re-arm), **2 ACHIEVED on
substance but NOT-YET-MEASURABLE over its seven-night span**, **3 split**
(forward lane NOT-YET-MEASURABLE at denominator 0, historical backfill GAP with
2 of 17 still fetchable), **4 GAP**, **5 split** (discard half ACHIEVED,
ledger/validity half GAP), **6 and 7 NOT-YET-MEASURABLE**. Read the review
before you act on any of those words:
`_plans/porter-finishes-2026-09-04/_evidence/porter-finishes/acceptance-review.md`
(22 transcripts and re-runnable scripts under `_evidence/porter-finishes/s12/`).
`run.py land` PARKED with `kind: default-unresolved`, as it does for every plan
here — the sole remote is the push-disabled public export and carries no
`master` — so the merge was by hand and is recorded in the plan's
`LAND_NOTICE.txt`. Suite on the merged tree: **6325 passed, 5 skipped, 0
failed**; ratchets 0 BLOCKING on all three; ruff clean.

**Three of the seven criteria are CALENDAR gaps, not defects, and the plan must
not be read as failing on them.** Criterion 2 measures 125 of 125 act+relevant
rows accounted, but only across four ATTENDED runs; seven SCHEDULED nights on
the landed code settle it no earlier than **2026-09-13**. Criterion 6 (a
scheduled morning read-back consuming marks on three different days) settles no
earlier than **2026-09-09** — the job is loaded, so it now needs only three
mornings on which a marks file is actually saved. Criterion 7
needs one live night with Pen 3 armed. **Re-dispatch s12 on or after 2026-09-13
to settle them** — the predecessor plan closed with three promises unmet and
forgotten, which is exactly what that re-dispatch prevents.

**Two real GAPs stand.** The Inbox is not near zero: 70 of 107 threads on
run264 carry a latest verdict of `read` or `noise`. And 16 historical runs hold
an ingestion ledger with no metrics row or no validity file. The discard half of
that criterion is ACHIEVED — zero rows sit in state `unknown`.

**Two measurements OVERTURNED stale evidence, so do not quote the older files.**
`attachment-backfill-verified.json` says `signed 0, awaiting_acceptance 23`;
re-running `backfill verify` on 2026-09-06 reads **`signed 23, awaiting 0`**, so
s07's ingest-bridge residual no longer reproduces on that population. And s08's
one stuck discard identity IS closed — run263's undo ledger carries the terminal
`aborted-not-applied` row, and the replay now reads `would_close 0`.

**The scheduled night of 2026-09-06 DIED at 02:21** — `batch_stop: session-died`,
"stale open proposal batch remains after audited recovery", `judged_conversations
0`, and an empty morning sheet. The cause was an open proposal batch this plan's
own evening runs left behind. It cleared on the 08:07 run. If a night reports
zero threads, read `~/.brain/logs/cos-nightly-launchd.out` for that line before
looking anywhere else.

**The read-back job is LOADED and armed — 2026-09-06, do not redo this.** Read
it back from `launchctl print gui/<uid>/com.brainiac.cos-readback`, never from
the file: program `~/.brainiac/venv/bin/python`, script
`.../profile-a-brain/tools/cos_sheet_readback.py`, schedule `{Hour 7, Minute 0}`,
state `not running` (correct — `RunAtLoad` is false), runs 0. s01's
parameterised checker passes LIVE on both jobs:
`tools/cos_nightly_schedule_check.py --label com.brainiac.cos-readback --hour 7
--minute 0` and the same for `cos-nightly --hour 2 --minute 0`.

**The render that installed it also broke the nightly plist, silently, and that
is now guarded (`55f5bddb`).** `install-cos-jobs.sh` read `BRAIN_VAULT` from the
shell; with it unset the script fell through to
`$HOME/DeveloperFolder/Brainiac/vault`, CREATED that directory, and rendered all
three jobs against it — rewriting a working nightly away from the live vault and
swapping `COS_PYTHON` from the brainiac venv to Homebrew's `python3`. Nothing
failed and nothing warned, because launchd keeps serving the loaded job from
memory; the only surface that could show it was comparing `launchctl print`
against the file. An unset `BRAIN_VAULT` now REFUSES (exit 2) rather than
inventing a vault, and leaves nothing behind. **If you ever render these jobs,
name the vault:**

    BRAIN_VAULT=$HOME/DeveloperFolder/<name>/vault \
    COS_PYTHON=$HOME/.brainiac/venv/bin/python \
    bash tools/launchd/install-cos-jobs.sh

It renders THREE jobs now, so it rewrites the nightly and remind plists too —
read all three back from `launchctl print`. It is NOT
`tools/launchd/reload-cos-nightly.sh`, which ends in `launchctl kickstart` and
fires a real night against the live mailbox.

**Owner ruling 2026-09-04: the P0 exemption is retired.** A thread with no action
for him is archived whatever its priority, P0 included. **s07 lifted every site
and it reached `master` on 2026-09-06** — this file said four sites; s12 counted
six, all now routed through one function. Read it back, not from here:
`git show master:tools/cos_judge_rules.py | grep -c "NEVER auto-archived"`
returns **0**. Until the 2026-09-06 land, that string was still verbatim on
`master`, which is what the scheduled 02:00 night runs, so every P0 archive was
silently dropped on every scheduled night for two days.
`triage.p0_never_noise` stays.

Read `_plans_index.md` and run `run.py status` before acting on this line.

**The Night Porter closed 2026-09-04** with 13 of 13 sessions terminal and its
acceptance review at 3 ACHIEVED / 3 GAP. The owner closed it with the three
promises unmet and named, and made them the opening items of the next plan:
(1) **attachment bytes never reach the vault** — the chip lane is clean
(210/210 chips carry an undo row with a per-step receipt, 85/85 `Ingested`
chips re-verify under both fix-03 clauses), but a chip certifies a signed note
for the thread's TEXT; of 26 chipped threads carrying real attachments only 3
join their bytes to a signed note, and 32 of 45 attachment filenames appear
nowhere under `raw/originals` — the one gap with a code-shaped cause;
(2) **the Outlook move-back lane has never minted a ruling** — it ran on 59 of
60 runs and classified all 405 returning threads `new_work`, correctly per
FB-02, because the lane can only fire on a reversal with no newer message and
this owner has never produced one (a specification finding, not a bug); and
(3) **the inbox is not zero** — 109 threads, 40 with a latest verdict of
`read`, 40 act threads with neither a draft nor a hold. Holds did NOT rise
(63 → 29). Review:
`_plans/night-porter-2026-08-25/_evidence/night-porter/acceptance-review.md`.
`run.py land` cannot complete here (`kind: default-unresolved` — the sole
remote is the push-disabled public export and carries no `master`), so the
merge is by hand; the plan branch has `master` merged into it and the full
suite on that tree is green (6494 passed, 5 skipped, 0 failed).
**DONE 2026-09-05 — do not redo this.** The nightly ran from an ad-hoc
detached worktree (`.claude/worktrees/cos-nightly-deploy` at `64be9267`,
9 commits behind). The owner re-rendered both launchd jobs from the main
checkout with `tools/launchd/install-cos-jobs.sh` and reloaded by hand. Read
back from `launchctl print`, never from the file: program is now
`.../profile-a-brain/tools/cos_nightly.sh`, schedule `{Hour 2, Minute 0}`,
state `not running` (correct — `RunAtLoad` is false). `cos-remind` re-rendered
byte-identical. **Trap: `tools/launchd/reload-cos-nightly.sh` ends with
`launchctl kickstart` and fires a real night against the live mailbox; the name
does not say so. To swap a path without running anything, use `bootout` then
`bootstrap` by hand.**

**Security Follow-up 2026-09 closed 2026-09-04** and is on `master` as `00360450` (15 sessions, 16 items, all DONE). Suite at the merge: 6010 passed, 0 failed. The automated `land` cannot run in this repo — the sole remote is the push-disabled public export, which has no `master` — so the merge was done by hand and recorded in the plan's `LAND_NOTICE.txt`. Acceptance review: `_plans/security-followup-2026-09-01/_evidence/security-followup/acceptance-review.md` — four of six criteria ACHIEVED, one DEFERRED by s05's own NO-GO, and one that was PENDING on Require-status-checks for the public `main` — **which the owner CLOSED on 2026-09-05 by declining it. Do not re-raise it.**

**Why he declined, because the reason is a fact about this repo and not a preference.** Nothing merges into `Autopsias/brainiac`'s `main`. Every commit in its history is a release squash that `tools/publish_public_uploads.py:260` pushes directly (`git push origin main`), and every pull request against `main` — ten of them, all Dependabot — was closed unmerged. Required status checks would therefore gate a path nobody uses and **block the only path anybody uses**: GitHub's rule is that a required check must read `successful`, `skipped` or `neutral` before a change reaches a protected branch, a freshly pushed commit carries no status at all, and that repo runs `enforce_admins: true` so the owner cannot bypass it for a release. Read in GitHub's documentation and in the pipeline's own code; NOT tested live, because the only live test is to enable it and attempt a release. The remaining protections stay on and were re-read from `gh api repos/Autopsias/brainiac/branches/main/protection` that day: `enforce_admins`, `required_linear_history`, no force pushes, no deletions. **If you ever want the guarantee, the fix is to move releases to a pull request — not to switch `enforce_admins` off**, which trades a protection that works for one this repo cannot use.

**The public `audit` check is red, and that is also closed — 2026-09-05, owner's call.** `pypdf==6.15.0` in the mirror's `requirements.lock` carries CVE-2026-84309/84310/84311, all denial-of-service, fixed in 6.16.1. **No user is exposed**: `pyproject.toml` declares `pypdf>=6.15.0`, a FLOOR, so an install resolves to the patched release; only the mirror's own CI pins the old one. This repo has pinned `pypdf==6.17.0` since `f76cd884`, so the next release carries the fix across and the check goes green with no action. Cutting a release to clear a lockfile was rejected as disproportionate — `tools/publish_public.py` is a full PyPI-and-npm release whose irreversible steps stop at gates only the owner can type through.

**M-2, the `.claude/settings.local.json` hand-edit, is DONE as of 2026-09-05 — do not re-raise it.** Both halves. The guard half had in fact been applied earlier and this file recorded it as missing until that day; the allow-list half the owner authorized in session and the assistant applied. Read back from the REAL command, `brain doctor` run in the main checkout, not from this line: `✅ SEC-07 guard wiring (repo-local settings) — current — no broad interpreter allow, brainiac-egress-guard.sh registered on PreToolUse`, and `✅ PreToolUse egress guard (SEC-07) (~/.claude) — current`. What was removed: the single line `"Bash(python3 *)"`, taking the repo allow-list from 30 entries to 29 — a broad interpreter allow runs arbitrary code with no prompt, around every PreToolUse guard the same file registers. The spec is `_plans/security-followup-2026-09-01/_evidence/security-followup/m2-settings-edit.md`; this file pointed at `_evidence/security-followup/m2-settings-edit.md`, which does not exist. Two things a later session needs. **A worktree reads `unmanaged`, not `warn`**, because it carries no `.claude/settings.local.json` — check the row from the main checkout or you will read a clean row from a file that is not there. And the file is **gitignored**, so this edit is invisible to every test, CI leg and fresh clone, and reverts on a new machine; the non-gating doctor row is the only surface that can report it. `clean` is no longer a provenance claim (A-12).

Commitment Judging closed 2026-09-03 and is on `master` as `ea9556f5`. SEVEN rounds of judging have
run, and **round 7 ended the question by measuring the thing five rounds never measured: recall.**
Revision 5 caught 16 commitments over 8 meetings and MISSED an estimated 181 — **recall 8.1%,
95% range [5.5%, 13.9%], and that is a CEILING because it credits every staged row as correct.**
Measured against pass B, a plain reader with no rulebook, so it is independent of the doctrine:
of 20 owner rulings on a uniform sample of the 452 pass-B rows revision 5 did not stage, 8 were real
commitments. **The precision gains of revisions 4 and 5 were bought by staging less** — revision 4
staged 39 of 265 turn pairs, revision 5 staged 16 of 294. A doctrine that catches one commitment in
twelve cannot deploy at any precision, so the precision census was not run (pooled n would have been
26, which clears 0.2 only at 0 or 1 errors).

The earlier arc still stands: rounds 1-2 were under-powered; round 3 recovered 8 meetings a
diarization artifact had excluded and measured 0.474 (9/19); revision 3 measured 0.727 (8/11)
held-out; revision 4 changed the UNIT to a turn pair and measured 0.282 (11/39); revision 5 closed
three defects and measured 0.100 (1/10) like-for-like against revision 3's 0.727 on the same 9
meetings. Three wording revisions made precision worse; changing the unit made it better.

**The revision loop is PAUSED, because the corpus cannot support a revision 6.** All 8 meetings
collapse two or more diarized speaker slots onto ONE name (one puts five slots on a single name);
67 slots resolve to 50 distinct labels and 28 of 67 are unnamed — yet all 8 carry `validation: pass`
and `speaker_resolution: owner-verified`. All eight extraction agents reported this independently.
Revision 5's `no-promisor` rule fires on an UNNAMED promisor and cannot fire on a WRONGLY NAMED one,
so rows pass every rule carrying attribution nobody can check. **The transcription pipeline's
speaker validation could not detect a collapse, so it had never reported one — a pipeline defect, not
a doctrine defect. FIXED 2026-09-04** in the separate Transcriber repo (`ade8469`): a D-05 detector
(`transcriber/validate.py detect_shared_name`) reports one real name holding two or more diarized
slots, graded HIGH when that name also self-addresses and MEDIUM otherwise, plus a distinctness rule
in the auto-resolver prompt. It REPORTS rather than refuses, because diarization does legitimately
split one person across slots. All 8 judging transcripts now exit 1 (7 of them exited 0 before);
3 transcripts with no repeated name stay silent. **The 8 meetings themselves cannot be repaired** —
none of their source audio is on this machine and none has a matching embedding sidecar. Loosening the doctrine against this corpus would fit a transcription artefact
rather than predict. One related suspicion was checked rather than assumed: 20 of 51 `duplicate`
drops repeat verbatim, but 18 sit in one meeting with an ASR loop; outside it 2 of 30, so that rule
is sound. Nine further revision-5 defects are open, four re-reported from round 6. `96eef39` stands
and nothing is deployed. Report: `docs/operations/commitment-judging-2026-09.md`; numbers in
`_plans/commitment-judging-2026-09-03/_evidence/judging/round6/rates.json`.

One Command per Vault closed 2026-08-31 and is on `master` as `c8d97ea`: one
host command, `brain provision-local <vault> --workspace <dir> --model-dir
<model>`, performs all six wires and is convergent — a second run changes
nothing, and a run on a half-wired vault repairs only what is missing.
`brain doctor` carries a non-gating `vault wiring` row per registered vault, and
`brain alerts` surfaces the count. Closed Stacks landed 2026-08-30.

Deliverables Shelf (closed 2026-08-24, merged as `e24d251`), Self-Healing Vault
(closed 2026-08-22) and Corpus Invariants (closed 2026-08-12) are complete. The
shelf itself is live: a generated folder OUTSIDE the vault
(`<vault>/../brain-deliverables`, [ADR 0010](docs/adr/0010-deliverables-shelf-outside-the-vault.md))
holding the latest version of every produced output, grouped by project and
refreshed by the nightly fold — see `docs/deliverables-shelf.md`. Known cosmetic
gap the owner deferred: the drop lane titles its anchor from the source filename,
so 41 of the 117 shelf rows read as filenames.

See `_plans_index.md` for all other plans (complete, halted-pending-closure, or
awaiting owner review — the index carries per-plan status).

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

**Active plans:** [The Night Porter](_plans/night-porter-2026-08-25/PLAN.html) · 10 sessions · run via `/plan-execute _plans/night-porter-2026-08-25` (built 2026-08-25, hardened 2026-08-25). Run `run.py status` on a plan before acting on this line.

**Security Follow-up 2026-09 closed 2026-09-04** and is on `master` as `00360450` (15 sessions, 16 items, all DONE). Suite at the merge: 6010 passed, 0 failed. The automated `land` cannot run in this repo — the sole remote is the push-disabled public export, which has no `master` — so the merge was done by hand and recorded in the plan's `LAND_NOTICE.txt`. Acceptance review: `_plans/security-followup-2026-09-01/_evidence/security-followup/acceptance-review.md` — four of six criteria ACHIEVED, one DEFERRED by s05's own NO-GO, and one **PENDING on two owner-only actions that are still open**: the hand-edit in `_evidence/security-followup/m2-settings-edit.md` (the outbound term guard is installed in the engine and `brain doctor` reports `guard_registered: false` on this machine — it is NOT running) and Require-status-checks on the public `main`. The engine may never write a permission file, so the first one is the owner's. `clean` is no longer a provenance claim (A-12).

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

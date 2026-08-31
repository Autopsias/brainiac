# CUT-03 — the 14-row walk record

**What this file is.** The walk protocol §4 requires one row per skill. This is
that record. `docs/operations/closed-stacks-s07-cutover-evidence.md` §6 cites
it, and closed-stacks s09 acceptance criterion 2 reads it.

**Its evidential class, stated once and plainly.** This is a RELAYED
TRANSCRIPT, not a captured log. The walk ran inside a Cowork cloud Linux
container that this host cannot reach — no `/Users`, no shared filesystem, no
way for the executing session to read the sandbox's stdout. The owner ran each
skill by hand and pasted its output back; the executing session scored it
against the criterion and recorded the outcome. Nothing here was machine-
captured from the VM, and it must never be cited as though it were.

## WHAT THIS RECORD DOES NOT CONTAIN

Read this before the verdict. The protocol's §4 handback asks for five fields
per row. This record carries three of them in full and one in part:

| §4 field | present? |
|---|---|
| skill | yes |
| invocation issued | yes |
| WORKING / BROKEN | yes |
| executed bundle sha | **NO — the STAGED sha from §1 is given instead** |
| output summary | **NO — absent for every one of the 14 rows** |

Plus, at the document level: the §2 verdict and the §1 mismatch list are **not
reproduced here**; §2 is recorded in the evidence document, and §1 recorded no
mismatch.

Neither gap is repairable from this host, and neither was invented to look
repaired. The relayed pastes were scored in-session and not retained verbatim,
so there is no output summary to transcribe; and nothing on the host can read
what the sandbox actually executed, so the sha below is what the installer
STAGED, not proof of what ran. Those are different claims. A reviewer asking
"could a stale bundle have produced these passes?" must answer NO from §2's
staging check, not from this column.

**What IS machine-checkable, and where.** The criterion and sha columns below
are extracted from the walk protocol's own §1 and §3 tables rather than
retyped, and `tests/test_evidence_test_counts_are_real.py` asserts that this
record still carries one row per skill the protocol names, in the protocol's
order. The host-side numbers this walk produced (34 Markdown files on the
mount, 27 containment probes, 853 MB) each name their command in the evidence
document and re-run on the host. The three engine defects the walk surfaced
each carry a test with a known-negative probe.

**Verdict: 14 exercised, 14 working, 0 broken.** Twelve of the fourteen
criteria were WRONG as authored — they tested the host's world, not the VM's —
and were corrected during the walk. That rewrite is load-bearing on the
verdict, so §6.5 of the evidence document states it, and each corrected row
below is marked. A reader who distrusts a criterion rewritten by the same
session that scored it should read those twelve rows as "criterion contested",
not as "skill proven".

---
## Row 1 — `vm-doctor`

- **Invoked by:** run vm-doctor
- **Verdict:** WORKING
- **Staged bundle:** VERSION 0.20.32, `SKILL.md` sha256 `c674eedd879b2b91` (from §1 — STAGED, not executed)
- **Output summary:** NOT RECORDED — see "What this record does not contain"
- **Criterion rewritten during the walk:** YES — see §6.5
- **Criterion as scored** (live text of the walk protocol §3 row, extracted not retyped):

  **CORRECTED 2026-08-30.** The three staging probes do NOT run, and that is the pass. The installer stages the engine at `<vault>/.brain/brain` and never on `PATH`; the only thing that ever put `brain` on `PATH` was the per-session bootstrap the skill deleted on 2026-08-28. So `command -v brain` finding nothing is the NORMAL staged state, not evidence of an unstaged workspace. WORKING = the skill reports a NON-`yes` VERSION-READY verdict — its own report format at `vm-doctor/SKILL.md:216` is `VERSION-READY: yes|no`, so a session following its contract prints `no`; this row said `unknown` until 2026-08-30, which is a value the skill never emits and would have marked a compliant session BROKEN — says verbatim that no `brain` is on this session's `PATH` and that staging cannot be confirmed from here, falls through to the desk digest, and points the owner at the host for the version. BROKEN = it hunts for the binary under the mount, exports a `PATH`, bootstraps a vault path, or reports "no staged engine".

## Row 2 — `brain-inbox`

- **Invoked by:** what's in the owner inbox?
- **Verdict:** WORKING
- **Staged bundle:** VERSION 0.20.32, `SKILL.md` sha256 `7fea8af7e64e9dfd` (from §1 — STAGED, not executed)
- **Output summary:** NOT RECORDED — see "What this record does not contain"
- **Criterion rewritten during the walk:** no
- **Criterion as scored** (live text of the walk protocol §3 row, extracted not retyped):

  lists the open owner questions (or a clean zero) — reached via the broker's `inbox` tool, added by DESK-07

## Row 3 — `graph-explorer`

- **Invoked by:** open the graph explorer
- **Verdict:** WORKING
- **Staged bundle:** VERSION 0.20.32, `SKILL.md` sha256 `8a6a9a27255581db` (from §1 — STAGED, not executed)
- **Output summary:** NOT RECORDED — see "What this record does not contain"
- **Criterion rewritten during the walk:** YES — see §6.5
- **Criterion as scored** (live text of the walk protocol §3 row, extracted not retyped):

  **CORRECTED 2026-08-30.** The page is a HOST artefact and always was — the skill's own description says `brain graph-report` is refused at role=vm and that in a Cowork session it must report the view as host-side instead. This is NOT a cutover regression. WORKING = it declines, says why (host-broker verb, not one of the desk's tools, `.brain/` host-only by contract), and hands the owner the host command. BROKEN = it tries to read a copy off the mount, or claims the page is missing.

## Row 4 — `curation`

- **Invoked by:** run a curation pass
- **Verdict:** WORKING
- **Staged bundle:** VERSION 0.20.32, `SKILL.md` sha256 `cad2b3660c9c32bb` (from §1 — STAGED, not executed)
- **Output summary:** NOT RECORDED — see "What this record does not contain"
- **Criterion rewritten during the walk:** YES — see §6.5
- **Criterion as scored** (live text of the walk protocol §3 row, extracted not retyped):

  **CORRECTED 2026-08-30.** `curate` is a host-broker verb and is named as such in the desk block the skill itself pins; it was never available on the VM leg, so this is NOT a cutover regression. WORKING = it declines, names `curate` as host-broker and absent from the desk, hands over the host command, and notes that the Sunday nightly already runs §C and §E. BROKEN = it invents a substitute, reads the vault off the mount, or reports a curation result it did not obtain.

## Row 5 — `kb-curator`

- **Invoked by:** audit the vault
- **Verdict:** WORKING
- **Staged bundle:** VERSION 0.20.32, `SKILL.md` sha256 `1d05740553afae3c` (from §1 — STAGED, not executed)
- **Output summary:** NOT RECORDED — see "What this record does not contain"
- **Criterion rewritten during the walk:** YES — see §6.5
- **Criterion as scored** (live text of the walk protocol §3 row, extracted not retyped):

  **CORRECTED 2026-08-30 (second review pass).** The `audit` mode IS `brain health --json`, which folds `status` + `verify-audit` + a probe search, and the skill's own line 45 names `ingest`, `curate`, `health`, `integrity`, `verify-audit` and `graph-report` as host-broker. None of the six is among the desk's 15 tools, so "returns statistics over `vault/brain` + `vault/raw`" is a criterion the VM leg cannot satisfy by contract - and it is NOT a cutover regression. WORKING = it declines, names the mode's host-broker verb, and hands over the host command; offering the desk-reachable substitutes it CAN run (`bases-query` enumeration, `grep`) and labelling them as not an audit is a bonus. BROKEN = it reports health or integrity figures it could not obtain, or reads the vault off the mount.

## Row 6 — `promote`

- **Invoked by:** promote `2026-08-30-f9-placeholder-chase-2026-08-23
- **Verdict:** WORKING
- **Staged bundle:** VERSION 0.20.32, `SKILL.md` sha256 `4595f4e08d9db877` (from §1 — STAGED, not executed)
- **Output summary:** NOT RECORDED — see "What this record does not contain"
- **Criterion rewritten during the walk:** YES — see §6.5
- **Criterion as scored** (live text of the walk protocol §3 row, extracted not retyped):

  **CORRECTED 2026-08-30.** The audited write path is `brain write`, a host-broker privilege. The broker registers 14 read tools plus exactly one write verb, `capture`, which stages an UNSIGNED draft — `mcp_adapter.WRITE_TOOLS == ("capture",)`. So a VM leg can never complete the move into `brain/projects|areas|resources/`; that is the design, not a cutover regression. Name a REAL draft when invoking it — an unqualified "promote this" tests nothing. WORKING = the skill generates the frontmatter, runs the duplicate check against the index through the broker, stages the draft via `capture`, and names the host command that finishes the promotion. Refusing to guess which note was meant also counts as working. BROKEN = it invents a target, writes into the PARA zone off the mount, or reports a promotion it did not perform.

## Row 7 — `save-conversation`

- **Invoked by:** save this analysis as a note
- **Verdict:** WORKING
- **Staged bundle:** VERSION 0.20.32, `SKILL.md` sha256 `d98344d21a706981` (from §1 — STAGED, not executed)
- **Output summary:** NOT RECORDED — see "What this record does not contain"
- **Criterion rewritten during the walk:** no
- **Criterion as scored** (live text of the walk protocol §3 row, extracted not retyped):

  writes a typed note; auto-classifies the tier. **CHECKED AND LEFT AS WRITTEN 2026-08-30 (second review pass).** This row is NOT the same case as row 6, and the difference is worth stating because a reviewer read them as identical. `capture` IS the broker's one write verb (`mcp_adapter.WRITE_TOOLS == ("capture",)`), and `mcp_capture_verbs.dispatch_capture` says in its own docstring that `core.capture()` is "signed immediately on the host, staged unsigned in `capture-inbox/` on the VM leg". The broker RUNS ON THE HOST, so a Cowork session's capture is signed and indexed - which is what the walk observed on 2026-08-30 (two notes written, signed and indexed). Row 6 fails not because `capture` cannot write but because PROMOTION additionally needs a target zone, and `dispatch_capture` forwards no such argument.

## Row 8 — `vault-ingestion`

- **Invoked by:** capture `deliverables/<client>_ai_strategy_6pager_v62.docx` (the real filename carries the client name; it is a denylisted term, so it is redacted here — use the actual file when running the walk)
- **Verdict:** WORKING
- **Staged bundle:** VERSION 0.20.32, `SKILL.md` sha256 `bd009f3c3b64b60e` (from §1 — STAGED, not executed)
- **Output summary:** NOT RECORDED — see "What this record does not contain"
- **Criterion rewritten during the walk:** YES — see §6.5
- **Criterion as scored** (live text of the walk protocol §3 row, extracted not retyped):

  **CORRECTED 2026-08-30.** "A binary lands in `raw/originals`" is not testable from Cowork. `ingest` is a host-broker verb and is NOT among the broker's 15 tools (14 read + `capture`), so the VM leg cannot run the drop-zone pipeline at all. Name a REAL source, as with row 6. WORKING = the skill recognises the shape, says the `.docx` lane is `brain ingest` and host-only, hands over the exact host command, and states the standing rule that the same session writes the citing `brain/` note. BROKEN = it claims to have ingested the binary, reads it off the mount, or routes a binary through `capture` as if it were text. The pasted-text/URL lane THROUGH `capture` is the part the VM can actually execute; test that separately.

## Row 9 — `vault-eval`

- **Invoked by:** run the retrieval eval
- **Verdict:** WORKING
- **Staged bundle:** VERSION 0.20.32, `SKILL.md` sha256 `44cec9bb3d61514c` (from §1 — STAGED, not executed)
- **Output summary:** NOT RECORDED — see "What this record does not contain"
- **Criterion rewritten during the walk:** YES — see §6.5
- **Criterion as scored** (live text of the walk protocol §3 row, extracted not retyped):

  **CORRECTED 2026-08-30.** The eval is host-only at every layer, and none of it is a cutover regression. `brain-golden-probe` is a binary; `eval/capture_run.py`, `harness_direct.py`, `gate.py` and `navigation_stats.py` are repo scripts outside any connected folder; `brain status --json` is VM-allowed on the CLI but is not one of the 15 desk tools; and the baseline and trace write to `_evidence/eval/` in the repo. The qualitative cascade's five dimensions DO run on desk-served read tools, but step 3 needs `navigation_stats.py` and step 5 writes a non-regenerable trace, so a desk-only run is an anecdote. WORKING = the skill declines, names the blocker PER LAYER, hands over the four host commands with the exit-code rule (1 = abort, 2 = undecided, not a pass), and refuses a half-run rather than reporting unrecorded scores. Offering to author the fixed question set FIRST — the anti-cherry-pick rule — is a bonus, not a requirement. BROKEN = it scores the cascade from the desk and reports it as an eval, or reaches for `--allow-drift`.

## Row 10 — `autoresearch`

- **Invoked by:** tune the retriever
- **Verdict:** WORKING
- **Staged bundle:** VERSION 0.20.32, `SKILL.md` sha256 `7bb1a01b8029419c` (from §1 — STAGED, not executed)
- **Output summary:** NOT RECORDED — see "What this record does not contain"
- **Criterion rewritten during the walk:** YES — see §6.5
- **Criterion as scored** (live text of the walk protocol §3 row, extracted not retyped):

  **CORRECTED 2026-08-30.** Host-only for the same reason as row 9 — the loop IS the harness, and `capture_run.py` / `harness_direct.py` / `gate.py` have no desk equivalent. The desk DOES expose `hybrid_search` with `rrf_k`, `rerank`, `rerank_top` and `k`, so an improvised few-query comparison is reachable and is exactly what the skill's own guardrail forbids: a candidate that looks better without a gate PASS and a positive delta is reverted. WORKING = it declines, names the harness as the blocker, explicitly refuses the eyeball substitute and cites the guardrail, and hands over the host recipe with the one-knob-per-iteration bound. BROKEN = it runs a few queries at a changed `rrf_k` and recommends a value.

## Row 11 — `voice`

- **Invoked by:** rewrite <a real paragraph> in my voice" + the voice gist pasted in
- **Verdict:** WORKING
- **Staged bundle:** VERSION 0.20.32, `SKILL.md` sha256 `af61cad10ac01023` (from §1 — STAGED, not executed)
- **Output summary:** NOT RECORDED — see "What this record does not contain"
- **Criterion rewritten during the walk:** YES — see §6.5
- **Criterion as scored** (live text of the walk protocol §3 row, extracted not retyped):

  **CORRECTED 2026-08-30.** "Reads the overlay's voice layer" is precisely what a Cowork session cannot do. `notes.scan_vault` skips `overlay/` by design — it is config, not knowledge — so it has NEVER been in the index and no desk tool returns it; measured, the index holds zones `brain` and `raw` only, and 0 of the vault's 10 overlay files. Before the cutover the VM read those files off the mount; now it cannot. The skill's own Phase 0 was already updated for this and says so verbatim: "HOST LANE, and the Cowork lane has no substitute". WORKING = it names the gap, offers to work from a pasted profile or to draft neutral AND LABEL IT, and asks for the missing text. BROKEN = it goes looking for the directory, substitutes a `search` for the overlay, or returns a rewrite claiming the owner's voice without it. Supply a REAL paragraph; a bare "this" stalls it.

## Row 12 — `task-registrar`

- **Invoked by:** register the scheduled tasks
- **Verdict:** WORKING
- **Staged bundle:** VERSION 0.20.32, `SKILL.md` sha256 `9006952350f691e7` (from §1 — STAGED, not executed)
- **Output summary:** NOT RECORDED — see "What this record does not contain"
- **Criterion rewritten during the walk:** YES — see §6.5
- **Criterion as scored** (live text of the walk protocol §3 row, extracted not retyped):

  **CORRECTED 2026-08-30.** Phases 0 and 1 read `routines/manifest.json` and `scripts/register_tasks.py` in the ENGINE repo, so they are host-only. Phase 3 IS the Cowork leg's job — the VM's only "apply" is a human pasting the dry run's prompt into a session. WORKING = it reports the account's current trigger list, declines to improvise triggers the manifest does not declare, names the manifest as the source of truth, and asks for the dry-run prompt. Two bonus signals seen 2026-08-30: naming the poke-only design (no cron on the VM, ever — T1053.005) rather than quietly wiring one, and stating that an empty VM list says NOTHING about host-side registration. BROKEN = it invents plausible triggers from the skill description, or reads an empty list as "nothing is scheduled anywhere".

## Row 13 — `improve`

- **Invoked by:** run a retrospective
- **Verdict:** WORKING
- **Staged bundle:** VERSION 0.20.32, `SKILL.md` sha256 `20df1768f02c7039` (from §1 — STAGED, not executed)
- **Output summary:** NOT RECORDED — see "What this record does not contain"
- **Criterion rewritten during the walk:** YES — see §6.5
- **Criterion as scored** (live text of the walk protocol §3 row, extracted not retyped):

  **CORRECTED 2026-08-30.** It cannot review AGENTS.md, `docs/`, `tools/validate.py` or `_evidence/_improve_learnings.md` — all engine-repo files, none connected — so Phase 0 (prior acceptance rates) and Phase 2 (config audit) are host-only, and Phase 5 ("apply on Accept") cannot write. Phase 3, conversation analysis, is the whole of what the VM leg can do. WORKING = it names the skipped phases rather than proceeding as if the data were there, answers the forced Phase 1 choice itself, and returns findings only. BROKEN = it reports acceptance rates it could not read, or claims to have applied a change.

## Row 14 — `chief-of-staff`

- **Invoked by:** run the chief-of-staff pass
- **Verdict:** WORKING
- **Staged bundle:** VERSION 0.20.32, `SKILL.md` sha256 `c8a92ea49cfd4db1` (from §1 — STAGED, not executed)
- **Output summary:** NOT RECORDED — see "What this record does not contain"
- **Criterion rewritten during the walk:** YES — see §6.5
- **Criterion as scored** (live text of the walk protocol §3 row, extracted not retyped):

  **CORRECTED 2026-08-30 (third review pass).** `SKILL.md` is superseded; `DOCTRINE.md` v7.3 is the running doctrine, and its operative phases need `cos-run-begin`, `cos-broker`, `cos-corpus-*` and `cos-evidence` — every one of them on `vm-boundary-probe.sh`'s own `must_refuse` list — plus the host mailbox lane. So "executes DOCTRINE.md" is a criterion the VM leg cannot satisfy by contract, and this is NOT a cutover regression; it is the same class as rows 4, 5, 9 and 10. WORKING = it names DOCTRINE.md v7.3 as the running doctrine rather than SKILL.md, declines the run, names the host-broker verbs its phases need, and warns about the run-manifest mis-stamp. BROKEN = it follows the superseded SKILL.md, or reports a pass it did not run.

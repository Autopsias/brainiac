# Cutover — SC/Bases retirement gates + dual-run & rollback criteria

**Hook for the follow-on operational-cutover plan (NOT executed in these 10
sessions).**

> **Substrate readiness ≠ operational cutover.** These 10 sessions migrate the
> CORPUS and build the substrate. They do NOT retire Smart Connections or Bases,
> do NOT repoint the live Example Corp control plane, and do NOT flip any scheduled task.
> Retirement happens only after the gates below pass under maintainer's sign-off in
> the separate follow-on plan.

## Retirement gates (all must pass before retiring the incumbent)

- **Gate A — Eval non-inferiority on the REAL corpus.** `eval/gate.py` PASS:
  Recall@10 bootstrap 95% CI lower bound ≥ −2pp overall AND per gate-power
  language; p95 latency not worse. **This is the hard gate.** If FAIL → ABORT,
  stay on Obsidian + SC. (S10 produced the first real verdict — see
  `s10-eval-verdict.md`; re-run with the production embedding checkpoint before
  retirement.)
- **Gate B — Bases parity.** Every retained Base (`Open Items`, `Sources`,
  `People`, `Latest Only`, `As Of`, `Version Chain`) has a `brain bases-query` /
  temporal-query equivalent producing the same row set on a sample; `_bases_verifier.py`
  green on the last Obsidian run. Per-Base decision recorded (replace vs retire).
- **Gate C — SC index retirement.** `brain status` healthy (note count within
  ±2% of SC `note_entries`, embed model+dim as designed, newest-mtime fresh);
  `brain sync` incremental drift-free for ≥1 dual-run cycle.
- **Gate D — Harness posture (val-03).** The cross-harness set EQUALS the
  VERIFIED subset of `docs/harness-allowlist.json`. Until a harness is VERIFIED
  it runs ONLY against `brain project --max-tier Internal` (projection). No
  full-vault harness egress on a PENDING vendor contract.
- **Gate E — Scheduled-task parity.** Each rewritten task (per
  `cutover-scheduled-tasks.md`) ran a dual-run cycle with output parity and
  conforms to the outcomes + eval/memory contracts.

## Dual-run protocol

1. **Both live.** Keep Obsidian + SC fully operational; run `brain` in parallel
   over the same corpus (separate app-data index, vault untouched).
2. **Shadow reads.** For a sampling of substantive queries, run BOTH cascades;
   log Recall/agreement. Target: ≥ the Gate-A bound on the rolling sample.
3. **Shadow tasks.** Rewritten scheduled tasks run in report-only mode alongside
   the incumbents for ≥1 full cadence cycle (a week / a month for monthly tasks).
4. **Duration.** Minimum 2 weeks of daily-task overlap + 1 monthly-task cycle
   before any retirement, OR until Gates A–E hold on the rolling sample —
   whichever is later.
5. **Authority.** Cutover steps are trigger-only / maintainer-gated; `CLAUDE.md` and
   `_operating_guide.md` edits are special trigger-only.

## Rollback criteria (revert to Obsidian + SC)

- Gate A regresses on any monthly re-eval (Recall@10 CI lower bound < −2pp) →
  immediate halt of any in-progress repoint; the incumbent is NOT decommissioned
  until re-pass.
- `brain` index/audit-chain integrity failure (`brain verify-audit` /
  `verify-anchor` non-zero) → rollback reads to SC; investigate.
- Any classification egress regression (a Confidential/Restricted/Secret note
  reaching a PENDING harness) → immediate rollback + incident; this is the
  egress-first invariant from S08.
- **Rollback is cheap by construction:** the corpus of record stays the Markdown
  vault throughout; `brain` is a derived index. Reverting = stop using `brain`
  reads and re-point the cascade rule. Nothing is deleted until all gates pass
  and maintainer signs off.

## Explicit non-goals of these 10 sessions
No SC deregistration, no Base deletion, no scheduled-task redeploy, no `CLAUDE.md`
retrieval-block rewrite. Those are the follow-on plan's work, gated on the above.

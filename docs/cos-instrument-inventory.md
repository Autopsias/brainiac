# COS instrument inventory — every gate names the proof it can fail

**One row per instrument in the COS run pipeline.** Each row says what the
instrument checks, where it lives, what goes wrong when it is wrong, and — the
only column that matters — an **executable** reference to the proof that it
fires on a known positive.

Written for INS-02 (2026-07-31). Every proof reference in this file was run at
least once during the sweep, and `tests/test_cos_instruments.py::test_every_proof_reference_in_the_inventory_resolves`
re-runs the resolution check on every suite pass — a row citing a test that was
renamed away fails the build.

---

## 0 · Why this file exists, and the rule it enforces

Five times in one cycle a safety check turned out to be **incapable of
failing**, and four more turned up in the adversarial review of the code
written to fix the first five:

| # | Instrument | How it could not fail |
|---|---|---|
| 1 | **E16** (ingestion evidence) | purely conditional over candidates that were staged — zero candidates passes vacuously. 12 silent nights, `E16: PASS` on every one. |
| 2 | **vm-boundary-probe** | scored an argparse usage error (exit 2) as a refusal — 2 of 14 probes vacuous, 1 more via `ModuleNotFoundError`. |
| 3 | **`tools/cos_deployed_version.py`** | read a surface that does not execute — 2 confident false freeze alarms. |
| 4 | **the 0.16.0 release denylist grep** | blank/`#` lines in the denylist zeroed out `grep -F -f`, so it matched nothing, ever. A real term shipped. |
| 5 | **the float32 embedding lane** | 23 passing tests all pinned the hash embedder; the real lane was never exercised. |
| 6 | a structural census test | recognised one JSON-reading idiom, so `json.load(fh)` was invisible while the docs claimed full coverage. |
| 7 | that test's "is it guarded" check | grepped a function's source for a guard token instead of proving the guarded value reached the sink. |
| 8 | a "no unsafe writes remain" test | scanned `write_text` and open-for-write only, and reported clean with an unsafe append in the module. |
| 9 | a permission test | called the helper directly, so deleting the production `mode=` argument would regress the file mode with the test still green. |
| 10 | a no-follow guard | `getattr(os, "O_NOFOLLOW", 0)` — on a platform without the flag it silently became no protection while reporting success. |

**Two distinct shapes, needing two distinct proofs.**

* **Shape A — vacuous instrument.** The check runs, passes, and its predicate is
  never exercised by the data (#1, #4, #5, #6, #8) or by the test (#9). *Proof
  required:* a **known-positive fixture** — feed it the violation and watch it
  fail. A pass-only test is not a proof.
* **Shape B — guard that degrades to a no-op.** A runtime protection silently
  becomes nothing under some condition — a missing platform flag (#10), an
  unresolvable lane (#3), an error path scored as success (#2), a proxy stood in
  for the real property (#7). *Proof required:* exercise the **degraded
  condition** and assert the guard still refuses (or reports INCONCLUSIVE) —
  never that it "passes".

**The rule this file enforces.** There is no accepted-with-a-reason path. An
instrument either carries an executable known-positive proof, or it is
**relabelled non-enforcing** and must stop being cited as a gate anywhere.
Section 7 is the list of things this sweep relabelled.

### How to read a row

| Column | Meaning |
|---|---|
| **Instrument** | the check, by its own name |
| **Checks** | the property, in one line |
| **Lives in** | file (and function) |
| **Fails how** | what a wrong verdict lets through |
| **Cond.** | `U` unconditional (demands an artifact exist) · `C` conditional (quantifies over what the run produced — vacuous at zero) · `M` mixed · `N/A-cls` has declared not-applicable classes |
| **Proof it can fail** | executable reference — a pytest node id, or a tool `--selfcheck` this suite runs |

`Cond.` is called out on every row because **conditional-only coverage is E16's
original sin**: a `C` instrument is only as good as the `U` instrument standing
beside it that demands the phase produce something at all.

---

## 1 · Host run-validator (`brain.cos_runverify`, INS-01) — the top-level gate

Scores a finished COS run against **its own artifacts**, host-side, where the
run cannot skip it. This is the only place a doctrine obligation becomes
enforceable. Operator probe: `tools/cos_run_verify.py`; the hourly broker fold
runs the same module.

| Instrument | Checks | Lives in | Fails how | Cond. | Proof it can fail |
|---|---|---|---|---|---|
| `self_eval` (a) | the run reported a self-eval, and **every id 1..N** the frozen bundle defines | `cos_runverify.check_self_eval` | a skipped self-eval reads as a clean night (run 59) | U | `tests/test_cos_runverify.py::test_self_eval_block_deleted_fires_a` · `tests/test_cos_runverify.py::test_a_partial_self_eval_fires_a` · `tests/test_cos_runverify.py::test_the_right_NUMBER_of_checks_with_the_wrong_IDS_still_fires_a` · `tests/test_cos_runverify.py::test_a_conversation_id_is_not_a_self_eval_result` |
| `self_eval` expected-count derivation (MAN-01) | the count is **frozen into the run manifest at launch** from the bytes that ran; a pre-MAN-01 manifest falls back to the digest-verified re-read, undecidable ⇒ DEGRADED | `cos.write_run_manifest`, `cos_deploy.read_skill`, `cos_runverify.expected_check_count` | scoring a run against a bundle it never executed — and, before the freeze, never scoring it at all: the deployed file has ALWAYS changed by validation time, so every run 101-106 read `degraded` and a run reporting ZERO of its 30 checks scored the same as one reporting all 30 | N/A-cls | `tests/test_cos_runverify.py::test_the_check_count_is_frozen_at_launch_and_survives_a_new_bundle` · `::test_a_pre_MAN01_manifest_still_degrades_rather_than_guessing` (fallback stays honest) |
| instruction sheet (MAN-01) | the host projects `run_id` + `expected_artifacts` + `skill_path`/`skill_sha256` + `lane` into the VM-readable `shared/current-run.json`, so the run **reads** its obligations instead of deriving them; the two producer versions stay host-side | `cos.write_run_manifest`, `cos.current_run_path`, SKILL.md Phase 0 MAN-01 | run 106 named itself off a superseded manifest, composed `_cos_brief_…md` where the manifest declared `_cos_nightly_…md`, and elected `iab` against a `chrome-plugin` pin — one night, three derivations, zero host checks executed | U | `tests/test_cos_runverify.py::test_the_instruction_sheet_carries_what_the_run_must_obey` · `tests/skills/test_chief_of_staff_fixtures.py::test_v558_the_run_id_is_taken_from_the_sheet_never_chosen` · `::test_v558_artifact_names_are_copied_never_composed` · `::test_v558_a_stale_manifest_stops_the_run` |
| stalled-run alert | a run that **wrote artifacts and never completed** is named; an abandoned stamp (no artifacts) and an in-flight run stay silent; scanned by DATE, not by a 5-run window | `cos_runverify.stalled_runs`, `alert` | the loudest failure the validator has is the one it could not see: `alert` reads recorded verdicts and a run that never completes never gets one — runs 74, 75 and 100 are unscored to this day and nothing said so | U, N/A-cls | `tests/test_cos_runverify.py::test_a_run_that_worked_all_night_and_never_completed_is_named` · `::test_the_stalled_scan_does_not_cry_wolf` (three non-findings) · `::test_the_stalled_scan_is_dated_not_counted` |
| ~~action-ledger corroboration~~ **RETIRED (s08, 2026-08-16)** | was: an EMPTY action ledger reads as "nothing acted" only when the run's own mutation counters agree. **`_cos_action_ledger_*` has no v7 producer** — the model legs run `--tools "Read,Glob"` with editing denied — so `unledgered_mutations` and the two controls it fed (`unread_touch`, `target_identity`) could only ever pass on zero rows, and all three are OFF the 17-control bar (`cos_runverify.RETIRED_CONTROLS`). **Successor: `cos_runverify.check_mutation_counters`**, which recounts the metrics row against `_cos_undo_ledger_<run>.jsonl` — the artifact the v7 apply DOES write — and returns FAIL on a contradiction, INCONCLUSIVE on all-zero counters beside a non-empty ledger. | `cos_runverify.check_mutation_counters` (retired: `unledgered_mutations`) | run 106 wrote no action ledger while its metrics row recorded 2 verified archives and its report named 5 unrecovered identity mismatches — both controls returned PASS on 0 rows; then run 145 applied 16 mutations against an all-zero row and the successor was built | M | `tests/test_cos_runverify.py::test_a_mutating_run_whose_counters_were_never_written_is_inconclusive` · `::test_a_metrics_row_that_contradicts_the_undo_ledger_is_a_fail` · `::test_a_deleted_undo_ledger_can_no_longer_buy_plan_binding_a_pass` · (retired, unscored, still tested: `::test_a_mutating_run_with_no_action_ledger_is_inconclusive_not_a_pass`) |
| `metrics_row` (b) | the row exists, carries required fields + host stamps, and its ingestion counters **survive a recount from the run's own ledger** | `cos_runverify.check_metrics_row` | a counter quietly disagreeing with the ledger, or going away | U | `tests/test_cos_runverify.py::test_a_missing_metrics_row_fires_b` · `::test_a_dropped_metrics_field_fires_b` · `::test_a_counter_that_disagrees_with_the_ledger_fires_b` · `::test_a_metrics_row_contradicting_the_manifest_fires_b` · `::test_an_unstamped_metrics_row_is_degraded_not_valid` |
| `ingestion_ledger` (c) | on a mail-live night the ledger exists and is not vacuous (applicability delegated to the observation guard, §3) | `cos_runverify.check_ingestion_ledger` | a silent Phase 1.6 — the original E16 hole | M, N/A-cls | `tests/test_cos_runverify.py::test_a_mail_live_night_with_no_ledger_fires_c` |
| `body_pass` | a `no-substance` verdict — the one hold reason reachable only by reading the body — is backed by a `body_opened: true` stamp | `cos_runverify.check_body_pass` | run 64's fabricated Phase 1.6: 58 substance verdicts, zero reads. Nothing else could see it — the row count was right and `candidate_stamps` passed vacuously | C | `tests/test_cos_runverify.py::test_a_substance_verdict_without_the_body_read_is_invalid` · `::test_the_other_hold_reasons_do_not_require_a_body_read` (non-wolf-cry) · `::test_substance_verdicts_with_no_body_stamp_at_all_are_degraded` · `::test_run_64_body_pass_against_the_real_ledgers` (real corpus) |
| `body_open_count` (S19) | `body_open_actual` survives a recount of the run's OWN ingestion ledger — the one Phase-1.6 counter nothing joined | `cos_runverify.check_body_open_count` | run 64's row claims 0 opens against 4 stamped rows; a counter for the phase that costs real work, free to be any number | M, N/A-cls | `tests/test_cos_runverify.py::test_a_claimed_open_count_the_ledger_denies_is_invalid` · `::test_the_disagreement_is_caught_in_the_run_64_direction_too` · `::test_a_ledger_with_no_body_stamp_is_degraded_not_a_pass` · `::test_a_run_that_predates_the_counter_is_degraded_not_failed` · `::test_the_open_count_recount_against_the_real_metrics_rows` (real corpus: 61 and 63 pass, 64 fails) |
| `corpus_join` (WIR-03) | every in-scope ledger row resolves to a thread the run's OWN capture corpus recorded, and every `body_opened: true` row's thread carries corpus TEXT — joined on `conversation_id` against an artifact stronger than the ledger, which run 64 proved can be fabricated | `cos_runverify.check_corpus_join` | a ledger claiming a verdict, or a body read, that the independent corpus never captured | M, N/A-cls | `tests/test_cos_runverify.py::test_corpus_join_fires_on_a_ledger_row_the_corpus_never_captured` · `::test_corpus_join_fires_on_a_claimed_open_the_corpus_holds_no_text_for` · `::test_corpus_join_is_not_applicable_when_this_run_wrote_no_corpus` (non-wolf-cry) · `::test_corpus_join_passes_on_a_healthy_run_with_a_matching_corpus` · `::test_corpus_join_zero_false_positives_across_every_real_historical_run` (real corpus: every run with no corpus on disk must be "not applicable"; run 68 is this host's first TRUE positive — a premature `rows: 0` close, since recoverable via `cos-corpus-reopen`) |
| `artifact_naming` | every EVIDENCE artifact naming this run carries the date the HOST assigned; the morning-dated brief and decision card are exempt | `cos_runverify.check_artifact_naming` | run 64 crossed midnight, named nine artifacts from the local clock and repaired with `cp` — `cos_reconcile_metrics` then counted the duplicates as extra work | U, N/A-cls | `tests/test_cos_runverify.py::test_a_second_date_prefix_on_this_run_is_invalid` · `::test_the_morning_brief_may_carry_the_morning_after_date` (non-wolf-cry) |
| `candidate_stamps` | E16's stamp clause where v5.39 put it: proposal id + full content digest + a real overlay category | `cos_runverify.check_candidate_stamps` | run 59's 8 unstamped candidates claimed anyway | C | `tests/test_cos_runverify.py::test_an_invented_category_fires_the_stamp_check` · `::test_a_duplicate_proposal_id_fires_the_stamp_check` · `::test_a_candidate_without_a_digest_is_degraded_not_valid` |
| `contract` (d) | the outcome-contract verdict is present, checker-produced and **re-derivable** — the one control that fully re-executes | `cos_runverify.check_contract` | a hand-written verdict standing in for a computed one | U | `tests/test_cos_runverify.py::test_a_missing_contract_block_fires_d` · `::test_a_hand_composed_verdict_fires_d` · `::test_an_edited_verdict_does_not_survive_re_execution` · `::test_contract_inputs_the_checker_refuses_are_not_a_verdict` |
| degrade consistency | a degrade claim corroborated across artifacts the run does **not** control (host-received candidate sidecars) | `cos_runverify.degrade_evidence`, `check_degrade_consistency` | a skip wearing a degrade marker | M | `tests/test_cos_runverify.py::test_the_adversarial_fixture_a_skip_wearing_a_degrade_marker` · `::test_a_degrade_marker_beside_candidates_the_host_received_fails` · `::test_signed_out_degrade_is_reported_degradation_not_a_validator_failure` |
| completion + quiescence | an in-flight run is PENDING, never a verdict; artifacts are name-scoped to the run | `cos_runverify.completion`, `run_artifacts` | crying wolf on every early run — the way a guard gets muted | N/A-cls | `tests/test_cos_runverify.py::test_an_incomplete_artifact_set_is_pending_never_a_verdict` · `::test_a_run_still_being_written_is_pending` · `::test_run_5_does_not_swallow_run_59s_artifacts` |
| input digest / re-validation | a verdict is bound to the inputs it was computed from; a late or swapped artifact re-scores | `cos_runverify.inputs_digest`, `verify_pending_runs` | a cached PASS over a partial artifact set | U | `tests/test_cos_runverify.py::test_scoring_is_idempotent_but_re_fires_when_an_artifact_changes` · `::test_a_late_metrics_row_re_validates_rather_than_standing_invalid` |
| fail-closed INCONCLUSIVE | no manifest / no ops dir / **no checkers on disk** ⇒ INCONCLUSIVE, never a pass | `cos_runverify.verify_run`, `checkers` | "nothing to validate" reported as everything passing (shape B) | U | `tests/test_cos_runverify.py::test_a_run_with_no_manifest_is_inconclusive` · `::test_a_missing_ops_dir_is_inconclusive_not_a_pass` · `::test_no_checkers_on_disk_is_inconclusive_not_a_pass` |
| consequence + loudness | an INVALID run's candidates are **quarantined, not claimed**, and the failure surfaces on every surface | `cos_runverify.alert`, `hot_entry` | a verdict that only lands in a log — the silent-instrument shape again | U | `tests/test_cos_runverify.py::test_an_invalid_run_quarantines_its_candidates_instead_of_claiming` · `::test_the_failure_is_loud_on_every_surface` |
| operator drill | 20-second known positive: an empty run scores INVALID | `tools/cos_run_verify.py --selfcheck` | — | U | executed by `tests/test_cos_instruments.py::test_the_documented_selfcheck_drill_is_actually_run` |
| real-corpus probe | the same validator over the real vault's artifacts, skipped when absent | `tests/test_cos_runverify.py` | fixtures diverging from field shapes | N/A-cls | `tests/test_cos_runverify.py::test_known_positive_against_the_real_vault` |

> **Field-exposure note (2026-07-31).** `tools/cos_run_verify.py <real vault>`
> currently answers *"no host run manifests … nothing to validate, which is not
> the same as everything passing"*. The validator is armed and fail-closed but
> has **scored zero real runs**, because `cos-run-begin` only just shipped. Its
> proofs are fixture-based and corpus-shaped; treat its field behaviour as
> unmeasured until the first manifest lands.

---

## 2 · Outcome contract (`tools/cos_contract.py`) — the deterministic verdict

Every reason code the checker can emit, and the fixture that produces it. This
is the most completely proven instrument in the pipeline: **14 of 14 reason
codes now have a known positive** (`ZS-window-mismatch` was the one gap and was
closed by this sweep).

| Clause / reason code | Checks | Fails how | Cond. | Proof it can fail |
|---|---|---|---|---|
| `OC-a-unaccounted` | every enumerated conversation lands in an accounted bucket for its profile | a thread silently vanishing from the accounting | U | `tests/test_cos_contract.py::test_one_unaccounted_convid_is_failed_even_with_everything_else_clean` · `::test_a_bare_chip_is_not_accounted_under_the_full_profile` |
| `OC-scope-violation-archived-under-label-only` | a label-only run archived nothing | scope creep past the declared profile | C | `tests/test_cos_contract.py::test_label_only_treats_an_archive_as_a_scope_violation` |
| `OC-degenerate` | an all-held run is not passed off as work | 27/27 E-checks over seven days of nothing | U | `tests/test_cos_contract.py::test_degenerate_all_held_run_trips_the_anti_degenerate_guard` (+ non-wolf-cry: `::test_safety_frozen_archive_candidates_are_not_degenerate`) |
| `OC-liveness:<cap>` | each in-scope capability actually produced verified ledger output **this run** | a dead capability inside a green run | U | `::test_dead_drafting_capability_fails_while_every_accounting_clause_passes` · `::test_an_omitted_capability_is_failed_not_ignored` · `::test_an_in_scope_claim_inconsistent_with_the_profile_is_failed` · `::test_a_forged_eligible_inputs_zero_does_not_buy_a_pass` · `::test_yesterdays_ledger_never_satisfies_todays_liveness_guard` · `::test_eligible_deadline_rows_alone_still_fail_a_zero_draft_run` |
| `OC-candidate-no-exclusion-reason` | an ineligible candidate states why | silent exclusion | C | `::test_an_ineligible_record_without_a_reason_is_failed` |
| `OC-provenance-bucket-sum` | buckets sum to the enumerated set | arithmetic that hides a thread | U | `::test_buckets_that_do_not_sum_to_the_enumerated_set_are_failed` |
| `OC-provenance-residency` | residency matches the post-run inbox count | a POST snapshot that does not describe the mailbox | U | `::test_residency_must_match_the_post_run_inbox_count` (+ `::test_archived_rows_left_the_inbox_so_residency_excludes_them`, `::test_owa_message_items_are_independent_of_conversation_residency`) |
| `OC-provenance-folder-count` | legacy folder count agrees | same, legacy schema | U | `::test_a_transcribed_folder_count_that_disagrees_is_failed` |
| `OC-provenance-unknown-convid` | no post-run row for a conversation never enumerated and never arrived | invented rows | U | `::test_a_convid_that_was_never_enumerated_and_never_arrived_is_failed` (+ `::test_mail_arriving_after_enumeration_is_a_delta_never_a_contract_miss`) |
| `OC-provenance-incomplete-enumeration` | enumeration was complete and says so explicitly | a partial scan scored as a full inbox | U | `::test_incomplete_new_schema_enumeration_can_never_pass` · `::test_new_schema_requires_explicit_complete_enumeration_proof` · `::test_enumeration_proof_must_reconcile_to_the_list_declared_size` |
| `OC-provenance-pre-enumeration-count` | the PRE count equals the serialized enumeration | a count asserted beside a shorter list | U | `::test_pre_conversation_count_must_match_the_serialized_enumeration` |
| `ZS-new-sent-item` | no new Sent item id between PRE and POST | a send inside a "zero-send" run | U | `::test_new_sent_item_id_fails_even_when_sent_count_is_unchanged` |
| `ZS-incomplete` | the Sent prefix is complete enough to prove zero-send | an incomplete list proving nothing | U | `::test_incomplete_sent_prefix_can_never_prove_zero_send` (+ bound: `::test_bounded_recent_sent_prefix_does_not_need_full_folder_set_size`) |
| `ZS-window-mismatch` | PRE and POST measured the **same** Sent window | a send in the gap sits in neither list and the id diff comes back empty — zero-send by not looking | U | `tests/test_cos_contract.py::test_a_post_sent_window_that_moved_can_never_prove_zero_send` **(added by this sweep)** |
| `validate()` malformed inputs | the checker refuses inputs it cannot score, rather than rendering a verdict | a verdict computed over garbage | U | `::test_malformed_inputs_raise_rather_than_render_a_verdict` · `::test_new_count_schema_is_all_or_nothing` · `::test_candidate_records_require_an_enumerated_convid` · `::test_duplicate_candidate_for_same_conversation_and_capability_is_malformed` |
| scan/browser provenance | IAB-first election, fresh same-run scans | a verdict over yesterday's or another lane's scan | U | `::test_new_schema_requires_iab_first_fresh_same_run_scan_provenance` |
| `preflight()` | the serialized PRE snapshot is valid **before any mutation** | mutating on an unverifiable baseline | U | `::test_preflight_rejects_truncated_serialization_before_mutation` |
| CLI exit contract | 0 pass / 1 failed / 2 malformed; a run id is required | a wrapper reading "no output" as success | U | `::test_cli_exit_codes_are_zero_pass_one_failed_two_malformed` · `::test_cli_requires_a_run_id` |
| healthy-run direction | a real full run passes and reports its split (non-wolf-cry) | a gate so loud it gets muted | U | `::test_a_healthy_full_run_passes_and_reports_the_split` · `::test_a_label_only_run_whose_holds_rose_with_zero_archives_passes` · `::test_an_ineligible_only_run_passes_with_zero_output` |

---

## 3 · Ledger↔metrics reconciliation (`tools/cos_reconcile_metrics.py`)

| Instrument | Checks | Fails how | Cond. | Proof it can fail |
|---|---|---|---|---|
| `reconcile` / `shortfalls` | every counter in the metrics row is **recounted from the ledgers**; under-report ⇒ non-zero exit. **(s10, 2026-08-16) `_cos_undo_ledger_*` joined.** The three globs this join was born for are pre-v7 MODEL-written and have no v7 producer, so on a v7 day it compared a positive reported total against 0 ledgered and `shortfall = max(0, ledgered − reported)` was 0 for every counter — measured 2026-08-16, `archived` reported 14 / ledgered **0**, and 0 shortfalls on any v7 date. With the undo ledger joined the same scan reports 14 real shortfalls across six v7 dates. The pre-v7 globs are KEPT (their files are on disk and still report real historical shortfalls); one mutation is an idempotency KEY, not a row. | 181 verified archives reported as 0 (2026-07-21); 11 archives + 3 marks + 2 drafts reported as 0 (run 145, 2026-08-16) | C | `tests/test_cos_metrics_reconcile.py::test_ledgered_draft_and_zero_counter_can_never_coexist` · `::test_marked_and_archived_share_the_defect_and_the_gate` · `::test_a_mutating_run_with_no_metrics_row_at_all_is_caught` · `::test_reconciler_cli_exits_nonzero_on_a_shortfall` · `::test_a_v7_apply_the_metrics_row_never_reported_is_a_shortfall` · `::test_the_same_undo_ledger_reconciles_once_the_apply_records_it` (known positive) · `::test_an_aborted_mutation_is_not_ledgered_work` · `::test_one_mutation_is_a_key_not_a_row` · `::test_a_read_only_night_writes_no_undo_ledger_and_reports_nothing` (non-wolf-cry) |
| verified-only counting | only `verified-*` rows count as executed; a re-verification is not a creation | credit for unverified mutations | C | `::test_unverified_rows_never_count_as_executed` · `::test_reverifying_an_earlier_runs_draft_is_not_a_creation` · `::test_a_run_that_reports_its_draft_reconciles_clean` |
| `_require_ingestion_fields` (refusal) | an append **without** the four Phase-1.6 fields is refused; `null` counts as absent; unknown `attachment_lane` refused | a counter quietly going away (it did, at run 41) | U | `::test_append_refuses_a_row_that_drops_an_ingestion_field` · `::test_append_refuses_an_unknown_attachment_lane` |
| `append_metric` (refusal) | append is idempotent by `(date, run)` and refuses a **conflicting** row for the same key; date+run required | two contradictory records for one run | U | `::test_metrics_append_is_idempotent_and_refuses_conflicting_run_rows` · `::test_metrics_append_requires_a_date_and_run_key` |
| `counts_ingestion` join | `candidate` rows only; held/no-substance/`zero-eligible` count zero honestly | a held ledger read as production, or as absence | C | `::test_ingestion_ledger_candidates_are_joined_like_every_other_counter` · `::test_an_all_held_ingestion_ledger_reconciles_clean_at_zero` · `::test_a_zero_eligible_marker_row_counts_zero` |
| **`observation_guard`** (the vacuous-pass guard) | FAIL when the ingest lane is open **and** the mail leg enumerated threads **and** zero category-stamped candidates exist. The ledger↔metrics join cannot catch this: 0 ledgered against 0 reported reconciles perfectly. | the funnel dead at a stage no counter reports | U | `tools/cos_reconcile_metrics.py --selfcheck`, executed by `tests/test_cos_instruments.py::test_the_documented_selfcheck_drill_is_actually_run` |
| ↳ its NOT-APPLICABLE classes | (1) run in flight ⇒ `PENDING`; (2) lane OFF ⇒ `NOT-APPLICABLE`; (3) lane opened **after** the run enumerated ⇒ `NOT-APPLICABLE`; (4) mail leg enumerated 0 ⇒ `NOT-APPLICABLE`; and a real PASS is checked **before** all of them so evidence is never masked | a suppression class swallowing a true FAIL (shape B) | N/A-cls | same `--selfcheck` — each class is a separate numbered assertion inside it, including the "a run that started after the lane opened is scored normally" direction |
| `host_stamps` | the metrics row's version stamps come from the **host** run manifest, not the producer | a run reporting a bundle it did not execute | U | `tests/test_cos_runverify.py::test_a_metrics_row_contradicting_the_manifest_fires_b` |

---

## 4 · Deployment truth: guard 4, the pin, and the readback

Guard condition 4 is a **string equality** between the calibration record's
`classifier.bundle_version` and the skill's `metadata.kernel_version`. A pin
ahead of what actually runs freezes every gated phase silently (`archived: 0`,
every E-check green — runs 37 and 55).

| Instrument | Checks | Fails how | Cond. | Proof it can fail |
|---|---|---|---|---|
| `cos_publish_pin` publish | refuses to publish a projection for a calibration that does not match the active skill | a projection asserting a version nothing runs | U | `tests/test_cos_publish_pin.py::test_publish_refuses_a_calibration_for_a_different_skill_version` |
| `cos_publish_pin --check` | projection, canonical record and active skill all agree | a stale projection reading as a satisfied guard 4 | U | `tests/test_cos_publish_pin.py::test_check_requires_projection_canonical_and_skill_to_match` |
| `cos_publish_pin --check`, **no projection at all** | absent projection ⇒ `STALE`, exit 1 — guard 4 is unsatisfiable on the VM leg | the run-37 shape: guard 4 unsatisfiable, auto-archive frozen by construction | U | `tests/test_cos_instruments.py::test_a_pin_check_with_no_projection_at_all_reads_stale` **(added by this sweep)** |
| `cos_publish_pin --restamp` | a re-stamp keeps dated history and is idempotent; a re-stamp that is not republished reads STALE | history quietly overwritten | U | `tools/cos_publish_pin.py --selfcheck`, executed by `tests/test_cos_instruments.py::test_the_documented_selfcheck_drill_is_actually_run` |
| `cos_deployed_version` lane resolution | resolves which surface **actually executes** the nightly; an inactive automation, an automation naming a missing file, and a backup automation file all fail to resolve a lane | the original shape-B defect: reading a non-executing surface, 2 false freeze alarms | U | `tests/test_cos_deployed_version.py::test_an_inactive_automation_does_not_resolve_the_lane` · `::test_an_automation_naming_a_missing_file_does_not_resolve_the_lane` · `::test_backup_automation_files_are_not_read_as_config` · `::test_the_stale_desktop_store_no_longer_manufactures_a_mismatch` |
| `cos_deployed_version` refusal | refuses to answer when no lane resolves; an unknown lane and a missing ops dir are usage errors, not answers | "no lane" reported as agreement | U | `::test_refuses_to_answer_when_no_lane_can_be_resolved` · `::test_an_unknown_lane_is_a_usage_error` · `::test_missing_cos_ops_dir_is_a_usage_error` |
| `cos_deployed_version --expect` | a version found on a non-executing surface never satisfies `--expect` | a pin move gated on a claim instead of a readback | U | `::test_a_version_on_a_non_executing_surface_never_satisfies_expect` · `::test_explicit_lane_lets_the_operator_assert_the_desktop_store` |
| run-report readback | both report wordings parse; "no version stated" is not a version | a phrase read as a version number | U | `::test_both_run_report_wordings_are_recognised` · `::test_a_run_report_that_states_no_version_is_not_a_version` |
| **cowork-desktop surface RETIRED** (DEP-03) | while an ACTIVE Codex automation executes another version, `--lane cowork-desktop` returns `UNSUPPORTED` (exit 2) **before** `--expect` is evaluated — the retired surface can neither satisfy nor refute an expectation, and `newest_deployed` is `None` | the two false freeze alarms: a stale store quoted as the deployment | U | `tests/test_cos_deployed_version.py::test_the_stale_desktop_surface_returns_unsupported_not_a_version` |
| retirement is reversible by the owner, not by a flag | a store that matches the executing lane answers normally again | a retirement nobody can undo, so it gets worked around | U | `::test_re_uploading_the_current_bundle_makes_the_surface_answerable_again` |
| run manifest never stamps from the retired surface | `cos_deploy.deployed_skill(lane="cowork-desktop")` raises `SurfaceUnsupported` | a run stamped with a version that never executed | U | `::test_the_run_manifest_never_stamps_from_the_retired_surface` |
| `brain doctor` deployed-skill row (DEP-02) | reports the executing lane, version, extraction-rules version, digest and path — and names the retired store when it disagrees; **never** gates the exit code | "which version runs tonight" answerable only by knowing which surface to read | N/A-info | `tests/test_doctor.py::test_cos_deployed_skill_row_names_the_executing_lane` · `::test_cos_deployed_skill_row_is_not_detectable_without_a_deployment` (known-negative: no COS deployment is HEALTHY) · `::test_cos_deployed_skill_row_never_gates_the_exit_code` |
| module drill | assert-based, over the two failures that actually happened, plus the DEP-03 refusal and its reversal | — | U | `tests/test_cos_deployed_version.py::test_the_module_selfcheck_passes` |

---

## 5 · VM boundary (`scripts/vm-boundary-probe.sh`)

Negative privilege probes run from inside the Cowork VM. The original defect is
the canonical shape B: `brain ingest-transcript` with no argument exited 2 on an
**argparse usage error**, and the probe scored that as a refusal.

| Probe class | Checks | Fails how | Cond. | Proof it can fail |
|---|---|---|---|---|
| every negative probe reaches the gate | each `must_refuse` invocation is well-formed enough to reach the role check — a usage error or crash is an **INVALID PROBE**, never a pass | the 2-of-14 vacuous probes | U | `tests/test_vm_boundary_probe.py::test_every_negative_probe_reaches_the_role_gate` · `::test_a_non_zero_exit_alone_is_not_scored_as_a_refusal` |
| refusal recognition | the probe's regex matches the engine's **real** refusal message | a changed message turning every probe vacuous | U | `::test_the_refusal_regex_matches_the_engines_real_message` |
| coverage of the host-only surface | every named host-only verb, and every host-only `cos-*` verb, is probed | a new privileged verb shipping unprobed | U | `::test_the_named_host_only_verbs_are_all_probed` · `::test_every_host_only_cos_verb_is_probed` |
| no false negatives | no probe asserts a refusal for a VM_ALLOWED verb; the one VM-allowed `cos-*` verb is exercised as a **positive** | a probe suite that would pass on a broken engine | U | `::test_no_probe_asserts_a_refusal_for_a_vm_allowed_verb` · `::test_the_one_vm_allowed_cos_verb_is_exercised_as_a_positive` |
| observation vs breach | host-private readability is reported as an **observation**, not scored as a breach | crying wolf on the mount's own geometry | N/A-cls | `::test_host_private_readability_is_an_observation_not_a_breach` |
| unimportable engine | an engine that will not import is INCONCLUSIVE, never a pass (the `ModuleNotFoundError` probe) | shape B | U | `::test_an_unimportable_engine_is_inconclusive_not_a_pass` |
| script integrity | the script is syntactically valid bash | a probe file that never ran at all | U | `::test_the_script_is_syntactically_valid_bash` |
| **durable staging** (DEP-02, 0.19.22) | the probe rides the wheel (`ENGINE_ASSET_FILES`) and is staged `0755` into `<workspace>/vault/.brain/` by BOTH staging paths — `brain update` and `tools/cowork_workspace_install.sh` | the caveat below, now closed: a hand copy the next re-stage deletes, leaving a boundary claim nobody can re-measure | U | `tests/test_update.py::test_stage_stages_vm_boundary_probe_executable` · `::test_cowork_installer_stages_the_boundary_probe_too` · lockstep: `tools/package_clients.py --validate-only` (proven able to fail — tampering with the `_assets` mirror copy exits 1 with `engine asset mirror stale`) |

> ~~The staged copy is a manual `cp`~~ — **closed in 0.19.22** by the staging
> row above. What is still not verified is that a VM is running the *current*
> script rather than an older staged one; the probe prints no version of its
> own. Tracked, not closed.

---

## 6 · Substrate + release instruments

| Instrument | Checks | Lives in | Cond. | Proof it can fail |
|---|---|---|---|---|
| bitemporal per-note types | ISO dates, real booleans, resolvable ids | `tools/validate.py::check_bitemporal_note` | C | `tests/test_bitemporal.py::test_malformed_date_is_error` · `::test_legacy_note_with_no_bitemporal_keys_still_validates_clean` |
| supersession: dangling link | `superseded_by` resolves | `validate.py::check_bitemporal_global` | C | `tests/test_bitemporal.py::test_dangling_superseded_by_is_error` |
| supersession: retirement completeness | `is_latest_version: false` requires `superseded_by` | same | C | `::test_is_latest_version_false_requires_superseded_by` |
| supersession: no self-supersession | a note cannot supersede itself | same | C | `::test_self_supersession_is_error` |
| supersession: no cycles | the chain is acyclic | same | C | `::test_cycle_is_error` |
| supersession: no forks | two successors cannot claim one predecessor | same | C | `::test_fork_two_successors_same_predecessor_is_error` |
| supersession: one latest per chain | at most one `is_latest_version: true` | same | C | `::test_more_than_one_latest_in_chain_is_error` |
| supersession: classification on both sides | both ends of every link carry an explicit `classification` | same | C | `::test_successor_missing_classification_in_chain_is_error` · `::test_predecessor_missing_classification_in_chain_is_error` |
| supersession: warn-only reciprocity | a missing reciprocal link WARNS, never errors | same | C | `::test_missing_reciprocal_link_is_warn_only` |
| valid-chain direction | a correct chain validates clean (non-wolf-cry) | same | C | `::test_valid_supersession_chain_validates_clean` |
| provenance type checks (PRV-01/02) | `provenance.sent` ISO, `provenance.verified` boolean, unknown subkey WARNS | `validate.py::check_provenance` | C | `tests/test_cos_instruments.py::test_provenance_type_checks_fire_on_a_known_positive` **(added by this sweep — it had no test at all)** |
| type lint (warn-only) | unrecognised `type:`, `concept` without counter-arguments, `decision` without a source anchor | `validate.py::check_type_lint` | C | `tests/test_typed_entities.py::test_unrecognized_type_warns_not_errors` · `::test_concept_without_counter_arguments_warns` · `::test_decision_without_source_anchor_warns` |
| alias collisions | cross-note alias collision warns; local invalids rejected | `validate.py::check_alias_collisions` | C | `tests/test_named_entity_retrieval.py::test_alias_validator_rejects_local_invalids_but_warns_cross_note_collision` |
| state-MOC section staleness | a stamped section past the threshold warns; warn-only never blocks | `validate.py::check_section_staleness` | C | `tests/test_zone_catalogs.py::test_section_one_day_past_threshold_is_flagged` · `::test_section_within_threshold_is_not_flagged` · `::test_staleness_lint_is_warn_only_never_blocks_the_gate` |
| **release contamination scan** | denylist terms absent from the export tree; **comments/blanks stripped first** (the 0.16.0 defect), missing denylist refused, `--denylist` required | `tools/publish_release.py::step_contamination_scan` | U | `tests/test_publish_release.py::test_step_contamination_scan_detects_a_real_hit` (the known positive) · `::test_step_contamination_scan_reports_zero_hits_for_a_denylist_term_absent_from_export` · `::test_step_contamination_scan_refuses_missing_denylist` · `::test_contamination_scan_refuses_without_denylist_flag` |
| release evidence pass | evidence hits are **informational**, labelled as such, and never fail the run | `publish_release.py` | N/A-cls | `tests/test_publish_release.py::test_evidence_hits_are_informational_only_and_never_fail_main` · `::test_evidence_hit_print_is_labeled_informational_not_expected_zero` |
| **shipped-mirror equality** | `src/brain/_assets/tools/*` is byte-identical to `tools/*` — the installed engine re-executes the mirror, this suite tests the checkout | `cos_runverify.tools_dir` fallback | U | `tests/test_cos_instruments.py::test_the_shipped_mirror_of_a_checker_is_byte_identical_to_the_tested_one` **(added by this sweep)** |
| inventory integrity | every proof reference in **this file** resolves to a real test or an executed `--selfcheck` | `docs/cos-instrument-inventory.md` | U | `tests/test_cos_instruments.py::test_every_proof_reference_in_the_inventory_resolves` |

### Commit-time reality — stated plainly

There is **no commit-time contamination gate.** `.pre-commit-config.yaml`
installs semgrep (`p/python`, `p/secrets`) only, which finds credentials and
never a client name. The denylist scan runs at **release/export** time and only
over the export tree. Anything that leaves this repo by another route — a
gearbox harvest, a hand-copied artifact — passes no confidentiality check at
all. That is a documented posture, not a gate, and it is not counted as one
here.

---

## 7 · The E-checks

> **SUPERSEDED FOR THE CURRENT BUNDLE (2026-08-14, DOCTRINE v7 / kernel v7.1).**
> Everything below describes the THIRTY self-reported E-checks of doctrine v1,
> and it was the honest reading of them. It is no longer the reading of the
> checks that run. Doctrine v7 §8 defines **TEN** checks, and both halves of the
> defect this section named are closed:
>
> * **The HOST answers them**, not the producer. `brain.cos_echecks` derives
>   every one from the run's own artifacts — the ingestion and undo ledgers, the
>   frozen plan and its binding, the sent baseline, the category-gate
>   recomputation, the grounding declaration and the run manifest's frozen
>   capability digest — and writes the answers into `_cos_nightly_<run>.md`
>   after the apply. No answer is a model self-claim. Each carries its
>   DENOMINATOR, so a check scored on a run that did nothing says so.
> * **`check_self_eval` decides on OUTCOMES.** Until 2026-08-14 it captured the
>   PASS/FAIL/N/A token and discarded it (`for n, _ in ...findall`), so a report
>   whose every check said FAIL scored the control PASS — probed, not asserted.
>   It now builds an `id -> result` map: any FAIL fails the control, a
>   duplicated id with conflicting results fails it, and every `N/A` is
>   corroborated against a HOST-DERIVED zero denominator (an `N/A` over a
>   non-zero denominator is a FAIL).
>
> Read the rest of this section as the record of what the v1 list WAS, and
> DOCTRINE.md §8.2 as the authority on what the current ten are.

**This was the load-bearing conclusion of the sweep.** The v1 E-checks lived in
the chief-of-staff skill and were executed by the model running the nightly,
which then wrote its own PASS/FAIL. Under the rule in §0 that is not a gate:
nothing independent could make one fail.

**What the host enforced THEN:**

* `cos_runverify.check_self_eval` proved **a result line exists for every id
  1..N**. It did not and could not check that a reported PASS was true.
* Seven checks have some clause re-executed host-side: **E1** (zero-send, via
  `cos_contract`), **E5** and **E15** (counters, via `cos_reconcile_metrics`),
  **E10** (metrics row exists + recounts), **E16** (stamps + ledger join),
  **E28** (the contract verdict — the only one **fully** re-derived), **E29**
  (ledger existence + counters + the observation guard).
* **The other 22 have no host control whatsoever.** Their only proof is a
  doctrine-text fixture in `tests/skills/test_chief_of_staff_fixtures.py`
  asserting the *sentence is present in SKILL.md*. That is a real proof of a
  real property — the rule has not been silently deleted — and it is **not** a
  proof that the check can fail on a bad run. The two must never be conflated
  again.

| E | Obligation (one line) | Cond. | Host control that can make it fail |
|---|---|---|---|
| E1 | action ledger contains only allowed verbs; zero send/delete/unread-touch; missing ledger is a FAIL | M | **partial** — the zero-send half via `cos_contract` `ZS-*` (§2) |
| E2 | brief sections 2–10 + companion exist, image containment | U | none |
| E3 | every response-warranted row has a drafts-ledger entry or a logged skip | C | none |
| E4 | every target-day calendar event appears or is skipped; blocked ⇒ N/A | C, N/A-cls | none |
| E5 | ledger counts equal state-file execution counts, verified rows only | C | **partial** — `cos_reconcile_metrics` shortfalls (§3) |
| E6 | every brain-sourced fact carries a resolvable note id | C | none |
| E7 | any skipped phase ⇒ banner + BLOCKED block | C | none |
| E8 | same-night re-run no-ops | C | none |
| E9 | every finding draft-captured; every `cos-ops/` write ledgered | C | none |
| E10 | calibration footer + **a metrics row for THIS RUN** | U | **yes** — `check_metrics_row` + `append_metric` |
| E11 | zero live web-egress while private context loaded | U (negative) | none — self-reported by the leg being constrained |
| E12 | trifecta preflight ran; companion carries the proof line | U | none |
| E13 | OpEx line present; exactly one record appended | U | none |
| E14 | one verdict line per substantive thread; no raw mail quote in evidence | M | none |
| E15 | every executed archive/mark row carries a verification result | C | **partial** — verified-only counting (§3) |
| E16 | candidate evidence, classification, dedup, stamps, ledger join | C | **yes** — `check_candidate_stamps` (stamp/join clause only) |
| E17 | every auto-archived row carries the full undo field set | C | none |
| E18 | IF Phase 4.6 registered/reviewed anything, findings + ledger exist | C | none |
| E19 | chip gate closed ⇒ zero applications; banner states rollout status | M | none |
| E20 | every chip clear carries the CLOSED trigger verbatim | C | none |
| E21 | ANTICIPATE component present; authority-matrix conformance | M | none |
| E22 | any-sender shadow lane + inbox-zero metrics integrity | M, N/A-cls | none |
| E23 | IF a chip exceeds 14 days on a Sunday, one stale-chip digest row | C, N/A-cls | none |
| E24 | transport preflight names both failure modes distinctly | U | none |
| E25 | every recurring-digest disposal had ≥2 same-subject instances | C | none |
| E26 | chip re-evaluation stayed inside the shared per-run cap | M, N/A-cls | none |
| E27 | exactly one invocation tier applied and named | U | none |
| E28 | the `outcome_contract` block exists and equals what the checker computes | U | **yes, fully re-executed** — `check_contract` (§1, §2) |
| E29 | ingestion + attachment run-obligation: ledger exists, cap recounted, counters equal ledger, never-category discipline, preview rendered | M, N/A-cls | **yes** (a)(c) — `check_ingestion_ledger`, `observation_guard`, counter recount |

**Conditionality tally:** 10 unconditional, 12 conditional-only, 7 mixed —
i.e. **12 checks are vacuous on a run that produced nothing**, which is the
exact failure mode E29 was created to answer for one of them.

---

## 8 · Known gaps carried forward — CLOSED by S11

The six defects found by S04's adversarial rounds were recorded with file:line
and fix shape in `docs/cos-ops.md` "Known gaps". All six are repaired, and each
repair was **proven able to fail**: the negative-control run
(`_evidence/cos2/s11/negative-controls.txt`) reverts each guard's property in
the real source, one at a time, and records the named test going red.

1. reader detection is idiom-bound (`json.load(fh)` invisible) — shape A —
   **CLOSED**: all parse idioms + module level + one hop through a `_read_*`
   helper, with a known-positive probe on the detector itself
2. the guard check is a source grep, not a dataflow proof — shape A —
   **CLOSED**: replaced by a taint check (parsed value → path expression),
   also probed with a known positive
3. `_safe_meta_path` is resolve-then-use (TOCTOU) — shape B — **CLOSED** by
   deleting the primitive: mount-written path fields are reduced to a basename
   joined onto a host-derived root, and the attachment payload is derived from
   the guarded id + the real directory entry
4. the single-writer lock is still on the mount — shape B — **CLOSED**:
   `config.writer_lock_path` → `config.host_lock_dir()`, off-mount, per vault
5. the no-raw-write gate exempts `os.open` unconditionally — shape A —
   **CLOSED**: the flags decide, an unresolvable flag expression counts as a
   write, non-literal modes and `r+` are caught
6. the shared-zone permission test bypasses its production wiring — shape A —
   **CLOSED**: driven through `write_run_manifest`, plus a separate assertion
   on the fchmod-before-replace ordering

Two bounds are stated rather than closed, and both are recorded in
`docs/cos-ops.md`: reader-hood does not propagate past the one `_read_*` helper
hop (the full closure was measured — 81 classified functions instead of 40, the
extra 41 pure orchestration), and `maintain_lock_path` remains on the mount as
a single-*runner* lock whose loss costs a duplicate maintenance run, not two
writers on one index.

**Round 2 (2026-08-02) re-audited the instruments themselves, and two of them
were reporting a property they did not hold** — the exact failure this whole
plan exists to remove, found in the plan's own tooling:

- *the taint check* returned ZERO findings for flows named in its own
  docstring: a `_read_*` helper's return value, taint through an intermediate
  binding, sinks other than `Path()`/`/`, and the `GUARDED:<consumer>`
  functions the census delegates to. All four are covered and each is a case in
  the known-positive probe. The repaired detector immediately found a real
  defect (`ingest_sweep`'s inline `os.path.basename` equality test, which a
  Windows-style traversal passes on POSIX);
- *the negative-control runner* scored on pytest's RETURN CODE, so a mutation
  that made the target test die of an unrelated exception scored identically to
  the guard's assertion firing — one case did exactly that. A control now
  passes only when the NAMED test failed and it failed on an assertion, read
  from a `pytest_exception_interact` plugin rather than from the `-rf` summary
  line (which prints assertion SOURCE text for a bare `assert`, so it cannot
  answer the question). Three previously uncovered guards — `_leaf_in`'s
  symlink refusal, `attachment_anchor_exists`' two-key logic, and
  `clear_attachment_anchor` — have controls now, and the suite passes the
  corrected bar.

**Round 3 (2026-08-02) added eight controls for the four findings it closed**
(the mount boundary read from mount-resident markers, the undiagnosed lock
fallback, the re-resolved claimed entry and its size cap, the destination name
taken from the sidecar on both lanes, the demotion re-check reading the sidecar
category, and a swallowed unreadable release record). **The suite is 42 cases,
all PASS** — the count is the runner's, and this is the only place the doc
restates it, because the previous revision said "33" while the runner had 34.
Two round-3 corrections are in the instruments themselves: the runner's
docstring described a per-case `expect` override it never implemented, and
`os.path.basename` was still listed as a taint-clearing guard in
`tests/test_cos_pathguard.py` — the very idiom round 2 caught — and is now
removed. One break is NOT expressible as a single-hunk mutation (restoring
bare-name matching in the release-record net spans two files); it was applied
by hand, recorded in the runner beside the case list, and the named test failed
on its assertion. Still open in the instrument, recorded in `docs/cos-ops.md`:
the taint detector does not clear a variable's clean state on REASSIGNMENT, and
the `_read_*`-leak control is scored by the census test rather than by the
taint test it is meant to prove.

**Round 4 (2026-08-02) replaced one control rather than adding one, so the
suite is still 42 cases, all PASS.** The compat single-writer lock it covered
was dropped (a lock file on the VM-writable mount cannot exclude anything, and
it put the lock acquisition's `ftruncate` behind a plantable name — restaging
the pinned engine first is an ordered release step now, see the INT-05 appendix
in `docs/release-runbook.md`). Its slot is taken by the property that outlived
it: removing `O_NOFOLLOW` from `lock._open_lock_fd` makes
`test_a_symlinked_lock_path_is_never_followed` go red on its own
`pytest.raises` — the lock open follows the link and truncates the decoy.

---

## 9 · INS-03 — can the producer contract be structurally smaller?

**Bounded finding. Answer: yes for one specific class, and the evidence says the
migration is already the thing that works. Everything else is WONTFIX for now,
with reasons.**

### What runs 50–60 actually violated (measured, 2026-07-31)

Read off the real run artifacts. **Only ids and counts are reproduced here — no
mail content, no names, no subjects.**

| Obligation | Result over runs 50–60 |
|---|---|
| **self-eval reported at all** | **2 of 11**. Nine runs wrote no E-check results in any recognisable form. |
| **self-eval keyed to the doctrine's ids** | **0 of 11**. Run 60 emits a full `E01`–`E29` block whose labels do not correspond to the doctrine's checks (its "E05" is a pin comparison; doctrine E5 is ledger completeness). Run 56 collapses checks into narrative ranges. The self-eval has drifted from an id-keyed check list into free-form self-narration. |
| **a metrics row for this run** (E10) | **7 of 11**. Runs 50, 53, 54 and 60 appended none. This is an unconditional obligation, and nothing noticed. |
| **the four Phase-1.6 counters** (E29c) | present on every run **after** v5.36 shipped (57, 58, 59); absent before. Rollout, not violation. |
| **candidate stamps** (E16) | run 59's eight unstamped candidates — the defect v5.39 was written for. |

**Position/density correlation.** The E-check section starts at **93.5% through
the file** (line 3470 of 3712) and is **10.6% of its 380,861 characters** — the
last block in a ~95k-token contract. The observed violation is not per-check
drift: it is **whole-section omission at the file tail**, all-or-nothing, in 9
of 11 runs. With n=11 and a binary outcome there is no meaningful per-check
position signal to compute, and claiming one would be exactly the kind of
instrument this document exists to reject. What the data does support: *the last
section of a very long contract is the one that gets dropped*, and the two runs
that did emit it emitted something that no longer matches the list.

Per-check size, for the record: median 1,068 characters, largest E29 (3,873),
E26 (3,647), E22 (3,459), E16 (3,140). The four largest are the four most
recently repaired — **the repair mechanism is "add prose to the check", and the
checks that have failed most are now the longest**.

### Which obligations are mechanically host-checkable

Every one of the four violation classes actually observed is host-checkable, and
three of the four already migrated:

| Class | Host-checkable? | Status |
|---|---|---|
| self-eval reported, all ids present | yes (ids only; verdict truth is not checkable) | **migrated** — `check_self_eval`, hardened to a set by this sweep |
| a metrics row exists and its counters recount | yes, fully | **migrated** — `check_metrics_row`, `append_metric` |
| ingestion ledger exists / is not vacuous | yes, fully | **migrated** — `check_ingestion_ledger` + `observation_guard` |
| candidate stamps + ledger join | yes, fully | **migrated** — `check_candidate_stamps`, and v5.39 moved the *source* host-side so the producer stops claiming them |

Beyond those, the checks that could migrate wholesale are the ones whose
predicate is a property of an **artifact the host can read**: E5, E13 (exactly
one OpEx record), E15, E17 (field-set completeness on ledger rows), E20 (trigger
string on clear rows), E25 (≥2 same-subject instances), E27 (exactly one tier
record). Those are file-shape assertions, not judgment.

The ones that **cannot** migrate are the ones whose predicate is about what the
model *did* rather than what it *wrote*: E11 (zero egress while private context
loaded), E12 (a preflight actually ran), E7 (a skipped phase is honestly
banner'd), E14's "evidence carries no raw mail quote", E6 (a fact is genuinely
brain-sourced). Those are self-report by construction — and they should be
**labelled** self-report rather than listed alongside enforced checks.

### The decision

**Recommended (and the trajectory already in flight): migrate obligations
host-side by CLASS, not one defect at a time, and shrink the doctrine to what
only the model can assert.** Each migration deletes prose from the producer
contract rather than adding to it — which is the opposite of the last four
repairs. The seven file-shape checks named above are the next batch.

**WONTFIX, with reasons, for this cycle:**

* **Splitting SKILL.md into separately-loaded phase modules.** The failure
  measured here is section omission at the tail, and a module the harness does
  not load is *guaranteed* omission rather than probable omission. Modularising
  is a real option only once the E-checks no longer live in the producer file at
  all — otherwise it moves the tail, it does not remove it.
* **Rewriting the 22 unenforced E-checks now.** They are the cheap half of the
  contract to state and the expensive half to enforce; rewriting them without
  host controls changes nothing measurable. §7's relabelling is the deliverable
  — they must stop being *counted* as gates in any report.
* **A per-check compliance metric.** n=11 with whole-section omission cannot
  support it. It becomes worth building once `cos-run-begin` manifests exist for
  a real window and `check_self_eval` has scored actual runs — currently zero.

**The trajectory is now a decision, not a drift:** the contract grows only where
the host cannot check, and every host migration must delete the corresponding
producer prose in the same change.

---

## 10 · Maintenance contract

**A new gate ships with an inventory row and an executable proof. A gate without
both is the defect, not the row's absence.**

1. **Write the known positive first.** Feed the instrument the violation and
   watch it fail *before* you make it pass. If you never saw it red, you do not
   have a gate.
2. **Prove the degraded condition too** (shape B). If the guard can become a
   no-op — a missing platform flag, an unresolvable lane, an unimportable
   module, an error path — assert it refuses or reports INCONCLUSIVE in that
   state. Never that it "passes".
3. **Drive it through production wiring.** A test that calls the helper directly
   cannot notice the production call site losing the argument.
4. **Prove the property, not a proxy.** Grepping a source file for a guard token
   is not proof that the guarded value reached the sink.
5. **Then add the row here.** The proof reference must be an executable pytest
   node id, or a `--selfcheck` that `tests/test_cos_instruments.py` runs. A
   documented drill nobody executes is a claim.
6. **No exemptions.** If you cannot build an honest proof cheaply, **relabel the
   instrument non-enforcing** and remove it from everything that cites it as a
   gate (§7 is the worked example). Do not invent a weak proof to fill a row.

### Row template

```markdown
| `<instrument name>` | <the property, one line> | `<file>::<function>` | <what a wrong verdict lets through> | U/C/M/N-A | `tests/test_x.py::test_the_known_positive` |
```

`Cond.` values: `U` unconditional · `C` conditional (state what makes it
vacuous) · `M` mixed · `N/A-cls` declared not-applicable classes (each class
needs its own assertion, or it is a suppression hatch).

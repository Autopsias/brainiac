---
alwaysApply: true
---

# Before you commit — test suite and quality ratchet

> Moved verbatim from `AGENTS.md` during the S05 context diet (2026-08-22).
> `AGENTS.md` remains canonical for Codex/Gemini; this rule is the Claude Code
> path-scoped copy — always-loaded because it names no directory.

### Running the test suite

Run the full suite in PARALLEL. Sequentially it takes ~15 minutes; with eight
workers it takes ~6, and the pass set is identical:

```bash
.venv/bin/python -B -m pytest -n 8 --dist loadfile --timeout 300 -q \
  --deselect tests/test_cos_runverify_body_corpus.py::test_corpus_join_zero_false_positives_across_every_real_historical_run \
  tests
```

Three parts of that line are load-bearing, and each is a measured lesson:

- **`--dist loadfile`** keeps every test in one FILE on one worker. That is what
  makes the parallel run stable — the `fcntl` lock tests, the autouse env
  isolation and the node suite all assume file-local ordering. Do not "improve"
  it to `--dist load`.
- **`--timeout 300`** bounds a hung test at five minutes. Without it, one hang
  blocks a gate until the gate's own timeout, and the run reports nothing.
- **No `-x`.** For consecutive runs you want the whole failure list, not the
  first one; a 2026-08-13 run was wasted re-running the suite to see the rest.

The one deselect asserts against LIVE host COS ledgers and is machine-bound by
design; it is deselected in the sequential gates too, so parallel coverage
equals sequential coverage. **Any OTHER deselect needs its cause written down
and re-checked** — two historical ones were blamed on parallelism and turned
out to be a date-rotted clock read and a live-vault leak (see
`tests/test_doctor.py::_no_live_cwd_vault`). A deselect that outlives its cause
is a check that cannot fail.

While working, run only the tests you changed. Run the whole suite ONCE, at the
gate.

### The quality ratchet at commit time

The pre-commit hooks include three ratchet checkers (file size, function
length, complexity). They judge ONLY the files you staged, and they block only
what your commit makes worse than every commit parent. Rules:

- **Never `git commit --no-verify`.** It skips EVERY hook, including semgrep
  and the packaging gate. No ratchet complaint justifies dropping those.
- If a ratchet hook still blocks you wrongly, skip that hook alone and say why
  in the commit body: `SKIP=file-size-ratchet git commit ...` (comma-separate
  for several: `SKIP=file-size-ratchet,complexity-ratchet`). CI
  (`quality-ratchet.yml`) re-runs all three checkers whole-project on every
  push, so a skip is visible, never final.
- **Merging a long-lived branch:** when the merge warns about inherited debt,
  re-record the baselines IN the merge commit — run
  `python3 tools/check_file_sizes.py --generate-baseline` (and the
  function-length and complexity siblings), review that the diff only admits
  files the branch already carried, `git add` the three baseline files, and
  complete the merge. Never regenerate a baseline to absorb debt authored in
  the commit itself. CI stays red until the re-record lands.
- The checkers in `tools/` are vendored copies; the source of truth is
  `~/.claude/scripts/quality/`. Never edit them here — re-sync with
  `python3 ~/.claude/scripts/quality/vendor_quality.py .`.

---

---
alwaysApply: true
---

# Before you commit — test suite and quality ratchet

> Moved verbatim from `AGENTS.md` during the S05 context diet (2026-08-22).
> `AGENTS.md` remains canonical for Codex/Gemini; this rule is the Claude Code
> path-scoped copy — always-loaded because it names no directory.

### Running the test suite

Run the full suite in PARALLEL. Sequentially it takes ~15 minutes; with eight
workers it takes ~4-11 depending on what else is running on this Mac (see the
last bullet below), and the pass set is identical:

```bash
.venv/bin/python -B -m pytest -n 8 --dist loadfile --timeout 300 -q \
  --deselect tests/test_cos_runverify_body_corpus.py::test_corpus_join_zero_false_positives_across_every_real_historical_run \
  tests
```

Four parts of that line are load-bearing, and each is a measured lesson:

- **`--dist loadfile`** keeps every test in one FILE on one worker. That is what
  makes the parallel run stable — the `fcntl` lock tests, the autouse env
  isolation and the node suite all assume file-local ordering. Do not "improve"
  it to `--dist load`.
- **`--timeout 300`** bounds a hung test at five minutes. Without it, one hang
  blocks a gate until the gate's own timeout, and the run reports nothing.
- **No `-x`.** For consecutive runs you want the whole failure list, not the
  first one; a 2026-08-13 run was wasted re-running the suite to see the rest.
- **`-n 8` assumes this Mac is otherwise idle. It is not, routinely.** Two
  self-hosted GitHub Actions runners execute OTHER repositories' suites here;
  they start on a push and no agent tool lists them — and `ListAgents` does not
  list another Claude session's own 8-worker suite either. On 2026-09-05 four
  runs here died before they finished, one of them a single reviewer process,
  and the same tree measured 673 s, 353 s and 257 s that morning with 6509
  tests passing every time: a 2.6x spread, no code difference. So read the load
  before any run whose SECONDS you intend to report, and again before you blame
  a kill on your own workload — `uptime`, `memory_pressure`, and
  `ps -eo pid,rss,etime,command | grep "[a]ctions-runners.*pytest"`. **Read
  memory with `memory_pressure`, never with `vm_stat`'s "Pages free".** This
  bullet said the runners "took free memory to 66 MB" until 2026-09-05, and
  both sessions that wrote that had read it off `Pages free` alone. On macOS
  that counter sits near zero as a matter of course: measured the same day it
  read 68 MB while `memory_pressure` reported the system 44% free with a full
  suite running, because inactive pages are reclaimable and `vm_stat | head -3`
  does not even show them. The four kills happened; the memory figure attached
  to them was never evidence of them, and the instrument this bullet used to
  name would have had a reader wait out a starvation that was not there.
  WAIT the runners out rather than lowering the worker count blindly:
  `until ! ps -eo command | grep -q "[a]ctions-runners.*pytest"; do sleep 20;
  done`. With them running, `-n 6` survives where `-n 8` does not. Never report
  a runtime delta as the effect of a code change unless both runs carry a load
  reading.

The one deselect asserts against LIVE host COS ledgers and is machine-bound by
design; it is deselected in the sequential gates too, so parallel coverage
equals sequential coverage. **Any OTHER deselect needs its cause written down
and re-checked** — two historical ones were blamed on parallelism and turned
out to be a date-rotted clock read and a live-vault leak (see
`tests/test_doctor.py::_no_live_cwd_vault`). A deselect that outlives its cause
is a check that cannot fail.

While working, run only the tests you changed. Run the whole suite ONCE, at the
gate.

After a run with ONE failure you have diagnosed and fixed, re-run that test
alone and count the suite as green. Do not run the suite "once more for a
clean reading", and do not retry a kill at fewer workers: on 2026-09-05 one
session ran it six times between review passes (12:51, 18:37, killed,
32:02 at `-n 4`, 10:18, 8:45), 82 minutes, on a tree whose only red was
explained each time.

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

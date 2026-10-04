# Sunday synthesis with Codex (opt-in)

The stock `com.brainiac.synthesis` job calls Claude CLI. A Codex-only host
therefore skips synthesis even when interactive Codex retrieval works.
`scripts/brain_synthesis_codex.py` is a bounded alternative: the host prepares
a classified packet, Codex returns JSON proposals, and the host validates,
sanitizes, signs, indexes and publishes new notes. Claude remains unchanged.

## Scope and limits

This adapter implements the standing source-linking lane, not every Claude
synthesis activity. It automatically creates only new `source-derived`
resource notes, with bare-id body citations and zone-qualified `source:`
anchors. It never edits existing notes, retires sources, or records decisions.
MOC/index refresh, promotion, supersession and existing-note link work are
review suggestions in `result.json`, **not completed operations**. Only bounded
knowledge context and cadence fields are provided; this is not a complete
watchdog audit. The shared success heartbeat means this bounded pass finished,
not that full Claude-lane parity was achieved. Use `brain doctor`, alerts and
the hourly folds for the wider health picture.

Defaults: at most 40 sources, 8 new notes, 120,000 source characters (16,000
per source), 12 knowledge notes (2,500 characters each), and 20 minutes of model
time. Short extraction stubs are skipped without starving later sources.
For an operator-run backfill, `--source-offset N` rotates the canonical eligible
lane without excluding sources or changing coverage counts; the weekly default
remains worst-first at offset zero.
Classification ceiling defaults to `Internal`. Export requires the explicit
`--allow-cloud-synthesis` operator opt-in, including for `--dry-run`.
`--max-tier MNPI` allows every classification, but must be an informed owner
choice; it does not remove execution budgets or signing controls.

Codex runs with a read-only sandbox, no shell/MCP/browser/apps/plugins/hooks,
and no inherited Brainiac key variables. It uses an ephemeral session in a
temporary non-project directory, ignores user config/rules, and requests a
JSON schema. The wrapper rejects observed tool calls or failed turns. This
limits tool access; it does not prove the model will ignore every injection
or produce semantically correct notes. Audit signatures attest host writes,
not the truth of generated prose. The bounded context is not a corpus-wide
semantic duplicate check; deterministic source-group IDs prevent exact retry
overwrites, not every possible conceptual duplicate.

The engine holds no model credentials. Install Codex CLI separately and sign
in locally with `codex login`. Do not copy or commit authentication files.
The adapter was exercised with Codex CLI 0.139.0; older versions may reject
flags/feature names. Do not work around that by enabling tools or bypassing
sandboxing. Run the synthetic smoke test after CLI updates. No desktop model
alias is hardcoded: omit `--model` for the CLI default, or supply a model
supported by your account. This is a local owner-run job, not a public CI job.
See [official non-interactive Codex documentation](https://learn.chatgpt.com/docs/non-interactive-mode).

## Validate before scheduling

Use the Python interpreter from the environment where Brainiac is installed.
Resolve all paths to absolute paths before putting them into launchd.
The adapter also ships under the installed package's `_assets/scripts/`.

```sh
python scripts/brain_synthesis_codex.py \
  --vault "$BRAIN_VAULT" --brain-bin "$(command -v brain)" \
  --codex-bin "$(command -v codex)" --preflight

python scripts/brain_synthesis_codex.py \
  --vault "$BRAIN_VAULT" --brain-bin "$(command -v brain)" \
  --codex-bin "$(command -v codex)" --smoke-test
```

Preflight checks local login without sending vault content. Smoke test sends
only a synthetic public garden example and creates no vault note or heartbeat.
After deciding the permitted export ceiling, test real proposals without writes:

```sh
python scripts/brain_synthesis_codex.py \
  --vault "$BRAIN_VAULT" --brain-bin "$(command -v brain)" \
  --codex-bin "$(command -v codex)" \
  --max-tier Internal --allow-cloud-synthesis --dry-run
```

Real runs refuse an incomplete initial index or missing semantic vectors.
Writer contention returns 75; retry once indexing finishes. Model work runs
outside the writer lock. Before writes, the host reacquires the lock, opens
a fresh index, rechecks source body hashes/classifications and refuses symlink
destinations. Partial signed batches are synced even if a later write fails;
there is no claim of all-or-nothing note creation.

## Replace the existing Sunday job on macOS

Do not add a third task. Back up the existing
`~/Library/LaunchAgents/com.brainiac.synthesis.plist`, unload it, and replace
only its `ProgramArguments` with these separate array entries:

```text
/absolute/path/to/brainiac-environment/bin/python
/absolute/path/to/scripts/brain_synthesis_codex.py
--vault
/absolute/path/to/vault
--brain-bin
/absolute/path/to/brain
--codex-bin
/absolute/path/to/codex
--max-tier
Internal
--allow-cloud-synthesis
```

Keep label `com.brainiac.synthesis`, Sunday 08:00 calendar interval,
`RunAtLoad=false`, log paths and a PATH that resolves your installed tools.
Retain the host's Keychain-account configuration if required; never put the
signing key itself in the plist. Validate with `plutil -lint`, then reload the
same agent. Omitting `--dry-run` enables signed writes, so do this only after
approving cloud export and reviewing the dry-run artifacts.

This initial adapter selects **one explicit vault** rather than iterating the
workspace registry. A multi-vault deployment needs a trusted sequential wrapper
on the same task with separate approved ceilings. Installer/provider persistence
is not yet automated: `install-brief-mac.sh` can restore the stock Claude plist.
Check the program arguments after reinstallation or an engine update. Restore
the backed-up plist to roll back; created notes remain audited, never deleted.

## Artifacts, heartbeat and tests

Private artifacts default to `~/.brain/synthesis-runs/<unique-run>/`: packet,
Codex events/stderr, proposals and final result. They may contain every approved
classification. Keep this directory and `~/.brain/synthesis-state.json` off VM
mounts and out of Git/cloud shares; directory/file modes are owner-only for
new artifacts. No automatic retention deletion is performed. `--runs-dir` and
`--state` can relocate them to another host-private location outside the vault.

Production updates the existing per-vault state with provider, return code,
attempt/success dates, duration and tokens. Cost is unknown (`null`), never
invented as zero. Failure/busy attempts do not earn a new success date; dry-run
and smoke-test do not touch production state. Model-proposed `action_required`
strings are untrusted review data in the private result, not executable tasks
or owner-inbox answers.

```sh
PYTHONPATH=src python -m unittest discover -s tests -p test_synthesis_codex.py -v
```

Tests use temporary vaults, a deterministic **test-only** embedder and an
in-memory signing key. They never use live vault data, Keychain or model auth.
The public release mirror omits the private full test suite; maintainers must
run that suite and the security/export gates before carrying this patch into
a release. A live production pass is separate evidence from fixture tests.

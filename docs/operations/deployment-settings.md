# Deployment settings

Every `BRAIN_` / `COS_` environment variable that picks a machine, path, key, model folder or mode is listed here. These stay settings.

Tuning knobs (thresholds, windows, retention days, caps, model names) are plain constants in the code since 2026-11-14 (owner ruling 3). The exceptions are the levers kept for the s07 registry, and the settings the nightly script reads.

Generated from the s06 census; the *Default / read line* column quotes the first source line that reads the name.

## Deployment settings

| Name | What it changes | Default / read line | Where it is read |
|---|---|---|---|
| `BRAIN_ANCHOR_DIR` | a folder | `override = os.environ.get("BRAIN_ANCHOR_DIR")` | src/brain/config_hostpaths.py:449 |
| `BRAIN_APP_DATA_DIR` | a folder | ```$BRAIN_APP_DATA_DIR`` is the other half of that pair: it moves the BASE and` | src/brain/config.py:18 |
| `BRAIN_AUDIT_KEYCHAIN_ACCOUNT` | keychain account name | `os.environ.get("BRAIN_AUDIT_KEYCHAIN_ACCOUNT")` | src/brain/audit.py:183 |
| `BRAIN_AUDIT_KEYCHAIN_SERVICE` | keychain item used to fetch a secret | `KEYCHAIN_SERVICE="${BRAIN_AUDIT_KEYCHAIN_SERVICE:-profile-a-brain-audit-key}"` | scripts/install-brief-mac.sh:60 |
| `BRAIN_AUDIT_KEY_CMD` | encryption or audit key source | `2. ``$BRAIN_AUDIT_KEY_CMD``  — shell command whose stdout is the PEM` | src/brain/audit.py:14 |
| `BRAIN_AUDIT_KEY_PEM` | encryption or audit key source | `AUDIT_KEY="${BRAIN_AUDIT_KEY_PEM:-}"` | scripts/install-brief-mac.sh:67 |
| `BRAIN_BIN` | a program path | `BRAIN_BIN="${BRAIN_BIN:-}"` | scripts/brain-brief.sh:51 |
| `BRAIN_CAPTURE_INBOX` | path | `override = os.environ.get("BRAIN_CAPTURE_INBOX")` | src/brain/config.py:420 |
| `BRAIN_CLAUDE_BIN` | a program path | `CLAUDE_BIN="${BRAIN_CLAUDE_BIN:-$(command -v claude \|\| echo "$HOME/.local/bin/claude")}"` | scripts/brain-synthesis.sh:30 |
| `BRAIN_CLAUDE_HOME` | deployment mode | `CLAUDE_HOME_ENV_VAR = "BRAIN_CLAUDE_HOME"` | src/brain/update.py:75 |
| `BRAIN_CONTEXTUAL_LLM` | path to a GGUF model file; unset turns the feature off | `OPT-IN upgrade via ``$BRAIN_CONTEXTUAL_LLM`` (path to a GGUF); without it, the` | src/brain/context.py:27 |
| `BRAIN_COS_DOWNLOADS_DIR` | a folder | `DL="${BRAIN_COS_DOWNLOADS_DIR:-$(plutil -extract EnvironmentVariables.BRAIN_COS_DOWNLOADS_DIR raw -o - \` | scripts/cos-check-run.sh:61 |
| `BRAIN_COS_OPS_DIR` | a folder | `"""CUT-01E: THE canonical COS operations dir — ``$BRAIN_COS_OPS_DIR`` or` | src/brain/config_hostpaths.py:430 |
| `BRAIN_COS_OWNER_ADDRESSES` | identity: the owner mail addresses | `OWNER_ADDRESSES_ENV = "BRAIN_COS_OWNER_ADDRESSES"` | tools/cos_signals_stale.py:56 |
| `BRAIN_COS_SHEET_LISTENER_PATH` | a file path | `LISTENER_PATH="${BRAIN_COS_SHEET_LISTENER_PATH:-}"` | tools/launchd/install-cos-jobs.sh:48 |
| `BRAIN_COS_TOOLS_DIR` | path | `if [ -z "${BRAIN_COS_TOOLS_DIR:-}" ]; then` | scripts/brain-brief.sh:100 |
| `BRAIN_DELIVERABLES_DIR` | a folder | `machine-owned tree inside the owner's own folder is not a thing to do quietly. ``$BRAIN_DELIVERABLES_DIR`` ove` | src/brain/deliverables_shelf.py:13 |
| `BRAIN_DIR` | a folder | `mkdir -p "$BRAIN_DIR/bin" "$BRAIN_DIR/model" "$SNAPSHOT_DEST" \` | tools/cowork_workspace_install.sh:183 |
| `BRAIN_EMBED_ONNX_DIR` | model folder | `or os.environ.get("BRAIN_EMBED_ONNX_DIR")` | src/brain/embed_onnx.py:238 |
| `BRAIN_EMBED_PROVIDERS` | execution providers | `gate ran on). On Apple Silicon, ``$BRAIN_EMBED_PROVIDERS=CoreMLExecutionProvider``` | src/brain/embed.py:69 |
| `BRAIN_ENCRYPTION_KEY` | encryption or audit key source | `from_env:   $BRAIN_ENCRYPTION_KEY  (base64 of 32 raw bytes)` | src/brain/encryption.py:16 |
| `BRAIN_ENCRYPTION_KEYCHAIN_ACCOUNT` | keychain account name | `os.environ.get("BRAIN_ENCRYPTION_KEYCHAIN_ACCOUNT")` | src/brain/encryption.py:132 |
| `BRAIN_ENCRYPTION_KEYCHAIN_SERVICE` | keychain item used to fetch a secret | `service = os.environ.get("BRAIN_ENCRYPTION_KEYCHAIN_SERVICE", KEYCHAIN_SERVICE_DEFAULT)` | src/brain/encryption.py:130 |
| `BRAIN_ENCRYPTION_KEY_CMD` | encryption key command | `from_cmd:   $BRAIN_ENCRYPTION_KEY_CMD (stdout = base64 key)` | src/brain/encryption.py:17 |
| `BRAIN_HOST_PY` | path to python (shell installer) | `_STAGING_PY="${BRAIN_HOST_PY:-python3}"` | tools/cowork_workspace_install.sh:72 |
| `BRAIN_INDEX_DIR` | a folder | `1. ``$BRAIN_INDEX_DIR``                  (explicit override; tests use this)` | src/brain/config.py:9 |
| `BRAIN_INGEST_TIER_GUARD_DISABLED` | safety switch, deliberately off by default | `DISABLED_ENV = "BRAIN_INGEST_TIER_GUARD_DISABLED"` | src/brain/ingest/tierguard.py:80 |
| `BRAIN_LAUNCH_AGENTS_DIR` | a folder | `LAUNCH_AGENTS_DIR="${BRAIN_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"` | scripts/install-brief-mac.sh:47 |
| `BRAIN_LOG_DIR` | a folder | `# Logs:         $BRAIN_LOG_DIR/brief-YYYY-MM-DD.log  (30-day rotation)` | scripts/brain-brief.sh:21 |
| `BRAIN_MAINTAIN_STATE` | path | `override = os.environ.get("BRAIN_MAINTAIN_STATE")` | src/brain/config.py:450 |
| `BRAIN_MODEL_CACHE` | model cache or folder | `"BRAIN_MODEL_CACHE": str(vault / ".brain" / "model"),` | scripts/brain-synthesis.sh:229 |
| `BRAIN_MODEL_SLOT_DIR` | model cache or folder | `files under `$BRAIN_MODEL_SLOT_DIR` (default ~/.cache/brain-model-slots) and` | src/brain/model_slot.py:11 |
| `BRAIN_NOTIFY` | notifications on/off | `NOTIFY_ENV = "BRAIN_NOTIFY"` | src/brain/maintenance_notify.py:25 |
| `BRAIN_OCR_LANG` | OCR language pack, depends on what is installed | `tesseract binary is absent/unusable. ``$BRAIN_OCR_LANG`` overrides; the` | src/brain/ingest/handlers/base.py:340 |
| `BRAIN_OVERLAY_DIR` | a folder | `help="overlay dir override (default: $BRAIN_OVERLAY_DIR or <vault>/overlay)",` | src/brain/cli_cmds/lifecycle_parser.py:24 |
| `BRAIN_REQUIRE_REAL_EMBEDDER` | deployment mode | `this guards: ``role=vm`` defaults ``$BRAIN_REQUIRE_REAL_EMBEDDER=1`` so the` | src/brain/config.py:65 |
| `BRAIN_RERANKER_ONNX_DIR` | model folder | `load a local exported reranker via ``local_dir`` / ``$BRAIN_RERANKER_ONNX_DIR``` | src/brain/rerank_onnx.py:77 |
| `BRAIN_RERANK_PROVIDERS` | ONNX execution provider, depends on the machine | ```$BRAIN_RERANK_PROVIDERS=CoreMLExecutionProvider``. Comma-separate for` | src/brain/rerank.py:66 |
| `BRAIN_ROLE` | host or VM role | `"BRAIN_ROLE": "vm",` | scripts/brain-synthesis.sh:227 |
| `BRAIN_ROUTINES_MANIFEST` | path | `"""Resolve the task manifest: explicit > ``$BRAIN_ROUTINES_MANIFEST`` >` | src/brain/init.py:126 |
| `BRAIN_RUNTIME_DIR` | a folder | `export BRAIN_RUNTIME_DIR="${BRAIN_RUNTIME_DIR:-$BRAIN_VAULT/.brain}"` | scripts/vm-boundary-probe.sh:32 |
| `BRAIN_SNAPSHOT_DIR` | a folder | `override = os.environ.get("BRAIN_SNAPSHOT_DIR")` | src/brain/config.py:400 |
| `BRAIN_SYNTHESIS_BRAIN_BIN` | path to the brain binary | `BRAIN_BIN="${BRAIN_SYNTHESIS_BRAIN_BIN:-$(command -v brain \|\| true)}"` | scripts/brain-synthesis.sh:31 |
| `BRAIN_SYNTHESIS_STATE` | a state file path | `# metered cost into health-history.jsonl. Honors $BRAIN_SYNTHESIS_STATE (the` | scripts/brain-synthesis.sh:468 |
| `BRAIN_UPDATE_ENGINE_ONLY` | update mode switch | `if os.environ.get('BRAIN_UPDATE_ENGINE_ONLY') == '1':` | src/brain/update.py:335 |
| `BRAIN_VAULT` | which vault the command works on | `VAULT="${BRAIN_VAULT:?BRAIN_VAULT must be set (path to your brain vault)}"` | scripts/brain-retrieval-watch.sh:25 |
| `COS_BODY_OPEN_CAP` | opt-in switch for a measurement run (unset by design) | `if [[ -n "${COS_BODY_OPEN_CAP:-}" ]]; then` | scripts/cos-run-now.sh:129 |
| `COS_CLAUDE_BIN` | a program path | `CLAUDE_BIN="${COS_CLAUDE_BIN:-$(command -v claude \|\| echo "$HOME/.local/bin/claude")}"` | tools/cos_nightly.sh:107 |
| `COS_PLAN_LOCK_FILE` | deployment mode | `PLAN_LOCK_FILE="${COS_PLAN_LOCK_FILE:-$HOME/.brain/locks/cos-plan-lock}"` | tools/cos_nightly.sh:365 |
| `COS_PYTHON` | python used | `PY="${COS_PYTHON:-python3}"` | tools/cos_ctl.sh:55 |
| `COS_REPO` | repo checkout | `if [ -n "${COS_REPO:-}" ] && [ "$COS_REPO" != "$REPO" ]; then` | tools/cos_ctl.sh:48 |
| `COS_RUN_LANE` | launch-lane selector for an attended run | `RUN_LANE="${COS_RUN_LANE:-codex-automation}"` | scripts/cos-run-now.sh:155 |
| `COS_TRANSPORT` | browser transport | `if [ "${COS_TRANSPORT:-ego}" = "ego" ]; then` | tools/cos_attachment_backfill_run.sh:99 |

## Read by the nightly script (not inlined: live-system boundary)

These are read by `tools/cos_nightly.sh`, or by a helper the nightly runs, and are tuning knobs. They were not inlined: editing the live nightly path breaks invariant I2. Revisit after the nightly stops being live.

| Name | Where it is read |
|---|---|
| `BRAIN_COS_ATTACHMENT_BATCH_BYTES` | tools/cos_attachment_fetch.py |
| `BRAIN_COS_ATTACHMENT_MAX_BYTES` | tools/cos_attachment_fetch.py |
| `BRAIN_COS_BATCHES` | tools/cos_nightly.sh |
| `BRAIN_COS_BATCH_REQUIRED_SECONDS` | tools/cos_nightly.sh, tools/cos_ego_arm.py |
| `BRAIN_COS_BATCH_THREAD_CAP` | tools/cos_nightly.sh |
| `BRAIN_COS_SINCE_DAYS` | src/brain/cos/_attachment_join_record.py, src/brain/cos/_constants.py, src/brain/cos/_learning_ledger.py, tools/cos_mutate_cli.py, tools/cos_mutate_plan.py, tools/cos_mutate_policy.py |
| `COS_ARCHIVE_REPLY_SIGNAL` | tools/cos_nightly.sh, tools/cos_archive_reply.py |
| `COS_BRIDGE_LOCK_TRIES` | tools/cos_nightly.sh |
| `COS_CHAIN_STATE` | tools/cos_nightly.sh |
| `COS_CHUNK_PARALLEL` | tools/cos_nightly.sh |
| `COS_DRAFT_CHUNK_SIZE` | tools/cos_batch_chunk.py, tools/cos_nightly.sh |
| `COS_DRAFT_LABELS` | tools/cos_nightly.sh, tools/cos_draft_labels.py |
| `COS_INTERVIEW` | tools/cos_nightly.sh |
| `COS_INTERVIEW_MAX_TURNS` | tools/cos_nightly.sh |
| `COS_INTERVIEW_MODEL` | tools/cos_nightly.sh |
| `COS_JUDGE_CHUNK_SIZE` | tools/cos_batch_chunk.py, tools/cos_nightly.sh |
| `COS_LEG_STDERR_MAX` | tools/cos_nightly.sh |
| `COS_MAX_TURNS` | tools/cos_nightly.sh |
| `COS_PREP` | tools/cos_nightly.sh |
| `COS_PREP_MAX_TURNS` | tools/cos_nightly.sh |
| `COS_PREP_MODEL` | tools/cos_nightly.sh |
| `COS_PROMPT_MAX_BYTES` | tools/cos_batch_chunk.py |
| `COS_PULSE` | tools/cos_nightly.sh |
| `COS_PULSE_MAX_TURNS` | tools/cos_nightly.sh |
| `COS_PULSE_MODEL` | tools/cos_nightly.sh |
| `COS_VOICE_CHECK` | tools/cos_nightly.sh |
| `COS_SINCE_DAYS` | tools/cos_nightly.sh |
| `COS_VOICE_MAX_TURNS` | tools/cos_nightly.sh |
| `COS_VOICE_MODEL` | tools/cos_nightly.sh |

## Levers kept for the s07 registry (owner ruling 2)

| Name | Where it is read |
|---|---|
| `BRAIN_COS_AUTOCAP_BULK_MAX_BATCH` | src/brain/cos/_constants.py |
| `BRAIN_COS_AUTOCAP_MIN_LOWER_BOUND` | src/brain/cos/_constants.py |
| `BRAIN_COS_AUTOCAP_MIN_VOLUME` | src/brain/cos/_constants.py |
| `BRAIN_COS_AUTOCAP_UNDO_HOURS` | src/brain/cos/_constants.py |
| `BRAIN_COS_AUTOCAP_WINDOW_DAYS` | src/brain/cos/_constants.py |
| `BRAIN_COS_AUTOCAP_WINDOW_VERDICTS` | src/brain/cos/_constants.py |
| `BRAIN_MULTI_GUARD` | src/brain/retrieval_ops.py |

`COS_DRAFT_LABELS` (the nightly draft-label step: `off` stops it, review date 2026-12-20) and `COS_ARCHIVE_REPLY_SIGNAL` (the archive-reply record: `off` stops it, review date 2026-12-20) are also levers and are listed above. `BRAIN_COS_AUTOCAP_EXPLORATION_K` and the other `BRAIN_COS_AUTOCAP_*` names, `BRAIN_DAILY_NOTE` and `BRAIN_MULTI_GUARD` are all levers; tests set most of them.

## Engine speed knobs that a live plist sets (not inlined: live-system boundary)

These three change how fast the embedder and the reranker run. A live plist sets them, so they were not inlined. The code defaults apply when they are unset.

| Name | What it changes | Where it is read |
|---|---|---|
| `BRAIN_EMBED_BATCH` | batch size of the embedder (the model spec supplies the default) | src/brain/embed.py |
| `BRAIN_EMBED_THREADS` | thread count of the embedder (default 4, capped at the core count) | src/brain/embed.py |
| `BRAIN_RERANK_THREADS` | thread count of the reranker (default 4, capped at the core count) | src/brain/rerank.py |

## Settings that a live plist sets

None of the 39 inlined names appears in any `~/Library/LaunchAgents/*.plist` (checked by name, read-only). The live nightly plist sets `BRAIN_VAULT`, `BRAIN_INDEX_DIR` and similar deployment names listed above.


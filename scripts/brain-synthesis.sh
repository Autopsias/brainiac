#!/usr/bin/env bash
# brain-synthesis — the SECOND sanctioned host scheduled task (weekly).
#
# Owner decision 2026-07-11: metadata automation (the nightly maintain folds)
# closed every organization gap EXCEPT synthesis — state/MOC notes drift
# behind raw/ because writing prose needs a model, which the engine
# deliberately does not hold (no API keys in brain). This task closes that
# gap: once a week it runs a HEADLESS kb-curator session (Claude Code CLI)
# against EVERY vault registered in ~/.brainiac/workspaces.json — vault-
# generic by construction, never hardcoded to one vault.
#
# Task name:    brain-synthesis (launchd label com.brainiac.synthesis)
# Schedule:     weekly, Sunday 08:00 local (one hour after the 07:00 nightly,
#               so the week's promote-scan + digest output is already fresh)
# Budget:       routines/manifest.json locked_counts.host_os_scheduled == 2
#               (amended 2026-07-11 for exactly this task — THE LOCK rule
#               says amend the manifest first; this is that amendment's task)
# Runtime:      host only. Each session is bounded (--max-turns) and scoped
#               to the workspace folder; it uses the same `brain` CLI +
#               skills the interactive sessions use.
# Logs:         $BRAIN_LOG_DIR/synthesis-YYYY-MM-DD.log (30-day rotation)
# Skip rules:   no claude CLI -> skip loudly; a workspace without a
#               kb-curator skill -> skipped and logged, never an error.
set -u

LOG_DIR="${BRAIN_LOG_DIR:-$HOME/.brain/logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/synthesis-$(date +%F).log"
REGISTRY="${BRAIN_WORKSPACES_JSON:-$HOME/.brainiac/workspaces.json}"
CLAUDE_BIN="${BRAIN_CLAUDE_BIN:-$(command -v claude || echo "$HOME/.local/bin/claude")}"
MAX_TURNS="${BRAIN_SYNTHESIS_MAX_TURNS:-60}"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

# 30-day log rotation (same posture as brain-brief.sh)
find "$LOG_DIR" -name 'synthesis-*.log' -mtime +30 -delete 2>/dev/null

if [ ! -x "$CLAUDE_BIN" ]; then
  log "SKIP-ALL: claude CLI not found ($CLAUDE_BIN) — synthesis needs a model"
  exit 0
fi
if [ ! -f "$REGISTRY" ]; then
  log "SKIP-ALL: no workspace registry at $REGISTRY"
  exit 0
fi

# bash 3.2 (macOS /bin/bash, what launchd invokes) cannot parse backticks in a
# heredoc inside $(); read -d '' avoids command substitution (returns 1 at EOF)
read -r -d '' PROMPT <<'EOF' || true
Run the kb-curator skill for this vault, weekly synthesis pass. FIRST run
`brain status --json` and `brain doctor`: if the maintain heartbeat is stale
(daily branch > 48h) or any required surface is flagged, queue a dated entry
to .brain/memory/hot.md describing what is broken — the scheduled umbrella
cannot report its own death, so this weekly session is its watchdog. Then
the synthesis scope:
(1) refresh any state/MOC note whose content lags this week's raw/ ingests
(check `brain recent` and the freshness signal on `brain search`); supersede
rather than edit when the old claim was true-then; (2) review the Sunday
promote-scan candidates in .brain/memory/hot.md and promote the ones that
meet the one-idea-per-note bar into typed brain/ notes; (3) update index.md
zone stamps. Follow AGENTS.md conventions exactly (frontmatter, wikilinks,
classification explicit on every new note). Finish with `brain sync --publish`
and a one-paragraph summary of what changed. Stay inside this workspace.
EOF

# Iterate REGISTERED host workspaces (dedup by vault_path) — vault-generic:
# a new `brainiac-install`/`brainiac-cowork-setup` registration is picked up
# on the next Sunday with zero extra wiring.
python3 - "$REGISTRY" <<'PY' | while IFS=$'\t' read -r VAULT WS; do
import json, sys
seen = set()
for e in json.load(open(sys.argv[1])).get("entries", []):
    if e.get("target") != "host":
        continue
    v, w = e.get("vault_path", ""), e.get("workspace_path", "")
    if not v or v in seen:
        continue
    seen.add(v)
    print(f"{v}\t{w or v}")
PY
  if [ ! -d "$WS/.claude/skills/kb-curator" ] && [ ! -d "$WS/.agents/skills/kb-curator" ]; then
    log "SKIP $VAULT: no kb-curator skill in $WS"
    continue
  fi
  log "START synthesis: vault=$VAULT workspace=$WS"
  # OBS-04: capture the CLI's `--output-format stream-json` NDJSON to OUT_JSON
  # via a DIRECT redirect — NEVER a `tee` pipe (finding [6]: piping claude's
  # stdout couples its liveness to the log sink, so a full/unwritable $LOG
  # would SIGPIPE-kill an otherwise-healthy run and trip a false "synthesis
  # died" watchdog). The direct-to-file NDJSON is durable and survives a kill
  # (each line is flushed as emitted); cost/duration/usage are lifted below
  # from its single `"type":"result"` line — STRUCTURED data only, never
  # scraped human text (a scraped format drifts silently to a wrong zero —
  # HARDENED correction 4). The CLI requires --verbose alongside stream-json.
  # A per-run unique name (date + pid + two $RANDOM draws) avoids the
  # same-second collision the old date-second+pid name allowed (finding [9]).
  OUT_JSON="$LOG_DIR/synthesis-out-$(date +%F)-$$-${RANDOM}${RANDOM}.json"
  ( cd "$WS" && BRAIN_VAULT="$VAULT" "$CLAUDE_BIN" -p "$PROMPT" \
      --max-turns "$MAX_TURNS" \
      --permission-mode acceptEdits \
      --output-format stream-json \
      --verbose \
      --allowedTools "Read,Grep,Glob,Edit,Write,Bash(brain *),Bash(python3 tools/*)" \
  ) < /dev/null > "$OUT_JSON" 2>>"$LOG"
  # < /dev/null: claude -p hangs forever reading a non-TTY stdin — and inside
  # this while-read loop it would otherwise inherit (and eat) the registry pipe
  RC=$?
  log "END synthesis: vault=$VAULT rc=$RC"
  # Render a human-readable transcript from the structured NDJSON into $LOG
  # (finding [5]): the prior `tee`'d raw NDJSON REPLACED the readable session
  # transcript the weekly-watchdog prompt tells an operator to open. Structured
  # fields only (assistant text turns + the final result) — never scraped
  # prose. A killed run renders whatever streamed before the kill; the full
  # machine NDJSON stays in $OUT_JSON (30-day retention) regardless.
  python3 - "$OUT_JSON" >> "$LOG" 2>>"$LOG" <<'PY' || true
import json, sys
try:
    with open(sys.argv[1]) as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "assistant":
                for b in (obj.get("message") or {}).get("content", []) or []:
                    if isinstance(b, dict) and b.get("type") == "text":
                        t = (b.get("text") or "").strip()
                        if t:
                            print(t)
            elif obj.get("type") == "result":
                r = (obj.get("result") or "").strip()
                if r:
                    print("--- result ---")
                    print(r)
except Exception:
    pass
PY
  # Heartbeat (WATCHDOG-01): the hourly maintain umbrella reads this file and
  # raises action_required when the last successful synthesis is > 8 days old
  # — nobody should have to read synthesis logs to learn the task died.
  # OBS-04: the same entry is extended with {tokens, duration_s, est_cost_usd}
  # so src/brain/maintenance.py's collect_health_metrics can lift the latest
  # metered cost into health-history.jsonl. Honors $BRAIN_SYNTHESIS_STATE (the
  # SAME override the Python readers consult — finding [3]: the writer used to
  # hardcode $HOME, so a set env silently split writer and reader and the
  # watchdog never fired).
  STATE="${BRAIN_SYNTHESIS_STATE:-$HOME/.brain/synthesis-state.json}"
  # Finding [3] companion: create the state file's parent dir before writing —
  # a $BRAIN_SYNTHESIS_STATE override can point at a not-yet-existing dir, and
  # a silent write failure there would freeze last_success and trip a FALSE
  # "synthesis stale" watchdog even though synthesis ran fine.
  mkdir -p "$(dirname "$STATE")" 2>>"$LOG" || true
  python3 - "$STATE" "$VAULT" "$RC" "$OUT_JSON" <<'PY'
import json, sys, datetime
path, vault, rc, out_json = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
try:
    state = json.load(open(path))
except Exception:
    state = {}
entry = state.setdefault(vault, {})
entry["last_attempt"] = datetime.date.today().isoformat()
entry["rc"] = rc
if rc == 0:
    entry["last_success"] = entry["last_attempt"]

# OBS-04 — meter from the structured --output-format stream-json NDJSON
# stream ONLY: scan every line, keep the (last) line with "type":"result" —
# that's the CLI's own final usage/cost summary for the run. Absent/zero/
# unparseable all record as null (never a scraped guess, never a bare 0
# masquerading as "measured").
cost = tokens = duration_s = None
try:
    result_obj = None
    with open(out_json) as fh:
        for raw_line in fh:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("type") == "result":
                result_obj = obj
    if result_obj is not None:
        c = result_obj.get("total_cost_usd")
        if isinstance(c, (int, float)) and c > 0:
            cost = round(c, 4)
        dm = result_obj.get("duration_ms")
        if isinstance(dm, (int, float)):
            duration_s = round(dm / 1000, 1)
        usage = result_obj.get("usage") or {}
        parts = [usage.get(k) for k in (
            "input_tokens", "output_tokens",
            "cache_creation_input_tokens", "cache_read_input_tokens")]
        nums = [p for p in parts if isinstance(p, (int, float))]
        if nums:
            tokens = int(sum(nums))
except Exception:
    pass
entry["tokens"] = tokens
entry["duration_s"] = duration_s
entry["est_cost_usd"] = cost
json.dump(state, open(path, "w"), indent=1)
PY
done

# 30-day rotation for the per-run usage-json captures (same posture as logs).
find "$LOG_DIR" -name 'synthesis-out-*.json' -mtime +30 -delete 2>/dev/null

log "synthesis run complete"
exit 0

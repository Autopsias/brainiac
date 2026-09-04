#!/bin/bash
# SEC-07 — Claude Code PreToolUse guard: refuse a tool call that would carry a
# term this vault classifies Confidential or above OFF THIS HOST.
#
# WHY THIS EXISTS. The classification gate decides what the model may READ. It
# says nothing about what the model then does. A web search is an outbound
# channel and nobody reviews a query string, so an injected instruction can put
# a codename into one and the term has left before any result comes back.
# `AGENTS.md` retrieval rule 4 has always said so; this script is what enforces
# it.
#
# EVERY DOOR, NOT TWO (M-2, 2026-09-02). Until now this guard watched
# `WebSearch|WebFetch` only, allowed everything when the decoder ring was
# empty, and reported a failed check as an ALLOW. All three were holes: a
# `Bash` curl and an MCP tool are outbound channels too, an empty ring made the
# all-clear meaningless, and a guard that fails open is a guard an attacker
# only has to break.
#
# DESTINATION, NOT TOOL NAME (rework 1, 2026-09-03). The first cut of that
# widening keyed on the TOOL, so `Bash` covered `curl https://…` and
# `brain search "<term>"` alike: searching this vault by codename — the single
# most normal thing anyone does with it — was REFUSED, and so was writing a
# vault note that mentions one. An over-refusing guard is uninstalled the same
# day, so a call is now checked only when its DESTINATION is off this host:
#   * WebSearch / WebFetch — third party by definition. Always checked.
#   * Bash                 — checked when the command names a program that
#                            moves bytes off the host, or carries a URL.
#   * Write / Edit / Multi… — checked when the target is OUTSIDE the working
#                            tree and outside $BRAIN_VAULT.
#   * mcp__* and anything unrecognised — always checked. An MCP server is
#                            another process; over-checking there is cheap.
#
#   CEILING OF THAT RULE, stated plainly. `OUTBOUND_RE` below is an
#   ENUMERATION, so it is a FLOOR, not a seal. It misses: an outbound program
#   not on the list, a shell alias or wrapper script whose name hides one, a
#   program invoked through a variable, and anything that sends bytes from
#   inside an interpreter one-liner without a literal URL. A LEADING PATH no
#   longer defeats it — every token is reduced to its basename before matching
#   (s06 round 2, H-2) — and the cost lands the safe way round: a filename whose
#   stem IS a listed program (`docs/http.md`) now sends the call to
#   `check-egress`, which still refuses only if it carries a declared term.
#   Over-checking, not over-refusing. The path rule for
#   writes misses a sync folder that lives INSIDE the working tree. Neither
#   rule sees dereferenced bytes at all: `curl -d @secret.md` names a path, not
#   a term, and passes. This raises the bar on declared terms in arguments; it
#   does not close the channel.
#
# IT SHIPS NO TERMS. `brain check-egress` reads them from the vault's own
# `overlay/keywords/` ring, merged over the one the nightly fold generates from
# the vault's own `project` notes.
#
# WHAT IT DOES NOT DO. It matches DECLARED terms on word boundaries. It cannot
# see a paraphrase, an undeclared codename, or a fact the model states in its
# own words. Treat it as a floor, not a seal.
#
# `Read`, `Grep` and `Glob` ARE SENT HERE, AND THEY ARE RECORD-ONLY (round 7).
# They send nothing off this host, so there is nothing for an egress guard to
# say about them, and a guard that refuses a read is a guard uninstalled the
# same day: they exit 0 below, before any destination test and before the
# engine call. They are matched anyway because the path a read NAMES is how a
# session enters a second vault, and that has to be written down before the
# vault's terms can refuse. This comment said the opposite until round 8 —
# believing it removes one half of that fix.
#
# The matcher itself lives in a gitignored settings file no test can see.
# `brain doctor`'s SEC-07 rows are the only surface that reports whether a
# given machine actually has it.
#
# FAIL-CLOSED HAS A CEILING TOO. Claude Code CANCELS a `PreToolUse` command
# hook that exceeds its `timeout`, and a cancelled hook is not a block — the
# tool call proceeds. This script cannot convert a timeout into a refusal, so
# the 5-second budget below is FAIL-OPEN and no amount of fail-closed logic in
# here changes that. It is a platform property, recorded rather than claimed
# away. TWO states this script deliberately ALLOWS, for the same reason: no
# `brain` resolvable at all, and no vault resolvable for this session. Neither
# is a check that FAILED; each is an absence of any decoder ring to judge
# against, and so an absence of authority to refuse. A ring that EXISTS and
# cannot be read is the opposite case and REFUSES (s06 round 2, H-1).
#
# THE PINS SPLIT THAT RULE, AND THE SPLIT IS DELIBERATE (s06 round 6). Both
# sidecars — `<script>.engine` and `<script>.registry` — are ABSENT on a host
# this engine never installed, and absence allows. But present-and-unusable is
# not absence, and the two pins answer differently on purpose:
#
#   .engine   names a file that is missing or not executable  -> REFUSE
#   .registry does not name an absolute path                  -> REFUSE
#   .registry names a registry that is not there yet          -> allow, no vault
#
# A missing ENGINE means the check cannot run, and a check that cannot run has
# no authority to allow. A missing REGISTRY means the check runs and finds an
# empty ring — the same answer a vault with no decoder ring gives, and the
# correct one for a host that has registered no vault yet. Refusing there would
# lock the owner out of their own shell on day one, and a guard that does that
# is uninstalled by lunchtime. `brain doctor` names every one of these states,
# INCLUDING the two that allow, because the whole failure class this surface
# exists for is a control that is silently off. The third state that used to
# allow — `$BRAINIAC_EGRESS_GUARD=off` — is gone: committed project settings can
# define a session's environment, so it was never an owner-only escape (H-5).
# The recovery procedure is external, and is stated with the code below.
#
# INSTALL (Claude Code). Place this file at ~/.claude/hooks/ and register it:
#
#   "hooks": {
#     "PreToolUse": [
#       { "matcher": "WebSearch|WebFetch|Bash|PowerShell|Write|Edit|NotebookEdit|Read|Grep|Glob|mcp__.*",
#         "hooks": [ { "type": "command",
#                      "command": "~/.claude/hooks/brainiac-egress-guard.sh",
#                      "timeout": 5 } ] }
#     ]
#   }
#
# ON A HARNESS-MANAGED ~/.claude (one a deploy repo owns, per
# `brain.session_hook.harness_managed`) the settings.json entry belongs to that
# repo, not to this engine. Author it there.

set -uo pipefail
# `-f` KILLS PATHNAME EXPANSION FOR THE WHOLE SCRIPT. Every value this guard
# handles is a path or a payload, and neither is a glob: an unquoted expansion
# of a vault path holding `*` would otherwise be replaced by whatever the
# directory happens to contain. This is the sibling of the round-8 space bug
# (a vault at `/…/alpha work/vault` split in two and left the session union
# silently), and one line closes the whole class rather than one loop at a
# time. `case` patterns are unaffected — `-f` governs expansion, not matching.
set -f

# THERE IS NO IN-BAND ESCAPE, and that is the fix (s06 round 2, H-5). This
# script used to honour `BRAINIAC_EGRESS_GUARD=off` from the inherited
# environment and call that owner-authenticated. It is not: Claude Code lets
# COMMITTED PROJECT SETTINGS define a session's environment variables, so a
# collaborator's repo change — or an injected settings edit in a session where
# edits are allowed — switched the guard off for every later call, and nothing
# reported it was off. An escape hatch an attacker can reach is not an escape
# hatch, it is the door.
#
# THE RECOVERY PROCEDURE IS EXTERNAL, and it is the only one:
#
#   1. Quit Claude Code.
#   2. Remove (or comment out) this hook's entry from the `PreToolUse` block of
#      ~/.claude/settings.json — or, on a harness-managed ~/.claude, from the
#      harness repo that owns that file.
#   3. Start a new session.
#
# Nothing inside a session can perform step 2 in a way an injected instruction
# could not also perform, which is the point. `brain doctor`'s SEC-07 rows
# report the guard as DISABLED whenever the effective environment carries ANY
# of the variables listed in `guard_settings.GUARD_ENV_VARS` — none of them does
# anything here any more, and a host that still sets one is a host whose owner
# believes it does.

# THE ENGINE IS PINNED, NOT LOOKED UP (s06 round 3, C-1). This read
# `${BRAIN_BIN:-brain}` from the inherited environment until 2026-09-03, which
# is the SAME door H-5 closed on `BRAINIAC_EGRESS_GUARD` and strictly wider:
# committed project settings define a session's environment, so
# `BRAIN_BIN=/bin/true` (or any nonexistent path) made every call exit 0 with
# the whole guard off, and nothing reported it. Authenticating the engine
# through project-controllable state is the bug; the fix is to stop doing it.
#
# The INSTALLER knows the answer for certain — it IS the engine — so
# `brain install-hook` writes that absolute path into a pin file beside this
# script (`brainiac-egress-guard.engine`, see `session_hook.engine_path`).
# Resolution order, and none of it reads an environment variable an untrusted
# session can set:
#
#   1. the pin beside this script  -> the managed engine. If the pin exists and
#      the engine it names is GONE, that is a broken managed install on a
#      machine that DOES have the guard, and it FAILS CLOSED. It is not the
#      "no engine here" case: something removed an engine this host had.
#   2. no pin, but the managed venv the installer uses -> that.
#   3. no pin and no managed venv -> the ABSOLUTE places an engine is actually
#      installed (`~/.local/bin`, `/usr/local/bin`, `/opt/homebrew/bin`,
#      `/usr/bin`), in that order.
#   4. still nothing -> `command -v brain` against the INHERITED `$PATH`, the
#      pre-2026-09-03 behaviour, for a hand-placed script or a repo checkout.
#   5. nothing at all -> exit 0. No engine, no decoder ring, no authority.
#
# STEP 4 EXISTS BECAUSE STEP 3 IS A LIST, AND A LIST IS A FLOOR (s06 round 7,
# Claude). Round 6 replaced `$PATH` wholesale — correctly for `jq`, which the
# script CALLS, and wrongly for the engine, which the script RESOLVES. This host
# keeps its engine at `$HOME/.local/bin/brain`, which the fixed system `PATH`
# cannot see: measured, a host with no managed venv silently exited 0 and the
# guard was gone. A missing engine is the one state that ALLOWS, so a resolution
# step that finds nothing does not fail closed — it disarms.
#
# CEILING, stated rather than claimed away: step 4 consults `$PATH`, and `$PATH`
# is an environment variable like any other. It is reached only on a host with
# NO pin, NO managed install and no engine in any standard location — one this
# engine never installed — and `brain doctor` names the state. On such a host
# the ONLY other outcome is exit 0, so a forged `brain` there buys an attacker
# exactly what its absence already gives them. `$HOME` in step 2 is the same
# shape.
# THE ENVIRONMENT IS NOT AN INPUT — INCLUDING `$PATH` (s06 round 6, Codex).
#
# Three rounds of this review closed one environment variable each: `$BRAIN_BIN`
# (round 3), `$BRAIN_VAULT` (round 5), and this round named `$BRAINIAC_HOME` and
# `$PATH`. Every one of those fixes asked "is THIS input trusted" when the
# question was "does this script trust the environment at all", so each closed
# one door and left the next one open. This block is the general answer.
#
# `$PATH` was the widest of them. Committed project settings define a session's
# environment, and this hook inherits it, so a repo could put its own directory
# first and supply a `jq` that renames the tool, empties the outbound text, or
# names any vault it likes — EVERY security-critical value in this script is
# produced by that one program. Pinning the engine never protected it. Nor was
# `jq` the only exposure: `#!/usr/bin/env bash` resolved the INTERPRETER the
# same way, so a forged `bash` ran the guard itself. The shebang above is now
# absolute, and `PATH` is replaced here — before anything external is called —
# with the fixed system directories, which no repository can write.
#
# `jq` is looked for by ABSOLUTE PATH in the three places package managers put
# it. A host that has it somewhere else takes the no-jq branch below, which is
# degraded toward over-checking and never toward allowing. Homebrew's prefix is
# writable by the owner, which is accepted for the same reason `$HOME` is: the
# threat this guard models is committed project CONTENT, which cannot write it.
GUARD_INHERITED_PATH="$PATH"
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
JQ=""
for _cand in /usr/bin/jq /opt/homebrew/bin/jq /usr/local/bin/jq; do
  if [ -x "$_cand" ]; then JQ="$_cand"; break; fi
done

_self="$0"
case "$_self" in */*) : ;; *) _self="./$_self" ;; esac
GUARD_PIN="${_self%.sh}.engine"

BRAIN_BIN=""
if [ -r "$GUARD_PIN" ]; then
  IFS= read -r BRAIN_BIN < "$GUARD_PIN" || BRAIN_BIN=""
  case "$BRAIN_BIN" in /*) : ;; *) BRAIN_BIN="" ;; esac
  if [ -z "$BRAIN_BIN" ] || [ ! -x "$BRAIN_BIN" ]; then
    printf '%s %s\n%s\n' \
      "brainiac-egress-guard: the PINNED engine named in $GUARD_PIN is missing" \
      "or not executable — REFUSING, fail-closed." \
      "  This host has the SEC-07 guard installed, so an absent engine is a \
broken install, not an absence of authority. Re-run \`brain install-hook\`, or \
remove this hook entry from the PreToolUse block of ~/.claude/settings.json \
OUTSIDE a session." >&2
    exit 2
  fi
elif [ -x "$HOME/.brainiac/venv/bin/brain" ]; then
  BRAIN_BIN="$HOME/.brainiac/venv/bin/brain"
else
  for _cand in "$HOME/.local/bin/brain" /usr/local/bin/brain \
               /opt/homebrew/bin/brain /usr/bin/brain; do
    if [ -x "$_cand" ]; then BRAIN_BIN="$_cand"; break; fi
  done
  if [ -z "$BRAIN_BIN" ]; then
    BRAIN_BIN="$(PATH="$GUARD_INHERITED_PATH" command -v brain 2>/dev/null || true)"
    case "$BRAIN_BIN" in /*) : ;; *) BRAIN_BIN="" ;; esac
  fi
fi
[ -n "$BRAIN_BIN" ] || exit 0                       # no engine here, no opinion

# THE VAULT COMES FROM THE HOST REGISTRY, PER INVOCATION (s06 round 5).
#
# Round 3 (C-1) closed `$BRAIN_BIN`. Round 4 closed the RUN-TIME half of its
# sibling `$BRAIN_VAULT` by pinning one vault path beside the script — and
# round 5 showed that only MOVED the bypass, in two ways both reviewers found
# independently:
#
#   * the installer resolved the vault to pin with `config.vault_root()`,
#     which reads `$BRAIN_VAULT` FIRST. Measured: `install()` run with
#     `BRAIN_VAULT=<attacker dir>` wrote that directory into the pin and
#     reported success. `brain update` re-runs the installer, so the poisoned
#     pin came back on every update — worse than the per-session bug it fixed.
#   * one pin per MACHINE, on a host the design treats as multi-vault (this
#     one has 4 registered). Whichever vault installed last answered for all
#     of them, so a term protected in vault B was checked against vault A's
#     ring and allowed, silently.
#
# The fix is not a third pin. `~/.brainiac/workspaces.json` is a HOST-OWNED
# registry that already ships (`workspaces.py`, `doctor_wiring.py`), lists
# `vault_path` + `workspace_path` per entry, and lives outside any project's
# control. The installer pins the REGISTRY's absolute path — a location, not
# an answer — and the vault is resolved HERE, per call, by matching the
# session's own directory against the registry. Nothing about which vault
# answers is settable from a repo file any more.
#
# CEILING, stated rather than claimed away: the pinned path is derived from
# `Path.home()`, so `$HOME` still selects a registry. That is the same
# accepted ceiling the engine lookup above already carries, and a `$HOME` a
# project can rewrite has broken far more than this guard.
#
# No pin, no registry, or no entry matching this session: NO VAULT. The ring
# is then empty and containment falls back to the session's own tree — the
# pre-existing behaviour on a host that never had a vault, and fail-safe
# rather than fail-open.
GUARD_REGISTRY_PIN="${_self%.sh}.registry"
GUARD_REGISTRY=""
if [ -r "$GUARD_REGISTRY_PIN" ]; then
  IFS= read -r GUARD_REGISTRY < "$GUARD_REGISTRY_PIN" || GUARD_REGISTRY=""
  case "$GUARD_REGISTRY" in /*) : ;; *) GUARD_REGISTRY="" ;; esac
  if [ -z "$GUARD_REGISTRY" ]; then
    printf '%s\n%s\n' \
      "brainiac-egress-guard: $GUARD_REGISTRY_PIN does not name an absolute \
path — REFUSING, fail-closed." \
      "  Re-run \`brain install-hook\`, or remove this hook entry from the \
PreToolUse block of ~/.claude/settings.json OUTSIDE a session." >&2
    exit 2
  fi
fi

payload="$(cat)"
tool=""
text="$payload"
target=""

# The OUTBOUND text, per tool. Reading the whole `tool_input` would also read
# `file_path`, and a vault note whose FILENAME carries a codename would then
# refuse every edit of itself — an over-refusing guard is switched off within a
# day, so precision is the point. Anything unrecognised (every MCP tool) falls
# back to the whole JSON arguments, which can only over-refuse.
if [ -n "$JQ" ]; then
  # ONE jq CALL, NOT SIX. Every field this script needs comes out of a single
  # invocation: six `jq` processes plus one per path lookup cost 287ms on the
  # read path, measured, against a 5-second budget that FAILS OPEN — and a
  # guard that is skipped for being slow is a guard that is not there. The
  # per-tool text is LAST and is the only field allowed to contain newlines:
  # everything before it reads as one line each, and the remainder is the text.
  #
  # THAT INVARIANT HAS TO BE ENFORCED, NOT ASSUMED (found by the s08 review,
  # 2026-09-04). This comment used to claim the text was "the only field that
  # can contain newlines", which was false — `tool_input.file_path` is
  # attacker-controlled and takes a newline happily. Measured: a `file_path` of
  # "/tmp/exfil.md\nX" shifted every later field by one line, so `cwd` read as
  # "X", `session_id` read as the cwd, and the named-path join read as the
  # session id. No vault resolved, the ring came back empty, no session-union
  # file was written, and an out-of-tree write carrying a Confidential term was
  # ALLOWED.
  #
  # THE REPAIR HAS TO REFUSE, NOT REWRITE (s08 round 2, Codex HIGH). The first
  # fix here stripped LF/CR out of every framing field, which kept the frame
  # aligned but made the guard decide about a path that is NOT the path the
  # tool writes. Measured through the real registry-pinned guard, with
  # cwd=<tree>/alpha/work: a `file_path` of "<tree>/alpha/wo\nrk/leak.md"
  # strips to "<tree>/alpha/work/leak.md", the containment check reads that as
  # an in-tree write and exits 0 — while the tool writes the sibling directory
  # "wo\nrk", which is under no containment base. So the strip stays (it keeps
  # the frame readable) but a framing field that CARRIED an LF or CR is now a
  # refusal in its own right. A real path, cwd or session id never contains
  # one; a payload that does is either broken or hostile, and the guard cannot
  # tell which. `$_dirty` is emitted FIRST so it cannot itself be shifted.
  _fields="$(printf '%s' "$payload" | "$JQ" -r '
      def unframe: tostring | gsub("[\n\r]"; "");
      def dirty: tostring | test("[\n\r]");
      # `tool_input` IS NOT GUARANTEED TO BE AN OBJECT (s08 round 3, Claude
      # HIGH). Indexing a string with `.file_path` is a jq ERROR, so the whole
      # filter exited 5 with no output, every field below came back empty —
      # including the dirty flag — no vault resolved, `$tool` was empty so
      # `--strict` never armed on the web tools, and a Confidential term left
      # the host. Measured on matched pairs, same term, only the SHAPE of
      # `tool_input` differing: Write object rc=2 / string rc=0; WebFetch
      # object rc=2 / string rc=0; WebFetch array rc=0. A non-object shape is
      # now an empty field set for the named-path logic AND is appended to the
      # per-tool text, so its content is still classified.
      def obj: if type == "object" then . else {} end;
      ((.tool_name // "") | unframe) as $t
      | (.tool_input | obj) as $i
      | (if (.tool_input | type) == "object" then [] else [(.tool_input | tostring)] end) as $raw
      | ([["tool_name",                (.tool_name  // "")],
          ["cwd",                      (.cwd        // "")],
          ["session_id",               (.session_id // "")],
          ["tool_input.file_path",     ($i.file_path     // "")],
          ["tool_input.notebook_path", ($i.notebook_path // "")],
          # `path` ONLY WHEN IT IS WHAT THE JOIN WOULD CONSUME (s08 round 3,
          # Claude MEDIUM). The named-path join filters `path` with
          # `startswith("/")`, so a relative or non-path value is discarded and
          # can alias nothing — but the matcher covers `mcp__.*`, where `path`
          # is an argument name whose meaning this guard does not know.
          # Refusing an MCP call over a multi-line argument it never reads is
          # the over-refusal the design paragraph above warns about.
          # `file_path` and `notebook_path` feed `$target` with no such filter,
          # so those two stay checked unconditionally.
          ["tool_input.path",
           ($i.path | if type == "string" and startswith("/") then .
                      else "" end)]]
         | map(select(.[1] | dirty)) | (.[0][0] // "0")),
        $t,
        (($i.file_path // $i.notebook_path // "") | unframe),
        ((.cwd // "") | unframe),
        ((.session_id // "") | unframe),
        ([$i.file_path, $i.notebook_path, $i.path]
         | map(select(type == "string" and startswith("/")) | unframe)
         | join("\u0001")),
        ((if   $t == "Bash" or $t == "PowerShell" then [$i.command]
          elif $t == "Write"     then [$i.content]
          elif $t == "NotebookEdit" then [$i.new_source]
          elif $t == "Edit"      then [$i.old_string, $i.new_string]
          elif $t == "MultiEdit" then [(($i.edits // [])[] | .old_string, .new_string)]
          elif $t == "WebSearch" or $t == "WebFetch"
               then [$i.query, $i.url, $i.prompt]
                    + (($i.allowed_domains // []) | map(tostring))
                    + (($i.blocked_domains // []) | map(tostring))
          else [($i | tostring)] end) + $raw
         | map(select(type == "string")) | join(" "))' 2>/dev/null)"
  {
    IFS= read -r _framing_dirty
    IFS= read -r tool
    IFS= read -r target
    IFS= read -r hook_cwd
    IFS= read -r hook_session
    IFS= read -r _named_join
    extracted="$(cat)"
  } <<GUARD_FIELDS
$_fields
GUARD_FIELDS
  # THE FRAME MUST HAVE ARRIVED AT ALL, AND IT MUST SAY WHICH FIELD. `$JQ ...
  # 2>/dev/null` hides any failure of that one call, and an empty field set is
  # the most permissive state this script has: no tool, no vault, no ring,
  # `--strict` unarmed. Line 1 is either the literal "0" or the NAME of the
  # first framing field carrying an LF or CR; anything else means the call did
  # not produce the frame — whatever the reason — and the guard refuses rather
  # than deciding on nothing. (s08 round 3; the round-2 report of this class
  # was measured false against a broken `jq` alone, and true against a payload
  # that breaks the filter — the check belongs here either way.)
  case "$_framing_dirty" in
    0) : ;;
    tool_name|cwd|session_id|tool_input.file_path|tool_input.notebook_path|tool_input.path)
      # A line break here is not repairable. Stripping it kept the frame
      # aligned but made the guard decide about a path that is NOT the path the
      # tool writes: with cwd at <tree>/alpha/work, a file_path of
      # "<tree>/alpha/wo\nrk/leak.md" strips to "<tree>/alpha/work/leak.md",
      # reads as an in-tree write and exits 0, while the tool writes the
      # sibling directory. (s08 round 2). The field is NAMED because on exit 2
      # this stderr is what the model sees, and the payload is already gone.
      printf '%s\n%s\n' \
        "brainiac-egress-guard: \`$_framing_dirty\` contains a line break — \
REFUSING, fail-closed." \
        "  A path, tool name, cwd or session id may not contain one: the guard \
would have to decide about a repaired value that is not what the tool \
receives. Re-issue the call with the line break removed from \
\`$_framing_dirty\`, or remove this hook entry from the PreToolUse block of \
~/.claude/settings.json OUTSIDE a session." >&2
      exit 2 ;;
    *)
      printf '%s\n%s\n' \
        "brainiac-egress-guard: the field extraction produced no usable frame \
— REFUSING, fail-closed." \
        "  \`jq\` is present but that call did not return the expected fields, \
so the guard has no tool name, no vault and no decoder ring to decide with. \
Re-run it with the payload on stdin to see why, or remove this hook entry \
from the PreToolUse block of ~/.claude/settings.json OUTSIDE a session." >&2
      exit 2 ;;
  esac
  named_paths="$(printf '%s' "$_named_join" | tr '\001' '\n')"
  [ -n "$extracted" ] && text="$extracted"
else
  # NO jq (s06 round 1, H-5). This used to leave `$tool` empty, which silently
  # dropped `--strict` on the web tools — the empty-ring refusal was OFF and
  # nothing said so. `grep -o` returns the FIRST match, so a `tool_name` string
  # appearing later inside `tool_input` cannot displace the real one. The
  # per-tool text extraction stays unavailable, so `text` remains the whole
  # payload and every destination test below falls to its checking branch:
  # degraded toward over-checking, never toward allowing.
  tool="$(printf '%s' "$payload" \
          | grep -o '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' \
          | head -1 | sed 's/.*"\([^"]*\)"$/\1/')"
  hook_cwd=""
  hook_session=""
  named_paths=""
  # NO jq ON A MANAGED HOST REFUSES (s06 round 7, Codex). Round 6 moved the
  # vault decision onto `jq`, so this branch left `hook_cwd` empty, the registry
  # resolved NO vault, and no vault ALLOWS — even under `--strict`. A host
  # carrying a `.registry` pin has the managed guard installed, so an absent
  # `jq` there is a broken install and not an absence of authority, exactly as
  # a `.engine` pin naming a missing engine is. A host with no pin keeps the
  # degraded-but-checking behaviour described above.
  if [ -n "$GUARD_REGISTRY" ] && [ -r "$GUARD_REGISTRY" ]; then
    printf '%s\n%s\n' \
      "brainiac-egress-guard: no usable \`jq\` — REFUSING, fail-closed." \
      "  This host pins a workspace registry ($GUARD_REGISTRY), so the guard \
resolves its vault through \`jq\`, and it is not at /usr/bin/jq, \
/opt/homebrew/bin/jq or /usr/local/bin/jq. Install jq into one of those, or \
remove this hook entry from the PreToolUse block of ~/.claude/settings.json \
OUTSIDE a session." >&2
    exit 2
  fi
fi

# WHICH VAULT ANSWERS FOR THIS SESSION (s06 round 5). Resolved per invocation
# from the host registry pinned above, by matching the session's own directory
# against each entry's `workspace_path` and its `vault_path`'s parent. The
# LONGEST match wins, so a workspace nested inside another resolves to itself
# rather than to its ancestor. An equal-length tie sorts by the vault path, so
# two entries claiming one directory always pick the same vault — registry file
# order decided it before, and a lookup whose answer depends on write order is
# a lookup that cannot be reasoned about.
#
# No jq means no lookup and therefore no vault, which is the same fail-safe
# degradation the block above takes: an empty ring plus containment against
# the session's own tree. It never widens what is allowed.
#
# A PATH IS A PATH, WHOEVER NAMED IT (s06 round 7, Codex). This resolved the
# session's `cwd` and nothing else, so a session sitting in beta could reach
# alpha by absolute path — `Read /alpha/...`, a `grep` over it, or
# `brain --vault /alpha/vault get` — and then send an alpha term, because only
# beta was ever recorded. The lookup is a function now and every path a watched
# call NAMES goes through it, so entering a vault by naming a file in it counts
# exactly as much as `cd`-ing there.
vault_for() {
  [ -n "$1" ] || return 0
  [ -n "$GUARD_REGISTRY" ] && [ -r "$GUARD_REGISTRY" ] && [ -n "$JQ" ] || return 0
  # BOTH SPELLINGS OF THE PATH (s06 round 6). The registry holds resolved
  # paths; a payload's need not be one, so a workspace reached through a
  # symlink matched nothing and silently resolved NO vault. Matching on the raw
  # string OR its `pwd -P` resolution can only find a vault where there was
  # none, never swap one for another.
  _p_real="$( (cd "$1" 2>/dev/null && pwd -P) || true)"
  if [ -z "$_p_real" ]; then
    _p_dir="${1%/*}"
    [ "$_p_dir" != "$1" ] && \
      _p_real="$( (cd "$_p_dir" 2>/dev/null && pwd -P) || true)"
  fi
  _v="$("$JQ" -r --arg cwd "$1" --arg real "$_p_real" '
      [ (.entries // [])[]
        | select((.vault_path // "") != "")
        | . as $e
        | [ (.workspace_path // empty),
            ((.vault_path // "") | sub("/[^/]+$"; "")) ]
        | .[]
        | select(. != "")
        | . as $base
        | select($cwd == $base or ($cwd | startswith($base + "/"))
                 or ($real != "" and ($real == $base
                                      or ($real | startswith($base + "/")))))
        | {len: ($base | length), vault: $e.vault_path} ]
      | sort_by([.len, .vault]) | last | .vault // ""' \
      "$GUARD_REGISTRY" 2>/dev/null || true)"
  case "$_v" in
    /*) [ -d "$_v" ] && printf '%s\n' "$_v" ;;
  esac
  return 0
}

# EVERY CANDIDATE IN ONE `jq` CALL. A shell loop calling `vault_for` per path
# forks `jq` once per candidate, and the Bash extraction below produces dozens
# on an ordinary command line — inside a 5-second budget that FAILS OPEN, that
# is the guard turning itself off on long commands. This resolves the whole list
# in one process; the shell still verifies each answer is an absolute existing
# directory before it counts.
vaults_for_many() {
  [ -n "$1$2" ] || return 0
  [ -n "$GUARD_REGISTRY" ] && [ -r "$GUARD_REGISTRY" ] && [ -n "$JQ" ] || return 0
  "$JQ" -r --arg blob "$1" --arg text "$2" '
      ([ (.entries // [])[]
         | select((.vault_path // "") != "")
         | . as $e
         | [ (.workspace_path // empty),
             ((.vault_path // "") | sub("/[^/]+$"; "")) ]
         | .[] | select(. != "")
         | {base: ., vault: $e.vault_path} ]) as $bases
      | ([ ($blob | split("\n") | map(select(. != "")))[]
          | (split("\t") | map(select(. != ""))) as $p
          | [ $bases[] as $b
              | select(any($p[]; . == $b.base or startswith($b.base + "/")))
              | {len: ($b.base | length), vault: $b.vault} ]
          | select(length > 0) | sort_by([.len, .vault]) | last | .vault ])
      # PLUS EVERY BASE THE RAW TEXT SPELLS OUT. The tokenizer above splits on
      # any character that cannot appear in a bare path, so a vault whose path
      # holds a space or a `*` is torn in half and never resolves — measured:
      # `cat "/tmp/x/alpha work/brain/index.md"` yields only `/tmp/x/alpha`.
      # Tokenizing a shell correctly is not a job for this guard, so it asks
      # the opposite question instead: does the text contain a base we already
      # know? That is exact whatever the quoting, and it costs nothing extra.
      # Over-collection only WIDENS the ring, which refuses more and leaks
      # less, so a coincidental substring is the safe direction to be wrong in.
      + ([ $bases[] as $b | select($text | contains($b.base)) | $b.vault ])
      | unique | .[]' "$GUARD_REGISTRY" 2>/dev/null || true
}

# The `pwd -P` half of `vault_for`, for one candidate, as a "raw<TAB>real" pair.
path_pair() {
  _p_real="$( (cd "$1" 2>/dev/null && pwd -P) || true)"
  if [ -z "$_p_real" ]; then
    _p_dir="${1%/*}"
    [ "$_p_dir" != "$1" ] && \
      _p_real="$( (cd "$_p_dir" 2>/dev/null && pwd -P) || true)"
  fi
  printf '%s\t%s\n' "$1" "$_p_real"
}

GUARD_VAULT="$(vault_for "$hook_cwd")"

# EVERY VAULT THIS SESSION HAS ENTERED, NOT JUST THE CURRENT ONE (s06 round 6,
# Codex HIGH). Resolving per invocation gave two vaults two rings, which was the
# point — but it also meant a `cd` CHANGED which ring answers while the
# conversation kept everything it had already read. Read an alpha-only codename,
# `cd` into beta, send it: only beta's ring was consulted and it does not know
# the term. Isolation BETWEEN vaults and containment WITHIN a session are two
# requirements, and the round-5 test asserted the second one away.
#
# So the session accumulates. Each resolved vault is appended once to a file
# named by the session id, and the check below runs against EVERY vault in it —
# a refusal from any of them refuses. Leaving a vault cannot lower the bar.
#
# The state lives beside the registry, NOT under `$HOME`: the directory comes
# from the pinned absolute path, so no environment variable moves it. The
# session id is taken from the payload and used as a FILENAME, so it is accepted
# only when it is entirely `[A-Za-z0-9_-]` — anything else, and this degrades to
# the single current vault rather than writing a path of the payload's choosing.
# An unwritable state directory degrades the same way; it never blocks the call
# on its own, because a guard that refuses when it cannot take notes is a guard
# uninstalled the same day.
# THE PATHS THIS CALL NAMES COUNT AS ENTERING (s06 round 7, Codex). A `--vault`
# argument inside a shell command is picked out of the text too: `brain --vault
# /alpha/vault get ...` from a beta session is the cheapest cross-vault read
# there is, and it never changes the working directory. Both are a FLOOR, and
# the header says so: a path built from a variable, or a read through the desk
# (which carries no directory at all), still reaches a vault this never sees.
#
# EVERY ABSOLUTE TOKEN IN THE TEXT, NOT JUST `--vault` (s06 round 8, Codex
# HIGH). Round 7 matched one unquoted `--vault /path` spelling with a `sed`
# expression. Every ordinary form missed it — `--vault "/alpha/vault"`,
# `--vault='/alpha/vault'`, `cat /alpha/vault/note.md`, `cd /alpha/vault` — and
# each of those is a read that never joins the union, which is the round-6 hole
# one spelling over. The shell is not tokenized here; the text is SPLIT on every
# character that cannot appear in a path, and every fragment starting with `/`
# becomes a candidate. Quotes, `=`, `;`, `|` and `&` are separators, so all four
# forms above yield the same candidate.
#
# Over-collection is the safe direction and is bounded: a fragment that resolves
# to no registered vault contributes nothing, and the whole list costs ONE `jq`
# call. The cap of 40 exists so a pathological command line cannot spend the
# fail-open budget on `pwd -P` subshells.
#
# THE CEILING, unchanged: a path built from a variable, and a read through the
# desk channel (which carries no directory at all), still reach a vault this
# never sees. This is a floor.
GUARD_NAMED=""
_maybe="$named_paths"
case "$tool" in
  Bash|PowerShell)
    _maybe="$_maybe
$(printf '%s' "$text" | tr -cs 'A-Za-z0-9_./~-' '\n' | grep '^/' | sort -u \
  | head -40)" ;;
esac
_pairs=""
_old_ifs="$IFS"
IFS='
'
for _cand in $_maybe; do
  IFS="$_old_ifs"
  [ -n "$_cand" ] && _pairs="$_pairs$(path_pair "$_cand")
"
  IFS='
'
done
IFS="$_old_ifs"
# READ THE ANSWERS LINE BY LINE, NEVER `for x in $(...)`. A vault path is a
# path, and a path may hold a space or a `*`. Unquoted command substitution
# splits on the default IFS and then globs, so `/Users/x/alpha work/vault`
# arrived as two fragments, both failed `[ -d ]`, and the vault silently never
# joined the session union — measured: the same cross-vault Confidential term
# exits 2 from `alphawork` and exits 0 from `alpha work`. Every other loop in
# this file pins IFS to a newline; this one did not.
while IFS= read -r _found; do
  case "$_found" in
    /*) [ -d "$_found" ] && GUARD_NAMED="$GUARD_NAMED
$_found" ;;
  esac
done <<GUARD_FOUND
$(vaults_for_many "$_pairs" "$text")
GUARD_FOUND

GUARD_VAULTS="$GUARD_VAULT"
case "$hook_session" in
  "" | *[!A-Za-z0-9_-]* ) hook_session="" ;;
esac
if [ -n "$GUARD_REGISTRY" ] && [ -n "$hook_session" ]; then
  _sdir="${GUARD_REGISTRY%/*}/egress-sessions"
  _sfile="$_sdir/$hook_session"
  # APPEND BLIND, DEDUP ON READ (s06 round 7, Claude). A `grep` then a `printf`
  # is check-then-act with no lock, and several PreToolUse hooks can be in
  # flight at once, so the check never removed the duplicate it was there to
  # prevent. Correctness never depended on it — the merge is idempotent — but
  # every duplicate line costs another ring resolution inside a 5-second budget
  # that FAILS OPEN. `sort -u` on the read side has no race to lose.
  _record="$(printf '%s\n%s\n' "$GUARD_VAULT" "$GUARD_NAMED" \
             | grep -v '^$' | sort -u)"
  if [ -n "$_record" ]; then
    if [ ! -d "$_sdir" ]; then
      mkdir -p "$_sdir" 2>/dev/null && chmod 700 "$_sdir" 2>/dev/null || true
    fi
    printf '%s\n' "$_record" >> "$_sfile" 2>/dev/null || true
  fi
  if [ -r "$_sfile" ]; then
    _seen="$(sort -u "$_sfile" 2>/dev/null || true)"
    [ -n "$_seen" ] && GUARD_VAULTS="$_seen"
  fi
fi

# RECORD-ONLY TOOLS STOP HERE. `Read`, `Grep` and `Glob` send nothing off this
# host, so there is nothing for an egress guard to say about them — but they are
# how a session ENTERS a vault it never `cd`-ed into, and that has to be written
# down before the vault's terms can refuse. They pay one registry lookup and no
# engine call, which is what keeps them off the fail-open budget.
case "$tool" in
  Read|Grep|Glob) exit 0 ;;
esac

# THE GUARD'S OWN CONTROL FILES ARE NOT A DESTINATION (s06 round 8, both
# reviewers, same class from two sides). Codex: the `.engine` pin names any
# absolute executable the guard will trust, so one allowed `Bash` write of
# `/bin/true` into it is a permanent, silent kill switch that `_pin_faults`
# reports as healthy. Claude: the session union under `egress-sessions/` is a
# plain file, so `rm` or `: >` erases every vault this session recorded and the
# doctor row cannot tell an erased session from one that never read anything.
# Neither write matches an outbound pattern, so both used to exit 0 unexamined.
#
# WHY THIS IS A FLOOR AND NOT A FIX. An agent holding arbitrary local shell can
# reach any file the owner can, and this refuses only the spellings that name
# the guard's own directories LITERALLY. `$HOME/.claude/hooks/...`, a path
# assembled from a variable, or a helper script that writes it, all pass. The
# real boundary is that a hook lives in the space of the account it guards;
# `docs/security-acceptances.md` A-06 states that, and this stops the direct
# form rather than pretending to close the class.
# BOTH SPELLINGS OF THE DIRECTORY, for the same reason `vault_for` matches
# both: `/tmp` is a symlink on macOS, so the resolved form alone missed the
# spelling a session actually types. Measured — the resolved-only version
# exited 0 on `printf /bin/true > /tmp/.../brainiac-egress-guard.engine`.
_self_dir_raw="${_self%/*}"
_self_dir="$( (cd "$_self_dir_raw" 2>/dev/null && pwd -P) || true)"
case "$_self_dir_raw" in /*) : ;; *) _self_dir_raw="" ;; esac
for _own in "$_self_dir" "$_self_dir_raw" \
            "${GUARD_REGISTRY%/*}/egress-sessions" "$GUARD_REGISTRY"; do
  [ -n "$_own" ] || continue
  # THE RESOLVED PATHS TOO, NOT ONLY THE TYPED TEXT. `$_pairs` already holds
  # every absolute path this call names beside its `pwd -P` resolution, built
  # for the ring union above — so `/tmp/...` typed against a hooks directory
  # that really lives at `/private/tmp/...` matches here without a second
  # resolver. Measured: the text-only version exited 0 on exactly that.
  case "$text $target $_pairs" in
    *"$_own"*)
      printf '%s\n%s\n' \
        "brainiac-egress-guard: this call names the guard's own control files \
($_own) — REFUSING." \
        "  The engine pin, the workspace registry and the session union decide \
what this guard trusts, so a session may not write them. Change them OUTSIDE a \
session, or re-run \`brain install-hook\`." >&2
      exit 2 ;;
  esac
done

# CANONICAL PATH, symlinks REJECTED, for a file that need not exist yet.
# `pwd -P` is the only portable symlink resolver in a POSIX shell (macOS has no
# GNU `readlink -f`), so the deepest EXISTING ancestor is resolved with it and
# the not-yet-created tail is re-appended. Prints nothing when the path cannot
# be resolved at all — the caller then CHECKS the call rather than exempting it.
#
# NO SYMLINK ANYWHERE ON THE PATH, THE LEAF INCLUDED (s06 round 3, H-3). Round
# 2 resolved a symlinked DIRECTORY correctly and left the LEAF open: `pwd -P`
# only ever ran on components for which `-d` succeeded, so
# `<tree>/note.md -> /tmp/sync/exfil.md` was stripped as an unresolved tail and
# re-appended under the canonical IN-TREE parent — exit 0, while the identical
# destination spelled directly was exit 2. One `ln -s` re-opened the finding.
# The round-2 test used a symlinked directory, which this code DID resolve, so
# it could never catch the leaf.
#
# The fix is structural rather than another special case: every component of
# the literal path is tested with `-L` before anything is resolved, and ANY
# symlink — leaf, ancestor, or dangling — makes the whole path unresolvable.
# Unresolvable means the caller CHECKS the call instead of exempting it, so the
# cost lands the safe way round. That also covers the two neighbours the leaf
# fix alone would miss: a DANGLING symlink (`-d`, `-e` and `-f` are all false
# on one, `-L` is true) and `..` written AFTER a symlinked component.
#
# WHAT IT STILL CANNOT DO — TOCTOU, and it is not fixable here. Any component
# can be swapped for a symlink between this check and the write that follows;
# a `PreToolUse` hook judges a STRING and the write happens later, in another
# process. Nothing in a hook closes that. It is recorded, not claimed away.
canon() {
  _p="$1"; _rel="${2:-$PWD}"; _tail=""
  [ -n "$_p" ] || return 1
  case "$_p" in
    /*) : ;;
    "~"|"~/"*) _p="$HOME${_p#\~}" ;;
    *) _p="$_rel/$_p" ;;
  esac
  while [ "${_p%/}" != "$_p" ] && [ "$_p" != "/" ]; do _p="${_p%/}"; done
  _probe="$_p"
  while [ -n "$_probe" ] && [ "$_probe" != "/" ]; do
    [ -L "$_probe" ] && return 1
    _next="${_probe%/*}"
    [ -n "$_next" ] || _next="/"
    [ "$_next" = "$_probe" ] && break
    _probe="$_next"
  done
  while [ -n "$_p" ] && [ ! -d "$_p" ]; do
    _tail="/${_p##*/}$_tail"
    _next="${_p%/*}"
    [ -n "$_next" ] || _next="/"
    [ "$_next" = "$_p" ] && return 1
    _p="$_next"
  done
  ( cd -P -- "$_p" 2>/dev/null && printf '%s%s\n' "$(pwd -P)" "$_tail" )
}

# Programs whose job is moving bytes off this host, plus a literal URL scheme
# (which catches `open`, a browser launch, and an interpreter one-liner that
# spells its destination out). See the CEILING note in the header: this is an
# enumeration and therefore a floor.
#
# GIT'S GLOBAL OPTIONS SIT BETWEEN THE TWO (s06 round 3, H-5). The subcommand
# used to have to follow `git` IMMEDIATELY, so `git push` was exit 2 while
# `git -C /repo push`, `git --git-dir=x push` and `git -c user.name=x push` —
# the same push, three documented spellings — were all exit 0. With a remote
# already configured there is no URL left in the line to fall back on, so the
# whole git half of this matcher was one flag away from off. `GIT_GLOBAL_RE`
# below is git's own global-option sequence, skipped before the subcommand is
# read. It is ENUMERATED and therefore a floor like everything else here: a
# global option not on this list still hides the subcommand.
GIT_GLOBAL_RE='(-[cC][[:space:]]+[^[:space:]]+|--(git-dir|work-tree|namespace|exec-path|config-env|super-prefix)(=|[[:space:]]+)[^[:space:]]+|--(no-pager|paginate|bare|literal-pathspecs|glob-pathspecs|noglob-pathspecs|icase-pathspecs|no-replace-objects|no-optional-locks)|-p)[[:space:]]+'
OUTBOUND_RE='(^|[^[:alnum:]_./-])(curl|wget|nc|ncat|netcat|telnet|ssh|scp|sftp|rsync|ftp|lftp|httpie|http|xh|gh|glab|aws|az|gcloud|gsutil|s3cmd|rclone|kubectl|mail|mailx|sendmail|msmtp|mutt|osascript)([^[:alnum:]_-]|$)|(^|[^[:alnum:]_-])git[[:space:]]+('"$GIT_GLOBAL_RE"')*(push|clone|fetch|pull|remote|ls-remote|send-email)|(https?|ftp|sftp|ssh)://'

# A LEADING PATH USED TO DEFEAT ALL OF IT (s06 round 2, H-2). The anchor class
# above excludes `/`, so `curl …` was refused and `/usr/bin/curl …` — the same
# program, on the list — was ALLOWED. Widening the class instead would make
# every `docs/http.md`-shaped filename a hit, so the TEXT is normalised rather
# than the pattern: each whitespace token is reduced to its basename, and the
# result is matched ALONGSIDE the original line. The original is what still
# carries a literal `https://` URL (basename-stripping eats one); the stripped
# copy is what turns `/usr/bin/curl` back into `curl`.
#
# `git push` also had no LEFT boundary until now, so `echo legit push` — "le-git
# push" — refused, which is a false refusal on ordinary prose in a guard whose
# stated failure mode is being uninstalled for over-refusing (M-1).
strip_paths() {
  # Each whitespace token reduced to its basename. Pure parameter expansion:
  # no `tr`, no `sed`, no subprocess — see `is_outbound` for why that matters.
  set -f                      # a `*` in the command must not glob to filenames
  # Word splitting IS the operation here. The directive below carried that
  # prose on its own line until 2026-09-04, which shellcheck parses as a
  # malformed key (SC1072/SC1073) and refuses — so the disable was never
  # in effect. A directive's line holds the directive and nothing else.
  # shellcheck disable=SC2086
  set -- $1
  _out=""
  for _w in "$@"; do _out="$_out ${_w##*/}"; done
  set +f
  printf '%s' "$_out"
}

# NO PIPE INTO `grep -q`, and that is a fix rather than a style choice.
# `grep -q` EXITS ON ITS FIRST MATCH, closing the pipe under whatever is still
# writing; that writer takes SIGPIPE; `set -o pipefail` turns the whole
# pipeline non-zero; and `|| exit 0` reads non-zero as "not outbound" and
# ALLOWS. A MATCH makes the guard more likely to allow — the exact inversion,
# with empty stderr and no way to tell it from a genuinely local command.
# The first cut of this path-stripping did the stripping in a `tr|tr|sed|tr`
# pipeline and hit it deterministically: this repo's own suite turned exit 2
# into exit 0 on `curl -d <term> https://…`. (The single-`printf` form it
# replaced did not fire it even on a 1 MB payload — measured 0/10 — which is
# exactly why it is worth removing the shape rather than the symptom.) Bash's
# own `=~` takes no pipe, forks nothing, and needs nothing on `$PATH`.
is_outbound() {
  shopt -s nocasematch
  if [[ "$1" =~ $OUTBOUND_RE ]] || [[ "$(strip_paths "$1")" =~ $OUTBOUND_RE ]]; then
    shopt -u nocasematch
    return 0
  fi
  shopt -u nocasematch
  return 1
}

# Is this call's DESTINATION off this host? An `exit 0` here is a call that
# never leaves, so there is nothing for an egress guard to say about it.
case "$tool" in
  WebSearch|WebFetch)
    : ;;                                    # third party by definition
  Bash|PowerShell)
    is_outbound "$text" || exit 0 ;;
  Write|Edit|MultiEdit|NotebookEdit)
    # A write INTO the working tree or into the vault is not egress — it is the
    # thing this vault is for. Anything else (a synced folder, a home-directory
    # drop, /tmp) is checked. `$BRAIN_VAULT` is read for the case where the
    # vault is not under the session's CWD.
    #
    # CANONICALISED, because a string prefix is not containment (s06 round 2,
    # H-4). This was `case "$target" in "$hook_cwd"/*)` against the RAW strings,
    # so `/tmp/exfil.md` was refused while `<tree>/../../../../../tmp/exfil.md`
    # — the same file — was allowed. The same destination must not pass or fail
    # on how it is spelled. Both sides are resolved through `canon` below, and a
    # target that will not resolve is CHECKED rather than exempted.
    #
    # A CONTAINMENT BASE MAY NOT BE THE WHOLE MACHINE (s06 round 3, C-1's
    # class). `$BRAIN_VAULT` used to be read from the same inherited
    # environment that `BRAIN_BIN` was, so `BRAIN_VAULT=/` — or `=$HOME` —
    # made every write target "inside the vault" and exempted the entire
    # filesystem from the guard. A base of `/` or of the home directory itself
    # is still refused as a base here: neither is a working tree. The variable
    # itself is GONE from this decision as of round 4 — the third base is now
    # `$GUARD_VAULT`, read from the pin file beside this script, never from
    # the environment the project can write.
    [ -n "$hook_cwd" ] || hook_cwd="$PWD"
    chome="$(canon "$HOME" "$PWD")"
    ctarget="$(canon "$target" "$hook_cwd")"
    if [ -n "$ctarget" ]; then
      for base in "$hook_cwd" "$PWD" "$GUARD_VAULT"; do
        [ -n "$base" ] || continue
        cbase="$(canon "$base" "$PWD")"
        [ -n "$cbase" ] || continue
        [ "$cbase" = "/" ] && continue
        [ -n "$chome" ] && [ "$cbase" = "$chome" ] && continue
        case "$ctarget" in
          "$cbase"/*) exit 0 ;;
        esac
      done
    fi ;;
  *)
    : ;;                                    # mcp__* and unrecognised: check
esac

# --strict (an empty decoder ring is itself a refusal) on the tools that reach
# a THIRD PARTY. Not on Bash or a file write: a vault whose ring has not filled
# yet would lock the owner out of their own shell, and a guard that does that
# is uninstalled the same afternoon. `check-egress` does NOT apply --strict to
# the no-vault case — see its "NO VAULT IS NOT A FAILED CHECK" note.
strict=()
case "$tool" in
  WebSearch|WebFetch) strict=(--strict) ;;
esac

# THE CHILD GETS A SCRUBBED ENVIRONMENT, and that is the CLASS closed rather
# than the two instances (s06 round 3, C-1). Every variable below changes what
# the engine ANSWERS, and every one of them is settable by a committed project
# settings `env` block:
#   BRAIN_BIN                    — which engine (the finding itself)
#   BRAINIAC_EGRESS_GUARD        — the retired in-band escape
#   BRAIN_OVERLAY_DIR            — WHICH decoder ring; point it at an empty
#                                  directory and the ring reads empty, which
#                                  allows on every non-strict tool
#   BRAIN_EGRESS_TERM_MIN_TIER   — the refusal threshold; `=MNPI` lets every
#                                  Confidential and Restricted term through
#   BRAIN_VAULT                  — WHICH vault, and therefore which ring; a
#                                  path to nothing reads as an empty ring and
#                                  allowed every non-strict call (s06 round 4)
# `$BRAIN_VAULT` was the one variable this list deliberately kept, on the
# reasoning that the worst a hostile value produced was the empty-ring state.
# That was wrong twice over: the empty ring ALLOWS on every non-strict tool
# (measured exit 0 on an outbound call carrying a live ring term), and the same
# variable was a write-containment base. It is scrubbed like the rest now, and
# the pinned vault is passed back in explicitly when there is one.
# `brain doctor` names every one of these, set or not.
# ONE CALL, EVERY RING THIS SESSION HAS ENTERED (s06 round 6). The first cut of
# the session set ran the engine once per vault, and that does not fit the
# budget: measured 0.96s for one vault and 2.19s for two, against a 5-second
# hook timeout that Claude Code resolves by CANCELLING the hook — which is an
# ALLOW. Four registered vaults on the reference host would therefore have
# disarmed the guard by being slow. `--extra-vault` merges the rings inside the
# engine instead, so the cost is one process for any number of vaults.
_primary=""
_extra=""
_old_ifs="$IFS"
IFS='
'
for _v in $GUARD_VAULTS; do
  IFS="$_old_ifs"
  [ -n "$_v" ] || continue
  if [ -z "$_primary" ]; then _primary="$_v"; else _extra="$_extra
$_v"; fi
  IFS='
'
done
IFS="$_old_ifs"

set --
IFS='
'
for _v in $_extra; do
  IFS="$_old_ifs"
  [ -n "$_v" ] && set -- "$@" --extra-vault "$_v"
  IFS='
'
done
IFS="$_old_ifs"

# `${_primary:+...}` OMITS `--vault` WHEN NO VAULT RESOLVED, AND THAT IS
# DELIBERATE (s10 llm-review-medium, Claude HIGH, answered 2026-09-04). The
# finding read the omission as a lockout: `check-egress` would fall back to
# `CWD/vault`, raise `VaultNotFoundError`, exit 3, and the fail-closed arm
# below would turn that into exit 2 for every outbound call in every session
# whose cwd sits outside a registered workspace.
#
# The ENGINE owns that case, and does it correctly: `egress_terms` catches
# `VaultNotFoundError`, reports `vault_resolved: false` and returns
# "allowed — NO VAULT resolvable from here", exit 0. Traced with `bash -x`.
# Deciding it a second time HERE would duplicate the rule in a place that
# cannot see the ring, so the guard does not.
#
# THE REAL RISK IS VERSION SKEW, not this line. That engine behaviour is NEW
# on this branch; 0.20.35 still raises. A guard from here paired with an
# engine from before it does produce exactly the lockout the finding
# describes — which is why `session_hook.install` writes the `.engine`
# sidecar beside the script, and why both reproductions of this "bug" used a
# rig whose sidecar was misnamed `<script>.sh.engine` and so fell through to
# an older `brain` on `$PATH`. The invariant is held by
# `test_a_cwd_outside_every_registered_vault_allows`, which fails with exit 2
# when the engine's no-vault allow is removed.
verdict="$(printf '%s' "$text" | env -u BRAIN_BIN -u BRAINIAC_EGRESS_GUARD \
  -u BRAIN_OVERLAY_DIR -u BRAIN_EGRESS_TERM_MIN_TIER -u BRAIN_VAULT \
  "$BRAIN_BIN" ${_primary:+--vault "$_primary"} check-egress "$@" \
  "${strict[@]+"${strict[@]}"}" 2>&1)"
status=$?

# FAIL CLOSED. Exit 6 is the refusal `check-egress` means; every OTHER non-zero
# exit is this guard unable to judge, and "unable to judge" is not "safe". Both
# block, and both print why — the reason is the only thing that lets the owner
# tell a real refusal from a broken engine. The escape is named in the message
# because a fail-closed path with no way out locks the owner out of the very
# `Edit` that would remove this hook.
if [ "$status" -eq 6 ]; then
  printf '%s\n' "$verdict" >&2
  exit 2            # Claude Code: block the tool call, show stderr to the model
fi
if [ "$status" -ne 0 ]; then
  printf 'brainiac-egress-guard: check FAILED (exit %s) — REFUSING, fail-closed. %s\n%s\n' \
    "$status" "$verdict" \
    "  If this is the engine broken rather than a real refusal, the recovery \
is EXTERNAL and deliberately so: quit Claude Code, remove this hook's entry \
from the PreToolUse block of ~/.claude/settings.json (or from the harness repo \
that owns it), and start a new session. No environment variable turns this off \
— committed project settings can define those." >&2
  exit 2
fi
exit 0

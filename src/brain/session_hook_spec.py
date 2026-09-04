"""What the two shipped hooks ARE — names, events, entries and matchers.

Split out of ``session_hook.py`` at the 2026-09-04 size ratchet so the
settings-mutation half can import the specs without a cycle. ``session_hook``
re-exports every name here, so ``brain.session_hook.<name>`` is unchanged for
every caller and test.
"""
from __future__ import annotations

from typing import Any, NamedTuple


HOOK_SCRIPT = "brainiac-alerts.sh"
HOOK_EVENT = "SessionStart"
HOOK_ENTRY = {
    "type": "command",
    "command": "",   # filled in per install — see `hook_command`
    "timeout": 10,
    "statusMessage": "Checking Brainiac health",
}

GUARD_SCRIPT = "brainiac-egress-guard.sh"
GUARD_EVENT = "PreToolUse"
#: Which tool calls the SEC-07 guard inspects. Claude Code matches a tool by
#: name, and an MCP tool's name is `mcp__<server>__<tool>`.
#:
#: Widened 2026-09-02 (M-2). `WebSearch|WebFetch` covered two doors and left
#: every other one open: a `Bash` curl, a `Write` into a synced folder, and any
#: MCP tool at all are outbound channels the model reaches without a prompt,
#: and the repo-local settings allowed `Bash(python3 *)` outright. Naming MCP
#: servers we cannot know was the old excuse for leaving them out; `mcp__.*`
#: needs no such knowledge.
#:
#: `Read`, `Grep` and `Glob` ARE matched, and they are RECORD-ONLY (s06 round 7).
#: They are inbound, and a guard that refuses a read is a guard uninstalled in a
#: day — so the script exits 0 for them before any destination test and before
#: the engine call, and they can never refuse. They are matched anyway because
#: the path a read NAMES is how the session learns it entered a second vault,
#: which is what keeps the cross-vault union honest. This comment asserted the
#: opposite until round 8; believing it would silently revert that fix.
GUARD_MATCHER = "WebSearch|WebFetch|Bash|PowerShell|Write|Edit|NotebookEdit|Read|Grep|Glob|mcp__.*"
GUARD_ENTRY = {
    "type": "command",
    "command": "",
    "timeout": 5,
}


class HookSpec(NamedTuple):
    """One placeable hook. Two ship today; the shape is the point.

    ``brainiac-egress-guard.sh`` was added on 2026-09-01 and reproduced this
    module's own founding defect for a day: it rode the wheel and NO install
    path placed it, which is exactly the "a hard wiring nothing installs is a
    soft one" failure the docstring above describes. A second hook could not be
    added without generalising, so it was generalised.
    """

    script: str
    event: str
    entry: dict[str, Any]
    matcher: str | None
    label: str
    #: What the OWNER loses when this hook is silently absent. The row exists
    #: to name that, not to report a missing file: "script not found" is not a
    #: consequence anyone can weigh.
    silent_symptom: str


HOOKS: tuple[HookSpec, ...] = (
    HookSpec(HOOK_SCRIPT, HOOK_EVENT, HOOK_ENTRY, None,
             "SessionStart alert hook",
             "sessions open with NO degradation banner, which reads exactly "
             "like a healthy vault"),
    HookSpec(GUARD_SCRIPT, GUARD_EVENT, GUARD_ENTRY, GUARD_MATCHER,
             "PreToolUse egress guard (SEC-07)",
             "a web search, shell command, file write or MCP call carrying a "
             "term this vault classifies Confidential or above leaves "
             "unchecked, and nothing records that it did"),
)


# A `~/.claude` that some harness repo DEPLOYS carries its own tooling. Two
# marker files are enough to recognise one, and both are cheap file reads.
HARNESS_MARKERS = ("scripts/gearbox", "scripts/deploy.pathspec")

# Installing `brain` — pick your client

This is the **3-tier onboarding index**. Every client reaches `brain` the same
underlying way (native shell — no MCP, no plugin, no daemon), but the exact
steps differ because Codex, Claude Code CLI, and Cowork discover skills and
run the binary differently. Full per-client reach-path matrix (roles,
verb restrictions, the one MCP exception): `docs/cutover/client-access-model.md` (s01).

| Client | Guide | Role | One-line summary |
|---|---|---|---|
| **Claude Code CLI** (Mac/Windows host) | [`claude-code.md`](./claude-code.md) | `host` — full read/write/maintenance | Clone → skills auto-load → `brain` on PATH → `brain init --full` |
| **Codex CLI** (Mac/Windows host) | [`codex.md`](./codex.md) | `host` — full read/write/maintenance | Clone → `.agents/skills/` auto-detected → same `brain init --full` |
| **Cowork** (Claude Desktop, Linux VM sandbox) | [`cowork.md`](./cowork.md) | `vm` — **read + draft only** | Clone (host-side) → install the `.brain/` runtime into the workspace → `setup-cowork` → paste the idempotent task-registration prompt |

If you only read one thing before starting: **Cowork is fundamentally
different from the other two.** It is read+draft only (no writes, no
scheduler, no signing key) and its tasks are on-demand brief/digest triggers,
never host maintenance. See `cowork.md` before assuming Cowork can do
anything the host clients do.

For the durable "what runs where and why" picture (not the step-by-step),
read [`new-owner.md`](./new-owner.md) — `brain init`, the personalization
overlay, the host/VM split, where the kernel skills + manifest live, and the
claude.ai web-chat gap. For the exact CLI verb reference:
`docs/cutover/brain-cli-verbs.md`. For the cutover-retirement side of this
(safely turning OFF the old Smart-Connections-wired scheduled tasks once a
new owner is live on `brain`): `docs/cutover/runbook.md`.

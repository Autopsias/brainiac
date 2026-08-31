---
name: setup-cowork
description: "RETIRED 2026-08-30 — superseded by /brainiac-cowork-setup and docs/install/cowork.md, which describe the current broker layout (vault off the Cowork mount) instead of the pre-cutover co-located one this skill taught. Kept only as a pointer so an old trigger phrase still lands somewhere useful. Triggers: 'set up Cowork', 'set up cowork for this vault', 'onboard this vault to Cowork', 'cowork setup', 'upload the skills to Cowork', 'get the brain skills working in Cowork'."
---

# setup-cowork — retired, use `/brainiac-cowork-setup`

This skill taught a manual, four-step Cowork onboarding checklist
(workspace-install by hand, skill uploads, a per-session env-and-symlink
bootstrap, the VM-role constraint) written for the layout where the vault
sat one level inside the Cowork-attached folder. Closed Stacks
(2026-08-27 → 2026-08-30) moved the vault off that mount — the workspace now
carries only the staged engine, model cache, skills and routines, and every
retrieval verb reaches the vault through the host `brain-mcp` broker instead
of a local `brain` binary. Nothing in the old checklist still applies as
written, so it is retired rather than rewritten around content
`/brainiac-cowork-setup` and `docs/install/cowork.md` already cover
end-to-end.

**Use instead:**

- **`/brainiac-cowork-setup`** — the one-command host setup, the split-brain
  guard, and the exact folder/session-prompt/skills instructions to finish
  in Cowork.
- **`docs/install/cowork.md`** — the full Quickstart, the broker-layout
  explanation, and the skill-upload order.
- **`docs/install/cowork-session-prompt.md`** — what a Cowork session is
  taught at start, staged into every workspace automatically.

If you were about to walk someone through this skill's old steps, stop and
run `/brainiac-cowork-setup` instead — it does the same job against the
current layout.

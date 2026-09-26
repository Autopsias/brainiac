# Brainiac — Kernel Skills

Daily-use skills for a [Brainiac](https://github.com/Autopsias/brainiac) vault: a local
second brain kept as plain Markdown and YAML, indexed on your own machine by the `brain`
command-line engine.

## What it does

The skills search, capture, curate and promote notes in your vault:

- `kb-curator` — audits and maintains the vault.
- `promote` — moves a draft note into a typed folder (projects, areas, resources, archive).
- `vault-ingestion` — captures a new source into `vault/raw/`.
- `save-conversation` — turns part of a chat into a typed note.
- `vault-eval` — measures retrieval quality.
- `overlay-style` — drafts and checks text against the vault owner's house style.
- `brain-inbox`, `graph-explorer`, `vm-doctor` — the owner-question queue, the graph page,
  and a health check inside Cowork.

## What it needs

- The `brain` engine installed on the same machine. Install it with the
  `brainiac-manager` plugin, or follow the
  [install guide](https://github.com/Autopsias/brainiac/tree/main/docs/install).
- Claude Code or Cowork. The skills run a local command, so they do not work in a
  plain chat on claude.ai.

## What it runs, sends and fetches

- The skills run the local `brain` CLI. The CLI reads and writes files in your vault.
- The plugin sends nothing to any third party and has no MCP server.
- The output of `brain` becomes part of your conversation with Claude. The CLI applies a
  classification filter to that output before Claude sees it.

## License

Apache-2.0. Report a security problem through the process in
[SECURITY.md](https://github.com/Autopsias/brainiac/blob/main/SECURITY.md).

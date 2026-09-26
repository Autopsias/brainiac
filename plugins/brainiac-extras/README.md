# Brainiac — Extras

Optional maintenance skills for a [Brainiac](https://github.com/Autopsias/brainiac)
vault: a local second brain kept as plain Markdown and YAML, indexed on your own
machine by the `brain` command-line engine. Install the kernel plugin first. Add
this one when you want the maintenance work one command away.

## What it does

- `curation` — finds stale links between notes and lists notes due for a revisit.
- `improve` — runs a retrospective on the current session and proposes changes to
  your conventions files, one at a time.
- `task-registrar` — registers the scheduled tasks that the vault's task manifest
  names, once per client.
- `autoresearch` — tunes the retriever's search settings against a golden query set
  and keeps a setting only when the score improves.

## What it needs

- The `brain` engine installed on the same machine. Install it with the
  `brainiac-manager` plugin, or follow the
  [install guide](https://github.com/Autopsias/brainiac/tree/main/docs/install).
- Claude Code or Cowork. The skills run a local command, so they do not work in a
  plain chat on claude.ai.

## What it runs, sends and fetches

- The skills run the local `brain` CLI and read files in your vault. `task-registrar`
  registers a scheduled task on your machine when you confirm it.
- The plugin sends nothing to any third party and has no MCP server.
- The output of `brain` becomes part of your conversation with Claude. The CLI applies
  a classification filter to that output before Claude sees it.

## License

Apache-2.0. Report a security problem through the process in
[SECURITY.md](https://github.com/Autopsias/brainiac/blob/main/SECURITY.md).

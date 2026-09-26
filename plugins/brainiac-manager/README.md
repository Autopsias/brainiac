# Brainiac — Manager

Install, update, check and remove [Brainiac](https://github.com/Autopsias/brainiac) on
your own machine. Brainiac is a local second brain kept as plain Markdown and YAML, with
a `brain` command-line engine that indexes it locally.

## What it does

- `brainiac-install` — installs the `brain` engine, checks that search works, and
  registers your vault.
- `brainiac-update` — refreshes the marketplace, the plugins and the engine.
- `brainiac-health` — one health readout for every registered vault. It only reads.
- `brainiac-cowork-setup` — stages the engine into a Cowork workspace.
- `brainiac-uninstall` — removes the scheduled job and the engine.

## What it needs

- macOS or Linux with Python 3, or Windows through `install.ps1`.
- Claude Code or Cowork. The skills run commands on your machine, so they do not work
  in a plain chat on claude.ai.

## What it runs, sends and fetches

Read this before you install. The skills change your machine:

- They download `install.sh` from `raw.githubusercontent.com/Autopsias/brainiac`.
- They install the `brainiac-cli` package from PyPI with `uv`, `pipx` or `pip --user`.
- On first semantic search, the engine downloads its embedding model (bge-m3-int8,
  about 563 MB) from Hugging Face.
- They register a daily maintenance job: a `launchd` job on macOS, a scheduled task on
  Windows.
- The plugin sends none of your vault content to any third party. The output of `brain`
  becomes part of your conversation with Claude.

`brainiac-uninstall` reverses the job and the engine install. It does not delete your
vault.

## License

Apache-2.0. Report a security problem through the process in
[SECURITY.md](https://github.com/Autopsias/brainiac/blob/main/SECURITY.md).

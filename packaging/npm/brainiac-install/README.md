# brainiac-install

Thin bootstrap for the [Brainiac](https://github.com/Autopsias/brainiac) `brain`
CLI engine. One command on any machine with Node.js >=18:

```
npx brainiac-install
```

## What it does

1. **Detects** an existing `brain` on PATH. If found, it skips reinstalling
   and points you at `brain update` instead.
2. **Installs the engine from PyPI** (`brainiac-cli[mcp]`), trying in order:
   `uv tool install` -> `pipx install` -> `python3 -m pip install --user`
   (Windows: `py -3` / `python`). First one available on PATH wins. If none
   of uv/pipx/Python >=3.9 are found, it prints the two official install
   commands (the uv standalone installer, and python.org) and exits — it
   never installs anything on your behalf beyond the `brainiac-cli` package
   itself.
3. **Verifies** the install (`brain --version`) and prints a PATH hint if the
   new shim directory isn't on PATH in the current shell yet.
4. **Initializes a vault** — prompts for a workspace directory (or accept one
   via `--vault <path>`) and runs `brain init --full --apply` against
   `<path>/vault`, so the bootstrap ends at a searchable vault, not just an
   installed CLI. Decline (or run non-interactively without `--vault`) and it
   prints the exact command to run yourself later.
5. **Optionally wires one client** with `--client <name>` (one of
   `claude-code`, `claude-desktop`, `codex`, `gemini`), delegating to
   `brain connect --client <name>`. Without the flag it just lists the
   available clients.

Add `--dry-run` to print the full command plan without executing anything —
no installs, no network access, no filesystem writes.

## What it never does

- **No telemetry.** This script makes zero network calls itself; every
  network access happens inside the tools it invokes (`uv`, `pipx`, `pip`),
  which you can inspect independently.
- **No shell strings.** Every subprocess is spawned from an explicit argv
  array (`spawnSync(..., { shell: false })`) against a binary this script
  resolved to an absolute path itself. The one narrow exception: on Windows,
  a resolved binary that turns out to be a `.cmd`/`.bat`/`.ps1` shim can't be
  exec'd without a shell, so *that one call* is shell-routed with fully
  static, hardcoded arguments — never user input, never a piped remote
  script.
- **No runtime dependencies.** Pure Node.js stdlib (`node:child_process`,
  `node:fs`, `node:os`, `node:path`, `node:readline`).
- **The engine is Python, distributed on PyPI** — this package is a thin
  wrapper that shells out to `uv`/`pipx`/`pip` to install it. It is not a
  reimplementation of `brain` in JavaScript, and it does not vendor or bundle
  the engine.
- **Not for Node-free machines.** If Node.js itself isn't available, this
  package can't run — use the `uvx`-based install path documented in the
  main repo's `docs/install/` instead.

## Flags

```
--dry-run           print the command plan; execute nothing
--vault <path>       workspace directory; vault created at <path>/vault
--no-vault           skip the vault-init step
--client <name>       claude-code | claude-desktop | codex | gemini
-h, --help           show help
```

## License

Apache-2.0

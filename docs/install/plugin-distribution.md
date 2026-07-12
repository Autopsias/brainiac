# Plugin-first distribution — brainiac-manager design note (SF-01)

Status: ACCEPTED 2026-07-04 · Contract for sessions s02–s04 · See also `docs/adr/0002-cowork-plugin-skill-delivery.md`.

## 0 · What was actually probed (not assumed)

Verified on this machine (`~/.claude/plugins/`) + official docs (code.claude.com/docs/en/plugins-reference, plugin-marketplaces), 2026-07-04:

- `/plugin marketplace add owner/repo` clones the marketplace repo as a **full git clone** at `~/.claude/plugins/marketplaces/<name>/` (`known_marketplaces.json` records source + installLocation + lastUpdated; verified `git remote -v` inside one).
- Installing a plugin **copies a snapshot** into `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`, pinned by version or commit SHA (`installed_plugins.json`). It is NOT a git repo.
- **Cache dirs are ephemeral by contract**: docs state `${CLAUDE_PLUGIN_ROOT}` "changes when the plugin updates… treat it as ephemeral and do not write state here"; orphaned version dirs are auto-deleted after ~7 days. Failed auto-updates have pruned/self-destructed checkouts in the wild (gh#69626, gh#40153). Stale `temp_git_*`/`temp_subdir_*.clone` litter observed locally in `cache/` confirms churn.
- **Auth path (private repo, fresh user):** docs, "Private repositories": *manual* installation and `/plugin marketplace update` use the user's **existing git credential helpers** — HTTPS via `gh auth login` / macOS Keychain / `git-credential-store`, or SSH with the key in `ssh-agent`. So a fresh user CAN add the private `Autopsias/brainiac` on host **without any in-URL PAT**, provided they have git credentials for the repo (they must — they cloned it). Only *background auto-update* needs a `GITHUB_TOKEN` env var; we don't rely on auto-update, so no token is ever required or stored. In-URL PATs are banned outright (stored plaintext in settings.json, gh#49694).
- **Fallback with zero ambient creds:** `/plugin marketplace add ~/brainiac` (local directory path) is a documented, fully supported source form. The documented host flow is therefore: `git clone` (authed once) → `/plugin marketplace add ~/brainiac` — the local-path add is the PRIMARY documented command, so the flow never depends on the marketplace-add code path re-authenticating.
- **Public flip (2026-07-04, evening) — ADR 0002 addendum:** `Autopsias/brainiac` went public. The earlier Cowork constraint (marketplace sync runs server-side unauthenticated, so it 404'd against the private repo — gh#61271, confirmed manually 2026-07-04) is now historical: syncing `Autopsias/brainiac` from Cowork's Customize → Plugins tab was re-verified live the same evening, showing all three plugins under a "brainiac" marketplace chip. Cowork's Plugins tab is now the **PRIMARY** skill-delivery path for Cowork, with Save-skill `.skill` zips as fallback. The GitHub-form add (`Autopsias/brainiac`, no clone required) is now the primary documented host command too, since no auth is needed against a public repo; local-path add remains a documented fallback.

## 1 · Decision: canonical checkout = `~/brainiac`, plugin = skill delivery only

The plugin checkout under `~/.claude/plugins/` is **never** the canonical code copy. It is pruned, re-cloned, version-switched, and self-destructs on failed updates — everything the install/update skills must survive.

- `~/brainiac` (a plain `git clone` of `Autopsias/brainiac`) is THE repo copy that `/brainiac-install`, `/brainiac-update`, and `/brainiac-cowork-setup` operate from: venv build, engine staging, skill zips, session prompts.
- The `brainiac-manager` plugin carries only the **lifecycle skills** (markdown). Each skill's first act is to **re-resolve the clone**: if `~/brainiac/.git` is missing, `git clone https://github.com/Autopsias/brainiac.git ~/brainiac`; else `git -C ~/brainiac pull --ff-only`. `/brainiac-update` must tolerate a missing/re-cloned checkout at any time.
- Durable mutable state (venv, registry, staged model) lives in `~/.brainiac/` — owned by us, untouched by Claude Code's plugin lifecycle.

## 2 · Decision: workspace registry `~/.brainiac/workspaces.json`

A typed store, written only through one helper (shipped in the repo, called by the skills).

```json
{
  "version": 1,
  "entries": [
    {
      "vault_path": "/Users/you/vault-repo",        // realpath-normalized
      "workspace_path": "/Users/you/vault-repo",    // realpath-normalized (Cowork: the staged workspace dir)
      "target": "host",                             // "host" | "cowork-vm"
      "host": "my-mac.local",                   // hostname at registration
      "arch": "arm64",
      "model_dir": "~/.brainiac/models/arctic-embed",
      "staged_at": "2026-07-04T10:00:00Z",
      "last_refreshed": "2026-07-04T10:00:00Z"
    }
  ]
}
```

- **Upsert key** = `(host, arch, target, realpath(vault_path), realpath(workspace_path))`. The helper realpath-normalizes both paths on every write, matches on the full key, updates in place on hit, appends on miss. No blind append; duplicates by key are impossible.
- **Migration rule:** on load, any pre-existing entry missing `host`/`arch`/`target` is stamped with the current hostname/arch and `target: "host"` if `workspace_path == vault_path` else `"cowork-vm"`, then rewritten. `version` bumps only on shape changes.
- **Concurrency:** the helper takes an **exclusive `flock`** on `~/.brainiac/workspaces.lock` around the whole read-modify-write, then writes `workspaces.json.tmp` + atomic `rename`. Lock + rename, not rename alone — concurrent `/brainiac-update` and `/brainiac-cowork-setup` must not lose an entry.
- **Membership:** the HOST vault **is** a registry member (`target: "host"`) — s02 registers it at `/brainiac-install`. The update loop dispatches per entry by `target`: host entries get engine/venv/prompt/skills refresh **only**; the Cowork stager runs **only** over `target: "cowork-vm"` entries. (This reconciles "registry of Cowork workspaces" with s02 recording the host vault.)

## 3 · Decision: update contract for `/brainiac-update`

Order: re-resolve clone → compat check → refresh. **Refreshes:**

1. `~/brainiac` clone (`git pull --ff-only`; re-clone if missing).
2. `~/.brainiac/venv` engine (reinstall from the clone when the engine version changed).
3. Per registry entry, by target:
   - all: `.brain/engine` staged copy, `AGENTS.md`, session prompt, skill artifacts (`.brain/skills/*.skill` zips for cowork-vm), then republish the snapshot (`brain sync --publish`).
   - host: nightly runner plist body **iff changed** (bootout + bootstrap the per-vault label, §5).
4. `last_refreshed` stamped per entry (through the flock helper).

**Never-touch list:** `vault/brain/` notes, `vault/raw/` (immutable), the audit chain, the **signing key** (PRESERVE-ONLY: install/update detect an existing audit key and never regenerate or rotate it — key rotation is a separate human-run procedure), the owner overlay (`vault/overlay/`), and the live `index.sqlite` beyond what `brain sync` itself does.

**Skill↔code compatibility gate:** the plugin ships `COMPAT_VERSION` in each lifecycle skill's frontmatter/body; the clone ships the same marker in `~/brainiac/dist/COMPAT` (written by the packager). `/brainiac-update` reads both **after** the `git pull`: if skill > code, the pull didn't land the matching code — abort with "re-run `/plugin marketplace update` + `git -C ~/brainiac pull`"; if skill < code, the plugin is stale — refresh plugins first, then re-run. Same-major proceeds. This closes the stale-artifact class on the update path itself.

## 4 · Decision: plugin naming/layout — new `plugins/brainiac-manager`

A separate plugin alongside `brainiac-kernel` / `brainiac-extras` in the same `brainiac`:

```
plugins/
├── brainiac-kernel/      # daily-use skills (unchanged)
├── brainiac-extras/      # optional admin skills (unchanged)
└── brainiac-manager/      # host-mutating lifecycle skills
    └── skills/
        ├── brainiac-install/SKILL.md
        ├── brainiac-update/SKILL.md
        ├── brainiac-cowork-setup/SKILL.md
        └── brainiac-uninstall/SKILL.md
```

Rationale: lifecycle skills mutate the host (launchd, venv, registry) — a user who only wants daily-use skills must be able to install the kernel without ever pulling host-mutating commands, and vice versa. Leave `version` **unset** in `plugin.json` so the commit SHA drives updates while iterating (docs: version-field pinning would silently freeze users).

**Documented host flow** (public repo, no clone needed first):

```
claude> /plugin marketplace add Autopsias/brainiac                # GitHub form — no clone, no creds
claude> /plugin install brainiac-manager@brainiac
claude> /brainiac-install <vault-path>
```

`/brainiac-install` still clones `~/brainiac` itself as the canonical code
checkout (§1) — the plugin only carries the lifecycle skills, never the
canonical code copy. `/plugin marketplace add ~/brainiac` (local-path add,
after `git clone https://github.com/Autopsias/brainiac.git ~/brainiac`)
remains a documented fallback for offline or credential-restricted
environments.

## 5 · Nightly model: single shared label (per-vault labels: decided, NOT implemented)

**Reality check (2026-07-05):** the per-vault-label decision below was never
implemented. The shipped code (`scripts/install-brief-mac.sh`,
`register_tasks.py`) registers ONE shared label,
`com.profile-a-brain.daily-brief` (Windows: `brain-daily-brief`), whose plist
carries a single `BRAIN_VAULT`. A second vault's `brain init --apply` therefore
**repoints** the nightly drain (observed live during the 2026-07-04
reference-deployment install). The lifecycle skills now describe the shared label and
gate any repoint behind an explicit user OK. Implement the per-vault design
below only if/when a host genuinely runs multiple active vaults:

- `vault_id = first 8 hex of sha256(realpath(vault_path))`; label = `com.brainiac.nightly.<vault_id>`; plist at `~/Library/LaunchAgents/com.brainiac.nightly.<vault_id>.plist`, program = the venv `brain maintain` with the vault path baked in.
- One label per `target: "host"` registry entry; `cowork-vm` entries never get one (the host's nightly for the backing vault is the drain floor).
- On migration, bootout the shared label and bootstrap the derived ones.

## 6 · Teardown: `/brainiac-uninstall`

Removal path for everything the lifecycle skills persist (else the nightly fires forever against a pruned checkout):

1. The shared `com.profile-a-brain.daily-brief` task: repoint it to a surviving vault if other host entries remain, or bootout + delete its plist when removing the last one.
2. Remove `~/.brainiac/venv` (and optionally `models/` on `--purge-models`).
3. Drop the registry entries it owns (flock helper); delete `~/.brainiac` entirely if the registry is then empty.
4. NEVER touches: any vault, `raw/`, notes, overlay, audit chain, **signing key** — uninstalling the framework must not damage the data. Prints what it left behind (vaults, key location) so the human can archive deliberately.
5. `~/brainiac` clone and the plugin itself are left for the user (`rm -rf ~/brainiac`, `/plugin uninstall brainiac-manager`) — the skill reports both commands but does not run destructive deletes on pre-existing directories.

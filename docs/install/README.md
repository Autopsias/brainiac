# Install Brainiac — choose your platform

Installing Brainiac puts three things in place: the **`brain` CLI engine**
(search/index/audit), your **vault** (plain Markdown notes on your own disk),
and a set of **skills** the AI harness uses to talk to it.

**The two-layer model, in one paragraph:** skills install via a plugin
marketplace wherever a plugin system exists (Claude Code, and Claude
Desktop's Plugins tab). The **engine and your vault always live on your own
computer** — the host. A sandboxed surface like Cowork (Claude Desktop's
disposable Linux VM) can install skills, but it can never host the engine or
own the vault, because nothing inside it survives past the session. Instead,
the host stages a read-only copy for Cowork to read from and a draft inbox
for it to write to. This is why "why can't the plugin install everything
inside Cowork?" has one answer: **Cowork has no persistent disk** — the host
is the only place the engine and vault can live.

## Pick your platform

| I use... | Follow |
|---|---|
| Claude Code — terminal, or Claude Desktop's **Code tab** | [Path A](#path-a--claude-code-host) |
| Claude Desktop with **Cowork** | [Path A](#path-a--claude-code-host), then [Path B](#path-b--cowork-claude-desktop) |
| Codex | [Path C](#path-c--codex) |
| Gemini CLI | [Path D](#path-d--gemini-cli) |

**Cowork always requires Path A first.** The engine and the vault live on the
host; there is no Cowork-only install. If you only use Cowork, you (or
someone with terminal access) still has to run Path A once on a real machine
before Path B can do anything.

---

## Path A — Claude Code (host)

Works whether you're in a terminal (`claude`) or Claude Desktop's **Code
tab** — both are Claude Code under the hood. The only difference is *how you
add the marketplace* in step 2: the `/plugin` slash command exists **only in
the terminal CLI** — in the Desktop app it returns "/plugin isn't available
in this environment" ([anthropics/claude-code#42142](https://github.com/anthropics/claude-code/issues/42142)).
Desktop users add it through the **Plugins tab UI** instead; the installed
plugins are shared (`~/.claude/plugins/`), so either route serves both
surfaces.

1. Open Claude Code.
2. Add the plugin marketplace (public repo, no clone, no credentials):

   **Terminal CLI:**

   ```
   /plugin marketplace add Autopsias/brainiac
   ```

   **Claude Desktop (Code tab or Cowork):** Customize → **Plugins** → add
   marketplace → enter `Autopsias/brainiac`. All three plugins appear under
   a "brainiac" marketplace chip (verified live 2026-07-04). Install from
   the same UI, then skip step 3.

3. Install the lifecycle plugin:

   ```
   /plugin install brainiac-manager@profile-a-marketplace
   ```

4. Run the installer, pointing at your vault:

   ```
   /brainiac-install <path-to-your-vault>
   ```

   What this does: clones `~/brainiac` (the canonical code checkout — the
   plugin itself only carries the lifecycle skills, not the code), runs
   `./install.sh`, verifies `brain search` works, registers the nightly
   maintenance task (`com.profile-a-brain.daily-brief`), provisions the audit
   signing key (idempotent — never rotates an existing key), records the
   vault in your workspace registry, and prints an explicit ✅/❌ report for
   every step.

   **About the path you give:** treat it as the **workspace** folder — the
   vault always lives at `<path>/vault` (the installer creates it; this
   matches what Cowork setup and the VM session prompt expect, so the same
   folder works everywhere). If you already have a vault, give the folder
   *containing* it, laid out as `<workspace>/vault`. `brain init --full
   --apply` (which this step runs) scaffolds the **overlay** layer
   (voice/brand/keywords/people) into the vault. It does not by itself
   populate `vault/brain/` and `vault/raw/` with sample notes — for a brand
   new vault, either point Path A at the bundled sample vault in `~/brainiac`
   first to see the shape, or start writing your own notes following
   `AGENTS.md`'s note shape.

   At the end it asks one question: *"Do you also use Claude Desktop's
   Cowork and want this brain available there?"* Say yes to run
   `/brainiac-cowork-setup` for you (this is Path B) — say no and you're
   done.

5. **Optional — daily-use skills:**

   ```
   /plugin install profile-a-kernel@profile-a-marketplace
   ```

   Adds the daily-use kernel skills (search/curate/capture/promote). Add
   `profile-a-extras@profile-a-marketplace` too if you want the optional
   admin skills.

**Fallback (offline / credential-restricted):**
`git clone https://github.com/Autopsias/brainiac.git ~/brainiac` then
`/plugin marketplace add ~/brainiac` (local-path add) — no ambient network
auth needed.

### Updating

```
brain doctor        # read-only: "am I current?" across every surface (v0.10.0+)
/brainiac-update    # runs the whole update, then self-verifies
```

`brain doctor` reports each surface's version and what's stale — engine venv,
CLI plugins, staged Cowork workspaces, marketplace cache, Desktop/Cowork store —
with the exact fix command per row, and mutates nothing.

`/brainiac-update` **self-executes** the update rather than printing a checklist:
marketplace refresh → downgrade-safe CLI-plugin reinstall → engine venv
reinstall → re-stage of every registered Cowork workspace → nightly-task refresh
if it changed → a final `brain doctor` verify, then a before→after version table
and one pass/fail. It auto-handles the reconciliation downgrade (installed newer
than the marketplace) with a clean reinstall. `brain update --dry-run` previews
every decision without changing anything. Your notes, `raw/`, audit chain, and
runtime state are never touched.

### Removing

```
/brainiac-uninstall
```

Removes this host's nightly maintenance task(s), the engine venv, and this
host's registry entries.

---

## Path B — Cowork (Claude Desktop)

**Requires Path A done first** — this stages a copy of what Path A already
installed; it does not install the engine itself.

1. On the **host**, in Claude Code, run:

   ```
   /brainiac-cowork-setup
   ```

   It asks one question: *"Which folder is your Cowork workspace?"* — this
   is the folder Cowork will mount; the vault lives at `<workspace>/vault`
   inside it.

   - Point it at **your real vault's parent folder** if you want Cowork
     reading and drafting into the *same brain* you use on the host. This is
     what most people want.
   - Point it at a **new empty folder** only if you deliberately want a
     separate, empty brain for Cowork — it will not see any of your existing
     notes.

   The skill stages the embedding model, installs a zero-install pure-Python
   runtime + `brain` shim into the workspace (no Docker, no compilers),
   registers the vault's nightly host maintenance task, and records the
   workspace in the registry.

2. In **Claude Desktop → Cowork**, add that folder as the project folder.

3. **Customize → Plugins → add marketplace `Autopsias/brainiac`** → install
   **Profile A — Kernel Skills** (`profile-a-extras` optional). **Do not
   install Brainiac Manager in Cowork** — it's host-only lifecycle tooling
   (install/update/uninstall/cowork-setup) that mutates things Cowork can't
   reach; it simply can't do its job from inside the VM.

   Fallback if the Plugins tab isn't reachable (offline session, blocked
   marketplace fetch): upload the `.skill` zips staged at
   `<workspace>/vault/.brain/skills/` via Cowork's Save-skill upload flow
   (kernel first, extras optional).

4. Paste the session prompt — printed by step 1, and also saved at
   `<workspace>/vault/.brain/routines/cowork-session-prompt.md` — into the
   Cowork project's custom instructions.

5. Test it: in the Cowork session, ask it to run `brain status` or search
   the brain.

**What Cowork can and can't do:** it's a **read + draft** surface — it reads
the host's published read-only snapshot and can stage draft notes/sources via
`brain draft-capture`. It never signs, indexes, or commits. The host drains
and commits Cowork's drafts (on the next `brain sync`, or via the nightly
maintenance task) and republishes the snapshot so the note becomes visible
back in Cowork.

---

## Path C — Codex

Codex has no plugin system, so this is a clone + guided-prompt flow, not a
plugin install.

```
git clone https://github.com/Autopsias/brainiac.git ~/brainiac
cd ~/brainiac && ./install.sh
```

Skills auto-load from `.agents/skills/` on clone — nothing else to wire up.
For the guided fallback prompt (useful if you want Codex to drive the whole
install interactively), see the Codex appendix in
[`ai-install.md`](./ai-install.md).

---

## Path D — Gemini CLI

Also a clone flow — no plugin system, no skill system.

```
git clone https://github.com/Autopsias/brainiac.git ~/brainiac
cd ~/brainiac && ./install.sh
```

Gemini CLI reads `AGENTS.md` via its `.gemini/` `contextFileName` setting —
that file's self-discovery paragraph is the whole interface; there's nothing
skill-shaped to install. Full per-harness wiring table:
[`../harness-wiring.md`](../harness-wiring.md).

---

## FAQ

**Why do I need Claude Code for a Cowork install?**
Cowork is an ephemeral, sandboxed Linux VM — nothing installed inside it
survives, and it can't hold the audit signing key or write to your real
vault. The engine and the vault must live on a persistent machine (the
host), and Claude Code is how you drive that host install.

**What's the difference between the three plugins?**
`brainiac-manager` is host-only lifecycle tooling (install/update/uninstall/
cowork-setup) — it mutates your machine, so it never belongs in Cowork.
`profile-a-kernel` is the daily-use skill set (search, curate, capture,
promote) — install it wherever you actually work, including Cowork.
`profile-a-extras` is optional admin/maintenance skills on top of the
kernel.

**How do I update?**
Run `brain doctor` (read-only) to see what's stale, then `/brainiac-update` on
the host to become current. `/brainiac-update` self-executes the marketplace
refresh, the downgrade-safe plugin reinstall, the engine reinstall, and a
re-stage of every registered Cowork workspace (no separate Cowork update step),
then verifies with `brain doctor` and prints a before→after table. `brain
update --dry-run` previews it without changing anything.

**How do I uninstall?**
`/brainiac-uninstall` on the host. It's idempotent and safe to re-run.

**Do I need a plugin at all?**
No — `git clone` + `./install.sh` (the [`README.md`](../../README.md)
one-command path) works everywhere and is what the plugin runs under the
hood. The plugin exists to save you the manual clone/PATH/registration
steps, not to gate the install behind it.

---

Already set up and starting a **second** vault/project on the same install?
See [`second-vault.md`](./second-vault.md) — `$BRAIN_VAULT`, re-running
`brain init --full`, the per-vault overlay, and the shared-index /
fixed-scheduled-task gotchas. For the durable "what runs where and why"
picture (not step-by-step): [`new-owner.md`](./new-owner.md).

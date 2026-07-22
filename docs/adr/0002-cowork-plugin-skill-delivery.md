# Cowork skill delivery stays zip-first; plugin-tab install is host-only + public-gated

The Brainiac plugin-first distribution plan
(`_plans/brainiac-plugin-first-distribution-2026-07-04/`) proposed that a Cowork
VM workspace get its kernel skills by installing the `brainiac-manager` plugin
from Cowork's **Customize → Plugins** tab, demoting the Save-skill `.skill` zip
uploads to a fallback. Pre-flight research (`/plan-harden`, 2026-07-04) found this
does not work from our repo: **Cowork's "Add marketplace" sync runs server-side
from an unauthenticated GitHub session**, so a marketplace whose plugin
`source.repo` is the **private** `Autopsias/brainiac` fails validation
("Repository not found… make sure the repository is public"). `gh auth`,
`GITHUB_TOKEN`, ssh-agent, and Keychain are all out of scope for that server-side
fetch (evidence: anthropics/claude-code#61271). The host Claude Code CLI is a
different path and may support a private add via an authed local git checkout —
but the Cowork VM leg cannot.

**Decision.** For the **Cowork VM**, the Save-skill `.skill` zips
(`.brain/skills/*.skill`, uploaded via the Save-skill flow) remain the **PRIMARY**
skill-delivery path. The "install the Brainiac plugin from the Cowork Plugins
tab" instruction is a **secondary** option, documented with an explicit *"once the
marketplace repo is public"* qualifier. The plugin-first flow
(`/plugin marketplace add` → `/brainiac-install` → `/brainiac-update`) is the
primary path for **host** Claude Code / Codex only. No install command may embed
an in-URL personal access token — Claude Code's private-repo add stores it
plaintext in `~/.claude/settings.json` (anthropics/claude-code#49694), which
contradicts the project's egress/at-rest posture; use a git credential helper or
a public marketplace repo instead.

**Confirmed (s07, manual check 2026-07-04).** The operator added `Autopsias/brainiac`
in Claude Desktop's Cowork Customize → Plugins tab and it failed with
"Marketplace sync failed. Check the repository URL and try again." — the
private-repo sync failure predicted above, reproduced firsthand. No further
action needed on this leg: Save-skill `.skill` zips stay PRIMARY for Cowork, and
the Plugins-tab path stays documented behind "once the marketplace repo is
public." No install command in the shipped docs embeds an in-URL PAT
(gh#49694) — confirmed on `docs/install/plugin-distribution.md` and
`docs/install/cowork.md`.

**Consequences.**
- s04 (`/brainiac-cowork-setup`) and s05 (docs rewrite) keep the zips primary for
  Cowork rather than demoting them; s07 reframes from "verify whether private
  works in Cowork" to "confirm the expected NO and record the public-repo upgrade
  path" — confirmed above.
- **Upgrade path (future):** publish a **public** marketplace repo with the
  plugin vendored in via relative paths (or a public mirror of the plugin
  subtree). Only then does the Cowork Plugins-tab install become primary; revisit
  this ADR at that point.
- Because Claude-Code-managed plugin checkouts are pruned/self-destruct on failed
  auto-update (anthropics/claude-code#69626, #40153), the canonical code copy
  stays a separate `~/brainiac` clone regardless of this decision — the plugin
  checkout is never treated as durable canonical state.

## Addendum 2026-07-04 (evening) — repo went public; upgrade path triggered

`Autopsias/brainiac` was made **public** 2026-07-04 (evening). Both
distribution legs were re-verified live against the public repo:

- **Host Claude Code:** `claude plugin marketplace add Autopsias/brainiac`
  (GitHub form, no clone, no credentials) + `plugin install
  brainiac-manager@profile-a-marketplace` + `profile-a-kernel` +
  `marketplace update` — all succeeded in a sandboxed-HOME test.
- **Cowork Plugins tab:** syncing `Autopsias/brainiac` now works — the
  directory shows all three plugins ("Brainiac Manager — host lifecycle",
  "Brainiac — Kernel Skills", "Brainiac — Extras") under a "brainiac"
  marketplace chip (screenshot evidence, 2026-07-04 20:13). The earlier
  "Marketplace sync failed" was the private-repo state (§ above), now
  resolved by the public flip alone — no code or Cowork-side change was
  needed.

**This is exactly the condition the Consequences section named as the
trigger:** *"Only then does the Cowork Plugins-tab install become primary;
revisit this ADR at that point."* That condition is now met.

**Decision (supersedes the PRIMARY/secondary framing above for Cowork).**
For the **Cowork VM**, the Plugins-tab install
(`brainiac-manager` from Customize → Plugins) is now the **PRIMARY**
skill-delivery path — it needs no zip upload and stays current via
`/plugin marketplace update`. The Save-skill `.skill` zip upload
(`.brain/skills/*.skill`) becomes the **fallback**, for cases where the
Plugins tab isn't usable (offline session, enterprise network blocking the
GitHub marketplace fetch, or a workspace that predates this addendum and
hasn't re-synced). The host-only framing is unchanged: the plugin-first flow
(`/plugin marketplace add` → `/brainiac-install` → `/brainiac-update`)
remains the host Claude Code / Codex path, and the GitHub-form add
(`Autopsias/brainiac`, not the local-path fallback) is now the primary
documented host command too, since no auth is required against a public
repo.

**Consequences of the addendum:**
- `docs/install/cowork.md`, `docs/install/plugin-distribution.md`,
  `docs/install/ai-install.md`, and the `brainiac-cowork-setup` /
  `brainiac-install` skills are updated to lead with the GitHub-form add and
  (for Cowork) the Plugins tab, with the local-path add / Save-skill zips
  kept as documented fallbacks.
- No further "once public" gate language should remain anywhere in the
  shipped docs — this addendum is the revisit the ADR called for.

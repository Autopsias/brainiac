# ADR-0006 — Distribution naming: marketplace/plugins renamed to `brainiac`; `brainiac` itself unavailable on PyPI/npm

- **Status:** Accepted 2026-07-11 (S01 checkpoint sign-off).
- **Cites:** ADR-0002 (plugin-first distribution), ADR-0004 (versioning
  release, plugin lockstep), `docs/install/plugin-distribution.md`,
  `_evidence/install-plan/names.md`.

## Context

The internal project slug is `profile-a-brain`; the public brand is
**Brainiac**. Before this ADR, the Claude Code plugin marketplace and its two
daily-use plugins still carried the internal name:
`.claude-plugin/marketplace.json` `name: "profile-a-marketplace"`, and
`plugins/profile-a-kernel` / `plugins/profile-a-extras`. A user who had just
added `Autopsias/brainiac` then had to type `@profile-a-marketplace` in the
very next command — a name that appears from nowhere and undermines trust in
the install flow. `brainiac-manager` (the lifecycle plugin) already used the
public brand.

Separately, this plan also needs a PyPI package name (for a future
`pip install brainiac-cli`) and an npm package name (for a future
`npm install -g brainiac-install`, the zero-install runtime shim). Both were
checked before naming anything, per this project's standing rule to verify a
capability/availability claim rather than assume it.

## Ruling 1 — Rename the marketplace and the two daily-use plugins to the public brand

**Decision.**

| Old | New |
|---|---|
| `profile-a-marketplace` (`.claude-plugin/marketplace.json` `name`) | `brainiac` |
| `plugins/profile-a-kernel` | `plugins/brainiac-kernel` |
| `plugins/profile-a-extras` | `plugins/brainiac-extras` |
| `plugins/brainiac-manager` | unchanged |

Evidence that a marketplace `name` differing from the repo slug is
supported: `docs/install/plugin-distribution.md` already documents (and this
project has run live) `Autopsias/brainiac` as the **repo** with
`profile-a-marketplace` as the **marketplace name** — i.e. the two were
already decoupled before this rename, just pointed at the internal name
instead of the brand. `/plugin marketplace add Autopsias/brainiac` resolves
by GitHub `owner/repo`, not by the marketplace's declared `name` — the name
is only used for the `@<marketplace>` suffix in subsequent
`/plugin install <plugin>@<marketplace>` calls. No evidence of a constraint
tying the two together was found in the official plugin-marketplaces docs
consulted for ADR-0002; if that assumption turns out wrong in practice, the
fallback is trivial (marketplace `name` can be reverted independently of the
repo slug with no data-model consequence — it's a JSON field, not a URL).

**Not renamed:** the repo/project slug `profile-a-brain` (launchd label
`com.profile-a-brain.daily-brief`, per-user app-data dir
`~/Library/Application Support/profile-a-brain`, PyPI/npm distribution names
below) — that is a separate, larger rename with its own blast radius
(running installs' launchd plists, app-data paths) and is explicitly out of
scope for this session.

## Ruling 2 — PyPI/npm: `brainiac` itself is taken; approved fallback names

**Verified 2026-07-11** (see `_evidence/install-plan/names.md` for the
runnable commands):

| Registry | `brainiac` | Approved fallback |
|---|---|---|
| PyPI | HTTP 200 (occupied) | **`brainiac-cli`** (HTTP 404 — free) |
| npm | HTTP 200 (occupied) | **`brainiac-install`** (HTTP 404 — free) |

**The split (hard-to-reverse public naming, so spelled out explicitly):**

- **Distribution name** (what you `pip install` / `npm install -g`):
  `brainiac-cli` (PyPI), `brainiac-install` (npm). These are the names that
  had to dodge the PyPI/npm squat on bare `brainiac`.
- **Python import package:** stays **`brain`** (`import brain`) —
  unaffected by the distribution name; `pip install brainiac-cli` still
  gives you `import brain`, same as today's `pip install profile-a-brain`
  does.
- **Console command:** stays **`brain`** — the CLI entry point users type is
  unaffected by either rename.

**Not applied in S01.** This ruling *records* the approved names for the
session(s) that actually cut the PyPI/npm distribution (a `pyproject.toml`
`[project].name` change + a release). Recorded here, in
`_evidence/install-plan/names.md`, so that later session inherits a decision
instead of re-researching it.

## Consequences

- Every quick-start command now reads consistently: add `Autopsias/brainiac`
  → `/plugin install brainiac-manager@brainiac` → `/brainiac-install`.
- Existing installs registered under the old names are not automatically
  migrated by a marketplace/plugin update alone — `brain doctor` detects the
  stale registration and `/brainiac-update` self-migrates
  (install-new-before-remove-old, recorded-state rollback on failure) — see
  NAM-03 / the updated `brainiac-update` skill.
- A future PyPI/npm cutover will ship under `brainiac-cli` / `brainiac-install`,
  never bare `brainiac` on either registry, while `brain` remains the
  user-facing command and import name unchanged.

# ADR-0004 — Versioning + release architecture

- **Status:** Accepted (plan `_plans/brainiac-v09-release-2026-07-05/`, session s01;
  Ruling 5 pre-decided by the owner 2026-07-05 and confirmed at the s01 checkpoint)
- **Date:** 2026-07-05
- **Cites:** ADR-0001 (clean-room export; public releases are squashed export
  commits, push URL deliberately `DISABLED://cleanroom-export-only-see-plan`),
  ADR-0002 (zip-first Cowork delivery, three-way packaging sync), ADR-0003
  (esp. Ruling f snapshot compatibility + Amendments), `pyproject.toml`,
  `tools/package_clients.py`, `.claude-plugin/marketplace.json`, the three
  `plugins/*/.claude-plugin/plugin.json`, `plugins/brainiac-manager/skills/
  brainiac-{install,update,uninstall}/SKILL.md`, `tools/workspace_registry.py`,
  `src/brain/index.py` (`SCHEMA_VERSION`), `src/brain/snapshot.py`
  (`schema_version`), `CHANGELOG.md`.

## Context — the current version landscape, stated as fact

As of 2026-07-05 the repo carries **two divergent version lines**:

- **Engine line (0.3.0):** `pyproject.toml [project] version = "0.3.0"`;
  `tools/package_clients.py` derives `dist/COMPAT` (`0.3.0`) and stamps
  `<!-- SKILL_VERSION: 0.3.0 -->` into every distributed SKILL.md copy from it
  (`read_source_version()` regexes pyproject); `brain --version` prints the
  installed package version via `importlib.metadata`.
- **Plugin line (1.x):** the three plugin manifests are hand-maintained —
  `brainiac-manager` **1.0.0**, `profile-a-kernel` **1.1.0**,
  `profile-a-extras` **1.1.0**. `package_clients.py --validate-only` checks
  only that each plugin.json *has* a `version`; it never propagates or
  compares it against pyproject.
- **CHANGELOG** says versions "track pyproject.toml" but maps public releases
  to opaque squashed-export tags (`v1`, `v2`, …) per ADR-0001 — i.e. internal
  0.3.0 would ship as "public tag v1".
- The git remote is deliberately disabled for push
  (`DISABLED://cleanroom-export-only-see-plan`); a release physically leaves
  this machine only via the ADR-0001 clean-room export.

This ADR rules on eight things before any release tooling is built. Sessions
s06+ implement against these rulings; deviation amends this ADR first.

---

## Ruling 1 — Version SSOT: `pyproject.toml [project] version`, everything else derived or validated-lockstep at package time

**Decision.** The single source of truth for Brainiac's version is
`pyproject.toml [project] version`. Every other version surface is either
**derived from it at package time** or **validated against it as a hard
packaging gate**:

| Surface | Relationship to SSOT | Mechanism |
|---|---|---|
| `brain --version` / `brain.__version__` | derived at install | `importlib.metadata` (already true) |
| `dist/COMPAT` | derived at package | `package_clients.write_compat_marker()` (already true) |
| `SKILL_VERSION` stamps in distributed skills | derived at package | `package_clients.stamp_skill_version()` (already true) |
| `plugins/*/.claude-plugin/plugin.json` `version` | **propagated + validated lockstep** (NEW — s06) | packager writes pyproject's version into all three plugin.json; `--validate-only` fails on any mismatch |
| CHANGELOG headings | validated at release | release procedure (Ruling 7) refuses to tag a version with no matching CHANGELOG section |
| git tags | derived at release | tag = `v<pyproject version>` (Ruling 7) |

A version bump is therefore **one edit** (pyproject) followed by one packager
run; nothing version-shaped is ever hand-maintained in two places.

**Rejected:** a separate `VERSION` file (a second file for a value setuptools
already requires in pyproject); deriving pyproject from plugin.json (backwards
— the engine is the substrate, plugins are packaging of it); hand-maintained
parallel versions "kept in sync by discipline" (the current state, and it has
already drifted — see Context).

## Ruling 2 — Semver semantics for a substrate

**Decision.** Brainiac follows semver where the "API" is the contract an
agent, a skill, or on-disk state depends on. Pre-1.0, the MAJOR column maps to
the MINOR digit (0.x → 0.(x+1)); the classification below is what matters.

**MAJOR (breaking):**

- Breaking the **CLI contract**: removing/renaming a verb or flag, changing
  the meaning or shape of machine-read output (JSON reports, status blocks),
  removing a verb from `VM_ALLOWED`.
- A **non-additive frontmatter schema change**: removing a key, changing a
  key's type/semantics, making an optional key required. (Per ADR-0003
  Ruling 2 and Ruling 6 below, frontmatter changes are additive-only — a
  MAJOR here should essentially never happen.)
- Breaking the **snapshot read contract** (ADR-0003 Ruling f "major skew"):
  a change an old VM reader cannot degrade around and must refuse.
- Changing the audit-chain record format such that old chains fail
  verification, or any change to the never-touch contract (Ruling 4).

**MINOR (additive):**

- New verbs, new flags, new capabilities; new `VM_ALLOWED` entries (trust
  widening still needs its own ADR — see Ruling 4's invariants).
- Additive frontmatter keys (the ADR-0003 bitemporal pattern); additive
  columns in the index or snapshot (`SCHEMA_VERSION` bump that a rebuild
  absorbs; snapshot "minor skew" the VM degrades around).
- New skills joining the packaging sync; new maintain-branch folds (inside
  the existing single OS task only).

**PATCH:** bug fixes, performance, docs, prompt/skill wording that changes no
contract, dependency bumps that change no behavior.

**Directional fail-fast (hardening, dual-model review 2026-07-05).** ADR-0003
Ruling f covers *new CLI meets old state* (VM degrades/refuses; host
auto-republishes). The reverse — an **old binary meeting newer on-disk
state** — must also fail deterministically, never silently:

- **Old CLI vs newer snapshot** `schema_version`: refuse with an explicit
  "snapshot is newer than this CLI — update the engine" error (the mirror of
  Ruling f's refuse branch).
- **Old CLI vs newer index** `SCHEMA_VERSION`: today `is_current()` mismatch
  forces a rebuild in *either* direction — a stale binary would silently
  rebuild a newer index down to its own older schema and smoke tests would
  never see it. Rule: when the on-disk `schema_version` meta is **greater**
  than the binary's `SCHEMA_VERSION`, the binary refuses to rebuild and
  reports "index was built by a newer brain — update the engine (or run
  `brain sync --rebuild` to force a downgrade)". Mismatch-lower keeps the
  existing silent-rebuild behavior (that is the upgrade path).
- `brain status` and `brain --version` surface both comparisons cheaply
  (Ruling 8), so the skew is visible before it bites.

**Rejected:** CalVer (updates need "is this compatible", not "when was
this"); treating index `SCHEMA_VERSION` as a public semver axis (it is an
internal derived-artifact marker; rebuilds absorb it — Ruling 6); "everything
is 1.0 and every release is MINOR" (hides exactly the breaking changes the
update skill must detect).

## Ruling 3 — CHANGELOG discipline: keep-a-changelog, Unreleased flow

**Decision.** `CHANGELOG.md` follows keep-a-changelog:

- One `## [Unreleased]` section at top; every merged change that is
  user-visible or contract-visible lands there **in the same commit/session
  that lands the change** (the parity build already works this way).
- At release (Ruling 7), Unreleased is renamed to `## [X.Y.Z] — YYYY-MM-DD`
  and a fresh empty Unreleased heading is opened. The release procedure
  refuses to proceed if Unreleased is empty or the target version already has
  a section.
- **What earns an entry:** anything a user, an installed skill, or the update
  path can observe — new/changed/removed verbs and flags, schema keys,
  packaging changes, trust-surface changes, dependency additions, migration
  requirements. **What does not:** internal refactors with no observable
  change, test-only changes, plan/_evidence artifacts.
- The header's mapping sentence is updated per Ruling 7: public tags are
  `v<semver>` (e.g. `v0.9.0`), superseding the opaque `v1, v2, …` counter in
  ADR-0001's prose. ADR-0001's *mechanism* (squashed export commits layered
  on the public branch, never force-replace) is unchanged; only the tag
  naming is refined so SECURITY.md and the CHANGELOG speak one language.

**Rejected:** generated changelogs from commit messages (local history is
never published per ADR-0001, and commit messages carry internal archaeology
the clean-room export exists to exclude); per-plugin changelogs (one product,
one version line per Ruling 5, one changelog).

## Ruling 4 — Update path: `/brainiac-update` ordering + the never-touch contract

**Decision.** `/brainiac-update` performs, in this order (each step gated on
the previous):

1. **Version-skew detection** — read the running skill's stamped
   `SKILL_VERSION`, the checkout's `dist/COMPAT`, and the installed
   `brain --version`; classify (fresh-dev / skill-newer / skill-older /
   in-sync) exactly as the SKILL.md's Step-2 table already does, and print
   the verdict before mutating anything.
2. **Engine venv reinstall** — `pip install` the checkout into
   `~/.brainiac/venv`; capture `OLD_VERSION -> NEW_VERSION`.
3. **SCHEMA_VERSION-conditional index rebuild** — the first post-update
   `brain sync` rebuilds iff the index's stored `schema_version` mismatches
   *lower* (Ruling 2's fail-fast governs the higher direction). No
   unconditional rebuild: on a no-schema-change update this is a no-op.
4. **Snapshot republish** — host detects snapshot-vs-CLI skew and
   republishes (ADR-0003 Ruling f host branch), so VM readers are never left
   on a stale schema after an engine bump.
5. **Skill re-stage** — re-stage every registered Cowork workspace via
   `tools/workspace_registry.py` entries (flock helper, never hand-edited
   JSON); refresh the nightly task **only if its bytes changed**, via
   unload/load of the *existing* label.
6. **User report** — per-workspace ✅/❌ plus the version transition, schema
   action taken (rebuilt / current / refused-newer), and snapshot republish
   status.

**Never-touch contract (hard, restated + extended).** An update never touches:
`vault/brain/` notes; `vault/raw/` (immutable, incl. `raw/originals/`); the
audit chain and WAL; the **signing key** (rotation is a separate human
procedure); the owner overlay (`vault/overlay/`); anything in `index.sqlite`
beyond what `brain sync` itself does; **and every state surface the parity
build created**: `.brain/memory/` (handoff/hot/lessons/archive, incl.
`recommendations-open.jsonl` + `recommendations-log.md`),
`.brain/maintain-state.json` + `.brain/maintain.lock`, `.brain/brief/`,
`.brain/graph/`, `vault/inbox/` + `inbox/_quarantine/`, the capture-inbox,
and all vault content generally. The stager may only ever recreate its own
`.brain/engine/` staging dir.

**Invariants an update may never breach:** the single-OS-task lock
(`routines/manifest.json` `locked_counts` host 1 / VM 0 — an update never
*adds* a scheduled task, only refreshes the existing one in place) and the
host/VM trust split (an update never widens `VM_ALLOWED` or grants the VM a
key; trust changes are ADR material, not update side effects).

**Rejected:** rebuild-always (turns every patch update into an embedding job);
snapshot republish before venv reinstall (would publish with the *old* code —
ordering is load-bearing); auto-rotating the signing key on update ("refresh
everything" semantics silently invalidating chain verification).

## Ruling 5 — HUMAN CONFIRMED: reconciliation

**Packaging identity: ONE version line.** *(Linchpin ruling. Pre-decided by
the owner 2026-07-05 and confirmed at the s01 human checkpoint; recorded in
`_evidence/rel/ruling5-confirmed.txt`.)*

**Decision.** Brainiac adopts a **single version line**: `pyproject.toml
[project] version` is the one SSOT, and the three plugin.json versions
(currently 1.0.0 / 1.1.0 / 1.1.0) are **re-based onto the engine line as a
one-time breaking packaging change** at the next release (the v0.9.0
release this plan builds). From then on, `package_clients.py` propagates the
pyproject version into all three plugin.json at package time, and
`--validate-only` treats **any plugin.json ≠ pyproject skew as a hard error**
(skew-is-error — this is the validator contract s06 builds against).

**Rationale.** The plugins are not independent products with independent
compatibility stories — every skill in them is stamped with the *engine's*
`SKILL_VERSION` and the update skill's entire skew-detection logic compares
skill stamps against the engine's `dist/COMPAT`. Two version lines mean every
user-facing report ("plugin 1.1.0, engine 0.3.0, skills 0.3.0") needs a
compat map nobody maintains, and the current drift (plugins hand-bumped to
1.x while the engine sits at 0.3.0) is the empirical proof that the map would
rot. One line makes "what version is Brainiac?" have one answer everywhere.

**Rejected: independent engine-vs-plugin version lines + a documented compat
map.** This is the legitimate alternative (it is how real marketplaces with
third-party plugins work), and under it the s06 validator would be the exact
*opposite* — skew-is-expected, validate only that the compat map covers the
pair. Rejected because there is one author, one repo, one release event, and
the plugins ship nothing but packaging of the engine's own skills; a compat
map here is bookkeeping for a decoupling that does not exist. If a genuinely
independently-versioned third-party plugin ever joins the marketplace, that
plugin gets its own line and this ruling gets an amendment — the kernel three
stay on the engine line regardless.

**Existing-install migration (documented, s06/s07 implement).** Re-basing
means the plugin versions move 1.1.0 → 0.9.x — semver-**backwards**. A
marketplace **in-place update refuses a downgrade**, so an existing install
that just pulls the marketplace will sit on "1.1.0 is newer than 0.9.0" and
never update. Therefore the update path for installs predating the
reconciliation must force a **clean plugin reinstall**, not an in-place
update: `/plugin uninstall` each of the three plugins, `/plugin marketplace
update`, `/plugin install` each again — and `/brainiac-update` must detect
the condition (installed plugin version > marketplace version) and print
exactly that instruction rather than a green report. This is a one-time cost,
paid once, at the moment the lines merge; it never recurs because from v0.9.0
on there is only one line. Plugin state (there is none beyond the plugin
files themselves — all vault/engine state lives outside the plugin dirs per
Ruling 4's never-touch list) survives trivially.

## Ruling 6 — Migration policy: rebuild-suffices vs real migration

**Decision.**

- **The index is derived state.** Any change to index shape (new columns, new
  tables, chunking changes, embedding-model change) is handled by bumping
  `SCHEMA_VERSION` and letting the next `brain sync` rebuild from Markdown
  truth. That is the *complete* migration story for the index — no ALTER
  scripts, ever. (Subject to Ruling 2's directional fail-fast: only a
  same-or-newer binary may rebuild.)
- **The snapshot is derived state.** Host republish (ADR-0003 Ruling f)
  is its complete migration story.
- **Versioning invariant: frontmatter is additive-only** (ADR-0003 Ruling 2
  precedent, now stated as an invariant). New keys are optional; existing
  notes stay valid unmodified. A change that would require editing existing
  notes' frontmatter is a **real data migration** and is presumed wrong —
  it needs its own ADR, a MAJOR bump, an explicit migration tool run through
  the audited write path (signed commits, WAL — the
  `tools/apply_live_migration.py` / `migrate_corpus.py` precedent), and can
  never run implicitly inside `/brainiac-update`.
- **When would a real migration ever be justified?** Only when the *truth
  layer* itself must change shape: a frontmatter key semantics correction, an
  id-scheme change, an audit-chain format change. Everything else — index,
  snapshot, graph artifacts, briefs, memory — is disposable derived state
  that rebuilds.

**Rejected:** versioned SQL migrations for the index (machinery for a
database that is definitionally reconstructible); silent in-place frontmatter
rewrites during update (breaches both the never-touch contract and the audit
chain's premise that every note mutation is a signed, deliberate write).

## Ruling 7 — The clean-room release pipeline

**Decision.** A release is a repeatable pipeline; every step below is
scriptable **except the last, which remains a HUMAN act**:

1. **Version + changelog gate:** bump `pyproject.toml [project] version`;
   promote `[Unreleased]` to `[X.Y.Z] — date` (Ruling 3). Refuse if either
   is missing.
2. **Package + lockstep validation:** run `tools/package_clients.py`
   (stamps COMPAT + SKILL_VERSION, propagates plugin.json versions per
   Ruling 5) then `--validate-only` — any skew is a hard stop.
3. **Clean-room export:** regenerate the export tree via
   `tools/export_cleanroom.py` (deterministic include/exclude + manifest,
   ADR-0001). What is exported: the public tree per the export manifest.
   What is excluded: git history, `_plans/`, `_evidence/`, session
   archaeology, overlay content, anything Ruling e of ADR-0003 classes as
   owner-Internal.
4. **Contamination scan — hard gate:** codename grep, session-ID grep, link
   check, secret scan, overlay-content scan — run **against the regenerated
   export tree**, never only the working tree (ADR-0001's core lesson). Any
   hit stops the release; there is no override flag.
5. **Local tag:** `git tag v<X.Y.Z>` on the local commit the export was cut
   from — local provenance for "what produced public v0.9.0".
6. **Public artifact landing:** the export becomes a new squashed commit
   layered on the public branch of `Autopsias/brainiac` (never
   force-replace, never a push of local history), tagged `v<X.Y.Z>`
   (refining ADR-0001's `vN` naming per Ruling 3).
7. **HUMAN publish:** the push URL stays
   `DISABLED://cleanroom-export-only-see-plan` permanently. Publishing means
   a human deliberately pushes the export commit + tag (via a temporary
   remote or a separate clone of the public repo) after eyeballing the
   contamination-scan report. No script, skill, or agent ever re-enables
   the remote or pushes; automation prepares, a human ships.
8. **User consumption:** users get the release via
   `/plugin marketplace add Autopsias/brainiac` → `/plugin install …` →
   `/brainiac-install` (host), or the Cowork zip path per ADR-0002.
   `/brainiac-update` picks up subsequent releases (with the one-time
   clean-reinstall exception of Ruling 5).

**Rejected:** re-enabling the remote for scripted pushes (deletes the single
strongest safeguard against publishing internal history); publishing wheels
to PyPI (adds a supply-chain surface the plugin/marketplace path doesn't
need — revisit only if a non-Claude-Code audience materializes); rewriting
local history with filter-repo instead of exporting (ADR-0001 already
adjudicated this).

## Ruling 8 — Version surfacing

**Decision.** The version must be inspectable at every layer a failure could
implicate:

- `brain --version` → `brain X.Y.Z` (exists; stays `importlib.metadata`).
- `brain status` gains a version block: package version, index
  `schema_version` (stored vs binary's `SCHEMA_VERSION`, with an explicit
  `index_newer_than_binary` flag per Ruling 2's fail-fast), snapshot
  `schema_version` skew (already surfaced per ADR-0003 Ruling f), and the
  maintain heartbeat it already reports. This makes the
  old-binary-meets-newer-state condition visible in one cheap command.
- `/brainiac-install` and `/brainiac-update` reports print the installed
  version (`OLD -> NEW` on update), the `dist/COMPAT` value, and the skill
  skew verdict from Ruling 4 step 1.
- Distributed skills carry their stamped `SKILL_VERSION` (exists); the
  update skill keeps comparing it against COMPAT.

**Rejected:** a version stamped into every generated artifact (briefs,
graphs — noise; those already carry their own `schema_version` where a
reader needs it); a network version-check ("is there a newer Brainiac?")
— deny-by-default egress applies to the tooling too, and the marketplace
already owns update discovery.

---

## Consequences

- **s06** builds the packager/validator changes to Ruling 5's contract:
  propagate pyproject → plugin.json, `--validate-only` skew-is-error, and
  the downgrade-detection message in `/brainiac-update`.
- **s05** (ADR-0005 Ruling 5) adds the complementary monotonic-VERSION guard
  (as opposed to this ruling's monotonic-PACKAGING-line guard): the release
  path refuses to cut any version not strictly greater than the highest
  semver-shaped local tag — see ADR-0005 Ruling 5 for the full rule.
- **s07** (and later install/update sessions) implement the forced clean
  plugin reinstall path and the Ruling 8 status/report surfacing; the
  Ruling 2 index fail-fast lands with the next `SCHEMA_VERSION`-touching
  session.
- The first reconciled release is **v0.9.0**: pyproject 0.3.0 → 0.9.0, all
  three plugin.json re-based to 0.9.0, CHANGELOG `[0.9.0]` section, public
  tag `v0.9.0` via the Ruling 7 pipeline.
- CHANGELOG's header mapping sentence is updated to the `v<semver>` tag
  scheme when the 0.9.0 section is cut.
- No trust-surface change anywhere in this ADR: `locked_counts` (host 1 /
  VM 0) and `VM_ALLOWED` are untouched, and Ruling 4 makes preserving both
  an explicit update-path invariant.

# Release runbook — clean-room release procedure

Authority: `docs/adr/0004-versioning-release.md` (Ruling 7, the pipeline this
runbook operationalizes) and `docs/adr/0001-publish-via-clean-room-export.md`
(the export contract). Read both before running a release; this doc is the
repeatable "how", not a re-litigation of the "why".

**Ground rule that never changes:** the git remote's push URL is the hard
sentinel `DISABLED://cleanroom-export-only-see-plan`. No script, skill, or
agent in this repo ever re-enables it or runs `git push` to origin. Every
step below prepares a release; only a human, deliberately, ships it (Step 8).

---

## 1. Preconditions (go / no-go)

All of the following MUST be true before cutting a release. Each is a
pass/fail gate except the eval baseline, which is **informational only** —
it is recorded for the owner's awareness and never blocks the cut.

| Gate | Command / evidence | Status required |
|---|---|---|
| Full test suite green | `python3 -m pytest -q` | 0 failed |
| Packaging lockstep valid at target version | `python3 tools/package_clients.py --validate-only` | exit 0 |
| Soak run: zero silent data loss | `_evidence/soak/report.md` | "No data-loss or availability-class bugs were found" |
| Contamination scan clean | this runbook §4 (hard gate, no override) | zero proper-noun/codename/note-id/person-name hits |
| Eval baseline | `_evidence/eval/heartbeat.txt` | RECORDED (may be a provisional/fast-sanity capture) — **never a blocker**; if the full-corpus reference baseline is deferred, note that explicitly in the release evidence and proceed anyway |

**Who authorizes the cut:** the owner (repo owner / maintainer running this
runbook locally). No CI system or agent self-authorizes a release — the human
running these steps is the authorizer by virtue of executing Step 3 (the
release commit) and Step 5 (the tag).

---

## 2. Version bump + changelog

The SSOT is `pyproject.toml [project].version` (ADR-0004 Ruling 1).

```
python3 tools/release.py set <X.Y.Z>      # or: bump major|minor|patch
```

This bumps `pyproject.toml`, rolls `CHANGELOG.md`'s `## [Unreleased]` into a
dated `## [X.Y.Z] — YYYY-MM-DD` section (refuses if Unreleased is empty or the
target version already has a section), and runs `tools/package_clients.py` to
propagate the version into all three `plugin.json` + `SKILL_VERSION` stamps +
`dist/COMPAT`.

If the SSOT is **already** at the target version (e.g. a prior session already
ran `release.py set`), `release.py` refuses with "pyproject.toml is already at
X.Y.Z" — that's expected; in that case just verify the CHANGELOG already has
the dated section (it does if `release.py` produced it) and skip straight to
validation below.

**Verify packaging lockstep:**

```
python3 tools/package_clients.py --validate-only
```

Must print `All three client packages built + validated OK.` and exit 0. Any
skew (a plugin.json ≠ pyproject) is a hard stop — ADR-0004 Ruling 5 makes this
a breaking packaging change on the first reconciled release (v0.9.0): the
three plugin manifests (previously `brainiac-manager` 1.0.0,
`brainiac-kernel`/`brainiac-extras` 1.1.0) are re-based **down** onto the
engine's line. This is intentional and documented in §6 below — it is not a
bug if a plugin version appears to move backwards for existing installs.

---

## 3. Ordered release steps

Run in this exact order — each step's precondition is the previous step's
postcondition.

1. **Verify working tree clean of unrelated changes.**
   ```
   git status --porcelain
   ```
   Only the version-bump + CHANGELOG + `docs/release-runbook.md` +
   package_clients-derived stamps (plugin.json ×3, any `dist/COMPAT`/
   `SKILL_VERSION` stamps that are git-tracked) should be pending. Stop and
   investigate if anything else is dirty.

2. **Commit the release commit — ONE commit.**
   ```
   git add pyproject.toml CHANGELOG.md docs/release-runbook.md \
       plugins/*/.claude-plugin/plugin.json
   git commit -m "release: cut v<X.Y.Z>"
   ```

3. **Assert HEAD's version == target.**
   ```
   grep '^version' pyproject.toml
   ```
   Confirm it reads the target version. Do not proceed if it doesn't.

4. **Create the annotated tag — LOCAL ONLY, on this commit.**
   ```
   git tag -a v<X.Y.Z> -m "$(sed -n '/^## \[<X.Y.Z>\]/,/^## \[/p' CHANGELOG.md | sed '$d')"
   ```
   (Or simpler: pass a message summarizing the CHANGELOG section by hand.)
   The tag is created with plain `git tag`; it is **never pushed**. Confirm
   with `git ls-remote --tags disabled-public-DO-NOT-PUSH` that nothing
   remote changed (the command itself will fail/no-op since the push URL is
   disabled — that failure is expected and correct).

5. **Run the export dry-run against the tagged commit** — see §4.

---

## 4. Clean-room export — allowlist contract (ADR-0001)

The export is produced by `tools/export_cleanroom.py`, which enumerates
**git-tracked files at HEAD** (`git ls-files`) and drops anything under an
explicit exclude-prefix list, then regenerates and includes the Cowork
`.skill` zips (the one gitignored exception).

### What is exported

Everything `git ls-files` returns MINUS the excludes below. In practice: all
source (`src/brain/`), all plugins (`plugins/*/`), `docs/`, top-level
project files (`pyproject.toml`, `CHANGELOG.md`, `README.md`, `install.sh`,
`.claude-plugin/`, `.codex/`, `.claude/settings.json`), the kernel's own
generic `vault/brain/*` scaffold notes and `overlay/template/*` starter
templates (these are framework meta-content and starter templates, not owner
data — see the contamination-scan adjudication below), and
`dist/cowork-skills/*.skill` (regenerated fresh, not stale).

### What is EXCLUDED (hard, by construction)

| Excluded path | Mechanism |
|---|---|
| `_plans/` | explicit prefix exclude in `export_cleanroom.py` |
| `_evidence/` | explicit prefix exclude in `export_cleanroom.py` |
| `_archive/` | explicit prefix exclude in `export_cleanroom.py` |
| `_workspace/` | explicit prefix exclude in `export_cleanroom.py` |
| `tests/` (corpus-derived — built using the owner's real vault as example data; excluded 2026-07-12) | explicit prefix exclude in `export_cleanroom.py` |
| `vault/.brain/` (runtime index/cache) | gitignored — never in `git ls-files` |
| `vault/inbox/` (ingestion drop zone) | gitignored — never in `git ls-files` |
| `.brain/` (host runtime: memory, maintain-state, brief, graph) | gitignored — never in `git ls-files` |
| `eval/runs/` (**including `eval/runs/s13-final.json`**, which embeds real corpus-derived query/note text) | gitignored — never in `git ls-files`; **explicitly re-verified below because an unlisted leak vector here would pass a bare name-grep** |
| `eval/qrels/`, `eval/golden_set*.json`, `eval/reformulations.json` | gitignored — never in `git ls-files` |
| `dist/` (except the regenerated cowork zips) | gitignored — never in `git ls-files` |
| owner overlay content (a real owner's `overlay/<theirs>/`) | never git-tracked in this repo in the first place — only the generic `overlay/template/` starter ships |
| git history itself | `export_cleanroom.py` copies working-tree files, never `.git/` |

**Verification command** (run every release, not just this one):
```
git check-ignore -v eval/runs/s13-final.json   # must print a match (confirms it's excluded)
git ls-files eval/runs/                         # must print nothing (confirms it's untracked)
```

### Running the export

```
python3 tools/export_cleanroom.py --output <scratch-dir>
```

Writes every allowed file into `<scratch-dir>` plus `<scratch-dir>/manifest.json`
(sorted list of every exported path + count).

---

## 5. Contamination scan — HARD GATE

Run over **both** the export tree and `_evidence/` (the soak report and other
evidence carry real filenames/paths that could themselves leak — ADR-0001's
core lesson is that a working-tree-only grep does not prove the shipped
artifact is clean, but `_evidence/` never ships either, so it needs its own
pass here for the owner's own assurance before anything is even considered).

**Denylist:** an external, non-repo file
(`~/brainiac-release-groundtruth.txt` on the release operator's machine) of
owner-derived sensitive terms (codenames, party names, note-id hashes,
distinctive corpus tokens). This file must NEVER be committed to the repo.

**Adjudication rule:** a true leak = owner note CONTENT, codenames, note-ids,
or person/party names appearing in the export tree. Some denylist entries are
common English words that may legitimately appear in unrelated framework
code/docs (e.g. "Discovery", "Retail", "Integration" as generic terms) — those
are benign and must be individually justified in the scan output as "common
word, framework context" rather than silently dropped. The gate requires
**zero** proper-noun / codename / note-id / person-name matches; there is no
override flag if a real one is found — fix the export exclude list (or the
tracked-file mistake) and re-run.

**Scan command (redacted output only — never raw matches or the denylist
itself lands in the repo or in committed evidence):**

```
DENYLIST=~/brainiac-release-groundtruth.txt
EXPORT_DIR=<scratch-dir>

# -I skips binary files: _evidence/ carries multi-GB benchmark/eval artifacts
# (indexes, model caches) that a binary-unaware grep chokes on for minutes
# with zero signal — always pass -I. -f takes its own argument (not bundled
# into -rFoiI): this repo's `grep` resolves to ugrep on macOS, which parses a
# bundled "-f<path>" as flag "-f" + filename "I", not GNU grep's more
# permissive bundling — split -f out for portability across both.
# counts + hashes only — never echo the matched line or the term itself
grep -rFoiI -f "$DENYLIST" "$EXPORT_DIR" 2>/dev/null | sort | uniq -c | \
  awk '{print length($0), "chars — count", $1}' \
  > <scratch-dir-or-evidence>/scan-counts-redacted.txt

grep -rFoiI -f "$DENYLIST" _evidence/ 2>/dev/null | sort | uniq -c \
  > /dev/null  # informational only — _evidence/ never ships, not a hard gate;
               # manual adjudication per the note below, not pass/fail
```

**One-command wrapper (GV-02):** `tools/publish_release.py` operationalizes
this section plus §2-§4 into one command with one pass/fail — see the
"Full pipeline" section at the bottom of this runbook.

In practice: run the grep, and for the redacted evidence file record ONLY:
the command used, the denylist path, the denylist term-count, the number of
raw hits (0 expected), and — if the count is non-zero — a one-line
classification per unique match ("common word X, N occurrences, framework
context, benign" or "REAL LEAK — STOP") without ever printing the matched
term or surrounding text.

---

## 6. Breaking-change transition for existing installs (Ruling 5)

The first reconciled release (v0.9.0) re-bases the three plugin manifests
(previously `brainiac-manager` 1.0.0, `brainiac-kernel` / `brainiac-extras`
1.1.0) **down** onto the engine's 0.9.x line. This is a one-time breaking
packaging change, not a bug.

**Consequence for an existing user on plugin 1.x:** a Claude Code in-place
`/plugin marketplace update` reads `1.1.0 -> 0.9.0` as a **downgrade** and
refuses to apply it. There is no in-place update path across this
reconciliation.

**Required transition — a clean reinstall:**
```
/plugin uninstall brainiac-manager
/plugin uninstall brainiac-kernel
/plugin uninstall brainiac-extras
/plugin marketplace update <marketplace-name>
/plugin install brainiac-manager
/plugin install brainiac-kernel
/plugin install brainiac-extras
```
No plugin-local state is lost — per ADR-0004 Ruling 4's never-touch contract,
all vault/engine/audit-chain/memory state lives outside the plugin
directories, so uninstall/reinstall of the plugin files themselves is safe.
This is a one-time cost paid once, at the moment the version lines merge; from
v0.9.0 on there is only one line and future updates go through
`/brainiac-update` normally.

---

## 7. Fresh-user consumption path

1. `/plugin marketplace add Autopsias/brainiac`
2. `/plugin install brainiac-kernel` (and optionally `brainiac-extras`)
3. `/plugin install brainiac-manager`
4. `/brainiac-install` — runs the host installer end-to-end: clone/locate
   `~/brainiac`, run `install.sh` (private venv, `brain` on PATH, first index
   build), verify `brain search ... --json` returns results, register the
   nightly maintenance task, provision the audit signing key, record the
   vault in the workspace registry, print a pass/fail report.
5. Cowork users: `docs/install/cowork.md` / `/brainiac-cowork-setup` (separate
   zero-install path per ADR-0002 — never conflate with the host installer).

---

## 7.5. Subsequent updates — the consume side (GV-02, no stale-cache no-op)

Every later release reaches an existing install through `/brainiac-update`
(`src/brain/update.py` `run_update()`), never a hand-run sequence. The
ordering is structural, not a convention a skill author could get wrong
again:

1. **Marketplace refresh FIRST, always** — `claude plugin marketplace update
   <marketplace>` runs before anything else reads plugin/version state.
2. **Only then** is `brain doctor`'s before-snapshot captured — the snapshot
   that feeds the per-plugin downgrade-safe reinstall decision (§6) reads the
   marketplace's on-disk checkout, i.e. the exact cache Step 1 just
   refreshed. Computing this snapshot before the refresh (a bug this session
   found and fixed — see `tests/test_update.py`'s
   `test_marketplace_refresh_runs_before_the_doctor_snapshot_used_for_comparison`
   and the companion static-ordering guard) would silently compare against
   pre-refresh data — precisely the "I clicked update and nothing happened"
   trap.
3. Engine venv reinstall, workspace re-stage, and the final `brain doctor`
   verify follow (unchanged from ADR-0004 Ruling 4's ordering).

This makes the stale-cache no-op **structurally impossible**, not just
policy: there is no code path in `run_update()` that reads marketplace state
before refreshing it. Regression coverage: `tests/test_update.py` (ordering
assertion + a static source-order guard so the two call sites can never
silently swap back), `tests/test_migration_verification.py` (extended
never-touch assertions — an update must leave `.brain/memory/`,
`maintain-state.json`, `maintain.lock`, and the audit chain byte-identical).

---

## 7.6. Publish to PyPI (human-run) — MUST precede Step 8 (PYP-03)

**Ordering contract (binding, not a suggestion):** publishing to PyPI (and,
later, npm) **PRECEDES** the clean-room export in Step 8. §7's fresh-user
consumption path and every "PyPI-first" doc this repo now ships
(`docs/install/README.md` Path A/C/D, README.md, the lifecycle skills)
describe `uv tool install brainiac-cli[mcp]` / `pip install brainiac-cli` as
the primary install command. If the public repo commit in Step 8 lands
**before** the matching version is actually on PyPI, every one of those docs
is a 404 for anyone who reads them that day. Never run Step 8 for a version
whose PyPI publish (this section) hasn't completed and been verified.

`tools/release.py` is **build automation only** (`bump`/`set` + the
packager) — it has no upload/publish subcommand and must never gain one.
Publishing itself is always a deliberate human act, the same class of
irreversible step as Step 8's `git push` to the public remote: a human
runs the commands below from their own authenticated `twine`/PyPI session.
No token, API key, or `.pypirc` credential is ever read, written, or
referenced by any script in this repo.

**1. Build the sdist + wheel** (scriptable, no auth needed):

```
python3 -m pip install --upgrade build twine   # once, into your own env — NOT this repo's
python3 -m build
```

Produces `dist/brainiac_cli-<X.Y.Z>-py3-none-any.whl` and the matching
`.tar.gz`. Confirm the version in the filename matches `pyproject.toml`'s
SSOT (§2) exactly.

**2. TestPyPI dry-run FIRST — never skip straight to production PyPI:**

```
python3 -m twine upload --repository testpypi dist/brainiac_cli-<X.Y.Z>*
```

(Requires a TestPyPI account + API token in the human's own session —
`twine` prompts for it or reads `~/.pypirc` on the operator's machine,
never this repo's.)

**3. Verify the TestPyPI artifact installs and runs, from a clean throwaway
environment** (a scratch dir/venv — never this repo's own `.venv` — see the
TestPyPI RC checklist below for the full script):

```
uv venv /tmp/brainiac-testpypi-check && cd /tmp/brainiac-testpypi-check
uv pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ 'brainiac-cli[mcp]'
./bin/brain --version   # confirm it prints exactly X.Y.Z
```

(`--extra-index-url https://pypi.org/simple/` is required — TestPyPI doesn't
mirror brainiac-cli's dependencies, only its own uploaded packages.)

**4. Only once step 3 passes, publish to production PyPI:**

```
python3 -m twine upload dist/brainiac_cli-<X.Y.Z>*
```

**5. Verify the real publish**, and run the self-check row that gates this
whole section:

```
brain doctor --check-registry --json
```

Confirm the "PyPI registry drift" row reads `current` (repo tag == installed
== PyPI latest, all `<X.Y.Z>`) — never `not-detectable` ("not yet published")
and never a marketplace/skills-ahead warning. This is the row that would have
caught a public repo shipped ahead of its PyPI publish; running it here,
before Step 8, is what makes the ordering contract above enforceable rather
than just documented.

**Only after this section's step 5 passes** does Step 8 (public git export +
push) become safe to run for this version.

---

## 7.7. TestPyPI RC gate (prepared here — a human runs it)

This subsection is a **checklist**, not an executed run — S07 prepares it so
a human release operator has a ready-to-follow script the first time a real
PyPI cut happens. The filled-in transcript of an actual run belongs at
`_evidence/install-plan/testpypi-rc.md` (see that file for the template and
required fields — its existence is a documented entry criterion for this
plan's human-checkpoint session).

1. Publish a **uniquely-versioned** build to TestPyPI (§7.6 steps 1-2) —
   never re-upload an existing version number; TestPyPI (like PyPI) refuses
   overwrites, and a throwaway pre-release suffix (e.g. `X.Y.Z.rc1`) keeps
   this dry run from colliding with a real release number.
2. Install with the **literal documented commands** — not a hand-tuned
   variant — from each of the three PyPI channels this session ships:
   ```
   uv tool install 'brainiac-cli[mcp]'
   pipx install 'brainiac-cli[mcp]'
   python3 -m pip install --user 'brainiac-cli[mcp]'
   ```
   (point each at `--index-url https://test.pypi.org/simple/
   --extra-index-url https://pypi.org/simple/` for the RC gate only — the
   real published commands never carry an index-url flag).
3. Exercise **upgrade**: bump the RC suffix, re-publish, run the matching
   channel's real upgrade command (`uv tool upgrade brainiac-cli` / `pipx
   upgrade brainiac-cli` / `pip install --user --upgrade brainiac-cli[mcp]`),
   confirm `brain --version` moved.
4. Exercise **uninstall** for each channel (`uv tool uninstall brainiac-cli`
   / `pipx uninstall brainiac-cli` / `pip uninstall -y brainiac-cli`),
   confirm `brain` is no longer on PATH afterward.
5. Record the full transcript (commands run + their output, PASS/FAIL per
   channel per step) at `_evidence/install-plan/testpypi-rc.md`.

---

## 7.8. Windows pre-release acceptance checklist (human-run, release gate)

A fresh, **non-admin** Windows machine (or VM) — never the release
operator's own dev box, which already has stale state from prior installs.
Transcript of this run is release evidence, same class as the soak report in
§1.

1. **Install:** `.\install.ps1` (default PyPI-first path — confirm it tries
   uv → pipx → pip --user and reports which one succeeded; also run
   `.\install.ps1 -Dev` separately if a checkout is available, to confirm the
   dev/offline path still works).
2. **Init:** `brain init --full --apply` against a fresh vault folder.
3. **First search:** confirm the seeded sample notes are findable
   (`brain search "..." --json` returns results, no embedder warning).
4. **Verify scheduled-task registration — BOTH host tasks, not just one:**
   ```
   .\scripts\install-brief-windows.ps1 -VaultPath <path>
   Get-ScheduledTask -TaskName 'brain-daily-brief-*'
   ```
   As of this session, `install-brief-windows.ps1` registers only the
   nightly umbrella task; the second locked host task (`brain-synthesis`,
   weekly) has **no Windows registration path yet** (macOS's
   `install-brief-mac.sh` registers both — see `routines/manifest.json`
   `locked_counts.host_os_scheduled: 2`). Record this as a known gap in the
   transcript rather than a checklist failure — closing it (a
   PowerShell-native `brain-synthesis` runner + Task Scheduler registration)
   is tracked follow-up work, not part of this session's scope.
5. **Update:** `brain update` (or `/brainiac-update`) — confirm `brain
   doctor`'s "Host engine install" row reports the correct channel and the
   before→after table shows a version move on a subsequent release.
6. **Uninstall:** `/brainiac-uninstall` — confirm the channel-aware removal
   (§ lifecycle skill) actually removes the engine via the right command for
   whichever channel step 1 used, and that the scheduled task(s) are gone.

---

## 7.9. Publish to npm (human-run) — after PyPI, before Step 8 (SUI-01)

The `brainiac-install` npx bootstrap (`packaging/npm/brainiac-install/`) is
published to npm **after** §7.6's PyPI publish completes and **before** Step
8's public export — same ordering contract as §7.6, and for the same
reason: the package's whole job is `npx brainiac-install` installing
`brainiac-cli` from PyPI, so publishing it ahead of PyPI would ship a
bootstrapper for a version that isn't there yet, and publishing it after
Step 8 would leave the freshly-public docs pointing at an `npx` command
that 404s.

Same human-only class as PyPI/`git push`: no script in this repo holds or
reads an npm token — a human runs `npm publish` from their own authenticated
npm session (`npm login` / `~/.npmrc`, never this repo's).

**1. Sync the package version with the release** — AUTOMATIC since v0.19.10.
`tools/package_clients.py` writes the SSOT version into
`packaging/npm/brainiac-install/package.json` on every build, and
`--validate-only` hard-fails on skew, exactly like the three `plugin.json`
manifests (ADR-0004 Ruling 5).

This used to be a hand-edit, and the hand-edit is precisely what went wrong:
cutting v0.19.10 through `tools/release.py` left the npm manifest at 0.19.9
while every lockstep check still reported OK, because the validator did not
know the fourth published artifact existed. Nothing to do here now beyond
confirming the packager ran:

```
python3 tools/package_clients.py --validate-only   # includes the npm manifest
```

**2. Pack + smoke-test the tarball** (repeats this session's smoke test —
never skip it, a broken tarball is a broken `npx` command for every user):

```
cd packaging/npm/brainiac-install
npm pack --pack-destination /tmp/brainiac-npm-check
cd /tmp/brainiac-npm-check
npm install -g --prefix ./install-prefix --install-strategy=hoisted ./brainiac-install-<X.Y.Z>.tgz
./install-prefix/bin/brainiac-install --dry-run   # confirm command plan, exit 0
```

**3. Publish:**

```
cd packaging/npm/brainiac-install
npm publish --access public
```

(`--access public` is required the first time a scoped-or-not new package is
published to a fresh npm account; harmless to repeat on subsequent
releases.)

**4. Verify the real publish** from a clean environment:

```
npx --yes brainiac-install@<X.Y.Z> --dry-run
```

Confirm it resolves the just-published version and prints the same command
plan as step 2's local smoke test.

**Only after this section's step 4 passes**, alongside §7.6's PyPI
verification, does Step 8 (public git export + push) become safe to run for
this version.

---

## 8. Public artifact landing + HUMAN publish (never scripted)

The export tree becomes a **new squashed commit** layered on the public
branch of `Autopsias/brainiac` (never a force-replace, never a push of local
history), tagged `v<X.Y.Z>`. This step is **always a human, deliberate act**:

1. A human clones the public repo fresh (or adds a temporary remote — never
   re-enables this repo's disabled `disabled-public-DO-NOT-PUSH` remote).
2. Copies the export tree's contents in, commits as a single squashed commit,
   tags it `v<X.Y.Z>`.
3. Eyeballs the redacted contamination-scan report from §5 one more time.
4. Pushes — from that separate clone/remote, never from this repo.

No script, skill, or agent in this repo ever performs step 4, or re-enables
`disabled-public-DO-NOT-PUSH`'s push URL. If any tooling is ever found to
attempt `git push` toward that remote, treat it as a bug against this ADR.

---

## 9. Post-publish defect / yank procedure

Because the push URL is permanently disabled here, "yanking" a bad public
release is **not** a local operation — deleting the local tag does nothing to
what a human already pushed to the public repo in Step 8.

If a published release turns out to be defective or itself contaminated:

1. **Do not** delete the local `v<X.Y.Z>` tag and consider the matter closed
   — that only affects this machine's local provenance record, not the
   public artifact.
2. On the **public** repo (via the same human-operated clone/remote used to
   publish), either:
   - **Supersede:** cut a new patch release (`vX.Y.Z+1`) with the fix,
     following this entire runbook again, and update `SECURITY.md`'s
     supported-versions stanza to mark the bad version unsupported.
   - **Retract (only if actively harmful, e.g. real contamination did ship):**
     a human deletes the bad tag on the **public remote** directly
     (`git push public :refs/tags/vX.Y.Z` from the human's own publish clone —
     never from this repo) and, if the commit itself must go, force-pushes a
     corrected squashed commit as a new public history — again, from the
     human's separate publish clone, never as an automated or scripted step
     from this repo.
3. Record the defect and the remediation (superseded-by version, or retracted
   + why) in `CHANGELOG.md` and `SECURITY.md` as normal entries — this repo's
   local tag stays exactly as it was cut; it is release **provenance**
   ("what local commit produced the bad public artifact"), not a live pointer
   to what's currently public.

---

## Summary — the full pipeline in one sequence

```
tools/release.py set X.Y.Z   (or bump)      # SSOT + CHANGELOG + monotonic-version guard (ADR-0005 Ruling 5, GV-01)
package_clients.py --validate-only          # lockstep gate + monotonic-version gate
git status --porcelain                      # tree clean check
git commit ...                              # ONE release commit
grep '^version' pyproject.toml              # assert version on HEAD
git tag -a vX.Y.Z ...                       # LOCAL tag, never pushed
export_cleanroom.py --output <scratch>      # dry-run export
contamination scan (denylist) over export (hard gate) + _evidence/ (informational, never ships)
install-from-export smoke test              # marketplace add + install + search
[HUMAN] publish export as squashed commit on public repo, from a separate clone
```

**One-command wrapper (GV-02):** `tools/publish_release.py` runs the
scriptable middle of this sequence (package validate → export →
contamination scan → optional local tag) as a single command with one
pass/fail — still stopping before the human publish step:

```
python3 tools/publish_release.py --check                                    # gates only, no scan/tag
python3 tools/publish_release.py --denylist ~/brainiac-release-groundtruth.txt          # + hard contamination gate
python3 tools/publish_release.py --denylist ~/brainiac-release-groundtruth.txt --tag    # + cut the local vX.Y.Z tag
```

It never touches the disabled remote and never pushes — Step 8's human act
is unchanged. `tools/release.py`'s own monotonic-version guard (GV-01) fires
first, at the `set`/`bump` step, before any of the above ever runs.

**Publish → consume, the full loop, cross-checked end-to-end (dry-run):**
`tools/publish_release.py --check` (publish side, this session's evidence:
`_evidence/update/monotonic-guard.txt` covers the guard;
`tests/test_cleanroom_export_smoke.py` covers the exported tree staging a
zero-install VM that reports the correct version) feeds exactly the same
exported tree `tests/test_export_cleanroom.py` and
`tests/test_cleanroom_export_smoke.py` assert on; the consume side
(§7.5 above, `src/brain/update.py` `run_update()`) always refreshes the
marketplace before reading any version-comparison state. The two ends meet
at the version SSOT: what `publish_release.py` validates as
`pyproject.toml`'s version is the exact value `/brainiac-update`'s
before/after table and `brain doctor` report on the consumer side.

---
name: publish-release
description: "Cut and publicly publish a new brainiac version end to end — version bump, scoped release commit, local tag, then the guarded pipeline (tools/publish_public.py: clean-worktree export, contamination gates with scanner self-test, TestPyPI → PyPI → npm → public git push) with the owner approving each irreversible act in-session. Triggers: \"publish the release\", \"cut and publish\", \"ship version X.Y.Z publicly\", \"release to PyPI\", \"push the new version to the public repo\", \"/publish-release\". NOT for local-only version cuts (tools/release.py alone), not for the clean-room export by itself (tools/publish_release.py), and never runs unattended or on a schedule."
disable-model-invocation: true
---

# publish-release — cut and publish a version, gates included

Runs the whole release: version cut → guarded publish → verification, with
the owner approving every irreversible act in-session. Authority for what
each step means: `docs/release-runbook.md` (§2–§3 the cut, §7.10 the
pipeline). This skill sequences them; it never re-defines them.

**Motivating failure (2026-07-29):** v0.19.17 went to PyPI without the
Windows fixes already committed — the tag was cut one commit early, the
export could have leaked a concurrent session's uncommitted work, and the
red Windows CI signal had been read as dependabot noise for six days. The
pipeline closes those classes; this skill makes running it the path of least
resistance.

## Hard rules

- NEVER pass `--confirm` for an act the owner has not approved **in this
  session** via the decision card in step 4. One flag per approved act; there
  is no confirm-everything.
- NEVER stage concurrent work into the release commit — stage the release
  artifacts by name (step 2), even if the tree carries another session's WIP.
- NEVER touch credentials: no reading `~/.pypirc`/`~/.npmrc`, no token
  handling, no `printenv` hunting. If twine/npm/git lack standing auth, their
  own errors say so — report the error and stop; configuring auth is the
  owner's, done outside this skill.
- A failed phase is a finding, not a retry loop: diagnose, fix, resume with
  `--from <phase>` (completed uploads are never re-run).

## Checklist (copy into the session and tick off)

```
[ ] 1. Preflight: [Unreleased] non-empty, WIP identified, prior tag verified
[ ] 2. Cut: release.py bump → scoped commit → local tag
[ ] 3. Verify: pipeline --dry-run (full suite, export, canary, build, CI signal)
[ ] 4. Decision card: owner approves acts (AskUserQuestion, one card)
[ ] 5. Execute: pipeline with --confirm per approved act + --consent-note
[ ] 6. Report: evidence transcript + what is now live where
```

## Steps

### 1 · Preflight

- `CHANGELOG.md` must have real content under `## [Unreleased]` — an empty
  section means there is nothing to release; stop and say so.
- `git status --short`: identify anything dirty that is NOT release material.
  It stays out of the release commit (Hard rules) — list it to the owner in
  the step-4 card so they know what is deliberately excluded.
- Confirm the previous release's tag exists and the working tree's
  `pyproject.toml` version equals PyPI's latest (the pipeline re-checks this,
  but catching it here saves a full dry-run).

### 2 · Cut the version

```
python3 tools/release.py bump patch        # or: bump minor | set X.Y.Z
```

Stage ONLY the bump artifacts, by name (never `git add -A`):
`pyproject.toml CHANGELOG.md src/brain/_version.py
packaging/npm/brainiac-install/package.json plugins/*/.claude-plugin/plugin.json`
plus any `SKILL_VERSION`-stamped `SKILL.md` the packager rewrote
(`git diff --name-only` and take the version-stamp-only ones). Commit as
`release: <X.Y.Z> — <one line from the CHANGELOG>`, then tag:

```
git tag -a v<X.Y.Z> -m "brainiac <X.Y.Z> — <one line>"
```

### 3 · Verify — the pipeline's dry-run

```
python3 tools/publish_public.py v<X.Y.Z> \
  --denylist ~/brainiac-release-groundtruth.txt --dry-run
```

Runs the full suite in a clean worktree at the tag, the export, both
contamination gates (self-test first), the build, and the Windows CI check,
then stops before anything uploads. Two expected stops and their meanings:

- **Contamination hits** — scrub the named class in the tracked files,
  re-commit, re-tag (delete + recreate the LOCAL tag), re-run step 3.
- **Windows CI red/unreachable** — read the linked run log first. Only if
  the failure is demonstrably unrelated to the matrix job (or IS the bug
  this release fixes) does it become an `--accept-windows-ci "<reason>"`;
  put the same reason in the step-4 card.

### 4 · The decision card — one AskUserQuestion

Present ONE card carrying: the dry-run's verified summary (verbatim from the
gate output), anything excluded from the release commit (step 1), any
Windows-CI acceptance reason, and the four acts as a multi-select
(TestPyPI / PyPI / npm / public git push) with "all four" as the recommended
option. The owner's selection is the consent — record their exact wording if
they add notes.

### 5 · Execute

```
python3 tools/publish_public.py v<X.Y.Z> \
  --denylist ~/brainiac-release-groundtruth.txt \
  --skip-tests \
  --confirm <act> [--confirm <act> ...] \
  --consent-note "owner via AskUserQuestion, <date>: <selection summary>" \
  [--accept-windows-ci "<reason from step 3>"]
```

`--skip-tests` is legitimate here and only here: step 3 just ran the suite
on this exact tag. Pass `--confirm` ONLY for the acts selected in step 4.
If a phase fails midway (e.g. npm auth missing): report the tool's own
error, let the owner fix auth outside the session, then resume with
`--from <phase>` plus the still-pending `--confirm` flags — consent from
step 4 carries over within the same session; a NEW session asks again.

### 6 · Report

Lead with what is now live: PyPI version, npm version, public repo tag —
each from the pipeline's own post-verify output, never assumed. Link the
evidence transcript (`_evidence/releases/publish-<X.Y.Z>.md`). If any act
was declined or failed, state plainly what is NOT published and what the
next command is. End with: Next / Needs you.

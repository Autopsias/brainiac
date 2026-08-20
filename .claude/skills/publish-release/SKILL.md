---
name: publish-release
description: "Cut and publicly publish a new brainiac version end to end — version bump, scoped release commit, local tag, then the guarded pipeline (tools/publish_public.py: clean-worktree export, contamination gates with scanner self-test, TestPyPI → PyPI → public git push, with npm published by CI on the tag) with the owner approving each irreversible act in-session. Triggers: \"publish the release\", \"cut and publish\", \"ship version X.Y.Z publicly\", \"release to PyPI\", \"push the new version to the public repo\", \"/publish-release\". NOT for local-only version cuts (tools/release.py alone), not for the clean-room export by itself (tools/publish_release.py), and never runs unattended or on a schedule."
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
[ ] 1. Preflight: ONE interpreter imports pytest+build+twine, tag exists,
       denylist present, [Unreleased] non-empty, WIP identified
[ ] 2. Cut: release.py bump → scoped commit → local tag
[ ] 3. Verify: pipeline --dry-run (full suite, export, canary, build, CI signal)
[ ] 4. Decision card: owner approves acts (AskUserQuestion, one card, FOUR acts)
[ ] 5. Execute: pipeline with --confirm per approved act + --consent-note
[ ] 6. Report: evidence transcript + what is now live where
[ ] 7. Propagate: host, staged workspace, Cowork plugin store, VM leg —
       then verify each moved (a publish updates none of them)
```

## Steps

### 1 · Preflight

**Run these four assertions FIRST — each one cost a failed run on 2026-08-16.**

```
PY=~/.brainiac/venv/bin/python3          # the interpreter that has all three
$PY -c "import pytest, build, twine"     # must ALL import, from ONE interpreter
git rev-parse --verify refs/tags/v<X.Y.Z>   # the pipeline needs the tag to exist
ls ~/brainiac-release-groundtruth.txt    # the denylist
```

- **One interpreter, all three modules.** The pipeline runs the suite AND
  twine with the same `sys.executable`. Homebrew's `python3` has pytest but
  no twine; a bare venv with twine has no pytest. Installing twine into a
  fresh venv "to fix it" swaps a step-7 failure for a step-3 failure. On this
  host `~/.brainiac/venv/bin/python3` is the one that satisfies all three —
  verify, never assume, and invoke the pipeline with it explicitly.
- **Do NOT check `npm whoami`, and never run `npm login`.** Since 2026-08-20
  the pipeline does not publish npm at all: the tag push fires
  `npm-publish.yml`, which publishes over OIDC with no credential and with a
  provenance attestation. The old hand publish is what needed a login that
  expires every two hours, and it broke the v0.20.22 run. If npm is missing at
  the end, the fix is the workflow, not an auth session — `post-verify` prints
  the two `gh` commands.
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
Windows-CI acceptance reason, and the acts as a multi-select.

**There are FOUR gated acts:** `testpypi`, `pypi`, `public-git`,
`release-asset`. It was five until 2026-08-20, when `npm` stopped being an
act this pipeline performs. Include `release-asset` explicitly — omitting it
stops the run near the end needing a second, separate approval (measured
2026-08-16) — and say what it carries: it attaches assets to the GitHub
release that CI already created on the tag push, and it has failed since
v0.20.5 on the mcpb handshake gate, so the owner is approving something with
a known failure history, not a formality.

**Say what `public-git` now also does.** Pushing the tag publishes
`brainiac-install` to npm, and an npm version is permanent. The pipeline's
gate text says this; the card must too, or the owner is approving a
consequence they were not shown.

The owner's selection is the consent — record their exact wording if they add
notes, and pass one `--confirm` per selected act. If they approve a subset
now and the rest later, that later approval is its own card and its own
`--consent-note`; never widen an earlier note to cover a new act.

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
If a phase fails midway: report the tool's own error, let the owner fix it
outside the session, then resume with `--from <phase>` plus the still-pending
`--confirm` flags — consent from step 4 carries over within the same session;
a NEW session asks again. Preflight is resume-aware (`expect_published`), so
`--from public-git` does NOT trip the already-on-PyPI guard. A resume also
reuses a suite pass recorded for the SAME commit sha, so the prefix is cheap.

**Auth surfaces the pipeline cannot solve for the owner** — each is the
owner's to hold, and the tool's own error is the report:

- **twine (PyPI/TestPyPI)** needs a credential it can find non-interactively
  (`~/.pypirc` section, or keyring). No terminal ⇒ no password prompt; it
  raises `EOFError`. TestPyPI tokens are separate from PyPI's.
- **npm needs no auth at all any more** — CI holds it, via OIDC. There is no
  URL to relay and no code to type. `post-verify` waits up to 10 minutes for
  the workflow plus registry lag; if it times out, read and re-fire the
  workflow with the two `gh` commands the error prints.
- **A missing module reads as an auth failure.** Measured 2026-08-16: the run
  died with `No module named twine` on line 1 of its own log, and that was
  diagnosed twice as a credential problem and once as a missing TTY before
  anyone read the first line. Step 1's `import pytest, build, twine` assertion
  exists to make this impossible; if a phase still fails, read the log from
  the TOP — `interactive=True` phases inherit stdout, so the real error is
  there, not in the tail.

### 6 · Report

Lead with what is now live: PyPI version, npm version, public repo tag —
each from the pipeline's own post-verify output, never assumed.

**Check PyPI on the simple index, not the JSON `latest` field.** Measured
2026-08-16: minutes after a successful upload,
`https://pypi.org/pypi/<pkg>/json` still reported `latest: 0.20.11` and
`0.20.13 present: False`, which reads as a failed publish. The authoritative
surfaces agreed the release was live:

```
curl -s https://pypi.org/simple/brainiac-cli/ | grep 0.20.13   # installers read this
curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/pypi/brainiac-cli/0.20.13/json
```

Likewise, never report an outcome from a harness "task completed" summary —
three times on 2026-08-16 one arrived saying exit 0 while the run's own log
ended `EXIT=1`. Read the log and the transcript.

Link the evidence transcript (`_evidence/releases/publish-<X.Y.Z>.md`). If any
act was declined or failed, state plainly what is NOT published and what the
next command is. Then do step 7 — the release is not finished here.

### 7 · Propagate — publishing updates NOTHING that consumes the release

Publishing moves the public artifacts. Every surface that RUNS the engine is
still on the old version until it is refreshed, and each one hides its
staleness differently. Walk all four (2026-08-16: Cowork sat two releases
behind for a day because only the public half had been done).

| Surface | Refresh | Verify it actually moved |
|---|---|---|
| Host engine + CLI plugins | `brain update --engine-src <verified checkout>` | `brain --version`; doctor "Host engine venv" row |
| Staged Cowork workspace (engine, model, skills, **ELF binaries**, vendor, AGENTS.md, prompt) | same `brain update` run | doctor rows: "Staged workspace", "Staged skill bundles", "Staged VM binary" |
| Cowork plugin store | **manual, inside Cowork** — no host process can drive it | doctor "Desktop/Cowork plugin store" rows, or the `plugin.json` mtimes under `local-agent-mode-sessions/*/*/rpm/plugin_*/` |
| Cowork VM leg | picks up the staged workspace | `brain doctor` + `brain status` FROM the VM |

- `--engine-src` must be the checkout Step 1 verified. Point it at a stale
  clone and the re-stage ships an old engine while reporting every step `ok`.
- **A VM reporting the new version does not prove the plugin store moved** —
  the VM reads the staged workspace, so it reads new even if the store never
  budged. Check the store's own files.
- Never accept a Cowork "Save and Replace" success toast as evidence: it can
  no-op (Anthropic #46844 / #46836). Re-read the version from the host.
- If the store says "already updated" while the host is newer, the release was
  probably never PUBLISHED — Cowork installs from the marketplace, not from
  this checkout. Re-check the public surfaces before touching Cowork again.

End with: Next / Needs you.

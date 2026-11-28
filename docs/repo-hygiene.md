# Repository Hygiene Runbook

> **Audience:** Maintainers cleaning local checkouts of `profile-a-brain` after
> plan runs or security scans. Assumes standard `git` CLI knowledge; `plan-execute`
> creates per-plan git worktrees under `.plan-worktrees/`, and `claude-security`
> writes timestamped scan directories named `CLAUDE-SECURITY-*` in the repo root.
> Running these dry-run checks first prevents accidental deletion of active work.

Use the dry-run commands first. Only run the prune commands after you verify
that no active session or unmerged work depends on the listed paths or branches.

---

## 1. Stale git worktrees

### Dry-run (inspect first)

List all registered worktrees, their branches, and lock states:

```bash
git worktree list
```

Check which worktree administrative entries are missing on disk and would be
pruned:

```bash
git worktree prune --dry-run --verbose
```

Before removing a worktree directory, verify it has no uncommitted changes and
that its branch is already merged into `master`:

```bash
git -C <worktree-path> status --short
git branch --merged master
```

### Prune (after confirming nothing is active or unmerged)

Prune missing worktree metadata:

```bash
git worktree prune --verbose
```

Remove a finished, unlocked worktree whose branch is merged:

```bash
git worktree remove <worktree-path>
```

If a finished plan worktree is marked `locked` (only after verifying no active
session is using it):

```bash
git worktree unlock <worktree-path>
git worktree remove <worktree-path>
```

Never pass `--force` unless you have already confirmed you want to discard
uncommitted changes inside that worktree.

---

## 2. Merged local branches

### Dry-run (inspect first)

List local branches already merged into `master` (excluding `master` and the
current branch):

```bash
git branch --merged master | grep -vE '^\*|^\s+master$'
```

### Prune (safe delete only)

Delete merged branches one by one (lowercase `-d` refuses to delete unmerged
branches):

```bash
git branch -d <branch-name>
```

Or delete all listed merged branches safely:

```bash
git branch --merged master | grep -vE '^\*|^\s+master$' | xargs -n 1 git branch -d
```

---

## 3. Leftover `CLAUDE-SECURITY-*` scan directories

`CLAUDE-SECURITY-*/` is ignored in root `.gitignore`, so scan runs never dirty
`git status`, but old directories accumulate on disk.

### Dry-run (inspect first)

List scan directories with sizes and timestamps:

```bash
ls -ldh CLAUDE-SECURITY-* 2>/dev/null || echo "No CLAUDE-SECURITY-* directories found."
du -sh CLAUDE-SECURITY-* 2>/dev/null || true
```

### Prune (after confirming no scan is running and reports are saved)

Remove a specific completed scan directory:

```bash
rm -rf ./CLAUDE-SECURITY-YYYYMMDD-HHMMSS
```

Or remove all scan directories older than 14 days (dry-run first by omitting
`-exec rm -rf {} +`):

```bash
find . -maxdepth 1 -type d -name 'CLAUDE-SECURITY-*' -mtime +14 -print
find . -maxdepth 1 -type d -name 'CLAUDE-SECURITY-*' -mtime +14 -exec rm -rf {} +
```

---

## 4. Cleanliness verification

After any cleanup pass, confirm the repository state and lint checks remain
green:

```bash
git status --short
ruff check .
pytest tests/test_ruff_per_file_ignores_are_live.py
```

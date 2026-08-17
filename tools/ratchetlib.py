"""Shared git plumbing for the three quality-ratchet checkers' --staged mode.

Source of truth: gearbox scripts/quality/ (deployed to ~/.claude/scripts/quality/).
Adopting repos vendor this file into tools/ next to the checkers via
vendor_quality.py — never edit a vendored copy; re-sync instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(project: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=project, capture_output=True, text=True,
    )


def staged_py_files(project: Path) -> list[str]:
    """Relative paths of staged .py files (added/copied/modified/renamed).

    Empty list when not in a git repo — staged mode then has nothing to judge.
    """
    result = _git(project, "diff", "--cached", "--name-only", "--diff-filter=ACMR")
    if result.returncode != 0:
        return []
    return [f for f in result.stdout.splitlines() if f.endswith(".py")]


def parent_sources(project: Path, rel: str) -> list[str]:
    """The file's content at each commit parent (HEAD, plus MERGE_HEAD mid-merge).

    A version the file does not exist in is omitted; a brand-new file returns [].
    The MERGE_HEAD side matters: a merge that lands a long-lived branch must be
    judged against what the branch already carried, not blocked as if the merge
    commit had authored that debt.
    """
    refs = ["HEAD"]
    # rev-parse, not a .git/MERGE_HEAD stat: .git is a file in worktrees.
    if _git(project, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
        refs.append("MERGE_HEAD")
    sources = []
    for ref in refs:
        result = _git(project, "show", f"{ref}:{rel}")
        if result.returncode == 0:
            sources.append(result.stdout)
    return sources

#!/usr/bin/env python3
"""Advance the cos-nightly DEPLOY WORKTREE to a named commit.

WHY A SEPARATE DEPLOY WORKTREE. The scheduled nightly cannot point at the
main checkout — every commit anyone makes there would deploy straight to the
live mailbox with no review — and it cannot point at a plan worktree either,
because that worktree disappears the moment its branch merges (see s01's
schedule.md evidence: the live job did exactly this on 2026-09-02). So the
nightly runs from ONE stable `git worktree` checked out detached at a
DELIBERATELY PROMOTED commit; this script is the one thing that moves it.

WHAT IT DOES, each run:
  1. Refuses if the deploy worktree has any uncommitted change — a promote is
     a checkout, and checking out over local edits silently discards them.
  2. Resolves --sha to a full commit hash and checks it out detached.
  3. Verifies HEAD now reads back that exact hash (never trust the checkout
     blindly).
  4. Records the promoted sha, in the worktree's PRIVATE git-dir (`git
     rev-parse --git-dir`) — outside the working tree, so the record itself
     can never make step 1's dirty check fail on the NEXT promote.

    python3 tools/cos_deploy_promote.py --deploy-repo <path> --sha <ref>

Exit 0 = promoted (or already at that sha). Exit 2 = usage / not a git
worktree / ref does not resolve. Exit 3 = refused: the deploy worktree is
dirty.
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path


class DeployPromoteError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise DeployPromoteError(
            f"git -C {repo} {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def promote(deploy_repo: Path, ref: str) -> dict:
    """Check the deploy worktree out to `ref`, refusing a dirty tree.

    Returns a dict describing the outcome; raises DeployPromoteError (with
    .exit_code set) on any refusal.
    """
    if not deploy_repo.is_dir():
        raise DeployPromoteError(f"no directory at {deploy_repo}")
    try:
        _git(deploy_repo, "rev-parse", "--is-inside-work-tree")
    except DeployPromoteError as exc:
        raise DeployPromoteError(
            f"{deploy_repo} is not a git worktree: {exc}") from exc

    dirty = _git(deploy_repo, "status", "--porcelain")
    if dirty:
        raise DeployPromoteError(
            f"REFUSING: {deploy_repo} has uncommitted changes:\n{dirty}\n"
            "A promote is a checkout — it would silently discard them. "
            "Commit, stash, or discard by hand first.", exit_code=3,
        )

    try:
        sha = _git(deploy_repo, "rev-parse", f"{ref}^{{commit}}")
    except DeployPromoteError as exc:
        raise DeployPromoteError(
            f"{ref!r} does not resolve to a commit in {deploy_repo}: {exc}") from exc

    previous_sha = None
    git_dir = Path(_git(deploy_repo, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = deploy_repo / git_dir
    state_path = git_dir / "cos-deploy-state.json"
    if state_path.is_file():
        try:
            previous_sha = json.loads(state_path.read_text()).get("deployed_sha")
        except (json.JSONDecodeError, OSError):
            previous_sha = None

    _git(deploy_repo, "checkout", "--detach", sha)
    head = _git(deploy_repo, "rev-parse", "HEAD")
    if head != sha:
        raise DeployPromoteError(
            f"checkout did not land on {sha}: HEAD reads {head}")

    state = {
        "deployed_sha": sha,
        "ref": ref,
        "promoted_at": datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "previous_sha": previous_sha,
    }
    state_path.write_text(json.dumps(state, indent=2) + "\n")

    return {
        "ok": True,
        "deploy_repo": str(deploy_repo),
        "deployed_sha": sha,
        "previous_sha": previous_sha,
        "state_path": str(state_path),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deploy-repo", required=True, type=Path,
                    help="path to the pinned deploy worktree")
    p.add_argument("--sha", required=True,
                    help="commit-ish to promote the deploy worktree to")
    args = p.parse_args(argv)

    try:
        result = promote(args.deploy_repo, args.sha)
    except DeployPromoteError as exc:
        print(f"cos_deploy_promote: {exc}", file=sys.stderr)
        return exc.exit_code

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

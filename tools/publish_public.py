#!/usr/bin/env python3
"""Guarded end-to-end public release pipeline (runbook §7.6-§8 in one command).

    python3 tools/publish_public.py v0.19.18 --denylist ~/brainiac-release-groundtruth.txt
    python3 tools/publish_public.py v0.19.18 --denylist <path> --dry-run       # verify only, no gates
    python3 tools/publish_public.py v0.19.18 --denylist <path> --from npm      # resume after a partial run

Owner decision 2026-07-29 (amending the runbook's earlier "publishing is
never scripted" rule): the pipeline ORCHESTRATES the release, but every
irreversible act — TestPyPI upload, PyPI upload, npm publish, public git
push — stops at an interactive gate that states what has been verified, what
is about to happen and why it cannot be undone, and proceeds only when the
operator types the exact version string. Automation composes the steps; the
human still performs each act.

Why this exists (measured, 2026-07-29): the manual chain shipped v0.19.17 to
PyPI WITHOUT the Windows fixes that were already committed — the tag was cut
one commit too early and nothing cross-checked tag content against intent.
The same day's post-mortem found two more latent classes: the clean-room
export copies file BODIES from the working tree (so a concurrent session's
uncommitted work would have shipped), and the only Windows CI signal had been
red for six days on dependabot branches where nobody read it.

Structural guarantees (not gate-dependent):
- Everything is built from a THROWAWAY WORKTREE at the tag. The dev repo's
  working tree — including any concurrent session's uncommitted work — cannot
  reach the artifact.
- The contamination scanner must first find a PLANTED canary (a real term
  from the operator's denylist injected into a scratch copy). A scanner that
  cannot fail is not allowed to pass anything (the 0.16.0 lesson: a blank
  denylist line silently zeroed every scan for twelve releases).
- The built sdist is extracted and scanned again — the artifact that ships,
  not just the tree it was built from.
- The public push happens from a FRESH CLONE of the public repo in a temp
  dir. This repo's `disabled-public-DO-NOT-PUSH` remote is never touched,
  never re-enabled, never pushed (ADR-0001 unchanged).
- No credential is ever read, written, or probed: twine/npm/git run in the
  operator's own authenticated sessions and prompt on their own.

Every phase appends to `_evidence/releases/publish-<version>.md` as it
completes, so an aborted run leaves a truthful partial transcript.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_REMOTE_NAME = "disabled-public-DO-NOT-PUSH"  # fetch URL is the public repo; push URL is disabled and stays so
PUBLIC_REPO = "Autopsias/brainiac"                  # the gh-api slug; the push URL is read from the remote above
DIST_MATRIX_WORKFLOW = "distribution-matrix.yml"

PHASES = [
    "preflight", "worktree", "tests", "export", "build", "windows-ci",
    "testpypi", "pypi", "npm", "public-git", "release-asset", "post-verify",
]


class PublishError(Exception):
    pass


class GateDeclined(Exception):
    pass


def _run(cmd: list[str], *, cwd: Path | None = None, interactive: bool = False,
         timeout: int | None = 1800,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Interactive=True hands the terminal to the child (twine/npm prompting
    for the operator's own credentials — this script never sees them)."""
    if interactive:
        return subprocess.run(cmd, cwd=cwd, timeout=timeout, env=env)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env=env)


AUTH_URL_RE = re.compile(r"https://\S*(?:npmjs\.com|npmjs\.org)/(?:auth|login)\S*")


def _run_pty(cmd: list[str], *, cwd: Path | None = None,
             timeout: int = 900) -> subprocess.CompletedProcess:
    """Run a command attached to a pseudo-terminal, streaming its output.

    npm's one-time-password step has two modes and it picks by asking whether
    stdout is a terminal. On a TTY it prints a click-to-authorize URL and waits
    for the browser round-trip; with no TTY it refuses outright (`EOTP`, "requires
    a one-time password"), which is a dead end for any non-interactive caller —
    a relayed authenticator code expires before a run reaches the publish step.
    A pty gets the URL flow, and the operator authorizes by clicking.

    Output is echoed as it arrives (so the URL is visible in a log being
    tailed, not just at exit) and returned in `stdout` for the caller to scan.
    A `Press ENTER`-style prompt is answered automatically — the browser round
    trip, not the keypress, is the actual human gate.
    """
    master, slave = pty.openpty()
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=slave, stdout=slave, stderr=slave,
                            close_fds=True)
    os.close(slave)
    chunks: list[str] = []
    pending = ""
    deadline = time.monotonic() + timeout
    try:
        while True:
            if time.monotonic() > deadline:
                proc.kill()
                raise PublishError(
                    f"{cmd[0]} timed out after {timeout}s waiting for browser authorization")
            if not select.select([master], [], [], 1.0)[0]:
                if proc.poll() is not None:
                    break
                continue
            try:
                data = os.read(master, 4096)
            except OSError:      # EIO — the child closed the pty
                break
            if not data:
                break
            text = data.decode("utf-8", "replace")
            chunks.append(text)
            sys.stdout.write(text)
            sys.stdout.flush()
            pending = (pending + text)[-400:]
            if re.search(r"press\s+(?:enter|any key)", pending, re.I):
                os.write(master, b"\n")
                pending = ""
    finally:
        os.close(master)
    return subprocess.CompletedProcess(cmd, proc.wait(), "".join(chunks), "")


def _need(proc: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    if proc.returncode != 0:
        out = getattr(proc, "stdout", "") or ""
        err = getattr(proc, "stderr", "") or ""
        raise PublishError(f"{what} failed (exit {proc.returncode})\n{out}\n{err}")
    return proc


# --------------------------------------------------------------------------
# evidence transcript — appended per phase so a crash leaves a partial record
# --------------------------------------------------------------------------

class Evidence:
    def __init__(self, version: str):
        self.path = REPO_ROOT / "_evidence" / "releases" / f"publish-{version}.md"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(
                f"# Publish transcript — {version}\n\n"
                f"Started {_dt.datetime.now().astimezone().isoformat(timespec='seconds')} "
                f"by tools/publish_public.py. Appended per phase; an aborted run\n"
                f"leaves this record truthful and partial.\n\n", encoding="utf-8")

    def record(self, phase: str, status: str, detail: str = "") -> None:
        stamp = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
        body = f"## {phase} — {status}\n{stamp}\n"
        if detail:
            body += f"\n{detail.rstrip()}\n"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(body + "\n")


# --------------------------------------------------------------------------
# the human gate
# --------------------------------------------------------------------------

def gate(version: str, act: str, why: str, verified: list[str], *,
         input_fn=input, dry_run: bool = False,
         confirmed: bool = False, consent_note: str | None = None) -> str:
    """Stop before an irreversible act. Two consent shapes, both deliberate:

    - Terminal run: the operator types the exact version string — a
      keystroke naming what ships, not a 'y'.
    - Harness run (`--confirm <act> --consent-note "..."`): the act was
      pre-authorized by the owner IN THE SAME SESSION (e.g. answering an
      AskUserQuestion decision card after reading the verified summary);
      the note recording who/how goes into the evidence transcript. A
      pre-authorization names ONE act — there is no --confirm-everything.

    Returns a one-line provenance string for the evidence transcript."""
    print(f"\n{'─' * 62}")
    print(f"GATE: {act}")
    print(f"  Why this gate exists: {why}")
    print("  Verified so far:")
    for item in verified:
        print(f"    - {item}")
    if dry_run:
        print("  (--dry-run: stopping here, nothing uploaded or pushed)")
        raise GateDeclined(f"dry-run stop before: {act}")
    if confirmed:
        print(f"  pre-authorized: {consent_note}")
        return f"pre-authorized — {consent_note}"
    if not sys.stdin.isatty():
        raise PublishError(
            f"gate '{act}' needs consent but there is no terminal to ask on — "
            f"either run interactively, or pass --confirm <act> with "
            f"--consent-note '<who authorized it and how>' after the owner "
            f"approved this specific act in-session")
    answer = input_fn(f"  Type the version to proceed [{version}], anything else aborts: ").strip()
    if answer != version:
        raise GateDeclined(f"operator declined at gate: {act} (typed {answer!r})")
    return "operator typed the version at the interactive gate"


# --------------------------------------------------------------------------
# phases
# --------------------------------------------------------------------------

def phase_preflight(tag: str, *, expect_published: bool | None = False) -> str:
    """Tag exists; tag name matches the pyproject version AT THE TAG; and the
    PyPI state matches what the run's starting phase implies.

    `expect_published` is deliberately tri-state, because "is this version
    supposed to be on PyPI already?" has three honest answers:

    * ``False`` — a fresh run. Publishing a version PyPI already serves would
      mean the tag's contents and the published artifact can differ silently.
    * ``None`` — a resume INTO the upload phases (`--from testpypi` / `pypi`).
      The upload may or may not have gone through before the run died; twine
      runs with ``--skip-existing``, so either state is fine and neither is
      evidence of a problem.
    * ``True`` — a resume PAST the upload (`--from npm` / `public-git` /
      `post-verify`). Here the version being absent is the anomaly: skipping an
      upload that never happened would ship an npm bootstrap pointing at
      nothing.

    A single boolean got this wrong in both directions: it dead-ended every
    post-upload resume on the already-published guard, and then dead-ended
    `--from pypi` for the opposite reason.
    """
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise PublishError(f"tag must look like vX.Y.Z, got {tag!r}")
    version = tag[1:]
    _need(_run(["git", "rev-parse", "--verify", f"refs/tags/{tag}"], cwd=REPO_ROOT),
          f"tag {tag} not found locally")
    show = _need(_run(["git", "show", f"{tag}:pyproject.toml"], cwd=REPO_ROOT),
                 f"pyproject.toml missing at {tag}")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', show.stdout)
    tagged_version = m.group(1) if m else None
    if tagged_version != version:
        # THE 0.19.17 failure class: the tag and the code it points at disagree.
        raise PublishError(
            f"tag {tag} points at a commit whose pyproject version is "
            f"{tagged_version!r} — the tag was cut on the wrong commit")
    # PyPI monotonicity — query the public JSON API (read-only, no auth).
    proc = _run(["curl", "-fsS", "--max-time", "30",
                 "https://pypi.org/pypi/brainiac-cli/json"])
    if proc.returncode == 0:
        data = json.loads(proc.stdout)
        released = set(data.get("releases", {}))
        latest = data["info"]["version"]
        if expect_published is None:
            state = "already serves" if version in released else "does not yet serve"
            return (f"tag/version consistent; PyPI {state} {version} "
                    f"(resume into the upload phases — either state is fine)")
        if expect_published:
            if version not in released:
                raise PublishError(
                    f"resuming past the pypi phase, but PyPI does not serve {version} — "
                    f"do not skip an upload that never happened")
            return f"tag/version consistent; PyPI already serves {version} (resume past upload)"
        if version in released:
            raise PublishError(
                f"{version} is already on PyPI — uploads are permanent per "
                f"version; cut a new patch instead of re-publishing")
        def _key(v: str) -> tuple[int, ...]:
            return tuple(int(x) for x in v.split(".")[:3] if x.isdigit())
        if _key(version) <= _key(latest):
            raise PublishError(
                f"{version} is not above PyPI's current latest {latest} — "
                f"publishing it would not become the default install")
        return f"tag/version consistent; PyPI latest {latest}, {version} is new and above it"
    return "tag/version consistent; PyPI unreachable (offline?) — monotonicity NOT verified"


def phase_worktree(tag: str, scratch: Path) -> Path:
    """A throwaway worktree at the tag: the ONLY tree anything downstream
    reads. Structurally excludes the dev repo's (possibly dirty) working tree."""
    wt = scratch / "worktree"
    _need(_run(["git", "worktree", "add", "--detach", str(wt), tag], cwd=REPO_ROOT),
          "worktree add")
    status = _need(_run(["git", "status", "--porcelain"], cwd=wt), "worktree status")
    if status.stdout.strip():
        raise PublishError(f"fresh worktree at {tag} is not clean:\n{status.stdout}")
    return wt


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------



sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.modules.setdefault("tools.publish_public", sys.modules[__name__])
# batch-2 drain: the phase families moved to siblings; every name below is
# re-imported so its `publish_public` module path is unchanged, and each
# sibling resolves its collaborators through THIS module at call time — a
# monkeypatch on publish_public._run (etc.) keeps governing them.
from tools.publish_public_checks import (  # noqa: E402,F401
    _load_denylist_terms, _scan_tree, phase_build, phase_export, phase_tests,
    phase_windows_ci, pytest_failure_summary, scanner_self_test)
from tools.publish_public_uploads import (  # noqa: E402,F401
    _archive_content_diff, _archive_members, _clean_venv_check,
    _non_pypi_index, _poll, _throwaway_venv, build_mcpb, phase_npm,
    phase_post_verify, phase_public_git, phase_pypi, phase_release_asset,
    phase_testpypi, sync_export_into_clone)
from tools.publish_steps import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

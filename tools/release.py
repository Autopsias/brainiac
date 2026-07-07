#!/usr/bin/env python3
"""S06 (REL-02) — the one bump command for the version SSOT.

ADR-0004 Ruling 1: ``pyproject.toml [project].version`` is the single source
of truth; everything else (plugin.json x3, SKILL_VERSION stamps, dist/COMPAT)
is derived or validated-lockstep by ``tools/package_clients.py`` at package
time. This script is the ONE place the SSOT itself is edited:

- ``bump major|minor|patch`` — semver-increment the SSOT.
- ``set <version>`` — jump straight to an explicit version (semver bump can
  only go forward within its own precedence — e.g. from 0.3.0, ``bump minor``
  can only ever reach 0.4.0, never 0.9.0; a reconciliation re-base or a
  deliberate jump needs an explicit target).

Both subcommands then:
1. **monotonic-version guard** (ADR-0005 Ruling 5, GV-01) — refuse if the
   target version is not strictly greater than the release baseline (see
   ``monotonic_baseline`` / ``assert_monotonic``),
2. write the new version into ``pyproject.toml``,
3. roll ``CHANGELOG.md``'s ``## [Unreleased]`` into a dated
   ``## [X.Y.Z] — YYYY-MM-DD`` section (refusing if Unreleased is empty, or if
   the target version already has a section — keep-a-changelog discipline,
   ADR-0004 Ruling 3),
4. run ``tools/package_clients.py`` to propagate the new version into every
   plugin.json + SKILL_VERSION stamp + dist/COMPAT (ADR-0004 Ruling 5).

Usage:
    python3 tools/release.py bump patch
    python3 tools/release.py bump minor
    python3 tools/release.py bump major
    python3 tools/release.py set 0.9.0
    python3 tools/release.py bump patch --dry-run
"""
from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
CHANGELOG_PATH = REPO_ROOT / "CHANGELOG.md"

VERSION_RE = re.compile(r'(?m)^(version\s*=\s*")([^"]+)(")')
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# ADR-0005 Ruling 5: only tags shaped exactly like a plain semver release
# count as a monotonic baseline candidate. Legacy opaque export tags (v1, v2,
# renamed to legacy-export-v1/v2 by this same session) never match this and
# are structurally ignored — no denylist needed, the shape IS the filter.
SEMVER_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class ReleaseError(Exception):
    pass


def read_version(text: str) -> str:
    m = VERSION_RE.search(text)
    if not m:
        raise ReleaseError(f"{PYPROJECT_PATH}: no top-level version = \"...\" found")
    return m.group(2)


def write_version(text: str, new_version: str) -> str:
    return VERSION_RE.sub(lambda m: f"{m.group(1)}{new_version}{m.group(3)}", text, count=1)


def bump_semver(current: str, part: str) -> str:
    m = SEMVER_RE.match(current)
    if not m:
        raise ReleaseError(f"current version {current!r} is not plain semver (X.Y.Z) — use `set` instead")
    major, minor, patch = (int(g) for g in m.groups())
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    elif part == "patch":
        patch += 1
    else:  # pragma: no cover - argparse choices already guard this
        raise ReleaseError(f"unknown bump part {part!r}")
    return f"{major}.{minor}.{patch}"


def _version_key(v: str):
    """``packaging.version.Version`` when available, else an integer-tuple
    fallback (same posture as ``brain.doctor._version_key`` — never a naive
    string compare, which fails at exactly the next release: 0.9.1 -> 0.10.0)."""
    try:
        from packaging.version import Version

        return Version(v)
    except Exception:
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)", v)
        if m:
            return tuple(int(x) for x in m.groups())
        return (0, 0, 0)


def list_semver_tags(*, cwd: Path = REPO_ROOT) -> list[str]:
    """Local ``git tag`` output, unsorted. Split out for test injection so the
    guard's tag-selection logic doesn't require a live git repo fixture per
    case."""
    out = subprocess.run(["git", "tag", "-l"], cwd=cwd, check=True, capture_output=True, text=True)
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def monotonic_baseline(current_pyproject_version: str, *, tags: list[str] | None = None, cwd: Path = REPO_ROOT) -> str:
    """ADR-0005 Ruling 5: the highest SEMVER-SHAPED tag (``^v\\d+\\.\\d+\\.\\d+$``),
    compared via ``packaging.version.Version`` — never string order. Legacy
    opaque tags (``v1``, ``v2``) do not match ``SEMVER_TAG_RE`` and are
    structurally ignored. Falls back to the current pyproject version if no
    semver-shaped tag exists yet."""
    if tags is None:
        tags = list_semver_tags(cwd=cwd)
    semver_versions = [m.group(1) for t in tags if (m := re.match(r"^v(\d+\.\d+\.\d+)$", t))]
    if not semver_versions:
        return current_pyproject_version
    return max([*semver_versions, current_pyproject_version], key=_version_key)


def assert_monotonic(new_version: str, baseline: str) -> None:
    """Refuse any target version not strictly greater than the baseline
    (ADR-0005 Ruling 5). Guards LOCAL re-tagging-downward only — publication
    is a human act out of scope (see the ADR). No override flag: a deliberate
    downgrade is a history-rewriting event that needs its own ADR."""
    if _version_key(new_version) <= _version_key(baseline):
        raise ReleaseError(
            f"refusing non-increasing version: target {new_version!r} is not strictly greater "
            f"than the release baseline {baseline!r} (highest semver-shaped local tag, or the "
            "current pyproject version if none exists) — ADR-0005 Ruling 5. There is no override; "
            "a deliberate downgrade needs its own ADR."
        )


def roll_changelog_unreleased(text: str, new_version: str, *, today: str) -> str:
    """Rename ``## [Unreleased]`` to ``## [X.Y.Z] — date`` and open a fresh
    empty Unreleased heading above it. Refuses if Unreleased is empty or the
    target version already has a section (ADR-0004 Ruling 3)."""
    if re.search(rf"(?m)^## \[{re.escape(new_version)}\] ", text):
        raise ReleaseError(f"CHANGELOG.md already has a [{new_version}] section")

    m = re.search(r"(?m)^## \[Unreleased\]\s*\n", text)
    if not m:
        raise ReleaseError("CHANGELOG.md: no '## [Unreleased]' heading found")
    start = m.end()
    next_heading = re.search(r"(?m)^## \[", text[start:])
    body_end = start + next_heading.start() if next_heading else len(text)
    body = text[start:body_end]
    if not body.strip():
        raise ReleaseError("CHANGELOG.md: [Unreleased] section is empty — nothing to release")

    dated_heading = f"## [{new_version}] — {today}\n"
    replacement = f"## [Unreleased]\n\n{dated_heading}"
    return text[:m.start()] + replacement + body + text[body_end:]


def run_packager(*, dry_run: bool) -> None:
    if dry_run:
        print("  (dry-run: skipping tools/package_clients.py)")
        return
    print("  running tools/package_clients.py to propagate the new version ...")
    subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "package_clients.py")], check=True, cwd=REPO_ROOT)


def apply_release(new_version: str, *, dry_run: bool) -> None:
    pyproject_text = PYPROJECT_PATH.read_text(encoding="utf-8")
    current = read_version(pyproject_text)
    if current == new_version:
        raise ReleaseError(f"pyproject.toml is already at {new_version}")

    baseline = monotonic_baseline(current)
    assert_monotonic(new_version, baseline)

    changelog_text = CHANGELOG_PATH.read_text(encoding="utf-8")
    today = datetime.date.today().isoformat()
    new_changelog = roll_changelog_unreleased(changelog_text, new_version, today=today)
    new_pyproject = write_version(pyproject_text, new_version)

    print(f"{current} -> {new_version}")
    if dry_run:
        print("  (dry-run: not writing pyproject.toml / CHANGELOG.md)")
    else:
        PYPROJECT_PATH.write_text(new_pyproject, encoding="utf-8")
        CHANGELOG_PATH.write_text(new_changelog, encoding="utf-8")
        print(f"  wrote {PYPROJECT_PATH.relative_to(REPO_ROOT)}")
        print(f"  wrote {CHANGELOG_PATH.relative_to(REPO_ROOT)} ([{new_version}] — {today})")

    run_packager(dry_run=dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_bump = sub.add_parser("bump", help="semver-increment the SSOT")
    p_bump.add_argument("part", choices=["major", "minor", "patch"])
    p_bump.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")

    p_set = sub.add_parser("set", help="jump the SSOT to an explicit version (e.g. a reconciliation re-base)")
    p_set.add_argument("version", help="target version, e.g. 0.9.0")
    p_set.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")

    args = parser.parse_args()

    try:
        if args.cmd == "bump":
            current = read_version(PYPROJECT_PATH.read_text(encoding="utf-8"))
            new_version = bump_semver(current, args.part)
        else:  # set
            new_version = args.version
            if not SEMVER_RE.match(new_version):
                raise ReleaseError(f"{new_version!r} is not plain semver (X.Y.Z)")
        apply_release(new_version, dry_run=args.dry_run)
    except ReleaseError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"FAIL: tools/package_clients.py exited {exc.returncode}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

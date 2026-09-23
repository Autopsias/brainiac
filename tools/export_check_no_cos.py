#!/usr/bin/env python3
"""Prove a public export carries no COS and still works (ADR 0013).

The COS (Chief of Staff) is the private email assistant. ``COS_EXCLUDES`` in
``tools/export_cleanroom.py`` says which files it is; this checker names no COS
path of its own, so it ships in the public tree and runs there too.

What it judges (the identity is printed on every run):

* no flag         the CURRENT WORKING TREE of the checkout you run it from,
                  exported by that checkout's exporter into a fresh temp folder
                  (gates run before a session commits, so HEAD would be stale);
* ``--rev SHA``   that commit, checked out detached into a fresh temp folder
                  and exported by ITS OWN exporter;
* ``--export-dir DIR``  an export that already exists;
* ``--dist FILE...``    built wheel / sdist members (default checks: paths,
                  imports, attrs — no engine install, no ``tests/`` needed);
* ``--static``    ``imports`` and ``attrs`` only, as a pure AST scan of the
                  working tree: no export, no module import (the fast drift guard).

Checks (``--only a,b``): ``paths`` (no file matches COS_EXCLUDES), ``imports``
and ``attrs`` (no UNGUARDED COS import or ``cos_`` method call on self/core;
on an export every ``brain`` module must also import), ``verbs`` (``brain
--help`` lists no ``cos``/``cos-*`` verb), ``manifests`` (plugin, package and
routine manifests name no COS path or skill), ``smoke`` (the core verbs and
``maintain --json`` on a made-up vault; see ``export_check_no_cos_smoke.py``).

``--report`` prints the boundary instead: every path classified, every link,
BORDERLINE files with a proposed ruling, and COS mention counts per shared file.

Exit 0 = every selected check passed; 1 = a finding; 2 = the check could not run.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_cleanroom as ec  # noqa: E402
from export_check_no_cos_scan import Link, scan_tree  # noqa: E402
from export_check_no_cos_smoke import Smoke, isolated_env  # noqa: E402

CHECKS = ("paths", "imports", "attrs", "verbs", "manifests", "smoke")
DIST_CHECKS = ("paths", "imports", "attrs")
STATIC_CHECKS = ("imports", "attrs")
MANIFEST_NAMES = {"plugin.json", "marketplace.json", "package.json", "manifest.json",
                  "pyproject.toml", "MANIFEST.in", "setup.cfg"}
MENTION = re.compile(r"\bCOS\b|(?i:\bcos[-_/]|chief[- _]of[- _]staff)")
BORDERLINE_MENTIONS = 15
IMPORT_ALL = r"""
import importlib, json, pkgutil, brain
bad = []
try:
    for m in pkgutil.walk_packages(brain.__path__, "brain.", onerror=lambda n: bad.append([n, "walk"])):
        try:
            importlib.import_module(m.name)
        except BaseException as e:
            bad.append([m.name, f"{type(e).__name__}: {e}"])
except BaseException as e:
    bad.append(["(walk)", f"{type(e).__name__}: {e}"])
print(json.dumps({"origin": brain.__file__, "failed": bad}))
"""


class CannotRun(Exception):
    """The judged tree could not be produced; exit 2, never a pass."""


@dataclass
class Target:
    root: Path
    files: list[str]
    identity: str
    kind: str                      # export | static | dist
    work: Path | None = None       # scratch space for subprocesses
    _links: tuple[list[Link], list[str]] | None = field(default=None, repr=False)

    def links(self) -> tuple[list[Link], list[str]]:
        if self._links is None:
            self._links = scan_tree(self.root, self.files, ec.is_cos_path)
        return self._links


def _run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kw)


def _git(repo: Path, *args: str) -> str:
    p = _run(["git", "-C", str(repo), *args])
    if p.returncode != 0:
        raise CannotRun(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def _repo() -> Path:
    return Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").strip())


def _identity(repo: Path) -> str:
    head = _git(repo, "rev-parse", "HEAD").strip()
    dirty = len(_git(repo, "status", "--porcelain").splitlines())
    return f"working tree {repo} at HEAD {head} ({dirty} dirty files)"


def _walk(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")
                  if p.is_file() and str(p.relative_to(root)) != "manifest.json")


def _export(repo: Path, out: Path) -> None:
    exporter = repo / "tools" / "export_cleanroom.py"
    # -B: the exporter imports a sibling module; without it a tools/__pycache__/
    # lands in the tree being judged and _identity counts it as a dirty file.
    p = _run([sys.executable, "-B", str(exporter), "--output", str(out), "--repo-root", str(repo)])
    if p.returncode != 0:
        raise CannotRun(f"export failed: {(p.stderr or p.stdout).strip()[-800:]}")


def target_default(tmp: Path) -> Target:
    repo = _repo()
    _export(repo, tmp / "export")
    return Target(tmp / "export", _walk(tmp / "export"), f"export of {_identity(repo)}", "export", tmp)


def _rev_checkout(tmp: Path, rev: str, cleanup: list) -> tuple[Path, str]:
    """Check ``rev`` out detached into a fresh folder; removed again on exit."""
    repo = _repo()
    sha = _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()
    wt = tmp / "rev"
    _git(repo, "worktree", "add", "--detach", str(wt), sha)
    cleanup.append(lambda: _run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)]))
    return wt, sha


def target_rev(tmp: Path, rev: str, cleanup: list) -> Target:
    wt, sha = _rev_checkout(tmp, rev, cleanup)
    _export(wt, tmp / "export")
    return Target(tmp / "export", _walk(tmp / "export"), f"export of --rev {sha} (its own exporter)", "export", tmp)


def target_static() -> Target:
    repo = _repo()
    return Target(repo, ec.tracked_files(repo), f"static scan of {_identity(repo)}", "static")


def _map_member(name: str, is_wheel: bool) -> str | None:
    if name.endswith("/"):
        return None
    if is_wheel:
        top = name.split("/", 1)[0]
        return name if top.endswith((".dist-info", ".data")) else f"src/{name}"
    return name.split("/", 1)[1] if "/" in name else None


def target_dist(tmp: Path, dists: list[str]) -> list[Target]:
    """One unpack root per artifact: a wheel and an sdist share paths, and one
    root would let a later clean copy overwrite an earlier contaminated one."""
    targets = []
    for i, d in enumerate(dists):
        path, root = Path(d), tmp / "dist" / str(i)
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as z:
                members = [(n, z.read(n)) for n in z.namelist() if not n.endswith("/")]
            files = _unpack(root, members, is_wheel=True)
        elif path.name.endswith((".tar.gz", ".tgz")):
            with tarfile.open(path) as t:
                members = [(m.name, t.extractfile(m).read()) for m in t.getmembers() if m.isfile()]
            files = _unpack(root, members, is_wheel=False)
        else:
            raise CannotRun(f"--dist takes a .whl or .tar.gz, not {d}")
        targets.append(Target(root, sorted(set(files)), f"dist members of {d}", "dist", tmp))
    return targets


def _unpack(root: Path, members: list[tuple[str, bytes]], *, is_wheel: bool) -> list[str]:
    out = []
    for name, data in members:
        rel = _map_member(name, is_wheel)
        if rel is None or rel.startswith("/") or ".." in Path(rel).parts:
            continue
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        out.append(rel)
    return out


# ------------------------------------------------------------------ checks
def check_paths(t: Target) -> list[str]:
    return [f"COS path: {rel}" for rel in t.files if ec.is_cos_path(rel)]


def _import_all(t: Target) -> list[str]:
    src = t.root / "src"
    env = isolated_env(src, t.work / "import-all")
    p = _run([sys.executable, "-B", "-c", IMPORT_ALL], env=env, cwd=t.work, timeout=600)
    if p.returncode != 0:
        return [f"import-all could not run: {p.stderr.strip()[-600:]}"]
    doc = json.loads(p.stdout.strip().splitlines()[-1])
    if not str(doc["origin"]).startswith(str(src)):
        return [f"import-all: brain imports from {doc['origin']}, not the export"]
    return [f"module does not import: {name}: {err}" for name, err in doc["failed"]]


def check_imports(t: Target) -> list[str]:
    links, errors = t.links()
    found = errors + [f"{k.render()}" for k in links if k.kind == "import" and not k.guarded]
    return found + (_import_all(t) if t.kind == "export" else [])


def check_attrs(t: Target) -> list[str]:
    links, _ = t.links()
    return [k.render() for k in links if k.kind == "attr" and not k.guarded]


def help_verbs(src: Path, work: Path) -> tuple[list[str] | None, str]:
    env = isolated_env(src, work / "verbs")
    p = _run([sys.executable, "-B", "-m", "brain.cli", "--help"], env=env, cwd=work, timeout=300)
    groups = re.findall(r"\{([a-z0-9,\-]+)\}", p.stdout)
    if p.returncode != 0 or not groups:
        return None, " | ".join((p.stderr or p.stdout).strip().splitlines()[-3:])
    return max(groups, key=len).split(","), ""


def check_verbs(t: Target) -> list[str]:
    verbs, err = help_verbs(t.root / "src", t.work)
    if verbs is None:
        return [f"`brain --help` failed: {err}"]
    return [f"COS verb: {v}" for v in verbs if v == "cos" or v.startswith("cos-")]


def cos_tokens() -> set[str]:
    """Every spelling of a COS path or COS skill name a manifest could use."""
    toks: set[str] = set()
    for p in ec.COS_EXCLUDES:
        toks.add(p)
        if p.startswith("src/"):
            toks.add(p[len("src/"):])
        rest = p[len("src/brain/"):] if p.startswith("src/brain/") else ""
        if "/" in rest.rstrip("/"):
            toks.add(rest)
        skill = re.search(r"skills/([^/]+)/$", p)
        if skill:
            toks.add(skill.group(1))
    return toks


def check_manifests(t: Target) -> list[str]:
    toks, found = sorted(cos_tokens()), []
    for rel in t.files:
        if Path(rel).name not in MANIFEST_NAMES or ec.is_cos_path(rel):
            continue
        text = (t.root / rel).read_text(encoding="utf-8", errors="replace")
        found += [f"{rel}: names COS {tok!r}" for tok in toks if tok in text]
    return found


def check_smoke(t: Target) -> list[str]:
    smoke = Smoke(t.root / "src", t.work / "smoke")
    findings = smoke.all()
    for line in smoke.log:
        print(f"    {line.splitlines()[0]}")
    return findings


CHECK_FUNCS = {"paths": check_paths, "imports": check_imports, "attrs": check_attrs,
               "verbs": check_verbs, "manifests": check_manifests, "smoke": check_smoke}


def run_checks(targets: list[Target], checks: list[str]) -> int:
    failed: list[str] = []
    for t in targets:
        print(f"export_check_no_cos: judging {t.identity}")
        for name in checks:
            findings = CHECK_FUNCS[name](t)
            print(f"[{name}] {'FAIL' if findings else 'PASS'} ({len(findings)} findings)")
            for f in findings:
                print(f"  {f}")
            if findings and name not in failed:
                failed.append(name)
    print(f"RESULT: {'FAIL (' + ', '.join(failed) + ')' if failed else 'PASS'}")
    return 1 if failed else 0


# ------------------------------------------------------------------ report
def classify(rel: str, text: str | None) -> str:
    if ec.is_cos_path(rel):
        return "COS"
    if rel.startswith(ec.EXCLUDE_PREFIXES) or rel.endswith(ec.EXCLUDE_SUFFIXES):
        return "PRIVATE"
    return "SHARED" if text and MENTION.search(text) else "CORE"


def _text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _rule_lines(files: list[str]) -> list[str]:
    out = ["== COS_EXCLUDES (tools/export_cleanroom.py): matches and near-misses per prefix"]
    for p in ec.COS_EXCLUDES:
        hits = [f for f in files if f.startswith(p)]
        stem = p.rstrip("_-/")
        near = [f for f in files if f.startswith(stem) and not ec.is_cos_path(f)]
        out.append(f"{len(hits):5d}  {p}   non-COS files sharing the stem: {near or 'none'}")
    return out


def _borderline(files, classes, counts, links) -> list[str]:
    out = ["== BORDERLINE (proposed rulings; the owner rules at the s02 checkpoint)"]
    linked = sorted({k.path for k in links})
    for rel in linked:
        n = sum(1 for k in links if k.path == rel)
        out.append(f"{rel}  [{n} link(s)]  proposed: stays core; guard every link in an ADR 0013 shape")
    for rel in sorted(r for r in files if classes[r] == "SHARED" and counts[r] >= BORDERLINE_MENTIONS
                      and r not in linked):
        out.append(f"{rel}  [{counts[rel]} COS mentions]  proposed: shared text; owner picks keep or "
                   "strip-in-export for all shared files at once")
    return out


def report(root: Path, files: list[str], identity: str) -> str:
    texts = {rel: _text(root / rel) for rel in files}
    classes = {rel: classify(rel, texts[rel]) for rel in files}
    counts = {rel: len(MENTION.findall(texts[rel] or "")) for rel in files}
    shipped = [r for r in files if classes[r] in ("CORE", "SHARED")]
    links, errors = scan_tree(root, shipped, ec.is_cos_path)
    tally = {c: sum(1 for v in classes.values() if v == c) for c in ("COS", "PRIVATE", "SHARED", "CORE")}
    lines = [f"export_check_no_cos --report: {identity}",
             f"{len(files)} tracked paths: " + ", ".join(f"{c} {n}" for c, n in tally.items()),
             "classes: COS = matches COS_EXCLUDES; PRIVATE = already left out by ADR 0001 rules; "
             "SHARED = ships and mentions COS; CORE = ships, no COS mention", ""]
    lines += _rule_lines(files) + ["", f"== core-to-COS links ({len(links)}; "
                                      f"{sum(not k.guarded for k in links)} unguarded)"]
    lines += [k.render() for k in links] + errors + [""]
    lines += _borderline(files, classes, counts, links) + ["", "== COS mention counts per SHARED file"]
    lines += [f"{counts[r]:6d}  {r}" for r in sorted(shipped, key=lambda r: (-counts[r], r))
              if classes[r] == "SHARED"]
    lines += ["", "== classification of every path"] + [f"{classes[r]:8s} {r}" for r in files]
    return "\n".join(lines) + "\n"


def _report_sources(args, tmp: Path, cleanup: list) -> list[tuple[Path, list[str], str]]:
    if args.export_dir:
        root = Path(args.export_dir).resolve()
        return [(root, _walk(root), f"export dir {root}")]
    if args.dist:
        return [(t.root, t.files, t.identity) for t in target_dist(tmp, args.dist)]
    if args.rev:
        wt, sha = _rev_checkout(tmp, args.rev, cleanup)
        return [(wt, _git(wt, "ls-files").splitlines(), f"tracked tree of --rev {sha}")]
    repo = _repo()
    return [(repo, _git(repo, "ls-files").splitlines(), _identity(repo))]


# ------------------------------------------------------------------ main
def _parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--rev", help="judge this commit, exported by its own exporter")
    src.add_argument("--export-dir", help="judge an existing export folder")
    src.add_argument("--dist", nargs="+", help="judge wheel / sdist members")
    src.add_argument("--static", action="store_true", help="AST-only imports+attrs on the working tree")
    ap.add_argument("--only", help=f"comma list from: {','.join(CHECKS)}")
    ap.add_argument("--report", action="store_true", help="print the boundary report instead of checking")
    return ap.parse_args(argv)


def _selected(args) -> list[str]:
    default = STATIC_CHECKS if args.static else DIST_CHECKS if args.dist else CHECKS
    if not args.only:
        return list(default)
    chosen = [c.strip() for c in args.only.split(",") if c.strip()]
    bad = [c for c in chosen if c not in CHECKS]
    if bad:
        raise SystemExit(f"unknown check(s): {bad}; choose from {CHECKS}")
    return [c for c in chosen if not args.static or c in STATIC_CHECKS]


def _targets(args, tmp: Path, cleanup: list) -> list[Target]:
    if args.static:
        return [target_static()]
    if args.rev:
        return [target_rev(tmp, args.rev, cleanup)]
    if args.dist:
        return target_dist(tmp, args.dist)
    if args.export_dir:
        root = Path(args.export_dir).resolve()
        if not (root / "src" / "brain").is_dir():
            raise CannotRun(f"{root} holds no src/brain; not an export")
        return [Target(root, _walk(root), f"export dir {root}", "export", tmp)]
    return [target_default(tmp)]


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    checks = _selected(args)
    tmp = Path(tempfile.mkdtemp(prefix="export-no-cos-"))
    cleanup: list = []
    try:
        if args.report:
            for src in _report_sources(args, tmp, cleanup):
                sys.stdout.write(report(*src))
            return 0
        return run_checks(_targets(args, tmp, cleanup), checks)
    except CannotRun as exc:
        print(f"export_check_no_cos: CANNOT RUN: {exc}", file=sys.stderr)
        return 2
    finally:
        for fn in cleanup:
            fn()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

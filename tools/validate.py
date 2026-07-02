#!/usr/bin/env python3
"""Profile A substrate conventions validator.

Checks the conventions defined in AGENTS.md / docs/substrate-spec.md against a
vault directory, reports default-deny (unlabelled) notes, and can regenerate
brain/backlinks.md.

Usage:
    python3 tools/validate.py <vault-dir>              # validate (exit 0 = clean)
    python3 tools/validate.py <vault-dir> --backlinks  # also regen backlinks.md
    python3 tools/validate.py <vault-dir> --okf        # also run optional OKF lint

Stdlib-only frontmatter parser with an optional PyYAML upgrade — runs on a bare
system python3 (e.g. a sandbox where the project .venv is broken).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CLASSIFICATIONS = ["Public", "Internal", "Confidential", "Restricted", "Secret"]
REQUIRED_BRAIN = {"id", "title", "type", "classification", "created", "updated"}
REQUIRED_RAW = {"id", "type", "classification", "captured", "origin", "immutable"}
# Frontmatter keys recognised by the optional OKF-aligned lint profile.
OKF_ALLOWED_KEYS = REQUIRED_BRAIN | REQUIRED_RAW | {
    "source", "tags", "sha256", "status", "provenance", "related",
}
JD_FILENAME = re.compile(r"^\d\d[. ]")          # Johnny-Decimal, e.g. "60.03 x"
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]")

errors: list[str] = []
warnings: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def split_frontmatter(text: str) -> tuple[str, str] | None:
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    return parts[1], parts[2]


def parse_frontmatter(block: str) -> dict:
    """Try PyYAML; fall back to a minimal flat key:value + inline-list parser."""
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(block)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    data: dict = {}
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line or line[0] in " \t-":
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            data[key] = [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
        else:
            data[key] = val.strip().strip("'\"")
    return data


def iter_md(root: Path):
    for p in sorted(root.rglob("*.md")):
        if "/.brain/" in str(p):
            continue
        yield p


def check_note(path: Path, zone: str, okf: bool) -> dict | None:
    text = path.read_text(encoding="utf-8")
    fm = split_frontmatter(text)
    rel = path.as_posix()
    if fm is None:
        err(f"{rel}: missing YAML frontmatter")
        return None
    meta = parse_frontmatter(fm[0])

    if JD_FILENAME.match(path.name):
        err(f"{rel}: Johnny-Decimal filename not allowed ({path.name})")

    required = REQUIRED_RAW if zone == "raw" else REQUIRED_BRAIN
    missing = required - set(meta)
    if missing:
        err(f"{rel}: missing required frontmatter keys: {sorted(missing)}")

    cls = meta.get("classification")
    if cls not in CLASSIFICATIONS:
        # Default-deny: unlabelled / unrecognised -> treated as Secret, withheld.
        warn(f"{rel}: classification '{cls}' -> DEFAULT-DENY (treated as Secret, "
             f"not surfaceable until labelled)")

    if zone == "raw":
        if str(meta.get("immutable")).lower() not in ("true", "yes", "1"):
            err(f"{rel}: raw source must carry immutable: true")
        if not meta.get("sha256"):
            err(f"{rel}: raw source must carry sha256")

    if okf:
        unknown = set(meta) - OKF_ALLOWED_KEYS
        if unknown:
            warn(f"[okf] {rel}: keys outside OKF profile: {sorted(unknown)}")

    return {"path": path, "rel": rel, "meta": meta, "body": fm[1], "zone": zone}


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    vault = Path(args[0]).resolve()
    do_backlinks = "--backlinks" in args
    okf = "--okf" in args
    if not vault.is_dir():
        print(f"error: {vault} is not a directory")
        return 2

    raw_dir, brain_dir = vault / "raw", vault / "brain"
    if not raw_dir.is_dir():
        err("vault/raw/ missing")
    if not brain_dir.is_dir():
        err("vault/brain/ missing")
    if not (brain_dir / "index.md").is_file():
        err("vault/brain/index.md missing")

    notes: list[dict] = []
    for p in iter_md(raw_dir) if raw_dir.is_dir() else []:
        n = check_note(p, "raw", okf)
        if n:
            notes.append(n)
    for p in iter_md(brain_dir) if brain_dir.is_dir() else []:
        if p.name == "backlinks.md":
            continue
        n = check_note(p, "brain", okf)
        if n:
            notes.append(n)

    # Wikilink resolution (warn-only) + backlink map.
    ids = {n["meta"].get("id") for n in notes if n["meta"].get("id")}
    backlinks: dict[str, set[str]] = {}
    for n in notes:
        src = n["meta"].get("id")
        for m in WIKILINK.finditer(n["body"]):
            target = m.group(1).strip()
            if target not in ids:
                warn(f"{n['rel']}: wikilink [[{target}]] does not resolve")
            else:
                backlinks.setdefault(target, set()).add(src)

    if do_backlinks:
        lines = [
            "---", "id: backlinks", "title: \"Backlinks (generated)\"",
            "type: index", "classification: Internal", "---", "",
            "# Backlinks (generated — do not hand-edit)", "",
        ]
        title_by_id = {n["meta"].get("id"): n["meta"].get("title", n["meta"].get("id"))
                       for n in notes}
        for tgt in sorted(backlinks):
            lines.append(f"## [[{tgt}]]")
            for s in sorted(x for x in backlinks[tgt] if x):
                lines.append(f"- [[{s}|{title_by_id.get(s, s)}]]")
            lines.append("")
        (brain_dir / "backlinks.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"wrote {brain_dir.as_posix()}/backlinks.md "
              f"({len(backlinks)} targets)")

    print(f"\nchecked {len(notes)} notes "
          f"({sum(1 for n in notes if n['zone']=='raw')} raw, "
          f"{sum(1 for n in notes if n['zone']=='brain')} brain)")
    for w in warnings:
        print(f"  WARN  {w}")
    for e in errors:
        print(f"  ERROR {e}")
    deny = sum(1 for n in notes
               if n["meta"].get("classification") not in CLASSIFICATIONS)
    print(f"\nsummary: {len(errors)} errors, {len(warnings)} warnings, "
          f"{deny} default-denied (unlabelled) notes")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Profile A substrate conventions validator.

Checks the conventions defined in AGENTS.md / docs/substrate-spec.md against a
vault directory, reports default-deny (unlabelled) notes, and can regenerate
brain/backlinks.md.

Usage:
    python3 tools/validate.py <vault-dir>              # validate (exit 0 = clean)
    python3 tools/validate.py <vault-dir> --backlinks  # also regen backlinks.md
    python3 tools/validate.py <vault-dir> --catalogs   # also regen per-zone catalog.md
    python3 tools/validate.py <vault-dir> --okf        # also run optional OKF lint

Stdlib-only frontmatter parser with an optional PyYAML upgrade — runs on a bare
system python3 (e.g. a sandbox where the project .venv is broken).
"""
from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path

CLASSIFICATIONS = ["Public", "Internal", "Confidential", "Restricted", "MNPI"]
REQUIRED_BRAIN = {"id", "title", "type", "classification", "created", "updated"}
REQUIRED_RAW = {"id", "type", "classification", "captured", "origin", "immutable"}
# Bitemporal keys (ADR-0003 ruling 2, TMP-01) — all optional; existing notes
# with none of these keys validate exactly as before.
BITEMPORAL_DATE_KEYS = ("document_date", "effective_date", "superseded_date")
BITEMPORAL_LINK_KEYS = ("superseded_by", "previous_version", "replaces")
BITEMPORAL_KEYS = set(BITEMPORAL_DATE_KEYS) | set(BITEMPORAL_LINK_KEYS) | {"is_latest_version"}
# Typed entity vocabulary (ADR-0003 ruling 3, TMP-04) — kernel enum extension.
# Core four (note/index/moc/source-derived) stay the brain/ default; the seven
# entity types are additive, nothing forces them on a vault. `source` remains
# the raw/-zone-only type and does NOT join the brain/ entity vocabulary.
CORE_BRAIN_TYPES = {"note", "index", "moc", "source-derived"}
ENTITY_TYPES = {"person", "company", "project", "meeting", "decision", "concept", "daily"}
BRAIN_TYPES = CORE_BRAIN_TYPES | ENTITY_TYPES
RAW_TYPES = {"source"}
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# TMP-05 — type-specific lint (warn-only): concept notes must carry a
# counter-arguments section; decision notes must anchor claims to a source
# (either a `source:` frontmatter key or a wikilink resolving to a raw/ note).
COUNTER_ARGUMENTS_HEADING = re.compile(r"^#{1,6}\s*counter[- ]?argument", re.I | re.M)
# Frontmatter keys recognised by the optional OKF-aligned lint profile.
OKF_ALLOWED_KEYS = REQUIRED_BRAIN | REQUIRED_RAW | BITEMPORAL_KEYS | {
    "source", "tags", "sha256", "status", "provenance", "related",
}
JD_FILENAME = re.compile(r"^\d\d[. ]")          # Johnny-Decimal, e.g. "60.03 x"
# Alias matched non-greedily, right-anchored to the FINAL ]] so an alias with
# nested brackets (e.g. "display [x]") doesn't drop the link (M-5; mirrors
# brain.graph._WIKILINK — fix both copies).
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:\|.+?)?\]\]")

# HYG-03 (ADR-0003) — the four PARA zones inside brain/ that get a generated
# --catalogs catalog.md (light PARA remains the only directory taxonomy).
PARA_ZONES = ("projects", "areas", "resources", "archive")
# HYG-03 — state-MOC / index.md freshness-stamp pattern: any heading whose
# very next non-blank line is "Updated: YYYY-MM-DD" is a freshness-stamped
# section (the state-MOC template's "## Section: ..." headings, and index.md's
# own zone headings once stamped). Warn-only, never blocks the gate.
SECTION_UPDATED = re.compile(r"^Updated:\s*(\d{4}-\d{2}-\d{2})\s*$")
# ponytail: no threshold is pinned in ADR-0003 for state-MOC sections
# specifically; reuses the ADR's one existing staleness precedent
# (DEFAULT_AUTORESEARCH_STALE_DAYS in src/brain/maintenance.py, also 90) so
# the vault has one staleness convention instead of two. Bump here if a
# tighter cadence turns out to matter more for live "state of play" notes.
STATE_MOC_STALE_DAYS = 90


def link_id(val) -> str | None:
    """Extract a bare note id from a raw id string or a "[[id]]"/"[[id|alias]]" wikilink."""
    if not isinstance(val, str) or not val.strip():
        return None
    m = WIKILINK.match(val.strip())
    return m.group(1).strip() if m else val.strip()


def parse_bool(val):
    """Return True/False for a real or string bool, None if not parseable as one."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        v = val.strip().lower()
        if v in ("true", "yes"):
            return True
        if v in ("false", "no"):
            return False
    return None

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


def iter_md(root: Path, vault: Path):
    for p in sorted(root.rglob("*.md")):
        s = str(p)
        if "/.brain/" in s:
            continue
        try:
            rel_parts = p.relative_to(vault).parts
        except ValueError:
            rel_parts = ()
        # C4: anchored to the vault-relative TOP-LEVEL segment only — a prior
        # unanchored "/inbox/" substring match wrongly excluded (and broke the
        # conventions gate for) any note under a directory named "inbox" at
        # ANY depth, e.g. brain/resources/inbox/reading-list.md.
        if rel_parts and rel_parts[0] == "inbox":
            continue
        # C5: raw/originals/ holds archived, immutable ingestion originals —
        # evidence, never a note; never part of the conventions gate.
        if rel_parts[:2] == ("raw", "originals"):
            continue
        yield p


def check_bitemporal_note(rel: str, meta: dict) -> None:
    """Per-note bitemporal checks (ADR-0003 ruling 2). All keys optional; a note
    carrying none of them is untouched by this function."""
    if not (BITEMPORAL_KEYS & set(meta)):
        return

    for key in BITEMPORAL_DATE_KEYS:
        val = meta.get(key)
        if val is not None and not ISO_DATE.match(str(val)):
            err(f"{rel}: {key} must be ISO-8601 (YYYY-MM-DD), got {val!r}")

    ilv_raw = meta.get("is_latest_version")
    ilv = parse_bool(ilv_raw) if ilv_raw is not None else None
    if ilv_raw is not None and ilv is None:
        err(f"{rel}: is_latest_version must be a boolean, got {ilv_raw!r}")

    superseded_by = link_id(meta.get("superseded_by"))
    if meta.get("superseded_by") is not None and superseded_by is None:
        err(f"{rel}: superseded_by must be a note id or [[wikilink]]")

    if ilv is False and not superseded_by:
        err(f"{rel}: is_latest_version: false requires superseded_by")
    if meta.get("superseded_date") and not superseded_by:
        err(f"{rel}: superseded_date requires superseded_by")

    nid = meta.get("id")
    if superseded_by and nid and superseded_by == nid:
        err(f"{rel}: a note may not supersede itself (superseded_by == id)")

    if superseded_by and ilv_raw is None:
        warn(f"{rel}: superseded_by present but is_latest_version is absent "
             f"(should be explicit false)")


def check_bitemporal_global(notes: list[dict]) -> None:
    """Cross-note supersession invariants (ADR-0003 ruling 2 + hardening rounds).

    Builds a directed graph old -> new from every declared supersession edge
    (either side may declare it: old.superseded_by, or new.previous_version /
    new.replaces) and checks:
      - superseded_by / previous_version / replaces resolve to a real note id
      - reciprocal link present (warn-only)
      - both sides of every edge carry an explicit classification (error)
      - no cycles, no forks (two successors claiming the same predecessor),
        no re-superseding an already-superseded note, at most one
        is_latest_version: true per chain
    """
    by_id = {n["meta"].get("id"): n for n in notes if n["meta"].get("id")}
    edges: set[tuple[str, str]] = set()  # (old, new)

    def add_edge(old: str | None, new: str | None, rel: str, field: str) -> None:
        if old is None or new is None:
            return
        if old not in by_id:
            err(f"{rel}: {field} [[{old}]] does not resolve to a note")
            return
        if new not in by_id:
            err(f"{rel}: {field} [[{new}]] does not resolve to a note")
            return
        edges.add((old, new))

    for n in notes:
        meta, rel, nid = n["meta"], n["rel"], n["meta"].get("id")
        sb = link_id(meta.get("superseded_by"))
        if sb:
            add_edge(nid, sb, rel, "superseded_by")
        for field in ("previous_version", "replaces"):
            prev = link_id(meta.get(field))
            if prev:
                add_edge(prev, nid, rel, field)

    # Reciprocal-link warning: old.superseded_by <-> new.previous_version/replaces.
    for old, new in edges:
        old_meta, new_meta = by_id[old]["meta"], by_id[new]["meta"]
        new_declares_back = link_id(new_meta.get("previous_version")) == old or \
            link_id(new_meta.get("replaces")) == old
        old_declares_fwd = link_id(old_meta.get("superseded_by")) == new
        if old_declares_fwd and not new_declares_back:
            warn(f"{by_id[old]['rel']}: superseded_by [[{new}]] has no reciprocal "
                 f"previous_version/replaces on the successor")

    # Classification presence on both sides of every supersession link.
    for old, new in edges:
        for side, side_id in (("predecessor", old), ("successor", new)):
            cls = by_id[side_id]["meta"].get("classification")
            if cls not in CLASSIFICATIONS:
                err(f"{by_id[side_id]['rel']}: {side_id} is part of a supersession "
                    f"chain ({old} -> {new}) but has no explicit classification")

    # Fork / re-supersession: an old id may point to exactly one new id.
    successors_of: dict[str, set[str]] = {}
    for old, new in edges:
        successors_of.setdefault(old, set()).add(new)
    for old, news in successors_of.items():
        if len(news) > 1:
            err(f"{by_id[old]['rel']}: {old} is superseded by more than one note "
                f"({sorted(news)}) — fork / re-supersession of an already-"
                f"superseded note is not allowed")

    # Cycle detection (DFS over the old->new graph).
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {i: WHITE for i in by_id}
    adj: dict[str, set[str]] = {}
    for old, new in edges:
        adj.setdefault(old, set()).add(new)

    def has_cycle(u: str, stack: list[str]) -> bool:
        color[u] = GRAY
        stack.append(u)
        for v in adj.get(u, ()):
            if color.get(v) == GRAY:
                cyc = " -> ".join(stack[stack.index(v):] + [v])
                err(f"supersession cycle detected: {cyc}")
                return True
            if color.get(v) == WHITE and has_cycle(v, stack):
                return True
        stack.pop()
        color[u] = BLACK
        return False

    for nid in list(by_id):
        if color.get(nid) == WHITE:
            has_cycle(nid, [])

    # Connected components (undirected) -> at most one is_latest_version: true.
    undirected: dict[str, set[str]] = {}
    for old, new in edges:
        undirected.setdefault(old, set()).add(new)
        undirected.setdefault(new, set()).add(old)
    seen: set[str] = set()
    for start in undirected:
        if start in seen:
            continue
        component = set()
        stack = [start]
        while stack:
            u = stack.pop()
            if u in component:
                continue
            component.add(u)
            stack.extend(undirected.get(u, ()) - component)
        seen |= component
        latest = [i for i in component
                  if parse_bool(by_id[i]["meta"].get("is_latest_version")) is True]
        if len(latest) > 1:
            err(f"supersession chain {sorted(component)} has more than one "
                f"is_latest_version: true note ({sorted(latest)})")


def check_type_lint(notes: list[dict]) -> None:
    """TMP-05 type-specific quality lint, warn-only (ADR-0003 ruling 3).

    - concept notes must carry a counter-arguments section.
    - decision notes must anchor their claim to a source: either a `source:`
      frontmatter key, or at least one wikilink resolving to a raw/ note.
    """
    raw_ids = {n["meta"].get("id") for n in notes if n["zone"] == "raw"}
    for n in notes:
        meta, rel, body = n["meta"], n["rel"], n["body"]
        ntype = meta.get("type")
        if ntype == "concept" and not COUNTER_ARGUMENTS_HEADING.search(body):
            warn(f"{rel}: concept note has no Counter-Arguments section")
        if ntype == "decision":
            if meta.get("source"):
                continue
            linked = {m.group(1).strip() for m in WIKILINK.finditer(body)}
            if not (linked & raw_ids):
                warn(f"{rel}: decision note has no source anchor "
                     f"(no source: key and no wikilink to a raw/ note)")


def check_section_staleness(notes: list[dict], today: object = None) -> None:
    """HYG-03 — state-MOC freshness-stamp lint (warn-only). Any heading whose
    next non-blank line is ``Updated: YYYY-MM-DD`` is a freshness-stamped
    section; flag it once it is older than STATE_MOC_STALE_DAYS. Applies to
    every brain/ note generically (the state-MOC template's ``## Section:``
    headings, and index.md's own stamped zone headings) — not gated on
    ``type: moc`` because index.md is ``type: index``."""
    today = today or datetime.date.today()
    for n in notes:
        if n["zone"] != "brain":
            continue
        lines = n["body"].splitlines()
        for i, line in enumerate(lines):
            if not line.lstrip().startswith("#"):
                continue
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j >= len(lines):
                continue
            m = SECTION_UPDATED.match(lines[j].strip())
            if not m:
                continue
            stamped = datetime.date.fromisoformat(m.group(1))
            age = (today - stamped).days
            if age > STATE_MOC_STALE_DAYS:
                heading = line.lstrip("#").strip()
                warn(f"{n['rel']}: section '{heading}' stale "
                     f"({age}d since {stamped.isoformat()}, threshold {STATE_MOC_STALE_DAYS}d)")


def build_zone_catalog(zone: str, zone_notes: list[dict]) -> str:
    """HYG-03 — a per-PARA-zone GENERATED catalog (do not hand-edit), the
    same posture as backlinks.md: derived purely from note frontmatter, so
    re-running --catalogs on an unchanged vault produces byte-identical
    output (deterministic, no wall-clock timestamp baked in)."""
    lines = [
        "---", f"id: catalog-{zone}", f"title: \"{zone.capitalize()} catalog (generated)\"",
        "type: index", "classification: Internal", "---", "",
        f"# {zone.capitalize()} catalog (generated — do not hand-edit)", "",
        "| id | title | type | updated | classification |",
        "|---|---|---|---|---|",
    ]
    for n in sorted(zone_notes, key=lambda n: n["meta"].get("id") or ""):
        meta = n["meta"]
        lines.append(
            f"| [[{meta.get('id', '')}]] | {meta.get('title', '')} | "
            f"{meta.get('type', '')} | {meta.get('updated', '')} | {meta.get('classification', '')} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


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
        # Default-deny: unlabelled / unrecognised -> treated as MNPI, withheld.
        warn(f"{rel}: classification '{cls}' -> DEFAULT-DENY (treated as MNPI, "
             f"not surfaceable until labelled)")

    if zone == "raw":
        if str(meta.get("immutable")).lower() not in ("true", "yes", "1"):
            err(f"{rel}: raw source must carry immutable: true")
        if not meta.get("sha256"):
            err(f"{rel}: raw source must carry sha256")

    check_bitemporal_note(rel, meta)

    ntype = meta.get("type")
    if ntype:
        accepted = RAW_TYPES if zone == "raw" else BRAIN_TYPES
        if ntype not in accepted:
            warn(f"{rel}: unrecognized type {ntype!r} for {zone}/ "
                 f"(accepted: {sorted(accepted)})")

    if okf:
        unknown = set(meta) - OKF_ALLOWED_KEYS
        if unknown:
            warn(f"[okf] {rel}: keys outside OKF profile: {sorted(unknown)}")

    return {"path": path, "rel": rel, "meta": meta, "body": fm[1], "zone": zone}


def main() -> int:
    # ponytail: module-level accumulators reset per call so tests (and any
    # in-process re-run) don't leak errors/warnings across invocations.
    errors.clear()
    warnings.clear()
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    vault = Path(args[0]).resolve()
    do_backlinks = "--backlinks" in args
    do_catalogs = "--catalogs" in args
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
    for p in iter_md(raw_dir, vault) if raw_dir.is_dir() else []:
        n = check_note(p, "raw", okf)
        if n:
            notes.append(n)
    for p in iter_md(brain_dir, vault) if brain_dir.is_dir() else []:
        if p.name in ("backlinks.md", "catalog.md"):
            continue
        n = check_note(p, "brain", okf)
        if n:
            notes.append(n)

    check_bitemporal_global(notes)
    check_type_lint(notes)
    check_section_staleness(notes)

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

    if do_catalogs and brain_dir.is_dir():
        by_zone: dict[str, list[dict]] = {z: [] for z in PARA_ZONES}
        for n in notes:
            if n["zone"] != "brain":
                continue
            rel_parts = n["path"].relative_to(brain_dir).parts
            if len(rel_parts) > 1 and rel_parts[0] in by_zone:
                by_zone[rel_parts[0]].append(n)
        for zone in PARA_ZONES:
            zone_dir = brain_dir / zone
            zone_dir.mkdir(parents=True, exist_ok=True)
            (zone_dir / "catalog.md").write_text(build_zone_catalog(zone, by_zone[zone]), encoding="utf-8")
            print(f"wrote {zone_dir.as_posix()}/catalog.md ({len(by_zone[zone])} notes)")

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

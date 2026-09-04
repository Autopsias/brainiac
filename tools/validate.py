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
import unicodedata
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
# Email provenance (PRV-01/PRV-02) — FLAT DOTTED keys, siblings of the
# pre-existing capture stamp `provenance.trust: untrusted`. All optional; a
# note carrying none of them validates exactly as before. `provenance.verified`
# is host-only (set when the ingest pipeline parsed the metadata out of the
# archived original itself); everything else may be an unverified claim.
PROVENANCE_PREFIX = "provenance."
PROVENANCE_DATE_KEYS = ("provenance.sent",)
PROVENANCE_BOOL_KEYS = ("provenance.verified",)
PROVENANCE_KEYS = set(PROVENANCE_DATE_KEYS) | set(PROVENANCE_BOOL_KEYS) | {
    "provenance.trust", "provenance.sender", "provenance.conversation_id",
    "provenance.subject",
    # DLV-09 — the producing surface that created this note
    # (`inbox-deliverables`, or a kernel skill's name). Stamped automatically
    # and INDEPENDENTLY of `deliverable:`, which is the judgment step: the
    # difference between the two is exactly the "produced but unmarked" set.
    "provenance.produced_by",
}
# DLV-01 — the deliverable marker. Both optional, both valid on a note of ANY
# type, and deliberately NOT members of the `type:` vocabulary: `type` is
# single-valued and load-bearing for retrieval (`type: decision` IS the
# decision layer, selected by exact equality; `type: project` drives PARA
# filing), so retagging a produced decision document would remove it from the
# decision layer. A produced decision stays `type: decision` and gains a key.
DELIVERABLE_KEYS = ("deliverable", "project")
# Frontmatter keys recognised by the optional OKF-aligned lint profile.
OKF_ALLOWED_KEYS = REQUIRED_BRAIN | REQUIRED_RAW | BITEMPORAL_KEYS | PROVENANCE_KEYS | set(
    DELIVERABLE_KEYS
) | {
    "source", "tags", "sha256", "status", "provenance", "related", "aliases",
    # ENF-04 — the ingest cross-tier guard's verdict, stamped on every note the
    # ingest pipeline writes (`brain.ingest.tierguard`). The status is on EVERY
    # such note, not only raises, so an unraised note proves the guard ran.
    "classification_guard", "classification_guard_leg",
    "classification_guard_reason",
    # M-3: flat scalar keys the ingest concealment scan stamps on every note it writes (absent = predates the scanner).
    *(f"injection_assessment.{k}" for k in ("scanner", "verdict", "markers", "hidden", "containers", "hidden_markers", "concealment_scan")),
}
JD_FILENAME = re.compile(r"^\d\d[. ]")          # Johnny-Decimal, e.g. "60.03 x"
# Alias matched non-greedily, right-anchored to the FINAL ]] so an alias with
# nested brackets (e.g. "display [x]") doesn't drop the link (M-5; mirrors
# brain.graph._WIKILINK — fix both copies).
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:\|.+?)?\]\]")

# HYG-03 (ADR-0003) — the four PARA zones inside brain/ that get a generated
# --catalogs catalog.md (light PARA remains the only directory taxonomy).
PARA_ZONES = ("projects", "areas", "resources", "archive")


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


# Mirrors brain.frontmatter._FENCE — the terminator is a LINE that is exactly
# ``---``, never a ``---`` inside one. If these two disagree, the engine and the
# validator disagree about note shape, which is what this file exists to prevent.
_FENCE = re.compile(r"^---(?=\r?$)", re.MULTILINE)


def split_frontmatter(text: str) -> tuple[str, str] | None:
    if not text.startswith("---"):
        return None
    match = _FENCE.search(text, 3)
    if match is None:
        return None
    return text[3:match.start()], text[match.end():]


def _strip_inline_comment(val: str) -> str:
    val = val.strip()
    if val[:1] in "'\"":
        return val
    idx = val.find(" #")
    return val[:idx].rstrip() if idx != -1 else val


def _unquote(val: str) -> str:
    val = _strip_inline_comment(val).strip()
    if len(val) >= 2 and val[:1] in "'\"" and val[-1:] == val[:1]:
        return val[1:-1]
    return val.strip("'\"")


def _inline_list(inner: str) -> list[str]:
    """Parse the limited quoted inline-list form the fallback promises."""
    out: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    for char in inner:
        if quote:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
            current.append(char)
        elif char == ",":
            item = "".join(current).strip()
            if item:
                out.append(_unquote(item))
            current = []
        else:
            current.append(char)
    item = "".join(current).strip()
    if item:
        out.append(_unquote(item))
    return out


def parse_frontmatter(block: str) -> dict:
    """Try PyYAML; fall back to scalar, inline-list, and alias block-list YAML."""
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(block)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    data: dict = {}
    block_aliases = False
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if block_aliases and line[0] in " \t" and stripped.startswith("-"):
            data.setdefault("aliases", []).append(_unquote(stripped[1:].strip()))
            continue
        if ":" not in line or line[0] in " \t-":
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        block_aliases = False
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            data[key] = _inline_list(inner)
        elif key == "aliases" and not val:
            data[key] = []
            block_aliases = True
        else:
            data[key] = _unquote(val)
    return data


def normalize_identity(value: str) -> str:
    """ADR-0008 identity normalization, duplicated for stdlib-only validation."""
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def check_aliases(rel: str, zone: str, meta: dict) -> None:
    """Validate optional owner-curated aliases (ADR-0008, local errors only)."""
    if "aliases" not in meta:
        return
    if zone != "brain":
        err(f"{rel}: aliases are allowed only on brain notes")
        return
    aliases = meta.get("aliases")
    if not isinstance(aliases, list):
        err(f"{rel}: aliases must be a list of strings")
        return
    if len(aliases) > 128:
        err(f"{rel}: aliases may contain at most 128 entries")
    seen: dict[str, int] = {}
    for idx, alias in enumerate(aliases, start=1):
        if not isinstance(alias, str):
            err(f"{rel}: aliases[{idx}] must be a scalar string")
            continue
        if not alias.strip():
            err(f"{rel}: aliases[{idx}] must contain non-whitespace text")
            continue
        if len(alias) > 256:
            err(f"{rel}: aliases[{idx}] exceeds 256 Unicode scalar values")
            continue
        norm = normalize_identity(alias)
        if norm in seen:
            err(f"{rel}: aliases[{idx}] duplicates aliases[{seen[norm]}] after identity normalization")
        else:
            seen[norm] = idx


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


def check_provenance(rel: str, meta: dict) -> None:
    """Per-note provenance checks (PRV-01/PRV-02). Every key optional.

    Known keys are type-checked (``provenance.sent`` must be an ISO-8601 date
    or datetime, ``provenance.verified`` a boolean); an unrecognised
    ``provenance.*`` subkey WARNS and never errors — the vocabulary is meant to
    grow without breaking the conventions gate."""
    for key in sorted(k for k in meta if str(k).startswith(PROVENANCE_PREFIX)):
        val = meta.get(key)
        if key not in PROVENANCE_KEYS:
            warn(f"{rel}: unrecognized provenance subkey {key!r} (known: "
                 f"{sorted(PROVENANCE_KEYS)})")
            continue
        if key in PROVENANCE_DATE_KEYS and val is not None:
            text = str(val).strip().replace(" ", "T")
            head = text[:10]
            ok = bool(ISO_DATE.match(head))
            if ok and len(text) > 10:
                try:
                    datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
                except ValueError:
                    ok = False
            if not ok:
                err(f"{rel}: {key} must be an ISO-8601 date or datetime, got {val!r}")
        if key in PROVENANCE_BOOL_KEYS and val is not None and parse_bool(val) is None:
            err(f"{rel}: {key} must be a boolean, got {val!r}")



def check_deliverable(rel: str, meta: dict) -> None:
    """DLV-01 type-checks for the orthogonal deliverable marker. Both keys are
    optional; a note carrying neither is untouched by this function.

    Type errors only — whether a marked note is a GOOD deliverable is the
    warn-only lint in :func:`check_type_lint`, never a gate failure."""
    raw = meta.get("deliverable")
    if raw is not None and parse_bool(raw) is None:
        err(f"{rel}: deliverable must be a boolean, got {raw!r}")
    project = meta.get("project")
    if project is not None and str(project).strip() and link_id(project) is None:
        err(f"{rel}: project must be a note id or [[wikilink]], got {project!r}")


def _source_anchored(meta: dict, body: str, raw_ids: set) -> bool:
    """Whether a note anchors its claim to a source: either a `source:`
    frontmatter key, or at least one wikilink resolving to a raw/ note."""
    if meta.get("source"):
        return True
    linked = {m.group(1).strip() for m in WIKILINK.finditer(body)}
    return bool(linked & raw_ids)


def check_type_lint(notes: list[dict]) -> None:
    """TMP-05 type-specific quality lint, warn-only (ADR-0003 ruling 3).

    - concept notes must carry a counter-arguments section.
    - decision notes must anchor their claim to a source.
    - a note marked `deliverable: true` must anchor the output it marks the
      same way (DLV-01). Warn-only, and deliberately: the marker is valid on a
      note of any type, including one whose payload has not been captured yet.
    """
    raw_ids = {n["meta"].get("id") for n in notes if n["zone"] == "raw"}
    for n in notes:
        meta, rel, body = n["meta"], n["rel"], n["body"]
        ntype = meta.get("type")
        if ntype == "concept" and not COUNTER_ARGUMENTS_HEADING.search(body):
            warn(f"{rel}: concept note has no Counter-Arguments section")
        anchored = _source_anchored(meta, body, raw_ids)
        if ntype == "decision" and not anchored:
            warn(f"{rel}: decision note has no source anchor "
                 f"(no source: key and no wikilink to a raw/ note)")
        if parse_bool(meta.get("deliverable")) and not anchored:
            warn(f"{rel}: deliverable: true has no source anchor "
                 f"(no source: key and no wikilink to a raw/ note)")


def check_alias_collisions(notes: list[dict]) -> None:
    """Warn when distinct brain notes claim the same normalized alias.

    Collisions are a legitimate state for history and supersession, so this is
    deliberately a quality nudge rather than a validation error.
    """
    owners: dict[str, list[tuple[str, str]]] = {}
    for note in notes:
        if note["zone"] != "brain":
            continue
        note_id = str(note["meta"].get("id") or note["rel"])
        aliases = note["meta"].get("aliases")
        if not isinstance(aliases, list):
            continue
        for alias in aliases:
            if isinstance(alias, str) and alias.strip() and len(alias) <= 256:
                owners.setdefault(normalize_identity(alias), []).append((note_id, alias))
    for norm, claimed in sorted(owners.items()):
        ids = sorted({note_id for note_id, _alias in claimed})
        if len(ids) > 1:
            warn(f"aliases collision {norm!r} claimed by notes {ids}")


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
    check_fence(rel, text)
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

    check_aliases(rel, zone, meta)
    check_bitemporal_note(rel, meta)
    check_provenance(rel, meta)
    check_deliverable(rel, meta)

    ntype = meta.get("type")
    if ntype:
        accepted = RAW_TYPES if zone == "raw" else BRAIN_TYPES
        if ntype not in accepted:
            warn(f"{rel}: unrecognized type {ntype!r} for {zone}/ "
                 f"(accepted: {sorted(accepted)})")

    if okf:
        # `provenance.*` is owned by check_provenance above (which warns on an
        # unrecognised subkey) — never double-reported here.
        unknown = {k for k in set(meta) - OKF_ALLOWED_KEYS
                   if not str(k).startswith(PROVENANCE_PREFIX)}
        if unknown:
            warn(f"[okf] {rel}: keys outside OKF profile: {sorted(unknown)}")

    return {"path": path, "rel": rel, "meta": meta, "body": fm[1], "zone": zone}



sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.modules.setdefault("tools.validate", sys.modules[__name__])
from tools.validate_invariants import (  # noqa: E402,F401
    SECTION_UPDATED,
    check_fence,
    STATE_MOC_STALE_DAYS,
    check_bitemporal_global,
    check_section_staleness,
    main,
)

if __name__ == "__main__":
    sys.exit(main())

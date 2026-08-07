"""Per-user personalization overlay (PER-01 / PER-02).

The substrate (`vault/brain/`, `vault/raw/`) is generic — it carries NO
hard-coded owner identity. Brand/voice/keyword/people content that used to be
wired straight into the kernel is a **data-driven slot** any new owner fills
with their own: the overlay.

Layout (the generic, owner-agnostic shape):

    overlay/
    ├── voice/      *.md  — durable writing voice (tone, register, sign-offs)
    ├── brand/      *.md  — naming/anonymisation/title conventions
    ├── keywords/   *.md  — glossary / acronym / codename decoder ring
    └── people/     *.md  — the always-on people this owner's notes reference

Each file carries a small frontmatter block (`overlay_type: <category>`) so a
validator can check shape without guessing from folder name alone. See
`overlay/README.md` for the full schema + starter scaffold.

This module is intentionally **filesystem-only** — it never constructs a
`BrainCore` or opens the index. `brain init --validate-overlay` has to work on
a brand-new install before any index exists, so overlay validation must not
depend on one.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from . import frontmatter

CATEGORIES: tuple[str, ...] = ("voice", "brand", "keywords", "people")

# CUT-01E: OPTIONAL categories — validated when present, never required.
# ``cos/`` carries the owner's chief-of-staff priority overrides (body list
# lines ``- <note-id>: high|normal|low|exclude``, read by `brain
# cos-priority-map`). MIGRATION: a pre-0.17 overlay simply has no cos/ dir and
# stays exactly as valid as before — adding the dir later needs no other
# change, and removing it is the complete rollback.
OPTIONAL_CATEGORIES: tuple[str, ...] = ("cos",)

# TAX-01/TAX-02 — the ingest/no-ingest category taxonomy lives in
# `overlay/cos/ingest.md`. It is a GATE (a `never` rule suppresses candidates
# outright), so hand-edited markdown cannot be trusted unvalidated: every rule
# line is shape-checked here, and anything the checker cannot recognise
# resolves to `propose` — the fail-CLOSED direction. Full spec:
# `docs/cos-ingest-taxonomy.md`; schema in `overlay/template/cos/ingest.md`.
INGEST_FILENAME = "ingest.md"
DISPOSITIONS: tuple[str, ...] = ("always", "propose", "never")
DEFAULT_DISPOSITION = "propose"
INGEST_LANES: tuple[str, ...] = ("text", "attachment", "both")
DEFAULT_LANE = "both"
TIERS: tuple[str, ...] = ("Public", "Internal", "Confidential", "Restricted", "MNPI")

_RULE_RE = re.compile(r"^-\s+([a-z0-9]+(?:-[a-z0-9]+)*)\s*:\s*(\S.*)$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_COMMENT_OPEN = "<!--"
_COMMENT_CLOSE = "-->"

# AUT-01/AUT-03 (ADR-0003 Ruling c/e): the HTML brief/digest renderers are
# pure-render — all overlay I/O happens here, once, before the render call.
# Two OPTIONAL frontmatter keys on a brand/*.md file (alongside the existing
# overlay_type/title/updated) let an owner brand the generated HTML without
# any new overlay category or schema ceremony. Absent -> neutral fallback
# (zero hard-coded owner content is the kernel/overlay contract).
_DEFAULT_BRAND_TITLE = "Brain Brief"
_DEFAULT_ACCENT = "#2563eb"
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def overlay_dir(
    vault: str | os.PathLike[str] | None = None,
    explicit: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve the active overlay directory.

    Precedence: ``explicit`` arg (``--overlay-dir``) > ``$BRAIN_OVERLAY_DIR`` >
    ``<vault>/overlay`` (the overlay travels with the user's vault, alongside
    ``raw/`` and ``brain/`` — see AGENTS.md §1).
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("BRAIN_OVERLAY_DIR")
    if env:
        return Path(env).expanduser().resolve()
    from . import config

    return config.vault_root(vault) / "overlay"


def _validate_category_file(path: Path, category: str) -> list[str]:
    """Return a list of human-readable issues for one overlay file (empty = OK)."""
    issues: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # pragma: no cover - unreadable file is rare
        return [f"{path.name}: unreadable ({type(exc).__name__}: {exc})"]

    meta, body = frontmatter.parse_text(text)
    if not meta:
        issues.append(f"{path.name}: missing or unparseable frontmatter")
        return issues

    declared = meta.get("overlay_type")
    if declared != category:
        issues.append(
            f"{path.name}: overlay_type={declared!r} does not match its "
            f"directory ({category!r})"
        )
    if not body.strip():
        issues.append(f"{path.name}: frontmatter present but body is empty")
    return issues


def _strip_noise(body: str) -> list[str]:
    """Drop fenced code blocks and HTML comments — the two places a template
    legitimately shows EXAMPLE rules that must never be read as real ones."""
    out: list[str] = []
    in_fence = False
    in_comment = False
    for line in body.splitlines():
        if in_comment:
            if _COMMENT_CLOSE in line:
                in_comment = False
            continue
        if _COMMENT_OPEN in line:
            # a whole-line or trailing comment opener; single-line comments close here
            if _COMMENT_CLOSE not in line.split(_COMMENT_OPEN, 1)[1]:
                in_comment = True
            continue
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return out


def parse_ingest_rules(body: str) -> dict[str, Any]:
    """Parse `overlay/cos/ingest.md` body rules. Never raises.

    Rule syntax (one list line per category)::

        - <category-id>: always|propose|never | lane=<lane> | min_tier=<Tier>

    Everything the parser cannot recognise resolves to ``propose`` and emits a
    WARNING rather than an error — the fail-CLOSED direction. A `never` rule
    suppresses candidates outright, so it is never inferred from a typo.
    """
    rules: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    for line in _strip_noise(body):
        m = _RULE_RE.match(line.rstrip())
        if not m:
            continue
        cat_id, rest = m.group(1), m.group(2).strip()
        parts = [p.strip() for p in rest.split("|")]
        disposition = parts[0].strip().strip("`")
        if disposition not in DISPOSITIONS:
            warnings.append(
                f"{cat_id}: unknown disposition {disposition!r} — "
                f"treated as {DEFAULT_DISPOSITION!r}"
            )
            disposition = DEFAULT_DISPOSITION
        rule: dict[str, Any] = {"disposition": disposition, "lane": DEFAULT_LANE,
                                "min_tier": None}
        for opt in parts[1:]:
            if not opt:
                continue
            key, sep, value = opt.partition("=")
            key, value = key.strip(), value.strip().strip("`")
            if not sep:
                warnings.append(f"{cat_id}: unparseable option {opt!r} — ignored, "
                                f"rule treated as {DEFAULT_DISPOSITION!r}")
                rule["disposition"] = DEFAULT_DISPOSITION
            elif key == "lane":
                if value in INGEST_LANES:
                    rule["lane"] = value
                else:
                    warnings.append(f"{cat_id}: unknown lane {value!r} — "
                                    f"treated as {DEFAULT_LANE!r}")
            elif key == "min_tier":
                if value in TIERS:
                    rule["min_tier"] = value
                else:
                    warnings.append(f"{cat_id}: unknown min_tier {value!r} — ignored "
                                    "(a category never lowers a tier)")
            else:
                warnings.append(f"{cat_id}: unknown option {key!r} — ignored")
        if cat_id in rules:
            warnings.append(f"{cat_id}: duplicate rule — treated as "
                            f"{DEFAULT_DISPOSITION!r}")
            rule = {"disposition": DEFAULT_DISPOSITION, "lane": DEFAULT_LANE,
                    "min_tier": None}
        rules[cat_id] = rule

    if not rules:
        warnings.append("no category rules found — the file declares no taxonomy")
    return {"rules": rules, "warnings": warnings}


def _ingest_report(path: Path) -> dict[str, Any]:
    """Shape-check one `cos/ingest.md`. Structural problems are ISSUES (they
    make the overlay invalid); rule-level problems are WARNINGS (fail closed to
    `propose`, per docs/cos-ingest-taxonomy.md §5)."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # pragma: no cover - unreadable file is rare
        return {"present": True, "rules": {}, "warnings": [],
                "issues": [f"{path.name}: unreadable ({type(exc).__name__}: {exc})"]}
    _meta, body = frontmatter.parse_text(text)
    parsed = parse_ingest_rules(body)
    return {"present": True, "rules": parsed["rules"],
            "warnings": [f"{path.name}: {w}" for w in parsed["warnings"]],
            "issues": []}


def validate_overlay(path: Path) -> dict[str, Any]:
    """Validate an overlay directory's shape. Pure filesystem check.

    Required shape: ``path`` exists and contains, for EACH of ``CATEGORIES``,
    a subdirectory with at least one ``*.md`` file whose frontmatter declares
    ``overlay_type: <category>``. Returns a report dict (never raises on a
    malformed/missing overlay — that is what ``valid: false`` is for).
    """
    if not path.exists():
        return {
            "overlay_dir": str(path),
            "exists": False,
            "valid": False,
            "categories": {
                c: {"present": False, "file_count": 0, "issues": [f"{c}/ missing (overlay dir does not exist)"]}
                for c in CATEGORIES
            },
            "errors": [f"overlay dir does not exist: {path}"],
            "warnings": [],
        }

    categories: dict[str, Any] = {}
    errors: list[str] = []
    warnings: list[str] = []

    for cat in CATEGORIES:
        cat_dir = path / cat
        issues: list[str] = []
        file_count = 0
        present = cat_dir.is_dir()
        if not present:
            issues.append(f"missing category directory: {cat}/")
        else:
            md_files = sorted(cat_dir.glob("*.md"))
            file_count = len(md_files)
            if file_count == 0:
                issues.append(f"{cat}/ exists but has no .md files")
            for f in md_files:
                issues.extend(_validate_category_file(f, cat))
        categories[cat] = {"present": present, "file_count": file_count, "issues": issues}
        errors.extend(f"{cat}: {issue}" for issue in issues)

    # Optional categories: shape-checked ONLY when present — a missing
    # optional dir is never an issue (backward-compatible with every
    # pre-existing overlay; see OPTIONAL_CATEGORIES).
    for cat in OPTIONAL_CATEGORIES:
        cat_dir = path / cat
        if not cat_dir.is_dir():
            categories[cat] = {"present": False, "file_count": 0,
                               "issues": [], "optional": True}
            continue
        issues = []
        md_files = sorted(cat_dir.glob("*.md"))
        for f in md_files:
            issues.extend(_validate_category_file(f, cat))
        entry: dict[str, Any] = {"present": True, "file_count": len(md_files),
                                 "issues": issues, "optional": True}
        # TAX-02: `cos/ingest.md` carries machine-read rules, so it gets a
        # rule-level shape check on top of the generic frontmatter check. The
        # file being ABSENT is not a problem at any level — absent means the
        # whole category feature is OFF (docs/cos-ingest-taxonomy.md §5).
        if cat == "cos":
            ingest_path = cat_dir / INGEST_FILENAME
            if ingest_path.is_file():
                ing = _ingest_report(ingest_path)
                entry["ingest"] = ing
                issues.extend(ing["issues"])
                warnings.extend(f"{cat}: {w}" for w in ing["warnings"])
            else:
                entry["ingest"] = {"present": False, "rules": {}, "warnings": [],
                                   "issues": []}
        categories[cat] = entry
        errors.extend(f"{cat}: {issue}" for issue in issues)

    return {
        "overlay_dir": str(path),
        "exists": True,
        "valid": len(errors) == 0,
        "categories": categories,
        "errors": errors,
        "warnings": warnings,
    }


# PRV-02 (HARDENED:codex-2) — the keyword decoder ring doubles as the owner's
# tier map: an OPTIONAL third table column naming a classification tier says
# "material mentioning this term is <Tier>". It is the ONLY thing that lowers
# an email-derived source off its MNPI default, so it is read strictly: a row
# whose third cell is not an exact tier name contributes nothing.
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_PLACEHOLDER_RE = re.compile(r"^<.*>$")


def _table_cells(line: str) -> list[str]:
    m = _TABLE_ROW_RE.match(line)
    if not m:
        return []
    return [c.strip().strip("`").strip() for c in m.group(1).split("|")]


def resolve_keyword_tiers(
    vault: str | os.PathLike[str] | None = None,
    explicit: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """``{term (casefolded): tier}`` from the overlay's ``keywords/*.md`` tables.

    Rows are ``| Term | Expansion | Classification |``; the third column is
    OPTIONAL (a glossary without it maps nothing, exactly as before). Template
    placeholders (``<ACRONYM>``) and header/separator rows are skipped. Never
    raises — an unreadable or absent overlay maps nothing.
    """
    out: dict[str, str] = {}
    kdir = overlay_dir(vault, explicit) / "keywords"
    if not kdir.is_dir():
        return out
    for f in sorted(kdir.glob("*.md")):
        try:
            _meta, body = frontmatter.parse_text(f.read_text(encoding="utf-8"))
        except Exception:  # pragma: no cover - unreadable file is rare
            continue
        for line in _strip_noise(body):
            cells = _table_cells(line)
            if len(cells) < 3:
                continue
            term, tier = cells[0], cells[2]
            if tier not in TIERS or not term or _PLACEHOLDER_RE.match(term):
                continue
            out[term.casefold()] = tier
    return out


def match_keyword_tier(
    text: str,
    vault: str | os.PathLike[str] | None = None,
    explicit: str | os.PathLike[str] | None = None,
) -> tuple[str | None, str | None]:
    """Highest tier any mapped keyword found in ``text`` resolves to, plus the
    term that matched. Word-boundary matching, case-insensitive."""
    tiers = resolve_keyword_tiers(vault, explicit)
    if not tiers:
        return None, None
    hay = text.casefold()
    best: tuple[str, str] | None = None
    for term, tier in sorted(tiers.items()):
        pattern = re.escape(term)
        if term[:1].isalnum():
            pattern = r"\b" + pattern
        if term[-1:].isalnum():
            pattern = pattern + r"\b"
        if not re.search(pattern, hay):
            continue
        if best is None or TIERS.index(tier) > TIERS.index(best[0]):
            best = (tier, term)
    return best if best else (None, None)


def load_ingest_rules(
    vault: str | os.PathLike[str] | None = None,
    explicit: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """The active ``cos/ingest.md`` taxonomy (TAX-01/TAX-02), or an empty,
    absent report. Never raises; see docs/cos-ingest-taxonomy.md §5 for the
    absent/unparseable semantics."""
    path = overlay_dir(vault, explicit) / "cos" / INGEST_FILENAME
    if not path.is_file():
        return {"present": False, "rules": {}, "warnings": [], "issues": []}
    return _ingest_report(path)


def category_min_tier(
    category: str,
    vault: str | os.PathLike[str] | None = None,
    explicit: str | os.PathLike[str] | None = None,
) -> str | None:
    """The ingest category's classification FLOOR, if it declares one."""
    rule = load_ingest_rules(vault, explicit)["rules"].get(str(category or ""))
    return rule.get("min_tier") if isinstance(rule, dict) else None


def resolve_brand(
    vault: str | os.PathLike[str] | None = None,
    explicit: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Resolve brand data for the HTML brief/digest renderers (AUT-01/AUT-03).

    Reads the FIRST ``overlay/brand/*.md`` file (sorted, deterministic) if
    present and takes its ``title`` plus two optional keys — ``owner_name``,
    ``accent_color`` (a ``#rgb``/``#rrggbb`` hex string; anything else is
    ignored, never trusted into CSS unvalidated). Never raises; a missing
    overlay, empty ``brand/`` category, or unreadable file all fall back to
    the NEUTRAL default below — the renderer must never depend on an owner
    overlay existing.
    """
    result: dict[str, Any] = {
        "present": False,
        "title": _DEFAULT_BRAND_TITLE,
        "owner_name": None,
        "accent_color": _DEFAULT_ACCENT,
    }
    brand_dir = overlay_dir(vault, explicit) / "brand"
    brand_files = sorted(brand_dir.glob("*.md")) if brand_dir.is_dir() else []
    if not brand_files:
        return result
    try:
        text = brand_files[0].read_text(encoding="utf-8")
    except Exception:
        return result

    meta, _body = frontmatter.parse_text(text)
    if not meta:
        return result

    title = meta.get("title")
    owner_name = meta.get("owner_name")
    accent = meta.get("accent_color")
    if isinstance(title, str) and title.strip():
        result["title"] = title.strip()
    if isinstance(owner_name, str) and owner_name.strip():
        result["owner_name"] = owner_name.strip()
    if isinstance(accent, str) and _HEX_COLOR_RE.match(accent.strip()):
        result["accent_color"] = accent.strip()
    result["present"] = True
    return result

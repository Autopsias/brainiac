"""HYG-03 — zone catalogs (--catalogs) + state-MOC per-section staleness lint.

Catalogs must be GENERATED (marked, never hand-edited semantically) and
deterministic (re-running --catalogs on an unchanged vault is a byte-identical
no-op, so validate-vault stays clean under repeated runs / CI). The staleness
lint is warn-only and gated at a boundary threshold.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import validate as V  # noqa: E402


def _index(vault: Path, body_extra: str = "") -> None:
    (vault / "brain" / "index.md").write_text(
        "---\nid: index\ntitle: Index\ntype: index\n"
        "classification: Internal\ncreated: 2026-01-01\nupdated: 2026-01-01\n"
        f"---\nbody\n{body_extra}"
    )


def _note(nid: str, title: str, updated: str = "2026-01-01", ntype: str = "note") -> str:
    return (
        f"---\nid: {nid}\ntitle: \"{title}\"\ntype: {ntype}\n"
        f"classification: Internal\ncreated: 2026-01-01\nupdated: {updated}\n---\nbody\n"
    )


def _vault(tmp_path: Path, projects: dict[str, str], resources: dict[str, str] | None = None) -> Path:
    vault = tmp_path / "vault"
    (vault / "brain" / "projects").mkdir(parents=True)
    (vault / "brain" / "resources").mkdir(parents=True)
    (vault / "raw").mkdir(parents=True)
    _index(vault)
    for nid, fm in projects.items():
        (vault / "brain" / "projects" / f"{nid}.md").write_text(fm)
    for nid, fm in (resources or {}).items():
        (vault / "brain" / "resources" / f"{nid}.md").write_text(fm)
    return vault


def _run(vault: Path, *flags: str) -> int:
    sys.argv = ["validate.py", str(vault), *flags]
    return V.main()


def test_catalogs_are_marked_generated_and_list_expected_fields(tmp_path):
    vault = _vault(tmp_path, {"proj-a": _note("proj-a", "Project A", "2026-03-01")})
    assert _run(vault, "--catalogs") == 0

    catalog = (vault / "brain" / "projects" / "catalog.md").read_text()
    assert "generated — do not hand-edit" in catalog
    assert "[[proj-a]]" in catalog
    assert "Project A" in catalog
    assert "2026-03-01" in catalog
    assert "Internal" in catalog

    # catalog.md itself is excluded from the conventions gate (like backlinks.md) —
    # a second full validate run (no frontmatter on it that would matter) stays clean.
    assert _run(vault) == 0


def test_catalog_generation_is_deterministic_across_reruns(tmp_path):
    vault = _vault(
        tmp_path,
        {"proj-a": _note("proj-a", "Project A"), "proj-b": _note("proj-b", "Project B")},
        {"res-a": _note("res-a", "Resource A")},
    )
    assert _run(vault, "--catalogs") == 0
    first = {
        p.name: p.read_text()
        for p in (vault / "brain").rglob("catalog.md")
    }
    assert _run(vault, "--catalogs") == 0
    second = {
        p.name: p.read_text()
        for p in (vault / "brain").rglob("catalog.md")
    }
    assert first == second


def test_catalogs_generated_for_all_four_para_zones_even_when_empty(tmp_path):
    vault = _vault(tmp_path, {"proj-a": _note("proj-a", "Project A")})
    assert _run(vault, "--catalogs") == 0
    for zone in V.PARA_ZONES:
        assert (vault / "brain" / zone / "catalog.md").is_file()
    # areas/archive had no notes -> catalog exists but its table has no rows
    areas_catalog = (vault / "brain" / "areas" / "catalog.md").read_text()
    assert "| [[" not in areas_catalog


# ---------------------------------------------------------------------------
# state-MOC per-section staleness lint (warn-only, boundary at STATE_MOC_STALE_DAYS)
# ---------------------------------------------------------------------------

def _moc_note(nid: str, section_updated: str) -> str:
    return (
        f"---\nid: {nid}\ntitle: \"State MOC\"\ntype: moc\n"
        f"classification: Internal\ncreated: 2026-01-01\nupdated: 2026-01-01\n---\n"
        f"# State MOC\n\n## Section: Current Priorities\nUpdated: {section_updated}\n\n- item\n"
    )


def test_section_within_threshold_is_not_flagged(tmp_path):
    today = datetime.date(2026, 7, 5)
    stamped = (today - datetime.timedelta(days=V.STATE_MOC_STALE_DAYS)).isoformat()
    vault = _vault(tmp_path, {"moc": _moc_note("moc", stamped)})
    V.errors.clear()
    V.warnings.clear()
    notes = []
    for p in V.iter_md(vault / "brain", vault):
        if p.name in ("backlinks.md", "catalog.md"):
            continue
        n = V.check_note(p, "brain", okf=False)
        if n:
            notes.append(n)
    V.check_section_staleness(notes, today=today)
    assert not any("stale" in w for w in V.warnings)


def test_section_one_day_past_threshold_is_flagged(tmp_path):
    today = datetime.date(2026, 7, 5)
    stamped = (today - datetime.timedelta(days=V.STATE_MOC_STALE_DAYS + 1)).isoformat()
    vault = _vault(tmp_path, {"moc": _moc_note("moc", stamped)})
    V.errors.clear()
    V.warnings.clear()
    notes = []
    for p in V.iter_md(vault / "brain", vault):
        if p.name in ("backlinks.md", "catalog.md"):
            continue
        n = V.check_note(p, "brain", okf=False)
        if n:
            notes.append(n)
    V.check_section_staleness(notes, today=today)
    assert any("stale" in w and "Current Priorities" in w for w in V.warnings)


def test_staleness_lint_is_warn_only_never_blocks_the_gate(tmp_path):
    stamped = "2020-01-01"  # deeply stale
    vault = _vault(tmp_path, {"moc": _moc_note("moc", stamped)})
    assert _run(vault) == 0  # exit 0 — warnings never fail the gate
    assert any("stale" in w for w in V.warnings)


def test_index_md_zone_headings_get_freshness_stamps_and_lint_applies(tmp_path):
    """index.md's own '## Projects' etc. headings use the same Updated: stamp
    convention — a stale one is flagged exactly like a state-MOC section."""
    vault = _vault(tmp_path, {"proj-a": _note("proj-a", "Project A")})
    _index(vault, body_extra="\n## Projects\nUpdated: 2020-01-01\n\n- [[proj-a]]\n")
    V.errors.clear()
    V.warnings.clear()
    notes = []
    for p in V.iter_md(vault / "brain", vault):
        if p.name in ("backlinks.md", "catalog.md"):
            continue
        n = V.check_note(p, "brain", okf=False)
        if n:
            notes.append(n)
    V.check_section_staleness(notes, today=datetime.date(2026, 7, 5))
    assert any("stale" in w and "index.md" in w for w in V.warnings)

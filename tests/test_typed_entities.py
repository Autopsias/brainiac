"""TMP-04/TMP-05 — typed entity vocabulary, templates, and type lint
(ADR-0003 ruling 3).

Type vocabulary is warn-only (legacy 4-type notes stay unaffected); concept
counter-arguments and decision source-anchoring are warn-only quality nudges,
never hard failures.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import validate as V  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_TYPES = [
    "person", "company", "project", "meeting", "decision", "concept", "daily",
]


def _index(vault: Path) -> None:
    (vault / "brain" / "index.md").write_text(
        "---\nid: index\ntitle: Index\ntype: index\n"
        "classification: Internal\ncreated: 2026-01-01\nupdated: 2026-01-01\n"
        "---\nbody\n"
    )


def _vault(tmp_path: Path, notes: dict[str, str], raw: dict[str, str] | None = None) -> Path:
    vault = tmp_path / "vault"
    (vault / "brain" / "resources").mkdir(parents=True)
    (vault / "raw").mkdir(parents=True)
    _index(vault)
    for nid, fm in notes.items():
        (vault / "brain" / "resources" / f"{nid}.md").write_text(fm)
    for nid, fm in (raw or {}).items():
        (vault / "raw" / f"{nid}.md").write_text(fm)
    return vault


def _run(vault: Path) -> int:
    sys.argv = ["validate.py", str(vault)]
    return V.main()


def test_templates_exist_and_declare_their_type():
    for t in TEMPLATE_TYPES:
        path = REPO_ROOT / "templates" / f"{t}.md"
        assert path.is_file(), f"missing template: {path}"
        text = path.read_text(encoding="utf-8")
        fm = V.split_frontmatter(text)
        assert fm is not None, f"{path}: missing frontmatter"
        meta = V.parse_frontmatter(fm[0])
        assert meta.get("type") == t


def test_new_entity_type_validates_clean_no_errors(tmp_path):
    note = (
        "---\nid: a\ntitle: \"A\"\ntype: person\nclassification: Internal\n"
        "created: 2026-01-01\nupdated: 2026-01-01\n---\nbody\n"
    )
    vault = _vault(tmp_path, {"a": note})
    assert _run(vault) == 0
    assert V.errors == []
    assert not any("unrecognized type" in w for w in V.warnings)


def test_unrecognized_type_warns_not_errors(tmp_path):
    note = (
        "---\nid: a\ntitle: \"A\"\ntype: bogus\nclassification: Internal\n"
        "created: 2026-01-01\nupdated: 2026-01-01\n---\nbody\n"
    )
    vault = _vault(tmp_path, {"a": note})
    assert _run(vault) == 0
    assert V.errors == []
    assert any("unrecognized type" in w for w in V.warnings)


def test_legacy_four_type_notes_unaffected(tmp_path):
    note = (
        "---\nid: a\ntitle: \"A\"\ntype: note\nclassification: Internal\n"
        "created: 2026-01-01\nupdated: 2026-01-01\n---\nbody\n"
    )
    vault = _vault(tmp_path, {"a": note})
    assert _run(vault) == 0
    assert V.errors == []
    assert V.warnings == []


def test_concept_without_counter_arguments_warns(tmp_path):
    note = (
        "---\nid: c1\ntitle: \"C\"\ntype: concept\nclassification: Internal\n"
        "created: 2026-01-01\nupdated: 2026-01-01\n---\n"
        "# C\n\n## Definition\n\nsome text\n"
    )
    vault = _vault(tmp_path, {"c1": note})
    assert _run(vault) == 0
    assert any("Counter-Arguments" in w for w in V.warnings)


def test_concept_with_counter_arguments_silent(tmp_path):
    note = (
        "---\nid: c1\ntitle: \"C\"\ntype: concept\nclassification: Internal\n"
        "created: 2026-01-01\nupdated: 2026-01-01\n---\n"
        "# C\n\n## Counter-Arguments\n\nit might be wrong because...\n"
    )
    vault = _vault(tmp_path, {"c1": note})
    assert _run(vault) == 0
    assert not any("Counter-Arguments" in w for w in V.warnings)


def test_decision_without_source_anchor_warns(tmp_path):
    note = (
        "---\nid: d1\ntitle: \"D\"\ntype: decision\nclassification: Internal\n"
        "created: 2026-01-01\nupdated: 2026-01-01\n---\n"
        "# D\n\n## Decision\n\nwe decided this\n"
    )
    vault = _vault(tmp_path, {"d1": note})
    assert _run(vault) == 0
    assert any("source anchor" in w for w in V.warnings)


def test_decision_with_source_key_silent(tmp_path):
    note = (
        "---\nid: d1\ntitle: \"D\"\ntype: decision\nclassification: Internal\n"
        "created: 2026-01-01\nupdated: 2026-01-01\nsource: \"[[raw/2026-01-01-x]]\"\n"
        "---\n# D\n\n## Decision\n\nwe decided this\n"
    )
    vault = _vault(tmp_path, {"d1": note})
    assert _run(vault) == 0
    assert not any("source anchor" in w for w in V.warnings)


def test_decision_with_raw_wikilink_silent(tmp_path):
    raw_note = (
        "---\nid: 2026-01-01-x\ntype: source\nclassification: Internal\n"
        "captured: 2026-01-01\norigin: verbal\nsha256: \"deadbeef\"\n"
        "immutable: true\n---\nraw body\n"
    )
    note = (
        "---\nid: d1\ntitle: \"D\"\ntype: decision\nclassification: Internal\n"
        "created: 2026-01-01\nupdated: 2026-01-01\n---\n"
        "# D\n\n## Decision\n\nsee [[2026-01-01-x]] for context\n"
    )
    vault = _vault(tmp_path, {"d1": note}, raw={"2026-01-01-x": raw_note})
    assert _run(vault) == 0
    assert not any("source anchor" in w for w in V.warnings)

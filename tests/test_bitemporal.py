"""TMP-01 — bitemporal frontmatter schema validator coverage (ADR-0003 ruling 2).

Keys are optional; a legacy note with none of them must validate exactly as
before. When present, they carry type/consistency checks and global
supersession-chain invariants (cycles, forks, dangling links, classification
presence on both sides, at most one is_latest_version:true per chain).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import validate as V  # noqa: E402


def _index(vault: Path) -> None:
    (vault / "brain" / "index.md").write_text(
        "---\nid: index\ntitle: Index\ntype: index\n"
        "classification: Internal\ncreated: 2026-01-01\nupdated: 2026-01-01\n"
        "---\nbody\n"
    )


def _note(nid: str, extra: str = "", classification: str | None = "Internal") -> str:
    cls_line = f"classification: {classification}\n" if classification else ""
    return (
        f"---\nid: {nid}\ntitle: \"{nid}\"\ntype: note\n{cls_line}"
        f"created: 2026-01-01\nupdated: 2026-01-01\n{extra}---\nbody\n"
    )


def _vault(tmp_path: Path, notes: dict[str, str]) -> Path:
    vault = tmp_path / "vault"
    (vault / "brain" / "resources").mkdir(parents=True)
    (vault / "raw").mkdir(parents=True)
    _index(vault)
    for nid, fm in notes.items():
        (vault / "brain" / "resources" / f"{nid}.md").write_text(fm)
    return vault


def _run(vault: Path) -> int:
    sys.argv = ["validate.py", str(vault)]
    return V.main()


def test_legacy_note_with_no_bitemporal_keys_still_validates_clean(tmp_path):
    vault = _vault(tmp_path, {"a": _note("a")})
    assert _run(vault) == 0
    assert V.errors == []


def test_valid_supersession_chain_validates_clean(tmp_path):
    old = _note("old", "superseded_by: new\nsuperseded_date: 2026-02-01\n"
                        "is_latest_version: false\n")
    new = _note("new", "previous_version: old\nis_latest_version: true\n")
    vault = _vault(tmp_path, {"old": old, "new": new})
    assert _run(vault) == 0


def test_dangling_superseded_by_is_error(tmp_path):
    a = _note("a", "superseded_by: ghost\nis_latest_version: false\n")
    vault = _vault(tmp_path, {"a": a})
    assert _run(vault) == 1
    assert any("ghost" in e for e in V.errors)


def test_malformed_date_is_error(tmp_path):
    a = _note("a", "document_date: not-a-date\n")
    vault = _vault(tmp_path, {"a": a})
    assert _run(vault) == 1
    assert any("document_date" in e for e in V.errors)


def test_is_latest_version_false_requires_superseded_by(tmp_path):
    a = _note("a", "is_latest_version: false\n")
    vault = _vault(tmp_path, {"a": a})
    assert _run(vault) == 1
    assert any("requires superseded_by" in e for e in V.errors)


def test_self_supersession_is_error(tmp_path):
    a = _note("a", "superseded_by: a\nis_latest_version: false\n")
    vault = _vault(tmp_path, {"a": a})
    assert _run(vault) == 1
    assert any("supersede itself" in e for e in V.errors)


def test_cycle_is_error(tmp_path):
    a = _note("a", "superseded_by: b\nis_latest_version: false\n")
    b = _note("b", "superseded_by: a\nis_latest_version: false\n")
    vault = _vault(tmp_path, {"a": a, "b": b})
    assert _run(vault) == 1
    assert any("cycle" in e for e in V.errors)


def test_fork_two_successors_same_predecessor_is_error(tmp_path):
    a = _note("a")
    b = _note("b", "previous_version: a\nis_latest_version: true\n")
    c = _note("c", "previous_version: a\nis_latest_version: true\n")
    vault = _vault(tmp_path, {"a": a, "b": b, "c": c})
    assert _run(vault) == 1
    assert any("more than one note" in e for e in V.errors)


def test_more_than_one_latest_in_chain_is_error(tmp_path):
    a = _note("a", "superseded_by: b\nis_latest_version: false\n")
    b = _note("b", "previous_version: a\nis_latest_version: true\n")
    c = _note("c", "previous_version: b\nis_latest_version: true\n")
    vault = _vault(tmp_path, {"a": a, "b": b, "c": c})
    assert _run(vault) == 1
    assert any("more than one is_latest_version: true" in e for e in V.errors)


def test_successor_missing_classification_in_chain_is_error(tmp_path):
    a = _note("a", "superseded_by: b\nis_latest_version: false\n")
    b = _note("b", "previous_version: a\nis_latest_version: true\n",
               classification=None)
    vault = _vault(tmp_path, {"a": a, "b": b})
    assert _run(vault) == 1
    assert any("no explicit classification" in e for e in V.errors)


def test_predecessor_missing_classification_in_chain_is_error(tmp_path):
    a = _note("a", "superseded_by: b\nis_latest_version: false\n",
               classification=None)
    b = _note("b", "previous_version: a\nis_latest_version: true\n")
    vault = _vault(tmp_path, {"a": a, "b": b})
    assert _run(vault) == 1
    assert any("no explicit classification" in e for e in V.errors)


def test_missing_reciprocal_link_is_warn_only(tmp_path):
    a = _note("a", "superseded_by: b\nis_latest_version: false\n")
    b = _note("b")  # no previous_version/replaces back to a
    vault = _vault(tmp_path, {"a": a, "b": b})
    assert _run(vault) == 0
    assert any("reciprocal" in w for w in V.warnings)

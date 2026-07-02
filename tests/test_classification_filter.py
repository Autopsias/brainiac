"""Deny-by-default classification filter (CORE-02)."""
from __future__ import annotations

import pytest

from brain import classification as cls


def test_tier_ordering():
    assert cls.rank("Public") < cls.rank("Internal") < cls.rank("Confidential") \
        < cls.rank("Restricted") < cls.rank("Secret")


@pytest.mark.parametrize("value", [None, "", "bogus", "secret", 42, "internal "])
def test_unrecognised_is_default_denied_as_secret(value):
    # "internal " (trailing space) IS recognised after strip; adjust expectation.
    if isinstance(value, str) and value.strip() in cls.RANK:
        assert not cls.is_default_denied(value)
    else:
        assert cls.is_default_denied(value)
        assert cls.normalize(value) == "Secret"
        assert cls.rank(value) == cls.RANK["Secret"]


def test_default_max_tier_is_conservative():
    flt = cls.ClassificationFilter()  # default Internal
    assert flt.allows("Public")
    assert flt.allows("Internal")
    assert not flt.allows("Confidential")
    assert not flt.allows("Restricted")
    assert not flt.allows("Secret")
    assert not flt.allows(None)  # unlabelled -> Secret -> denied


def test_elevation_is_explicit():
    flt = cls.ClassificationFilter(max_tier="Restricted")
    assert flt.allows("Confidential")
    assert flt.allows("Restricted")
    assert not flt.allows("Secret")
    assert not flt.allows(None)  # still default-denied


def test_only_secret_cap_surfaces_unlabelled():
    flt = cls.ClassificationFilter(max_tier="Secret")
    assert flt.allows(None)
    assert flt.allows("Secret")


def test_filter_and_report():
    items = [
        {"id": "a", "classification": "Public"},
        {"id": "b", "classification": "Internal"},
        {"id": "c", "classification": "Restricted"},
        {"id": "d"},  # unlabelled
    ]
    flt = cls.ClassificationFilter(max_tier="Internal")
    surfaced = flt.filter(items)
    assert {i["id"] for i in surfaced} == {"a", "b"}
    rep = flt.redaction_report(items)
    assert rep["total"] == 4
    assert rep["surfaced"] == 2
    assert rep["withheld"] == 2
    assert rep["withheld_unlabelled_default_deny"] == 1


def test_bad_max_tier_rejected():
    with pytest.raises(ValueError):
        cls.ClassificationFilter(max_tier="TopSecret")


def test_casing_mismatch_is_detected_but_still_fail_closed():
    # F-04: a wrong-case known tier is STILL default-denied (fail-closed), but a
    # diagnostic surfaces so it does not vanish silently.
    assert cls.casing_mismatch("internal") == "Internal"
    assert cls.casing_mismatch("PUBLIC") == "Public"
    assert cls.casing_mismatch("Internal") is None  # canonical -> no warning
    assert cls.casing_mismatch("bogus") is None
    # strict matching: lowercase 'internal' is treated as Secret (not surfaced at default)
    flt = cls.ClassificationFilter(max_tier="Internal")
    assert not flt.allows("internal")
    rep = flt.redaction_report([{"id": "x", "classification": "internal"}])
    assert "casing_mismatch_warnings" in rep
    assert any("Internal" in w for w in rep["casing_mismatch_warnings"])

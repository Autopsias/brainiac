"""RET-08 — the actionable-elevation nudge on the egress gate.

A starved result at the default Internal cap reads to an agent as "the vault is
empty" and drives it to web search (leaking internal topics outward). When the
gate withholds anything, `_filter_dicts` attaches a `hint` telling the agent to
elevate `--max-tier`, surfaced in both `--json` (egress.hint) and the text
footer — so the fix reaches the JSON-consuming Cowork agent, not just a human.
"""
from __future__ import annotations

from brain import cli
from brain.classification import TIERS


def _items():
    # one Internal (surfaces at default) + several above the cap
    return [
        {"id": "pub", "classification": "Public"},
        {"id": "int", "classification": "Internal"},
        {"id": "conf", "classification": "Confidential"},
        {"id": "restr", "classification": "Restricted"},
        {"id": "mnpi", "classification": "MNPI"},
    ]


def test_hint_present_and_actionable_when_withheld():
    surfaced, report = cli._filter_dicts(_items(), "Internal")
    assert report["withheld"] > 0
    hint = report.get("hint", "")
    assert "--max-tier" in hint                       # tells the agent HOW
    assert str(report["withheld"]) in hint            # and how many it's missing
    assert "Internal" in hint                         # names the current cap


def test_no_hint_at_top_tier_nothing_withheld():
    _surfaced, report = cli._filter_dicts(_items(), TIERS[-1])   # MNPI
    assert report["withheld"] == 0
    assert "hint" not in report                       # no false nudge


def test_no_hint_when_everything_surfaces_below_top():
    # only Public/Internal items, capped at Internal → nothing withheld → no hint
    items = [{"id": "a", "classification": "Public"},
             {"id": "b", "classification": "Internal"}]
    _surfaced, report = cli._filter_dicts(items, "Internal")
    assert report["withheld"] == 0
    assert "hint" not in report


def test_footer_includes_hint_line_only_when_present():
    _s, withheld_report = cli._filter_dicts(_items(), "Internal")
    footer = cli._egress_footer(withheld_report)
    assert footer.startswith("--")
    assert withheld_report["hint"] in footer          # hint appended on its own line
    assert footer.count("\n") == 1

    _s2, clean_report = cli._filter_dicts(_items(), TIERS[-1])
    clean_footer = cli._egress_footer(clean_report)
    assert "withheld" in clean_footer
    assert "\n" not in clean_footer                   # no dangling hint line


if __name__ == "__main__":   # ponytail: runnable without pytest
    test_hint_present_and_actionable_when_withheld()
    test_no_hint_at_top_tier_nothing_withheld()
    test_no_hint_when_everything_surfaces_below_top()
    test_footer_includes_hint_line_only_when_present()
    print("egress-hint checks PASS")

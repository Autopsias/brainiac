"""Overlay ingest rule parsing."""
from __future__ import annotations

from typing import Any, Callable, Pattern, Sequence


def _parse_rule_options(
    category_id: str,
    options: Sequence[str],
    rule: dict[str, Any],
    warnings: list[str],
    *,
    default_disposition: str,
    ingest_lanes: Sequence[str],
    default_lane: str,
    tiers: Sequence[str],
) -> None:
    """Apply one category rule's optional lane and tier settings."""
    for option in options:
        if not option:
            continue
        key, separator, value = option.partition("=")
        key, value = key.strip(), value.strip().strip("`")
        if not separator:
            warnings.append(
                f"{category_id}: unparseable option {option!r} — ignored, "
                f"rule treated as {default_disposition!r}"
            )
            rule["disposition"] = default_disposition
        elif key == "lane":
            if value in ingest_lanes:
                rule["lane"] = value
            else:
                warnings.append(
                    f"{category_id}: unknown lane {value!r} — "
                    f"treated as {default_lane!r}"
                )
        elif key == "min_tier":
            if value in tiers:
                rule["min_tier"] = value
            else:
                warnings.append(
                    f"{category_id}: unknown min_tier {value!r} — ignored "
                    "(a category never lowers a tier)"
                )
        else:
            warnings.append(f"{category_id}: unknown option {key!r} — ignored")


def parse_ingest_rules(
    body: str,
    *,
    strip_noise: Callable[[str], list[str]],
    rule_pattern: Pattern[str],
    dispositions: Sequence[str],
    default_disposition: str,
    ingest_lanes: Sequence[str],
    default_lane: str,
    tiers: Sequence[str],
) -> dict[str, Any]:
    """Parse ingest rules with fail-closed warnings."""
    rules: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    for line in strip_noise(body):
        match = rule_pattern.match(line.rstrip())
        if not match:
            continue
        category_id, rest = match.group(1), match.group(2).strip()
        parts = [part.strip() for part in rest.split("|")]
        disposition = parts[0].strip().strip("`")
        if disposition not in dispositions:
            warnings.append(
                f"{category_id}: unknown disposition {disposition!r} — "
                f"treated as {default_disposition!r}"
            )
            disposition = default_disposition
        rule: dict[str, Any] = {
            "disposition": disposition,
            "lane": default_lane,
            "min_tier": None,
        }
        _parse_rule_options(
            category_id,
            parts[1:],
            rule,
            warnings,
            default_disposition=default_disposition,
            ingest_lanes=ingest_lanes,
            default_lane=default_lane,
            tiers=tiers,
        )
        if category_id in rules:
            warnings.append(
                f"{category_id}: duplicate rule — treated as "
                f"{default_disposition!r}"
            )
            rule = {
                "disposition": default_disposition,
                "lane": default_lane,
                "min_tier": None,
            }
        rules[category_id] = rule

    if not rules:
        warnings.append("no category rules found — the file declares no taxonomy")
    return {"rules": rules, "warnings": warnings}

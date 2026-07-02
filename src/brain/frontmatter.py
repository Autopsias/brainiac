"""YAML frontmatter parse/serialise — stdlib-first, PyYAML if available.

Mirrors the conventions validator (tools/validate.py) so the engine and the
validator never disagree on note shape. Runs on a bare system python3.
"""
from __future__ import annotations

from typing import Any


def split(text: str) -> tuple[str, str] | None:
    """Return (frontmatter_block, body) or None if no leading frontmatter."""
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    return parts[1], parts[2]


def parse(block: str) -> dict[str, Any]:
    """Parse a frontmatter block. PyYAML if importable, else a flat mini-parser."""
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(block)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    data: dict[str, Any] = {}
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


def parse_text(text: str) -> tuple[dict[str, Any], str]:
    """Convenience: full note text -> (meta, body). Empty meta if none."""
    fm = split(text)
    if fm is None:
        return {}, text
    return parse(fm[0]), fm[1]

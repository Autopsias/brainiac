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
            data[key] = _strip_inline_comment(val).strip("'\"")
    return data


def _strip_inline_comment(val: str) -> str:
    """Strip a trailing unquoted ``  # comment`` from a scalar value.

    A quoted value keeps a literal ``#`` (``'#internal'`` is a value, not a
    comment); YAML requires the ``#`` be preceded by whitespace to start a
    comment, so ``val#x`` (no space) is left alone too."""
    val = val.strip()
    if val[:1] in "'\"":
        return val
    idx = val.find(" #")
    if idx == -1:
        return val
    return val[:idx].rstrip()


def parse_text(text: str) -> tuple[dict[str, Any], str]:
    """Convenience: full note text -> (meta, body). Empty meta if none."""
    fm = split(text)
    if fm is None:
        return {}, text
    return parse(fm[0]), fm[1]


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def set_keys(text: str, updates: dict[str, Any]) -> str:
    """Return ``text`` with each ``updates`` key set in the frontmatter block —
    replacing an existing ``key: ...`` line in place, appending new keys at the
    end of the block otherwise. Body and every other line are untouched.

    Line-based (mirrors the flat mini-parser above), not a full YAML re-dump —
    the values this is used for (``brain supersede``'s bitemporal keys) are all
    bare scalars, so preserving the rest of the block byte-for-byte matters more
    than a general YAML writer would buy us.
    """
    fm = split(text)
    if fm is None:
        raise ValueError("set_keys: text has no frontmatter block")
    block, body = fm
    remaining = dict(updates)
    out_lines: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped and ":" in stripped and stripped[0] not in " \t-#":
            key = stripped.split(":", 1)[0].strip()
            if key in remaining:
                out_lines.append(f"{key}: {_scalar(remaining.pop(key))}")
                continue
        out_lines.append(line)
    for key, val in remaining.items():
        out_lines.append(f"{key}: {_scalar(val)}")
    new_block = "\n".join(ln for ln in out_lines if ln.strip() != "") + "\n"
    return f"---\n{new_block}---{body}"

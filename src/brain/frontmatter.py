"""YAML frontmatter parse/serialise — stdlib-first, PyYAML if available.

Mirrors the conventions validator (tools/validate.py) so the engine and the
validator never disagree on note shape. Runs on a bare system python3.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any


# Identity comparison is deliberately narrower than general search
# normalization: it keeps punctuation and accents, normalizes Unicode to NFC,
# casefolds, and collapses whitespace. Title-phrase eligibility uses a
# separate tokenization so it never changes the identity contract.
_PHRASE_TOKEN = re.compile(r"[\w]+(?:[-/][\w]+)*", re.UNICODE)


def normalize_identity(value: object) -> str:
    """Return the ADR-0008 normalized identity form for a string.

    Non-string inputs deliberately normalize to ``\"\"``. Frontmatter
    validation rejects those inputs before indexing; this defensive behavior
    keeps malformed foreign Markdown from manufacturing an identity lookup.
    """
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFC", value).casefold()
    return " ".join(text.split())


def phrase_tokens(value: object) -> list[str]:
    """Tokenize normalized text only for title-phrase eligibility."""
    return _PHRASE_TOKEN.findall(normalize_identity(value))


def identifier_shaped(token: str) -> bool:
    """Whether a one-token literal query is specific enough for evidence."""
    alnum = "".join(ch for ch in token if ch.isalnum())
    return (len(alnum) >= 8 or any(ch.isdigit() for ch in token)
            or any(ch in token for ch in "-_/"))


def split(text: str) -> tuple[str, str] | None:
    """Return (frontmatter_block, body) or None if no leading frontmatter."""
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    return parts[1], parts[2]


def _unquote(value: str) -> str:
    value = _strip_inline_comment(value).strip()
    if len(value) >= 2 and value[:1] in "'\"" and value[-1:] == value[:1]:
        return value[1:-1]
    return value.strip("'\"")


def _inline_list(inner: str) -> list[str]:
    """Parse the small YAML inline-list subset accepted by the fallback.

    The fallback intentionally remains a tiny parser, but aliases are allowed
    to contain commas inside quoted display names, so splitting blindly on
    every comma would make valid inline YAML silently disappear.
    """
    items: list[str] = []
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
                items.append(_unquote(item))
            current = []
        else:
            current.append(char)
    item = "".join(current).strip()
    if item:
        items.append(_unquote(item))
    return items


def parse(block: str) -> dict[str, Any]:
    """Parse a frontmatter block. PyYAML if importable, else a flat mini-parser.

    In addition to scalar fields and inline lists, the fallback intentionally
    supports the standard block-list form for ``aliases``.  This keeps a
    no-PyYAML install aligned with the validator and the ADR-0008 schema.
    """
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(block)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    data: dict[str, Any] = {}
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


def yaml_scalar(value: Any) -> str:
    """Serialise ``value`` as a SAFE one-line YAML scalar.

    Quotes when the value carries YAML-special characters and — the part a
    bare ``f'"{v}"'`` gets wrong — escapes an embedded backslash/quote and
    strips raw control characters, so free text (an email subject, a sender
    display name) can never inject a second frontmatter line into a signed
    note. Mirrors ingest/pipeline._build_frontmatter's own escaping.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    text = "".join(" " if ch < " " or ch == "\x7f" else ch for ch in text)
    if (any(c in text for c in (":", "#", "[", "]", "{", "}", ",", '"', "\\", "'"))
            or text != text.strip() or not text or not text[:1].isalnum()):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


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

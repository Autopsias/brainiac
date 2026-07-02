"""Capture-path frontmatter enforcement (UX-01).

Both host-native clients (Codex/Claude Code/Gemini CLI) and sandboxed VM clients
(Cowork) route through enforce() to guarantee frontmatter before any write.
The host signs and indexes; the VM drops to capture-inbox/ unsigned and unindexed.

    host:  enforce() → write_note() → incremental sync → snapshot
    VM:    enforce() → draft_capture() (capture-inbox/; unsigned, unindexed)
           host drain-on-invoke picks it up on the next brain run

No signing key is ever read or resolved here.
"""
from __future__ import annotations

import datetime
import hashlib
from typing import Any

from . import frontmatter as fm

REQUIRED_KEYS: tuple[str, ...] = ("id", "type", "classification", "created")
_CAPTURE_CLASSIFICATION_DEFAULT = "Internal"
_CAPTURE_TYPE_DEFAULT = "note"


def _today() -> str:
    return datetime.date.today().isoformat()


def _derive_id(body: str) -> str:
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    return f"capture-{h}"


def enforce(content: str, *, override: dict[str, Any] | None = None) -> str:
    """Return content with all required capture frontmatter guaranteed.

    Rules:
    - Existing keys are NEVER overwritten (additive only).
    - ``override`` keys take precedence over both existing and defaults.
    - Missing ``classification`` defaults to ``Internal`` so a captured note
      is usable by default without requiring --max-tier elevation.
    - Always sets ``status: draft`` and ``provenance.trust: untrusted`` so the
      host drain treats it as untrusted input during ingest validation.
    - Preserves all other existing frontmatter keys.

    The result is STILL UNTRUSTED from the host's perspective until drain-on-invoke
    signs it — the host promote step validates, signs, indexes, and updates status.
    """
    override = override or {}
    meta, body = fm.parse_text(content)

    nid = override.get("id") or meta.get("id") or _derive_id(body)
    ntype = override.get("type") or meta.get("type") or _CAPTURE_TYPE_DEFAULT
    ncls = (
        override.get("classification")
        or meta.get("classification")
        or _CAPTURE_CLASSIFICATION_DEFAULT
    )
    ncreated = override.get("created") or meta.get("created") or _today()
    nupdated = override.get("updated") or meta.get("updated") or _today()
    ntitle = override.get("title") or meta.get("title") or str(nid)

    block: dict[str, Any] = {
        "id": nid,
        "title": ntitle,
        "type": ntype,
        "classification": ncls,
        "created": ncreated,
        "updated": nupdated,
        "status": "draft",
        "provenance.trust": "untrusted",
    }
    # Preserve any other keys from the original frontmatter (non-clobbering).
    for k, v in meta.items():
        if k not in block:
            block[k] = v

    lines = ["---"]
    for k, v in block.items():
        sv = str(v)
        # Quote values containing YAML-special characters.
        if any(c in sv for c in (":", "#", "[", "]", "{", "}", ",")):
            sv = f'"{sv}"'
        lines.append(f"{k}: {sv}")
    lines.append("---")
    lines.append("")
    lines.append(body.lstrip("\n"))

    return "\n".join(lines)


def validate(content: str) -> list[str]:
    """Return a list of validation errors (empty list = valid).

    Called by the host drain before signing to validate untrusted capture
    content. Checks that required keys are present and classification is a
    known tier.
    """
    from .classification import TIERS

    meta, _body = fm.parse_text(content)
    errors: list[str] = []

    if not meta:
        errors.append("no frontmatter")
        return errors

    for key in REQUIRED_KEYS:
        if key not in meta:
            errors.append(f"missing required key: {key}")

    cls_val = str(meta.get("classification", ""))
    if cls_val and cls_val not in TIERS:
        errors.append(f"unknown classification: {cls_val!r} (valid: {list(TIERS)})")

    return errors

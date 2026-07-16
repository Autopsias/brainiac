"""Retro fold — the self-improvement loop (PUSH redesign, field 2026-07-13).

Once a week the synthesis session runs this over the vault's own maintenance
output, scanning for FAILURE SIGNATURES the engine produced — future-dated
folds, absolute-path leakage, duplicate findings re-reported under fresh
idempotency keys, unbounded hot.md growth — and, for each engine-level defect,
writes a ready-to-run prompt file into ``.brain/engine-feedback/`` (the exact
shape of the hand-compiled field report that motivated this redesign). Any
interactive session (or the owner) can then fire that prompt at the engine
repo. This converts the manual ``/improve`` retrospective into a standing
automatic behaviour.

Detectors are PURE and deterministic (testable); the judgment of what to do
about a signature stays with the synthesis model that reads the prompts.
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

# hot.md is one "<!-- idempotency-key: K -->\n## <date> — <title>\n<body>" block
# per finding, blank-line separated.
_KEY_RE = re.compile(r"<!--\s*idempotency-key:\s*(?P<key>[^>]+?)\s*-->")
_HEADER_DATE_RE = re.compile(r"^##\s*(?P<date>\d{4}-\d{2}-\d{2})\b", re.MULTILINE)
_ABS_PATH_RE = re.compile(r"(?:/Users/|/home/|[A-Z]:\\\\)[^\s`)]+")
_ISO_IN_KEY_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_DATED_ARTIFACT_RE = re.compile(r"^(?:brief|digest)-(\d{4}-\d{2}-\d{2})\.html$")

HOT_MD_SOFT_MAX_BYTES = 32_768  # ~32 KB: the size hot.md reached UNREAD in the field


def _blocks(hot_md_text: str) -> list[tuple[str, str]]:
    """Split hot.md into ``(key, block_text)`` per idempotency-keyed finding."""
    out: list[tuple[str, str]] = []
    for chunk in re.split(r"\n(?=<!--\s*idempotency-key:)", hot_md_text or ""):
        m = _KEY_RE.search(chunk)
        if m:
            out.append((m.group("key").strip(), chunk))
    return out


def detect_future_dates(hot_md_text: str, today: datetime.date) -> list[str]:
    """Idempotency keys or headers stamped with a date AFTER ``today`` — the
    signature of a fold that computed a future date (field bug 1)."""
    hits: list[str] = []
    for key, block in _blocks(hot_md_text):
        dates = _ISO_IN_KEY_RE.findall(key) + _HEADER_DATE_RE.findall(block)
        for ds in dates:
            try:
                if datetime.date.fromisoformat(ds) > today:
                    hits.append(f"{key} (dated {ds})")
                    break
            except ValueError:
                continue
    return hits


def detect_absolute_paths(hot_md_text: str) -> list[str]:
    """Absolute host paths leaked into fold output — they go stale on a vault
    move (field bug 3). Returns the offending keys."""
    hits: list[str] = []
    for key, block in _blocks(hot_md_text):
        if _ABS_PATH_RE.search(block):
            hits.append(key)
    return hits


def detect_duplicate_findings(hot_md_text: str) -> list[str]:
    """The same finding body re-reported under >1 distinct idempotency key —
    the fold churning identical content under fresh (e.g. date-based) keys."""
    seen: dict[str, str] = {}
    dupes: list[str] = []
    for key, block in _blocks(hot_md_text):
        # Normalize away the key line + the dated header so only the finding
        # body drives equality.
        body = _KEY_RE.sub("", block)
        body = _HEADER_DATE_RE.sub("##", body).strip()
        if not body:
            continue
        if body in seen and seen[body] != key:
            dupes.append(key)
        else:
            seen.setdefault(body, key)
    return dupes


def detect_future_artifacts(brief_dir: Path, today: datetime.date) -> list[str]:
    hits: list[str] = []
    if not brief_dir.is_dir():
        return hits
    for f in sorted(brief_dir.glob("*.html")):
        m = _DATED_ARTIFACT_RE.match(f.name)
        if not m:
            continue
        try:
            if datetime.date.fromisoformat(m.group(1)) > today:
                hits.append(f.name)
        except ValueError:
            continue
    return hits


def scan(hot_md_text: str, brief_dir: Path, today: datetime.date,
         hot_md_bytes: int = 0) -> dict[str, list[str]]:
    """Run every detector; returns ``signature -> evidence list`` for the
    non-empty ones only."""
    raw = {
        "future-dates": detect_future_dates(hot_md_text, today),
        "future-artifacts": detect_future_artifacts(brief_dir, today),
        "absolute-paths": detect_absolute_paths(hot_md_text),
        "duplicate-findings": detect_duplicate_findings(hot_md_text),
    }
    findings = {k: v for k, v in raw.items() if v}
    if hot_md_bytes > HOT_MD_SOFT_MAX_BYTES:
        findings["hot-md-bloat"] = [f"{hot_md_bytes} bytes (> {HOT_MD_SOFT_MAX_BYTES})"]
    return findings


_TITLES = {
    "future-dates": "Maintenance fold stamped a future date",
    "future-artifacts": "Future-dated brief/digest artifacts present",
    "absolute-paths": "Absolute host paths leaked into fold output",
    "duplicate-findings": "Identical findings re-reported under fresh idempotency keys",
    "hot-md-bloat": "hot.md grew large without owner consumption",
}


def render_engine_feedback(signature: str, evidence: list[str],
                           today: datetime.date) -> tuple[str, str]:
    """Return ``(slug, markdown)`` — a ready-to-run engine-repo prompt for one
    signature, shaped like the field report that motivated this redesign."""
    slug = f"{today.isoformat()}-{signature}"
    title = _TITLES.get(signature, signature)
    ev = "\n".join(f"- `{e}`" for e in evidence[:20])
    if len(evidence) > 20:
        ev += f"\n- … {len(evidence) - 20} more"
    md = (
        f"# Engine feedback ({today.isoformat()}): {title}\n\n"
        f"The retro fold detected the **{signature}** failure signature in this "
        f"vault's own maintenance output. This is an ENGINE defect (the vault "
        f"can't fix it in config) — run this prompt against the Brainiac engine "
        f"repo.\n\n"
        f"## Evidence\n{ev}\n\n"
        f"## Ask\n"
        f"Reproduce the signature (measure first), find the root cause in "
        f"src/brain/, fix it once at the shared code path, add a regression "
        f"test, and ship through the normal wheel + package_clients.py path. "
        f"When fixed, this feedback file can be deleted.\n"
    )
    return slug, md

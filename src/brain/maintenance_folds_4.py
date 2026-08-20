"""Self-organization folds: version chains, PARA filing, autodedup, decisions."""
from __future__ import annotations

import datetime
import itertools
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any
import hashlib
import os


# ---------------------------------------------------------------------------
# Self-organization folds (owner decision 2026-07-11): metadata, versioning,
# PARA zoning and navigation are AUTOMATIC nightly maintenance, not user
# input. Synthesis (writing new prose notes) remains session work — these
# folds only manage METADATA and generated views, never note bodies.
# ---------------------------------------------------------------------------
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|.+?)?\]\]")
_PARA_ZONES = ("projects", "areas", "resources", "archive")
_VERSION_ID_RE = re.compile(r"^(?P<base>.+?)-v(?P<num>\d{1,3})$")
_LEADING_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")


def version_family_key(note_id: str) -> tuple[str, int] | None:
    """(family, version) for an id that names an explicit document version
    (``…-v12``), else None. The family key strips ONE leading capture-date
    prefix so re-captures of the same document line up
    (``2026-07-09-…-annex-v12`` and ``2026-05-27-…-annex-v10`` are one
    family). Deliberately conservative: only a trailing ``-v<digits>``
    counts — ``-v4-0``, ``-vf``, ``-vcomentada-26`` never chain."""
    m = _VERSION_ID_RE.match(note_id)
    if not m:
        return None
    base = _LEADING_DATE_RE.sub("", m.group("base"), count=1)
    return base, int(m.group("num"))


def auto_version_chains(core: Any) -> dict[str, Any]:
    """VER-01: stamp supersession chains across explicit version families.

    Groups indexed notes by ``version_family_key``, orders each family by
    (version, valid-date, id), and retires each predecessor via the AUDITED
    ``core.supersede`` path (both sides signed, journaled, invariant-checked
    — never a raw frontmatter poke). Idempotent: an already-retired
    predecessor is skipped; a predecessor already superseded by something
    OUTSIDE the computed chain (a human's manual call) freezes its family —
    reported, never overridden.

    A family is only chained when its order is UNAMBIGUOUS — version numbers
    all distinct AND non-decreasing in valid-date once sorted by number. The
    family key strips one leading capture-date prefix so re-captures line up,
    which also merges genuinely INDEPENDENT series of the same document; when
    it does, the linear (number, date) sort invents nonsense. Measured
    2026-07-27 on a live vault, two shapes: one family held `…-v2` three
    times (three separate captures months apart) and the computed chain
    linked v1 -> v1 and ran BACKWARDS in time (the later v1 superseded by
    the earlier v2); another had distinct numbers but numbers and dates
    disagreed (a mid-sequence version dated two months BEFORE its
    neighbours) and carried two live heads. Such families were saved only
    by the conflict
    freeze, which then re-reported them as owner action every run — an alert
    with no owner action behind it, since the MANUAL chains were already
    correct. Ambiguous families are now skipped quietly (``skipped_ambiguous``
    — informational, never action-required); ``skipped_conflict`` keeps its
    original meaning: an orderable family whose manual chain disagrees, which
    IS a human call."""
    rows = core.index.conn.execute(
        "SELECT id, is_latest_version, superseded_by, "
        "COALESCE(NULLIF(effective_date,''), NULLIF(document_date,''), created) "
        "FROM notes").fetchall()
    families: dict[str, list[tuple[int, str, str, dict[str, str]]]] = {}
    for nid, ilv, sup_by, vdate in rows:
        key = version_family_key(str(nid))
        if key is None:
            continue
        fam, num = key
        families.setdefault(fam, []).append(
            (num, str(vdate or ""), str(nid),
             {"is_latest": str(ilv or ""), "superseded_by": str(sup_by or "")}))

    report: dict[str, Any] = {"chained": [], "skipped_conflict": [],
                              "skipped_ambiguous": [], "errors": []}
    for fam, members in sorted(families.items()):
        if len(members) < 2:
            continue
        members.sort()
        ordered = [m[2] for m in members]
        meta = {m[2]: m[3] for m in members}
        # Unambiguous-order gate (see docstring). Duplicate version numbers
        # mean several captures of the SAME document version — not a
        # supersession — and a valid-date that moves backwards as the version
        # number rises means numbers and time disagree about what is newer.
        # Either way the engine cannot derive the order, so it declines
        # instead of inventing one.
        nums = [m[0] for m in members]
        dates = [m[1] for m in members]
        if len(set(nums)) != len(nums) or any(
                b and a and b < a for a, b in zip(dates, dates[1:])):
            report["skipped_ambiguous"].append(fam)
            continue
        # A manual chain pointing outside the computed order freezes the family.
        conflict = any(
            meta[nid]["superseded_by"] and meta[nid]["superseded_by"] != ordered[i + 1]
            for i, nid in enumerate(ordered[:-1]))
        if conflict:
            report["skipped_conflict"].append(fam)
            continue
        for old_id, new_id in zip(ordered[:-1], ordered[1:]):
            if meta[old_id]["superseded_by"] == new_id:
                continue  # already chained — idempotent re-run
            try:
                core.supersede(old_id, new_id, reason="auto version-chain (nightly self-organization)")
                report["chained"].append({"old": old_id, "new": new_id, "family": fam})
            except Exception as exc:  # noqa: BLE001 — one bad family never aborts the fold
                report["errors"].append({"family": fam, "old": old_id,
                                         "new": new_id, "error": str(exc)})
                break
    return report


# ---------------------------------------------------------------------------
# DDP-01: HIGH-CONFIDENCE auto-dedup tier — nightly fold, same daily branch as
# VER-01 above. Retires ONLY the provably-safe duplicate tier (sha256-identical
# bodies, same classification, same zone+type, neither side a recurring
# generated-artifact pattern) through the audited `core.supersede` path.
# Everything else (cosine-similar-but-not-identical, mixed
# classification/zone/type, recurring-pattern pairs) stays with the weekly
# synthesis / owner inbox exactly as before — this fold makes zero judgment
# calls, only arithmetic ones.
# ---------------------------------------------------------------------------
_AUTODEDUP_DEFAULT_CAP = 10


def _normalize_body_for_hash(body: str) -> str:
    """Strip trailing whitespace per line + surrounding blank lines. Only
    trailing-whitespace noise is normalized — real content differences (even
    one word) must still produce a different hash."""
    return "\n".join(ln.rstrip() for ln in body.splitlines()).strip()


def _body_bytes(body: str) -> int:
    """The body's size in UTF-8 BYTES — the unit ``$BRAIN_FAMILY_MIN_BODY`` is
    declared in and the unit this fold REPORTS. ``len(str)`` counts Unicode
    scalars, so 400 CJK characters (1,200 bytes) read as 400 and were refused
    below a 1,024-"byte" floor while 400 ASCII characters were the same number.
    One semantics, at every site that consults the floor (ENF-01 round 2)."""
    return len(body.encode("utf-8"))


def _floor_bytes(body: str) -> int:
    """The body's size AS THE DUPLICATE-IDENTITY TEST SEES IT — normalized
    first, then measured in UTF-8 bytes.

    The floor and the identity hash must measure the SAME string or the floor
    is decorative. ``body_sha256`` hashes ``_normalize_body_for_hash(body)``,
    which strips per-line trailing whitespace and surrounding blank lines; the
    floor used to measure the raw bytes. So two copies of an 18-byte failed-OCR
    stub padded with trailing spaces hashed identically AND measured 1,118
    bytes each — above a 1,024-byte floor — and were auto-retired (reproduced,
    adversarial review round 3, 2026-08-10). Whitespace padding is exactly what
    failed OCR and PDF text extraction emit, so this was the live case, not a
    contrived one.

    Only whitespace is discounted: real content still counts every byte."""
    return _body_bytes(_normalize_body_for_hash(body))


def body_sha256(body: str) -> str:
    """sha256 of the BODY ONLY (post-frontmatter), normalized per
    ``_normalize_body_for_hash``. Frontmatter (ids, dates, classification)
    differs trivially between re-ingestions of the same content — hashing
    the body alone is what makes those re-ingestions detectable as
    duplicates in the first place."""

    return hashlib.sha256(_normalize_body_for_hash(body).encode("utf-8")).hexdigest()


def autodedup_max_per_run() -> int:
    """Bounded trickle, never a mass migration (owner cap). Malformed/missing
    env falls back to the default rather than raising."""

    try:
        return max(0, int(os.environ.get("BRAIN_AUTODEDUP_MAX_PER_RUN", "").strip()
                           or _AUTODEDUP_DEFAULT_CAP))
    except ValueError:
        return _AUTODEDUP_DEFAULT_CAP


def render_autodedup_hot_entry(result: dict[str, Any], today: datetime.date) -> str:
    """ONE log line per run, only when the fold actually did something
    (retired a pair, or hit the cap) — a record, never a queue item
    (self-organizing-vault / PUSH-interaction posture, same as
    graph_hygiene's hot entry)."""
    lines = [f"## {today.isoformat()} — Auto-dedup (DDP-01)"]
    retired = result.get("retired", [])
    lines.append(
        f"- **Context:** {len(retired)} sha256-identical duplicate pair(s) "
        f"auto-superseded this run (cap {result.get('cap')})."
    )
    for pair in retired[:10]:
        lines.append(f"  - `{pair['old']}` -> `{pair['new']}`")
    if result.get("truncated"):
        lines.append(
            f"- **Remainder:** {result['truncated']} more provably-safe "
            "duplicate pair(s) found but left untouched this run (cap "
            "reached) — will be picked up next run."
        )
    return "\n".join(lines) + "\n"


def auto_para(vault: Path, audit: Any | None = None) -> dict[str, Any]:
    """PAR-01: file brain/ notes into their PARA zone by METADATA, not by a
    human dragging files. Two deliberately small rules:

    - ``type: project``          -> ``brain/projects/``
    - ``is_latest_version: false`` (retired by a supersession chain)
                                  -> ``brain/archive/``

    Generated views (``type: index``/``moc``) and everything else stay where
    they are. Moves are by-id-safe: wikilinks target ids, not paths, and the
    next index sync reconciles paths.

    THE MOVE IS AUDITED (2026-08-18). The audit chain is keyed on PATH, so a
    bare ``rename`` broke a correctly-signed note in two directions at once:
    ``content_drift`` reported the old path ``missing`` (unexplained drift, so
    the health verdict went DEGRADED) and ``unsigned_notes`` counted the new
    path as never signed (an absolute ratchet, so the invariant regressed and
    stayed regressed). Measured on a live reference vault, where one note written
    through ``brain write`` was reported unsigned by the same engine that had
    just signed it. "The next sync reconciles paths" was true of the INDEX and
    false of the CHAIN.

    ``audit`` is the host's ``AuditChain``. Signing happens BEFORE the rename
    and a key failure SKIPS the move (fail closed, exactly like ``write_note``)
    — a fold that cannot sign must never produce an unsigned note. Called
    without a chain (tests, the VM leg) it reports every candidate as skipped
    rather than moving unsigned."""
    from . import frontmatter as fm
    from .audit import KeyUnavailable
    from .notes import sha256_text

    brain_dir = vault / "brain"
    report: dict[str, Any] = {"moved": [], "errors": [], "skipped_unsigned": []}
    if not brain_dir.is_dir():
        return report
    for p in sorted(brain_dir.rglob("*.md")):
        if p.name in ("backlinks.md", "catalog.md", "index.md"):
            continue
        try:
            text = p.read_text(encoding="utf-8")
            meta, _ = fm.parse_text(text)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append({"file": str(p), "error": str(exc)})
            continue
        ntype = str(meta.get("type") or "")
        if ntype in ("index", "moc"):
            continue
        retired = str(meta.get("is_latest_version")).lower() == "false"
        dest_zone = ("archive" if retired
                     else "projects" if ntype == "project" else None)
        if dest_zone is None or p.parent.name == dest_zone:
            continue
        dest_dir = brain_dir / dest_zone
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / p.name
        if dest.exists():
            report["errors"].append({"file": str(p), "error": f"collision at {dest}"})
            continue
        old_rel = p.relative_to(vault).as_posix()
        new_rel = dest.relative_to(vault).as_posix()
        note_id = str(meta.get("id") or p.stem)
        if audit is None:
            report["skipped_unsigned"].append(
                {"id": note_id, "reason": "no audit chain — refusing to move "
                                          "a signed note to an unsigned path"})
            continue
        # Sign the destination FIRST. If this raises, nothing moved.
        try:
            audit.append(verb="write", path=new_rel,
                         reason=f"auto-para: filed {note_id} into {dest_zone}/",
                         content_sha256=sha256_text(text))
        except KeyUnavailable as exc:
            report["skipped_unsigned"].append({"id": note_id, "reason": str(exc)})
            continue
        try:
            p.rename(dest)
        except OSError as exc:
            audit.append(verb="write_failed", path=new_rel,
                         reason=f"auto-para rename failed: {type(exc).__name__}: {exc}")
            report["errors"].append({"file": str(p), "error": str(exc)})
            continue
        # Retire the old path so content_drift stops reporting it `missing`.
        audit.append(verb="delete", path=old_rel,
                     reason=f"auto-para: {note_id} moved to {new_rel}")
        report["moved"].append({"id": note_id, "to": f"brain/{dest_zone}/"})
    return report


# ---------------------------------------------------------------------------
# Decision-capture nudge (DEC-01, 2026-07-11). Measured failure, G&P
# benchmark round 6: a real perimeter decision lived FIVE DAYS in a slide
# deck ("decided 6-Jul", "decision taken") without a `type: decision` note —
# so every decision-first agent was confidently stale, and the
# decision-layer-authoritative rule amplified the gap. This fold closes the
# loop: every maintain run scans RECENTLY captured non-decision notes for
# decision language and queues each hit ONCE to hot.md as a decision-note
# candidate. A nudge, not a writer — capturing the decision note stays
# owner/synthesis work (P-10 human gate), so false positives cost one
# hot-queue line, never a wrong decision record.
# ---------------------------------------------------------------------------
_DECISION_LANGUAGE_RE = re.compile(
    r"(?<![a-z])("
    r"decided(?:\s+on)?\s+\d|decided:\s|decision\s+(?:was\s+)?taken|"
    r"we\s+(?:have\s+)?decided|formally\s+approved|approved\s+on\s+\d|"
    r"signed\s+off\s+on|sign-off\s+given|"
    r"foi\s+decidido|decidiu-se|decis[aã]o\s+tomada|aprovado\s+em\s+\d"
    r")", re.IGNORECASE)
DECISION_CAPTURE_LOOKBACK_DAYS = 3
DECISION_CAPTURE_MAX_CANDIDATES = 10


def decision_capture_scan(
    conn: Any, today: datetime.date,
    lookback_days: int = DECISION_CAPTURE_LOOKBACK_DAYS,
    limit: int = DECISION_CAPTURE_MAX_CANDIDATES,
) -> list[dict[str, Any]]:
    """Notes captured within ``lookback_days`` whose body carries decision
    language but whose ``type`` is not ``decision``. Returns at most
    ``limit`` candidates (id, date, phrase, snippet), newest first. Pure
    read — no writes, no egress concern (consumers queue to host-only
    hot.md)."""
    since = (today - datetime.timedelta(days=lookback_days)).isoformat()
    # Retired version-family members (is_latest_version: false) are excluded:
    # every sibling of a versioned deck repeats the same decision language —
    # only the family head is a meaningful capture candidate (live run
    # 2026-07-11: retired 6pager versions crowded the candidate cap).
    rows = conn.execute(
        "SELECT id, type, body, "
        "COALESCE(NULLIF(effective_date,''), NULLIF(document_date,''), created) "
        "FROM notes WHERE type != 'decision' AND created >= ? "
        "AND COALESCE(is_latest_version,'') != 'false' "
        "ORDER BY created DESC", (since,)).fetchall()
    out: list[dict[str, Any]] = []
    for nid, ntype, body, vdate in rows:
        m = _DECISION_LANGUAGE_RE.search(body or "")
        if not m:
            continue
        start = max(0, m.start() - 80)
        snippet = " ".join((body[start:m.end() + 120]).split())
        out.append({"id": str(nid), "type": str(ntype or ""),
                    "date": str(vdate or ""), "phrase": m.group(0),
                    "snippet": snippet})
        if len(out) >= limit:
            break
    return out


def render_decision_capture_hot_entry(c: dict[str, Any], today: datetime.date) -> str:
    return "\n".join([
        f"## {today.isoformat()} — decision-capture candidate: `{c['id']}`",
        f"- **Context:** a freshly captured source (valid date {c.get('date') or '?'}) "
        f"carries decision language (“{c['phrase']}”) but no `type: decision` "
        f"note records it.",
        f"- **Snippet:** …{c['snippet']}…",
        "- **Owner input needed:** if this is a real decision, capture it as a "
        "`type: decision` note (and `brain supersede` whatever it reverses); "
        "if not, ignore — this entry never repeats for this note.",
    ]) + "\n"

# Cross-section binds, deferred past this module's own defs.

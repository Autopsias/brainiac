"""WAT-01 — the corpus-invariants watchdog: seven cheap counts, trended,
alerting, plus its own dead-man's switch.

Corpus properties drifted for months each because nobody had a NUMBER for
them. This module computes those counts and nothing else; the nightly
``maintain`` umbrella runs it as a daily fold (see ``BrainCore.maintain``,
the ``corpus_invariants`` block), persists them into ``maintain-state.json``
+ ``health-history.jsonl``, and ``brain health-report`` renders them under
"Corpus invariants".

The seven:

1. ``unlinked_sources``  — ``raw/``-zone notes with ZERO inbound reference
   (a wikilink from any note body, a ``source:``/``replaces:`` frontmatter
   link from a ``brain/`` note, or an indexed supersession link). Excludes
   the vault's own re-ingested operational output (ENF-06), which is not
   knowledge anyone should be writing a note about.
2. ``cross_tier_twins``  — ``<ingest-date>-<slug>`` / ``<slug>`` id pairs
   whose two notes carry DIFFERENT ``classification`` (an Internal-capped
   reader reaching Restricted substance through the twin).
3. ``cross_tier_duplicates`` — the same exposure found by CONTENT instead of
   by filename (ENF-03): documents that exist twice at two tiers however
   their ids are spelled. Metric 2 sees ONE filename shape and reads 0 while
   these sit in the corpus; metric 3 is blind to a name twin whose two copies
   are genuinely different documents. Neither contains the other, so both
   ship.
4. ``cross_tier_candidates`` — the UNDECIDED half of metric 3: pairs sharing
   a document's vocabulary but not its word order. Reported, never guessed
   at, never silently counted as clean.
5. ``unguarded_ingests`` — ENF-04: sources the ingest pipeline admitted while
   the cross-tier guard was UNAVAILABLE (no index, a read error, or the env
   kill switch). Should be 0 and stay 0, so it ratchets. The guard's raises,
   its refusals below the ENF-01 floor, and the sources predating it are
   reported beside it — per leg for raises, because an aggregate cannot tell
   a clean corpus from a dead leg.
6. ``subfloor_families`` — supersession families whose members share one
   normalized body hash (a DDP-01 auto-link) and where some member's body is
   below the ``$BRAIN_FAMILY_MIN_BODY`` floor (auto-superseding on empty-OCR
   stubs).
7. ``unreachable_gold``  — READ, never re-run, from the newest
   ``eval/s06_reachability.py`` artifact: gold documents no ranking leg
   reaches at all.
8. ``unsigned_notes`` — indexed notes without an audit-chain entry; its
   implementation lives in ``invariant_unsigned_notes``.

All eight are no-model and read-only. 1-4 and 6 read the index (plus the
``brain/`` zone's frontmatter, ~hundreds of small files); 5 reads ``raw/``
frontmatter (~1.7s on the reference vault); 7 reads one JSON artifact; 8
reads the audit chain once and stats each note (no note bodies).

**Thresholds are ABSOLUTE and RATCHETING, never percent** (hardening G3). A
percent-regression check against a trailing baseline dies at zero — the exact
state this plan's backfills drive three of these metrics toward — and an
absolute threshold pinned at today's large baseline never fires again once
the backfill lands. So each metric carries a FLOOR: the best (lowest) value
ever recorded, persisted in ``maintain-state.json``. A regression is
``value > floor + tolerance`` (tolerance default 0, env-tunable per metric).
That one rule works identically at 2,132 and at 0: as a backfill ratchets the
floor down to zero, the very next new violation fires.

**The dead-man's switch** (owner ruling 2026-08-10, "we need an alarm on the
alarm too"): the fold's own last-successful-run date is part of the health
record, and a row older than ``$BRAIN_INVARIANTS_MAX_AGE_DAYS`` (default 3)
— or missing entirely on a vault whose other branches DO run — is itself a
finding. It is checked from lanes that do not depend on the fold firing:
``brain doctor``/``brain health-report`` (a STALE row -> DEGRADED),
``maintenance.degradation_findings`` (-> the per-day notify marker the
SessionStart alerts hook reads), and the weekly synthesis watchdog prompt.

Because the fold writes a real ``maintain-state.json`` row, ES-01's own
branch-liveness gate applies to it too (``_CADENCE_DAYS["corpus_invariants"]``
= 1, loud past 2 cadences). So a stale row is caught TWICE, at 2 days by
ES-01 and at ``$BRAIN_INVARIANTS_MAX_AGE_DAYS`` by the explicit check — the
explicit one is the tunable contract and the one that also covers a row that
is MISSING ENTIRELY, which ES-01 structurally cannot see (it iterates the
rows that are present).
"""
from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path
from typing import Any

from .invariant_reachability import unreachable_gold
from .invariant_unsigned_notes import unsigned_notes

# The metric names, in report order. Every consumer (state, health
# record, report, alerting) iterates THIS tuple rather than re-listing them.
INVARIANT_METRICS = (
    "unlinked_sources",
    "cross_tier_twins",
    "cross_tier_duplicates",
    "cross_tier_candidates",
    "unguarded_ingests",
    "subfloor_families",
    "unreachable_gold",
    "unsigned_notes",
)

# The state key this fold owns inside maintain-state.json. It is a plain
# (non-`_`-prefixed) key on purpose: `maintenance.maintain_escalation` and
# `doctor.check_vm_maintain_heartbeat` iterate those and therefore give the
# fold ES-01 liveness escalation for free, on top of the explicit
# max-age check below.
STATE_KEY = "corpus_invariants"

MAX_AGE_DAYS_ENV = "BRAIN_INVARIANTS_MAX_AGE_DAYS"
DEFAULT_MAX_AGE_DAYS = 3
REACHABILITY_GLOB_ENV = "BRAIN_INVARIANTS_REACHABILITY_GLOB"
SAMPLE_CAP = 10

_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}-(?=.)")


# ---------------------------------------------------------------------------
# G12 — the exclusion set, defined ONCE here and imported by every consumer
# (the s05/s06/s07 backfills and the weekly lane import these, they never
# re-derive "which sources are linkable"). Some sources are unlinkable BY
# DESIGN; counting them keeps a "drive it to zero" population from ever
# reaching zero, and silently dropping them makes "0 unlinked" mean "0 except
# the ones we skip". So they are excluded AND counted, per reason.
# ---------------------------------------------------------------------------
QUARANTINE_PATH_SEGMENTS = ("_quarantine", "_duplicate", "_candidates")
NON_KNOWLEDGE_ZONES = ("inbox", "overlay", ".brain")
GENERATED_MAP_BASENAMES = frozenset({"backlinks.md", "catalog.md"})

# ENF-06 — a vault's OWN operational output, re-ingested as a `raw/` source.
# These arrive carrying the frontmatter they were written with, so they declare
# their own kind in the FIRST block of their body, while the ingest wrapper
# stamps every one of them `type: source`. That embedded type is the only
# signal separating "an audit record this vault emitted" from "a client
# document that happens to be about an audit": the wrapper frontmatter of the
# two is byte-identical, and `generated_by:` — which would have been the honest
# marker — is present on 5 sources out of 2,217.
#
# The set is deliberately CONSERVATIVE and explicit, never a prefix match. Kinds
# a human might author about the business — report, review, analysis, proposal,
# memo — are NOT here, and neither are the Chief-of-Staff briefs: those are
# machine-produced but they are about the business, so they stay in the
# knowledge population where a reader can reach them.
OPERATIONAL_SOURCE_TYPES = frozenset({
    "alert",
    "audit",
    "cos-nightly",
    "cos-nightly-companion",
    "eval",
    "graph-health-alert",
    "log",
    "runbook",
    "workspace",
})

EXCLUSION_REASONS = (
    "quarantined",       # verification or signing FAILED — never expected to be linked
    "superseded",        # is_latest_version: false — the successor carries the links
    "non_knowledge_zone",  # inbox/ overlay/ .brain/ — not knowledge, never indexed
    "generated_map",     # backlinks.md / catalog.md — generated, not authored
    "operational_artifact",  # this vault's own audit/log/alert output, re-ingested
)


# The body prefix `_unlinked_rows` pulls out of the index to read that block.
# One source in this corpus is 4.2MB, so the whole body is never loaded here.
EMBEDDED_FRONTMATTER_HEAD = 8000


def embedded_source_type(head: str) -> str:
    """The ``type:`` a re-ingested source declares in its OWN leading
    frontmatter block, lowercased, or ``""`` when it declares none.

    Reads the body prefix the index already holds, so this costs no file I/O.
    Scanning the frontmatter of every raw source off disk instead measured 4.4s
    per fold on a 2,217-source corpus, which would have made this check the
    dominant cost of the whole nightly invariants fold.

    Deliberately does NOT require the closing ``---`` to be present. An earlier
    regex did, and a 600-char prefix silently missed 262 of the 531 sources
    that declare a type — the longest leading block in this corpus is 23,611
    characters. Scanning line by line until the closing fence OR the end of the
    prefix means a truncated block still yields its type, which sits in the
    first few lines of every artifact this has been measured against.
    """
    s = (head or "").lstrip()
    if not s.startswith("---"):
        return ""
    for line in s.split("\n")[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"type:\s*(\S+)", line)
        if m:
            return m.group(1).strip().strip("\"'").lower()
    return ""


def link_coverage_exclusion(
    *, path: str = "", zone: str = "", is_latest_version: Any = None,
    embedded_type: str = "",
) -> str | None:
    """The reason ``path`` is excluded from the link-coverage population, or
    ``None`` when it counts. One definition, four callers (G12).

    ``embedded_type`` is opt-in per caller and defaults to "": the reasons do
    not all transfer between metrics (see the cross-tier metric, where a
    superseded twin still leaks and must still count), so a caller passes it
    only when "this vault wrote it" genuinely removes the row from ITS
    population.
    """
    parts = (path or "").replace("\\", "/").split("/")
    basename = parts[-1] if parts else ""
    if any(seg in QUARANTINE_PATH_SEGMENTS for seg in parts):
        return "quarantined"
    if str(zone or "") in NON_KNOWLEDGE_ZONES:
        return "non_knowledge_zone"
    if basename in GENERATED_MAP_BASENAMES:
        return "generated_map"
    if str(is_latest_version or "").strip().lower() == "false":
        return "superseded"
    if str(embedded_type or "").strip().lower() in OPERATIONAL_SOURCE_TYPES:
        return "operational_artifact"
    return None


def _resolve(resolver: dict[str, str], target: str) -> str | None:
    """Resolve one wikilink target to a note id. Beyond
    ``graph._build_resolver``'s id/stem/title keys this also strips a leading
    zone directory (``[[raw/2026-06-27-foo]]``), which is the shape AGENTS.md
    §2 prescribes for a ``source:`` link and which the plain resolver misses."""
    t = (target or "").strip()
    if not t:
        return None
    from .link_targets import resolve_target
    hit = resolve_target(resolver, t)
    if hit:
        return hit
    if "/" in t:
        return resolve_target(resolver, t.rsplit("/", 1)[-1])
    return None


def _frontmatter_links(vault: Path, paths: list[str]) -> list[str]:
    """Every ``[[...]]`` target appearing in the FRONTMATTER of ``paths``.

    The index stores the post-frontmatter body only, so a ``source:
    "[[raw/…]]"`` provenance link — the canonical way a ``brain/`` note cites
    the raw source it derives from — is invisible to a body-only scan and
    would make every properly-cited source read as "unlinked". Reading the
    frontmatter block back off disk is the cheap fix.

    ponytail: only the ``brain/`` zone is read (a few hundred small files);
    ``raw/`` sources do not cite each other and are the large ones. If that
    ever stops holding, widen the caller's path list, not this function.
    """
    from . import frontmatter as fm
    from .graph import parse_wikilinks

    out: list[str] = []
    for p in paths:
        path = Path(p)
        if not path.is_absolute():
            path = vault / p
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, _body = fm.parse_text(text)
        if not meta:
            continue
        # Scan the rendered frontmatter mapping, not the raw text: values may
        # be lists (`aliases`) or scalars, and json.dumps flattens both.
        out.extend(parse_wikilinks(json.dumps(meta, default=str)))
    return out


def _unlinked_rows(
    conn: Any, vault: Path,
) -> tuple[list[dict[str, Any]], dict[str, int], int]:
    """The shared derivation behind metric 1 and the weekly linking lane:
    ``(unlinked rows sorted by id, excluded_by_reason, population size)``.

    A source counts as REFERENCED when any note's body wikilinks it, when a
    ``brain/`` note's frontmatter cites it (``source:``/``replaces:``/…), or
    when it sits on either end of an indexed supersession link."""
    from .graph import _build_resolver, parse_wikilinks

    # substr, not the whole body: only the leading frontmatter block is read
    # here, and one source in this corpus is 4.2MB.
    rows = conn.execute(
        "SELECT id, title, path, zone, is_latest_version, superseded_by, "
        f"previous_version, classification, created, substr(body, 1, {EMBEDDED_FRONTMATTER_HEAD}) "
        "FROM notes"
    ).fetchall()
    resolver = _build_resolver([(r[0], r[1] or "", r[2] or "") for r in rows])

    population: list[dict[str, Any]] = []
    excluded_by_reason: dict[str, int] = {}
    brain_paths: list[str] = []
    referenced: set[str] = set()
    for nid, title, path, zone, is_latest, sup_by, prev, cls, created, head in rows:
        if str(zone or "") == "brain":
            brain_paths.append(str(path or ""))
        for link in (sup_by, prev):
            tgt = _resolve(resolver, str(link or ""))
            if tgt and tgt != nid:
                referenced.add(tgt)
        if str(zone or "") != "raw":
            continue
        reason = link_coverage_exclusion(
            path=str(path or ""), zone=str(zone or ""), is_latest_version=is_latest,
            embedded_type=embedded_source_type(head or ""))
        if reason:
            excluded_by_reason[reason] = excluded_by_reason.get(reason, 0) + 1
            continue
        population.append({
            "id": str(nid), "title": str(title or ""), "path": str(path or ""),
            "classification": str(cls or ""), "created": str(created or ""),
        })

    for (nid, body) in conn.execute("SELECT id, body FROM notes"):
        for target in parse_wikilinks(body or ""):
            tgt = _resolve(resolver, target)
            if tgt and tgt != nid:
                referenced.add(tgt)
    for target in _frontmatter_links(vault, brain_paths):
        tgt = _resolve(resolver, target)
        if tgt:
            referenced.add(tgt)

    unlinked = sorted(
        (rec for rec in population if rec["id"] not in referenced),
        key=lambda rec: rec["id"])
    return unlinked, excluded_by_reason, len(population)


def unlinked_sources(conn: Any, vault: Path, *, cap: int = SAMPLE_CAP) -> dict[str, Any]:
    """``raw/``-zone sources nothing references (metric 1)."""
    unlinked, excluded_by_reason, population = _unlinked_rows(conn, vault)
    return {
        "value": len(unlinked),
        "population": population,
        "excluded": sum(excluded_by_reason.values()),
        "excluded_by_reason": excluded_by_reason,
        "sample": [rec["id"] for rec in unlinked[:cap]],
    }


# ---------------------------------------------------------------------------
# BAK-04 — the standing weekly linking lane.
#
# The s05-s07 backfills clear the accumulated unlinked-source backlog by hand.
# Steady state is the weekly Sunday synthesis session (`brain-synthesis.sh`,
# AGENTS.md §6): each week it links the worst N sources that arrived since it
# last ran, so a normal week's intake never becomes a new backlog. There is NO
# new scheduled task — the nightly `corpus_invariants` fold, which already
# derives this exact population every day, drops a worst-first candidate file
# where the synthesis session reads it.
# ---------------------------------------------------------------------------
LINK_LANE_BUDGET_ENV = "BRAIN_WEEKLY_LINK_BUDGET"
# 40, from measurement rather than taste: the reference vault's raw/ intake
# over the last 12 ISO weeks runs a median of 33.5 sources/week (the one 1,232
# outlier is the corpus migration, not a week of work). 40 clears a normal
# week with headroom; a backlog-clearing week is a deliberate override, not
# the default.
DEFAULT_LINK_LANE_BUDGET = 40
LINK_LANE_RELPATH = ".brain/curation/unlinked-sources.json"
# Worst-first = most sensitive first, then longest-unlinked. The plan's whole
# premise is that the most sensitive content is the least findable, so tier
# outranks age; `created` ascending breaks the tie so a source that has sat
# unlinked for months outranks one that arrived yesterday.
_TIER_ORDER = {"Public": 0, "Internal": 1, "Confidential": 2, "Restricted": 3, "MNPI": 4}
LINK_LANE_SELECTION = (
    "classification descending (unlabelled ranks MNPI), then created ascending, then id"
)


def link_lane_budget() -> int:
    try:
        n = int(os.environ.get(LINK_LANE_BUDGET_ENV, "").strip())
    except ValueError:
        return DEFAULT_LINK_LANE_BUDGET
    return n if n > 0 else DEFAULT_LINK_LANE_BUDGET


def link_lane_candidates(
    conn: Any, vault: Path, *, limit: int | None = None,
) -> dict[str, Any]:
    """The worst-first slice of the unlinked-source population the weekly
    synthesis session should link next.

    Same population and the SAME exclusion set as metric 1 (G12) — this never
    re-derives "which sources are linkable".

    ponytail: re-runs the metric-1 derivation rather than threading its result
    through the fold (~1s/day on the reference vault). If the fold ever gets
    latency-sensitive, pass `_unlinked_rows`' output in instead of recomputing.
    """
    budget = link_lane_budget() if limit is None else limit
    unlinked, excluded_by_reason, population = _unlinked_rows(conn, vault)
    ranked = sorted(
        unlinked,
        key=lambda rec: (
            # An unlabelled note ranks as the most restrictive tier, exactly as
            # the egress gate treats it (AGENTS.md §5) — so it sorts FIRST.
            -_TIER_ORDER.get(rec["classification"], _TIER_ORDER["MNPI"]),
            rec["created"] or "", rec["id"],
        ),
    )
    return {
        "schema_version": 1,
        "generated": datetime.date.today().isoformat(),
        "budget": budget,
        "selection": LINK_LANE_SELECTION,
        "total_unlinked": len(unlinked),
        "population": population,
        "excluded_by_reason": excluded_by_reason,
        "candidates": ranked[:budget],
    }


def write_link_lane(conn: Any, vault: Path, *, limit: int | None = None) -> dict[str, Any]:
    """Compute the lane and drop it at ``LINK_LANE_RELPATH``. Returns the
    payload (minus the candidate bodies) so the fold can report it."""
    payload = link_lane_candidates(conn, vault, limit=limit)
    out = Path(vault) / LINK_LANE_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(out)
    return {"path": str(out), "written": len(payload["candidates"]),
            "total_unlinked": payload["total_unlinked"], "budget": payload["budget"]}


def cross_tier_twins(conn: Any, *, cap: int = SAMPLE_CAP) -> dict[str, Any]:
    """``<ingest-date>-<slug>`` / ``<slug>`` id pairs at DIFFERENT
    classifications (metric 2). Reports the whole twin population alongside
    the cross-tier subset — a twin count alone says nothing about exposure."""
    rows = conn.execute("SELECT id, classification FROM notes").fetchall()
    cls = {str(r[0]): str(r[1] or "") for r in rows}
    pairs = 0
    cross: list[tuple[str, str]] = []
    for nid in sorted(cls):
        if not _DATE_PREFIX.match(nid):
            continue
        stem = _DATE_PREFIX.sub("", nid, count=1)
        if stem not in cls:
            continue
        pairs += 1
        if cls[nid] != cls[stem]:
            cross.append((nid, stem))
    return {
        "value": len(cross),
        "pairs": pairs,
        "sample": [f"{a} ({cls[a] or 'unlabelled'}) / {b} ({cls[b] or 'unlabelled'})"
                    for a, b in cross[:cap]],
    }


# ---------------------------------------------------------------------------
# ENF-03 — the cross-tier near-duplicate detector. CONTENT, never filenames.
#
# `cross_tier_twins` above compares ONE filename shape (`<date>-slug` vs
# `slug`). Every other way one document appears twice — a second embedded
# date, a rename, an OCR-vs-text-layer re-extraction, a `-v1`/`-v2` pair, a
# mangled accent — is invisible to it, so it reads 0 while real cross-tier
# duplicates sit in the corpus. ENF-02 tried to close that on FILENAMES and
# was withdrawn whole: its 138 "detections" were 138 filename matches and 0
# content matches. So this detector never looks at an id.
#
# TWO measures, because one of them cannot honestly decide on its own, and a
# detector that guesses is the failure mode this plan exists to prevent:
#
#   SAME DOCUMENT (decided)  5-word-shingle Jaccard >= 0.60. Word ORDER is
#       load-bearing: two documents share 5-word runs only when one is a copy,
#       a re-extraction or a light edit of the other. This is s03's pre-stated
#       definition (2026-08-10), unchanged, so the >= 0.90 coverage bar is
#       judged on the basis it was written for.
#   SHARES THE SUBSTANCE (undecided)  word-SET Jaccard >= 0.60 while the
#       shingle measure says less. Same vocabulary, different word order: a
#       reformatted copy, a later revision of the same deck — or two genuinely
#       different documents about one subject. The detector will NOT guess.
#       These are REPORTED as unclassified candidates, never merged, never
#       silently counted as clean. (Measured on the reference vault: the
#       shingle population is a strict SUBSET of the word-set population, so
#       one screen serves both.)
#
# The ENF-01 body-size floor applies before either: two notes are never judged
# the same document on a body too short to carry evidence of anything.
# ---------------------------------------------------------------------------
CROSS_TIER_SHINGLE = 5
CROSS_TIER_MIN_TOKENS = 40
CROSS_TIER_SAME_DOC = 0.60      # 5-word-shingle Jaccard: the same document
CROSS_TIER_CANDIDATE = 0.60     # word-set Jaccard: shares the substance
CROSS_TIER_SKETCH = 192         # bottom-k sketch width, screening only
# Screen gate as a fraction of the sketch. A pair at word-set Jaccard 0.60
# lands near 0.43*k shared sketch entries (resemblance j/(2-j)); 0.25*k is
# ~5 sigma below that, and the screen's real-world recall is not assumed from
# this arithmetic — it is MEASURED against an exhaustive all-pairs scan by
# `tools/crosstier_coverage.py`, which is where the reported coverage number
# comes from.
CROSS_TIER_SCREEN = 0.25
# `link_coverage_exclusion` reasons this metric SKIPS. Deliberately NOT
# `superseded`: retiring a note does not remove it from the index
# (`--latest-only` is opt-in), so a superseded low twin is still fully
# readable at its own classification and is still the exposure. Excluding it
# here would blind the detector to exactly the population s04 just linked.
# Superseded notes are therefore KEPT and reported as `retained_superseded`.
CROSS_TIER_SKIP_REASONS = ("quarantined", "non_knowledge_zone", "generated_map")

_CT_FRONTMATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)
_CT_WORD = re.compile(r"\w+", re.U)


def _ct_tokens(body: str) -> list[str]:
    """The normalized token stream both measures are computed over: a leading
    frontmatter block stripped (191 of the July re-ingests carry the source
    file's frontmatter pasted INTO the body — an extraction artifact, not
    content), NFC, casefold, then ``\\w+`` words.

    ``\\w+`` and not ``str.split()``, and that choice is measured rather than
    stylistic: splitting on whitespace leaves punctuation glued to the token,
    so a re-extraction that moves one comma breaks the 5-word runs around it.
    Measured on the reference deployment (ENF-03, 2026-08-12), a live pair of
    one document held at Internal and at MNPI scores 0.484 whitespace-split
    and **0.788** here — invisible against a 0.60 threshold under the one, a
    decided conflict under the other. Punctuation drift is precisely what a
    second extraction pass produces, so the tokenizer must not be sensitive
    to it."""
    import unicodedata
    text = _CT_FRONTMATTER.sub("", body or "", count=1)
    return _CT_WORD.findall(unicodedata.normalize("NFC", text).casefold())


def _ct_shingles(tokens: list[str], k: int = CROSS_TIER_SHINGLE) -> set[str]:
    return {" ".join(tokens[i:i + k]) for i in range(len(tokens) - k + 1)}


def _jaccard(a: set[Any], b: set[Any]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter) if inter else 0.0


def _ct_sketch(tokens: list[str], k: int = CROSS_TIER_SKETCH) -> frozenset[int]:
    """Bottom-k sketch of the word set — the SCREEN only, never a verdict.

    ``zlib.crc32`` and not a cryptographic digest because this runs over every
    note every night and its collisions cost nothing: a survivor is re-checked
    EXACTLY on the real token sets before it is called anything. Holding the
    full shingle sets for the whole corpus instead costs ~760 MB (measured on
    the reference vault); the sketch costs ~4 MB."""
    import zlib
    hashes = sorted({zlib.crc32(t.encode("utf-8")) for t in set(tokens)})
    return frozenset(hashes[:k])


def screen_gate(a: int, b: int) -> int:
    """Shared sketch entries a pair must reach to survive the screen, for
    sketches of size ``a`` and ``b``. THE one definition — the ingest guard
    (``ingest.tierguard``) imports this one rather than carrying its own.

    It SCALES with the smaller sketch, and that is a fix, not a flourish. A
    bottom-k sketch of a set smaller than k IS the whole set, so a document
    above the ENF-01 body floor whose vocabulary is under
    ``CROSS_TIER_SKETCH * CROSS_TIER_SCREEN`` (48 distinct words) — a form, a
    rate card, a repetitive template — can never reach 48 shared entries, and
    a verbatim cross-tier copy of it scoring 5-word-shingle Jaccard 1.000 was
    discarded by the screen before anything measured it. Identical to the old
    fixed ``CROSS_TIER_SKETCH * CROSS_TIER_SCREEN`` (48) wherever both sketches
    are full width, so nothing above the vocabulary threshold changes.

    Loosening the screen can only ADD survivors, never change a verdict: every
    survivor is re-verified exactly on the real token sets afterwards."""
    return max(1, int(CROSS_TIER_SCREEN * min(a, b)))


def _ct_exclusion(path: str, zone: str, ilv: Any) -> str | None:
    return link_coverage_exclusion(path=path, zone=zone, is_latest_version=ilv)


def cross_tier_duplicates(
    conn: Any, *, cap: int = SAMPLE_CAP, detail: bool = False,
) -> dict[str, Any]:
    """Documents that exist twice at DIFFERENT classifications, found by
    content (metric 5, and its undecided half metric 6).

    Returns three numbers that are never collapsed into one:
    ``value`` (decided conflicts), ``candidates`` (undecided), and
    ``coverage`` (the fraction of the exposure population this detector can
    fingerprint at all, with ``population`` as its stated denominator)."""
    from .classification import normalize as tier_of
    from .index import _family_min_body
    from .maintenance import _floor_bytes

    floor = _family_min_body()
    rows = conn.execute(
        "SELECT id, classification, zone, path, is_latest_version, body FROM notes"
    ).fetchall()

    excluded_by_reason: dict[str, int] = {}
    retained_superseded = 0
    population = 0
    too_short = 0
    subfloor = 0
    docs: list[tuple[str, str, frozenset[int]]] = []
    for nid, cls, zone, path, ilv, body in rows:
        reason = _ct_exclusion(str(path or ""), str(zone or ""), ilv)
        if reason:
            excluded_by_reason[reason] = excluded_by_reason.get(reason, 0) + 1
            if reason in CROSS_TIER_SKIP_REASONS:
                continue
            retained_superseded += 1
        population += 1
        # Both exclusions are counted over the SAME population, independently,
        # so neither hides the other: `too_short` stays comparable with s03's
        # 96.1 % fingerprintable fraction (which had no floor), and `subfloor`
        # is the additional ENF-01 refusal on top of it.
        tokens = _ct_tokens(body or "")
        short = len(tokens) < CROSS_TIER_MIN_TOKENS
        below = _floor_bytes(body or "") < floor
        too_short += short
        subfloor += below
        if short or below:
            continue
        docs.append((str(nid), tier_of(cls), _ct_sketch(tokens)))

    # -- screen. Only CROSS-tier pairs can be a finding, so the tier check
    # comes first and most of the O(n^2) never touches a set operation. The
    # gate scales with the smaller sketch (see `screen_gate`) — a fixed 48
    # discarded every low-vocabulary document, verbatim copy or not.
    survivors: list[tuple[int, int]] = []
    for i in range(len(docs)):
        _, tier_i, sketch_i = docs[i]
        for j in range(i + 1, len(docs)):
            if docs[j][1] == tier_i:
                continue
            if len(sketch_i & docs[j][2]) >= screen_gate(len(sketch_i), len(docs[j][2])):
                survivors.append((i, j))

    # -- verify EXACTLY, on the real token sets, for the survivors only.
    wanted = sorted({docs[i][0] for i, _ in survivors} | {docs[j][0] for _, j in survivors})
    tokens_by_id: dict[str, list[str]] = {}
    for start in range(0, len(wanted), 400):
        chunk = wanted[start:start + 400]
        q = ",".join("?" * len(chunk))
        for nid, body in conn.execute(
                f"SELECT id, body FROM notes WHERE id IN ({q})", chunk):
            tokens_by_id[str(nid)] = _ct_tokens(body or "")

    words: dict[str, set[str]] = {}
    shingles: dict[str, set[str]] = {}
    conflicts: list[dict[str, Any]] = []
    unclassified: list[dict[str, Any]] = []
    for i, j in survivors:
        aid, atier, _ = docs[i]
        bid, btier, _ = docs[j]
        toks_a = tokens_by_id.get(aid)
        toks_b = tokens_by_id.get(bid)
        if toks_a is None or toks_b is None:
            continue
        if aid not in words:
            words[aid] = set(toks_a)
        if bid not in words:
            words[bid] = set(toks_b)
        word_j = _jaccard(words[aid], words[bid])
        if word_j < CROSS_TIER_CANDIDATE:
            continue
        if aid not in shingles:
            shingles[aid] = _ct_shingles(toks_a)
        if bid not in shingles:
            shingles[bid] = _ct_shingles(toks_b)
        shingle_j = _jaccard(shingles[aid], shingles[bid])
        rec = {"a": aid, "a_tier": atier, "b": bid, "b_tier": btier,
                "shingle_jaccard": round(shingle_j, 4),
                "word_jaccard": round(word_j, 4)}
        (conflicts if shingle_j >= CROSS_TIER_SAME_DOC else unclassified).append(rec)

    conflicts.sort(key=lambda r: -r["shingle_jaccard"])
    unclassified.sort(key=lambda r: -r["word_jaccard"])

    def _fmt(rec: dict[str, Any], key: str) -> str:
        return (f"{rec[key]:.3f} {rec['a_tier']} {rec['a']} / "
                f"{rec['b_tier']} {rec['b']}")

    out: dict[str, Any] = {
        "value": len(conflicts),
        "candidates": len(unclassified),
        "population": population,
        "comparable": len(docs),
        "coverage": round(len(docs) / population, 4) if population else None,
        "coverage_basis": (
            "comparable / population; population = indexed notes minus "
            + "/".join(CROSS_TIER_SKIP_REASONS)
            + " (superseded notes are RETAINED, they still leak); comparable = "
            f"those with a body >= {floor}B and >= {CROSS_TIER_MIN_TOKENS} tokens"),
        "too_short": too_short,
        "subfloor": subfloor,
        "floor": floor,
        "excluded_by_reason": excluded_by_reason,
        "retained_superseded": retained_superseded,
        "screened": len(survivors),
        "sample": [_fmt(r, "shingle_jaccard") for r in conflicts[:cap]],
        "candidate_sample": [_fmt(r, "word_jaccard") for r in unclassified[:cap]],
    }
    if detail:
        out["conflicts"] = conflicts
        out["unclassified"] = unclassified
    return out


def cross_tier_candidates_entry(dup: dict[str, Any]) -> dict[str, Any]:
    """Metric 6 — the UNDECIDED half of metric 5, ratcheted on its own so a
    growing pile of pairs the detector cannot decide alerts exactly like a
    growing pile it can. ``cross_tier_twins`` had no undecided bucket at all,
    which is why its "0 unclassified" was structurally incapable of being
    anything else (s12 acceptance review, criterion 3)."""
    if dup.get("error"):
        return {"value": None, "error": dup["error"]}
    return {
        "value": dup.get("candidates"),
        "population": dup.get("population"),
        "comparable": dup.get("comparable"),
        "coverage": dup.get("coverage"),
        "sample": dup.get("candidate_sample") or [],
    }


# ---------------------------------------------------------------------------
# ENF-04 — the ingest-time cross-tier guard's own numbers (metric 7).
#
# The guard (`brain.ingest.tierguard`) stamps EVERY note the ingest pipeline
# writes with `classification_guard: clear|raised|subfloor|unavailable`, so its
# outcome is recorded in the note's own signed frontmatter rather than in a
# side ledger that can be lost or forged. This reads them back.
#
# ONE of the four statuses ratchets, and the choice is deliberate. `raised` and
# `subfloor` are MONOTONE over an append-only zone (`raw/` is immutable), so a
# min-ever floor would alert on every single firing of a working guard — a
# false-alarm generator, not a watchdog. `unavailable` is different: it means
# the guard was supposed to run and COULDN'T (no index, a read error, or
# `$BRAIN_INGEST_TIER_GUARD_DISABLED`). It should be 0 and stay 0, so the
# ratchet is exactly right there and fires precisely when the guard dies.
#
# The invariant the guard SERVES — one document, one classification — already
# ratchets, as `cross_tier_duplicates`/`cross_tier_candidates`. Holding those
# at their floor is the guard's job; this metric only proves the guard is alive
# and shows what it did (AGENTS.md §4 rule 6).
# ---------------------------------------------------------------------------
def ingest_guard(vault: Path, *, cap: int = SAMPLE_CAP) -> dict[str, Any]:
    """ENF-04 — the ingest guard's verdicts, read back off ``raw/``.

    ``value`` is the count of sources admitted while the guard was UNAVAILABLE
    (the only non-monotone, should-be-zero number here). Every other status is
    reported beside it, per leg for raises, so "0 raised" can never quietly
    mean "the guard never looked"."""
    from . import frontmatter as fm
    from .ingest.tierguard import GUARD_KEY, GUARD_LEG_KEY, GUARD_STATUSES

    by_status = {s: 0 for s in GUARD_STATUSES}
    by_leg: dict[str, int] = {}
    unstamped = 0
    unknown = 0
    sample: list[str] = []
    raw = Path(vault) / "raw"
    total = 0
    for path in sorted(raw.glob("*.md")):
        total += 1
        try:
            meta, _body = fm.parse_text(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        status = str(meta.get(GUARD_KEY) or "").strip()
        if not status:
            # Ingested before ENF-04 shipped. Reported with its own number so
            # the denominator is honest — never folded into `clear`, which
            # would claim a check that never happened.
            unstamped += 1
            continue
        if status not in by_status:
            unknown += 1
            continue
        by_status[status] += 1
        if status == "raised":
            leg = str(meta.get(GUARD_LEG_KEY) or "unrecorded")
            by_leg[leg] = by_leg.get(leg, 0) + 1
            if len(sample) < cap:
                sample.append(f"{path.stem} -> {meta.get('classification')} ({leg})")
    return {
        "value": by_status["unavailable"],
        "sources": total,
        "raised": by_status["raised"],
        "raised_by_leg": by_leg,
        "clear": by_status["clear"],
        "subfloor": by_status["subfloor"],
        "unstamped": unstamped,
        "unknown_status": unknown,
        "sample": sample,
    }


def subfloor_families(conn: Any, *, cap: int = SAMPLE_CAP) -> dict[str, Any]:
    """Supersession families auto-linked on a body BELOW the
    ``$BRAIN_FAMILY_MIN_BODY`` floor (metric 3).

    A family here is a connected component over the indexed supersession
    links whose members all share ONE normalized body hash — the DDP-01
    auto-dedup shape. It counts as sub-floor when any member's body is
    shorter than the floor, which is how a 122-byte ``[no text detected]``
    OCR stub silently retires a different document."""
    from .index import _family_min_body
    from .maintenance import _floor_bytes, body_sha256

    floor = _family_min_body()
    rows = conn.execute(
        "SELECT id, body, superseded_by, previous_version FROM notes").fetchall()
    # UTF-8 BYTES — the unit the floor is declared in and the unit the `B`
    # label below claims. `len(str)` counts Unicode scalars, and chars <= bytes,
    # so this metric flagged a strict SUPERSET of what the writer refuses: a
    # 1,100-byte / 900-character Portuguese family was legitimately merged by
    # `auto_dedup_tier1` and simultaneously counted here as a violation. On an
    # absolute-threshold invariant (any value > 0 is a regression) that is a
    # permanent, unclearable false alarm on a multilingual corpus. Same helper
    # as the writer and the ranker (ENF-01, adversarial review round 2).
    #
    # NORMALIZED bytes (round 3), the same string ``body_sha256`` above hashes
    # and the same one ``auto_dedup_tier1`` now measures. Measuring raw bytes
    # here let a whitespace-padded 18-byte stub read as 1,118 bytes, so the
    # metric agreed with the writer that a sub-floor family was fine.
    info = {str(r[0]): (body_sha256(r[1] or ""), _floor_bytes(r[1] or "")) for r in rows}

    parent: dict[str, str] = {nid: nid for nid in info}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    linked: set[str] = set()
    for nid, _body, sup_by, prev in rows:
        for other in (sup_by, prev):
            o = str(other or "")
            if not o or o not in info or o == nid:
                continue
            linked.add(str(nid))
            linked.add(o)
            ra, rb = find(str(nid)), find(o)
            if ra != rb:
                parent[ra] = rb

    families: dict[str, list[str]] = {}
    for nid in linked:
        families.setdefault(find(nid), []).append(nid)

    hits: list[str] = []
    for root, members in sorted(families.items()):
        hashes = {info[m][0] for m in members}
        if len(hashes) != 1:
            continue  # a real revision family, not a byte-identical auto-link
        if min(info[m][1] for m in members) >= floor:
            continue
        hits.append(f"{min(info[m][1] for m in members)}B: " + ", ".join(sorted(members)))
    return {
        "value": len(hits),
        "families": len(families),
        "floor": floor,
        "sample": hits[:cap],
    }


def corpus_invariants(conn: Any, vault: Path, *, cap: int = SAMPLE_CAP) -> dict[str, Any]:
    """All four metrics in one read-only pass. Never raises on one metric —
    a metric that blows up records ``{"value": None, "error": …}`` so the
    other three still trend."""
    out: dict[str, Any] = {}
    # Metrics 5 and 6 are two halves of ONE scan (decided / undecided), so the
    # scan runs once and both entries read its result.
    memo: dict[str, dict[str, Any]] = {}

    def _duplicates() -> dict[str, Any]:
        if "dup" not in memo:
            try:
                memo["dup"] = cross_tier_duplicates(conn, cap=cap)
            except Exception as exc:  # noqa: BLE001 — surfaced on BOTH halves
                memo["dup"] = {"value": None, "error": f"{type(exc).__name__}: {exc}"}
        return memo["dup"]

    computers = {
        "unlinked_sources": lambda: unlinked_sources(conn, vault, cap=cap),
        "cross_tier_twins": lambda: cross_tier_twins(conn, cap=cap),
        "cross_tier_duplicates": _duplicates,
        "cross_tier_candidates": lambda: cross_tier_candidates_entry(_duplicates()),
        "unguarded_ingests": lambda: ingest_guard(vault, cap=cap),
        "subfloor_families": lambda: subfloor_families(conn, cap=cap),
        "unreachable_gold": lambda: unreachable_gold(vault),
        "unsigned_notes": lambda: unsigned_notes(conn, vault, cap=cap),
    }
    for name in INVARIANT_METRICS:
        try:
            out[name] = computers[name]()
        except Exception as exc:  # noqa: BLE001 — one broken metric never sinks four
            out[name] = {"value": None, "error": f"{type(exc).__name__}: {exc}"}
    return out


# ---------------------------------------------------------------------------
# The ratchet (G3): absolute thresholds anchored on the best value ever
# recorded, so the SAME rule alerts from a 2,132 baseline and from a zero one.
# ---------------------------------------------------------------------------
def tolerance_env(metric: str) -> str:
    return f"BRAIN_INVARIANTS_{metric.upper()}_TOLERANCE"


def metric_tolerance(metric: str) -> int:
    try:
        return max(0, int(os.environ.get(tolerance_env(metric), "").strip() or 0))
    except ValueError:
        return 0


def metric_values(metrics: dict[str, Any]) -> dict[str, int]:
    """``{metric: value}`` for the metrics that produced a real number this
    run. A ``None`` (errored, or no artifact yet) is OMITTED — it must not
    ratchet a floor down or read as a regression."""
    values: dict[str, int] = {}
    for name in INVARIANT_METRICS:
        entry = metrics.get(name)
        if isinstance(entry, dict) and isinstance(entry.get("value"), int):
            values[name] = int(entry["value"])
    return values


def update_floors(prev_floors: dict[str, Any] | None, values: dict[str, int]) -> dict[str, int]:
    """The new floor per metric: the lowest value ever recorded. A metric's
    first observation seeds its own floor, so the first run never alerts."""
    floors = {k: int(v) for k, v in (prev_floors or {}).items() if isinstance(v, int)}
    for name, value in values.items():
        floors[name] = min(floors[name], value) if name in floors else value
    return floors


def invariant_regressions(
    prev_floors: dict[str, Any] | None, values: dict[str, int],
) -> list[dict[str, Any]]:
    """Metrics that came in ABOVE their recorded floor plus tolerance. Empty
    on the very first run of each metric (no floor yet -> nothing to regress
    against)."""
    floors = {k: int(v) for k, v in (prev_floors or {}).items() if isinstance(v, int)}
    out: list[dict[str, Any]] = []
    for name in INVARIANT_METRICS:
        if name not in values or name not in floors:
            continue
        tol = metric_tolerance(name)
        value, floor = values[name], floors[name]
        if value > floor + tol:
            out.append({
                "metric": name, "value": value, "floor": floor, "tolerance": tol,
                "summary": (f"corpus invariant '{name}' regressed: {value} "
                            f"(best recorded {floor}"
                            + (f", tolerance {tol}" if tol else "") + ")"),
            })
    return out


def render_invariants_hot_entry(
    regressions: list[dict[str, Any]], metrics: dict[str, Any], today: datetime.date,
) -> str:
    """ONE hot.md LOG line per regressing run (never a queue item, never
    owner-input-needed — same PUSH posture as every other fold)."""
    lines = [f"## {today.isoformat()} — Corpus invariants: regression"]
    for r in regressions:
        lines.append(f"- **{r['metric']}:** {r['value']} (best recorded {r['floor']}"
                      + (f", tolerance {r['tolerance']}" if r["tolerance"] else "") + ")")
        entry = metrics.get(r["metric"]) if isinstance(metrics.get(r["metric"]), dict) else {}
        for s in (entry.get("sample") or [])[:3]:
            lines.append(f"  - `{s}`")
    lines.append(
        "- **Next:** see the 'Corpus invariants' section of `brain health-report` "
        "for the trend. A corpus-invariant fix never ships without its metric in "
        "the same change (AGENTS.md §4) — so this is a real corpus change, not a "
        "reporting artifact.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The dead-man's switch (owner ruling 2026-08-10). Pure, no I/O — every lane
# (doctor, health-report, degradation notifications, the weekly synthesis
# watchdog) calls these against an already-loaded maintain-state.
# ---------------------------------------------------------------------------
def max_age_days() -> int:
    try:
        return max(1, int(os.environ.get(MAX_AGE_DAYS_ENV, "").strip()
                          or DEFAULT_MAX_AGE_DAYS))
    except ValueError:
        return DEFAULT_MAX_AGE_DAYS


def invariants_age_days(
    state: dict[str, Any], today: datetime.date | None = None,
) -> int | None:
    """Days since the fold last COMPLETED, or ``None`` when it never has."""
    d = today or datetime.date.today()
    entry = state.get(STATE_KEY) if isinstance(state.get(STATE_KEY), dict) else {}
    last_run = entry.get("last_run")
    if not last_run:
        return None
    try:
        return (d - datetime.date.fromisoformat(str(last_run))).days
    except ValueError:
        return None


def liveness_finding(
    state: dict[str, Any] | None, today: datetime.date | None = None,
) -> tuple[str, str] | None:
    """``(dedup_key, text)`` when the invariants row is stale or missing —
    the alarm ON the alarm. ``None`` when it is fresh.

    A fold that has died cannot report its own death, so this keys on the
    EXPECTED state row rather than on iterating the rows that happen to be
    present: a row missing entirely, on a vault where other branches DO run,
    is the engine-restage failure mode this exists to catch. A vault where
    NOTHING has ever run (fresh install) is silent."""
    d = today or datetime.date.today()
    state = state or {}
    branches = [k for k, v in state.items()
                if not str(k).startswith("_") and isinstance(v, dict)]
    if not branches:
        return None
    limit = max_age_days()
    if STATE_KEY not in branches:
        return ("invariants-liveness",
                "corpus-invariants watchdog has NEVER run on this vault while "
                f"{len(branches)} other maintain branch(es) have — the fold is "
                "missing from this engine build (restage?)")
    age = invariants_age_days(state, d)
    if age is None:
        return ("invariants-liveness",
                "corpus-invariants watchdog has no successful run recorded "
                "(present but never completed)")
    if age > limit:
        return ("invariants-liveness",
                f"corpus-invariants watchdog last succeeded {age}d ago "
                f"(> {limit}d) — the {len(INVARIANT_METRICS)} corpus counts "
                "are unwatched")
    return None


def state_regressions(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The regressions the last fold run recorded — read back out of
    maintain-state so the notification/report lanes never recompute them."""
    entry = (state or {}).get(STATE_KEY)
    if not isinstance(entry, dict):
        return []
    regs = entry.get("regressions")
    return [r for r in regs if isinstance(r, dict)] if isinstance(regs, list) else []

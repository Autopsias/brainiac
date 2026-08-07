"""VER-01 — deduce version links from EMAIL CONTEXT (propose-first, never auto).

When a newly committed document looks like a newer version of one already in
the vault, this module says so **with the evidence that triggered it** — and
says nothing at all when the evidence is thin or self-contradictory. It never
writes: :mod:`brain.cos` turns each candidate into an owner-batch proposal
(VER-02), and only an owner ACCEPT reaches the audited ``core.supersede``.

Two automatic tiers already exist and are deliberately left alone:

* **sha256-identical duplicates** — ``maintenance.auto_dedup_tier1`` (DDP-01);
* **explicit ``…-vN`` id families** — ``maintenance.auto_version_chains``
  (VER-01's arithmetic half), whose decline-on-ambiguity discipline this
  module mirrors.

This is the third, DEDUCED tier, and it starts propose-only.

The decision rule (deliberately narrower than "≥2 signals, ≥1 metadata")
--------------------------------------------------------------------------
A pair is proposed only when ALL of these hold:

1. **Direction** — the candidate successor's valid date is strictly newer.
2. **A LINK** — one of exactly two, never both:

   * ``FAMILY_EMAIL`` — a **HOST-VERIFIED** email link: the two notes share a
     ``provenance.conversation_id`` or a ``provenance.sender``, and BOTH sides
     carry ``provenance.verified: true``. A VM-authored subject, display name
     or filename is a CLAIM (see :mod:`brain.provenance`); it may corroborate a
     proposal but can never produce one. This is what stops a spoofed
     sender/subject/filename from reaching the owner batch as a supersede
     proposal at all.
   * ``FAMILY_NAME`` (CUR-01) — a shared document NAME, and ONLY for a pair
     where neither side carries ANY ``provenance.*`` email keys. An ordinary
     vault document ("contract Draft" / "contract Final") has no email
     provenance to join on, so the email class can never see it. A note that
     DOES have email provenance never reaches this join: VERIFIED provenance
     saying "different counterparty" is negative evidence, and UNVERIFIED
     provenance means the note came through an untrusted lane, which makes its
     title and filename claims too. Recurring generated artifacts (``daily-*``,
     transcripts — ``index._boilerplate_patterns``, the same list DDP-01 uses)
     and trust-mismatched pairs are excluded from this join in
     :func:`generate`.

3. **An identity signal** — the two normalized name stems match, or both sides
   carry an ordinal version marker that advances. Without one of these, a pair
   is two DIFFERENT documents that happen to share a thread. (A ``FAMILY_NAME``
   pair has this by construction — the name IS the join.)
4. **An evidence signal** — near-duplicate content, or that same advancing
   version marker.

Both classes are **propose-only**. Nothing here ever writes a supersession:
:mod:`brain.cos` stages each candidate, one aggregated owner question per run
carries them, and only an ACCEPT reaches the audited ``core.supersede``.

Why not "content similarity + one metadata signal"? Because that is the
measured precision trap: two distinct agreements cut from one boilerplate
template, sent by the same counterparty, sit in the same 0.82-0.92 (and often
higher) similarity band as two genuine versions of one document. Requiring a
NAME identity signal is what separates them — the template siblings have
different names and no version markers. Similarity alone never proposes, and
here it cannot even nominate.

**Similarity is measured on stripped bodies.** Email boilerplate — quoted
reply chains, forward headers, signature blocks — dominates raw cosine between
any two messages in a thread (measured on this module's own fixtures: two
unrelated one-line replies under one quoted chain score 0.996). Bodies go
through :func:`similarity_text` before embedding, and the vectors are computed
from the stripped body ALONE — no title/zone contextual prefix, which would
inflate agreement between two notes that merely share a title.

Ambiguity DECLINES rather than guessing, exactly like ``auto_version_chains``:
version markers that disagree with the dates (or that do not advance at all)
end as ``ambiguous``, are logged once, and are never proposed.
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import frontmatter, provenance
from .maintenance import version_family_key
from .notes import sha256_text

# -- owner-tunable knobs ------------------------------------------------------
#: cosine floor for the near-duplicate content signal. Default matches `brain
#: integrity`'s near-dup default (0.95). Documented range: 0.90-0.99 — below
#: 0.90 template siblings start clearing it even after boilerplate stripping;
#: above 0.99 only byte-near-identical bodies qualify (which DDP-01 already
#: handles). Owner override: $BRAIN_VERSIONLINK_MIN_SIMILARITY.
MIN_SIMILARITY_ENV = "BRAIN_VERSIONLINK_MIN_SIMILARITY"
DEFAULT_MIN_SIMILARITY = 0.95
MIN_SIMILARITY_RANGE = (0.90, 0.99)

#: how far back a note counts as "recently committed" (the successor side).
WINDOW_DAYS_ENV = "BRAIN_VERSIONLINK_WINDOW_DAYS"
DEFAULT_WINDOW_DAYS = 14

#: The graduation key's ruleset component (mirrors the producer's
#: ``extraction_rules_version``): bump ONLY when the signal rules above change,
#: which resets this class's accumulated owner evidence. See cos.category_stats.
RULES_VERSION = "vl-2"

#: Which JOIN produced a pair — recorded on every proposal so a run's log line
#: says what the currency layer is actually seeing.
FAMILY_EMAIL = "email"   # a HOST-VERIFIED shared conversation or sender
FAMILY_NAME = "name"     # CUR-01: a shared document name, no email provenance
#: the ingest-taxonomy category every version-link verdict accrues against, so
#: the class rides the S05 ledger with no special-case code.
CATEGORY = "version-link"

# ponytail: bounded scan, not an incremental index. The corpus-wide near-dup
# backend (index.near_dup) embeds EVERY note; this fold instead nominates pairs
# from cheap metadata and embeds only the survivors (<= MAX_PAIRS * 2 texts per
# run, memoised per note). Upgrade path if a vault outgrows it: persist the
# stripped-body vectors beside the chunk vectors and look them up.
MAX_PARTNERS = 25          # candidate predecessors considered per new note
MAX_PAIRS = 200            # similarity computations per fold run
MAX_BODY_CHARS = 20_000    # embed input cap — a version diff is near the top


def min_similarity() -> float:
    raw = os.environ.get(MIN_SIMILARITY_ENV, "")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MIN_SIMILARITY
    lo, hi = MIN_SIMILARITY_RANGE
    return min(max(val, lo), hi)


def window_days() -> int:
    try:
        return max(1, int(os.environ.get(WINDOW_DAYS_ENV, "")))
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_DAYS


# -- name normalization (NFC FIRST — macOS readdir hands back NFD) ------------
_EXT_RE = re.compile(r"\.[a-z0-9]{1,5}$")
_DATE_TOKEN_RE = re.compile(r"\b(?:\d{4}[-_.]?\d{2}[-_.]?\d{2})\b")
_COPY_SUFFIX_RE = re.compile(r"\(\s*\d+\s*\)")
_SEP_RE = re.compile(r"[\W_]+", re.UNICODE)
#: Ordinal WORD markers, lowest rank first — iteration order decides an
#: ambiguous name ("final draft" reads as the lower, more conservative rank).
#: EN + PT/ES, because the reference corpus is trilingual and a `rascunho`
#: family is the same family as a `draft` one. `vf` is the "version final"
#: shorthand `version_family_key` deliberately refuses to chain.
_MARKER_WORDS = {"draft": 0, "rascunho": 0, "borrador": 0,
                 "final": 100, "vf": 100}
#: The prefixes an ORDINAL NUMBER can hang off. Longest first: Python's
#: alternation is first-match-wins at a position, and `v` would otherwise
#: shadow `versao` on every backtrack-free engine reading of this pattern.
_NUMERIC_MARKER_WORDS = ("revisión", "revisão", "revisao", "revision",
                         "versión", "versão", "versao", "version",
                         "rev", "ver", "v")
#: Everything a family STEM must lose before two names can be compared.
_MARKER_STRIP_WORDS = (*_MARKER_WORDS, "clean", "copy", "comentada",
                       "marked[ _-]?up")
_NUMERIC_MARKER_RE = re.compile(
    r"\b(?:" + "|".join(_NUMERIC_MARKER_WORDS) + r")[ ._-]*(\d{1,3})\b",
    re.IGNORECASE)
_MARKER_STRIP_RE = re.compile(
    r"\b(?:" + "|".join(_NUMERIC_MARKER_WORDS) + r")[ ._-]*\d{1,3}\b"
    r"|\b(?:" + "|".join(_MARKER_STRIP_WORDS) + r")\b", re.IGNORECASE)
#: a stem shorter than this is not a distinguishing name ("doc", "att")
MIN_FAMILY_STEM = 4


def nfc(value: Any) -> str:
    """NFC-normalize before ANY name comparison.

    macOS ``readdir`` returns NFD, frontmatter written elsewhere is NFC, and a
    byte-exact compare silently fails on every accented name — the exact bug
    this repo has already been burned by. Every stem/marker helper below
    normalizes first.
    """
    return unicodedata.normalize("NFC", str(value or ""))


def family_stem(name: Any) -> str:
    """The name stem two versions of one document share, or ``""``.

    Strips one file extension, capture/document date tokens, ``(2)`` copy
    suffixes and version markers, then folds separators and case. Returns
    ``""`` when what is left is too short to distinguish anything.
    """
    text = nfc(name).strip().casefold()
    if not text:
        return ""
    text = _EXT_RE.sub("", text)
    text = _COPY_SUFFIX_RE.sub(" ", text)
    # Date tokens go BEFORE separators are folded (`2026-07-01` is a date,
    # `2026 07 01` is three numbers); markers go AFTER (`_v2` carries no word
    # boundary until the underscore is a space).
    text = _DATE_TOKEN_RE.sub(" ", text)
    text = _spaced(text)
    stem = " ".join(_MARKER_STRIP_RE.sub(" ", text).split())
    return stem if len(stem.replace(" ", "")) >= MIN_FAMILY_STEM else ""


def _spaced(text: str) -> str:
    """Separators folded to single spaces, so ``\\b``-anchored marker patterns
    see ``_v2`` / ``-final`` as words."""
    return " ".join(t for t in _SEP_RE.split(text) if t)


def version_marker(name: Any) -> tuple[str, int] | None:
    """``("num", n)`` / ``("word", rank)`` for an ordinal version marker, else
    ``None``. Numeric wins when both are present (``"contract v2 final"`` is
    version 2), and the two scales are NEVER compared — a numeric marker on one
    side and a word marker on the other is ambiguity, not an ordering."""
    text = _spaced(nfc(name).casefold())
    if not text:
        return None
    nums = _NUMERIC_MARKER_RE.findall(text)
    if nums:
        return "num", int(nums[-1])
    for word, rank in _MARKER_WORDS.items():
        if re.search(rf"\b{word}\b", text):
            return "word", rank
    return None


# -- provenance reading (HOST-VERIFIED ONLY) ----------------------------------
_ADDR_RE = re.compile(r"<([^<>]+)>")


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().casefold() == "true"


def sender_key(value: Any) -> str:
    """The comparable identity of a sender: the bare address when the value is
    a ``Display Name <a@b>`` pair, else the whole value, NFC + casefolded.
    Display names are attacker-chosen even on a verified header; the address is
    what the host actually parsed."""
    text = nfc(value).strip()
    if not text:
        return ""
    m = _ADDR_RE.search(text)
    return (m.group(1) if m else text).strip().casefold()


def verified_provenance(meta: Mapping[str, Any]) -> dict[str, str]:
    """The email provenance of ``meta`` **iff the host verified it**.

    ``provenance.verified`` is host-earned (S04): set only by the ingest
    pipeline from the archived original's own headers, stripped from every
    VM-writable surface, and fail-closed at the drain. An unverified claim
    returns ``{}`` — it never becomes a version-deduction signal.
    """
    if not _truthy(meta.get(provenance.VERIFIED_KEY)):
        return {}
    out: dict[str, str] = {}
    conv = nfc(meta.get(provenance.CONVERSATION_KEY)).strip()
    if conv:
        out["conversation_id"] = conv
    sender = sender_key(meta.get(provenance.SENDER_KEY))
    if sender:
        out["sender"] = sender
    sent = nfc(meta.get(provenance.SENT_KEY)).strip()
    if sent:
        out["sent"] = sent
    return out


# -- one note, as this module needs it ----------------------------------------
class NoteView:
    """The projection of one in-scope note the signal rules read."""

    __slots__ = ("id", "title", "path", "zone", "classification", "content_hash",
                 "body_sha", "meta", "prov", "valid_date", "commit_date",
                 "names", "stems", "marker", "retired", "has_predecessor", "body",
                 "email_linkable", "email_claimed", "untrusted")

    def __init__(self, *, row: Mapping[str, Any], meta: Mapping[str, Any],
                 body: str) -> None:
        self.id = str(row["id"])
        self.title = str(row.get("title") or self.id)
        self.path = str(row.get("path") or "")
        self.zone = str(row.get("zone") or "")
        self.classification = str(row.get("classification") or "")
        self.content_hash = str(row.get("content_hash") or "")
        self.meta = dict(meta)
        self.body = body
        self.body_sha = str(meta.get("sha256") or "")
        self.prov = verified_provenance(meta)
        #: does this note carry a HOST-VERIFIED link another note can join on?
        self.email_linkable = bool(
            self.prov.get("conversation_id") or self.prov.get("sender"))
        #: does it carry an email provenance story AT ALL, verified or not?
        #: CUR-01's name families are for the notes that carry NONE. A note
        #: with UNVERIFIED `provenance.*` claims arrived through an untrusted
        #: lane, which makes its title/filename attacker-chosen too — it must
        #: not sidestep the verification gate by falling back to the name join.
        self.email_claimed = self.email_linkable or any(
            str(meta.get(k) or "").strip() for k in provenance.CLAIM_KEYS)
        #: DDP-01's trust guard, same two keys: a `status: draft` or
        #: `provenance.trust: untrusted` note must never be OFFERED as the
        #: successor that retires a trusted one. Propose-only is not a licence
        #: to put a spoofable "Contract FINAL" draft in front of the owner.
        self.untrusted = (
            str(meta.get("status", "")).strip().casefold() == "draft"
            or str(meta.get("provenance.trust", "")).strip().casefold() == "untrusted")
        self.retired = (
            bool(str(meta.get("superseded_by") or "").strip())
            or str(meta.get("is_latest_version", "")).strip().casefold() == "false")
        self.has_predecessor = bool(
            str(meta.get("previous_version") or meta.get("replaces") or "").strip())
        self.valid_date = _valid_date(row, meta, self.prov)
        self.commit_date = max(str(row.get("created") or ""),
                               str(row.get("updated") or ""))
        # Every name this DOCUMENT can be identified by: its own id/title plus
        # the archived original's filename (`origin:` — what the counterparty
        # actually called the file).
        origin = nfc(meta.get("origin")).strip()
        origin_name = Path(origin).name if origin else ""
        self.names = tuple(n for n in (origin_name, self.title, self.id) if n)
        self.stems = {s for s in (family_stem(n) for n in self.names) if s}
        # The SUBJECT is deliberately excluded from the identity stems and used
        # only for ordinal markers ("…— v2 attached"). A subject belongs to a
        # THREAD, not a document: every attachment in one conversation shares
        # it, so treating it as identity makes "same thread" and "same
        # document" the same test — which is exactly how two different
        # agreements sent under one subject line become a false supersede.
        subject = nfc(meta.get(provenance.SUBJECT_KEY)).strip() if self.prov else ""
        self.marker = next(
            (m for m in (version_marker(n) for n in self.names + ((subject,) if subject else ()))
             if m is not None), None)


def _valid_date(row: Mapping[str, Any], meta: Mapping[str, Any],
                prov: Mapping[str, str]) -> str:
    """The date this note's content is valid AT — same precedence
    ``auto_version_chains`` uses, with the HOST-VERIFIED sent date slotted in
    ahead of the capture date (a re-capture months later is not a new version).
    """
    for value in (row.get("effective_date"), row.get("document_date"),
                  meta.get("effective_date"), meta.get("document_date"),
                  prov.get("sent"), row.get("created"), meta.get("captured")):
        text = str(value or "").strip()
        if text:
            return text[:10]
    return ""


# -- the decision rule --------------------------------------------------------
def pair_key(a: str, b: str) -> str:
    """Direction-INDEPENDENT identity of a pair. A rejected pair is never
    re-asked, not even with the two notes swapped."""
    lo, hi = sorted((str(a), str(b)))
    return f"{lo}::{hi}"


def analyze(old: NoteView, new: NoteView, *, similarity: float | None,
            threshold: float) -> dict[str, Any]:
    """Verdict for ONE oriented pair (``new`` is the later-dated side).

    Returns ``{"verdict": "propose"|"ambiguous"|"skip", "reason": str,
    "signals": {...}}``. ``signals`` records WHICH signals fired and their
    values — evidence the owner sees, never a bare confidence number.
    """
    signals: dict[str, Any] = {}

    # 1 — direction. Equal dates cannot be ordered; earlier is not a version.
    if not (old.valid_date and new.valid_date and new.valid_date > old.valid_date):
        return {"verdict": "skip", "reason": "no strictly newer valid date",
                "signals": signals}
    signals["newer_date"] = {"old": old.valid_date, "new": new.valid_date}

    # 2 — the LINK. Two ways in, and a note only ever gets one of them:
    #   * a HOST-VERIFIED shared conversation or sender (the original class —
    #     unverified provenance is not a signal, and never was);
    #   * CUR-01: a shared document NAME, for a pair where NEITHER side carries
    #     ANY `provenance.*` email keys. Ordinary vault documents ("contract
    #     Draft" / "contract Final") have no email provenance to join on, so
    #     the email class could never see them. A note that DOES carry email
    #     provenance never reaches this join: VERIFIED headers saying "two
    #     different counterparties" are negative evidence about the pair (the
    #     measured template-sibling trap), and UNVERIFIED ones mean the note
    #     came through an untrusted lane, where its title and filename are
    #     claims as well — neither may fall back to the weaker join.
    shared = sorted(old.stems & new.stems)
    if shared:
        signals["name_family"] = shared[0]
    if old.prov.get("conversation_id") and (
            old.prov["conversation_id"] == new.prov.get("conversation_id")):
        signals["conversation"] = old.prov["conversation_id"]
    if old.prov.get("sender") and old.prov["sender"] == new.prov.get("sender"):
        signals["sender"] = old.prov["sender"]
    if signals.get("conversation") or signals.get("sender"):
        signals["family_class"] = FAMILY_EMAIL
    elif signals.get("name_family") and not (old.email_claimed or new.email_claimed):
        signals["family_class"] = FAMILY_NAME
    else:
        return {"verdict": "skip",
                "reason": "no HOST-VERIFIED shared conversation or sender, and "
                          "no shared document name on two notes without one",
                "signals": signals}

    # 3 — identity: same name stem, and/or an ordinal marker that advances.
    marker_ok = False
    if old.marker and new.marker:
        signals["version_markers"] = {"old": list(old.marker), "new": list(new.marker)}
        same_scale = old.marker[0] == new.marker[0]
        if same_scale and new.marker[1] > old.marker[1]:
            marker_ok = True
        elif signals.get("name_family") or (
                similarity is not None and similarity >= threshold):
            # Markers exist on both sides and DISAGREE with the dates (or do
            # not advance at all) for a pair we would otherwise have proposed.
            # Decline — mirrors auto_version_chains' skipped_ambiguous.
            return {
                "verdict": "ambiguous",
                "reason": ("version markers do not advance with the dates "
                           f"({old.marker[0]}:{old.marker[1]} dated "
                           f"{old.valid_date} vs {new.marker[0]}:"
                           f"{new.marker[1]} dated {new.valid_date})"
                           if same_scale else
                           "version markers are on different scales "
                           f"({old.marker[0]} vs {new.marker[0]})"),
                "signals": signals}
    if marker_ok:
        signals["version_advance"] = {"old": old.marker[1], "new": new.marker[1]}

    if similarity is not None:
        signals["similarity"] = round(similarity, 6)
    near_dup = similarity is not None and similarity >= threshold

    if not (signals.get("name_family") or marker_ok):
        return {"verdict": "skip",
                "reason": "no name-identity signal (different documents in one "
                          "thread look exactly like this)", "signals": signals}
    if not (near_dup or marker_ok):
        return {"verdict": "skip",
                "reason": f"content similarity {similarity!r} below "
                          f"{threshold} and no advancing version marker",
                "signals": signals}
    if near_dup:
        signals["near_duplicate"] = {"score": round(similarity or 0.0, 6),
                                     "threshold": threshold}
    return {"verdict": "propose", "reason": "deduced from email context",
            "signals": signals}


# -- similarity ---------------------------------------------------------------
_SIGNATURE_CUT_RE = re.compile(r"^\s*(?:--\s*|__+|-{5,})$")


def similarity_text(body: str) -> str:
    """The material a body actually contributes: quoted reply chains, forward
    headers and the signature block REMOVED.

    Deliberately not ``cos.canonical_evidence_text``, which de-quotes and KEEPS
    those lines (correct for a content fingerprint, wrong here): email
    boilerplate otherwise dominates cosine between any two messages in one
    thread, and two unrelated one-line replies under a long quoted chain score
    ~0.996. The forward-header pattern is shared with that function so the two
    stay aligned.
    """
    from .cos import _FORWARD_WRAPPER_RE

    kept: list[str] = []
    for raw in str(body or "").splitlines():
        if _SIGNATURE_CUT_RE.match(raw):
            break
        line = raw.strip()
        if not line or line.startswith(">") or _FORWARD_WRAPPER_RE.match(line):
            continue
        kept.append(line)
    return " ".join(" ".join(kept).split()).casefold()[:MAX_BODY_CHARS]


def _similarity(index: Any, cache: dict[str, list[float]],
                a: NoteView, b: NoteView) -> float | None:
    from .vectors import cosine

    vecs = []
    for note in (a, b):
        if note.id not in cache:
            text = similarity_text(note.body)
            if not text:
                cache[note.id] = []
            else:
                cache[note.id] = index.embedder.embed_batch(
                    [text], is_query=False)[0]
        vecs.append(cache[note.id])
    if not vecs[0] or not vecs[1]:
        return None
    # float() is LOAD-BEARING, not cosmetic. `vectors.cosine` is annotated
    # `-> float` but returns whatever arithmetic on its inputs produces, and the
    # REAL embedder hands back numpy `float32` rows — so the score, and every
    # signal dict it lands in, becomes a `numpy.float32` that `json.dumps`
    # refuses. That killed the whole fold at its first serialization
    # (`TypeError: Object of type float32 is not JSON serializable`, measured
    # 2026-07-31 on the deployed engine against real ingested .eml pairs), and
    # the test suite never saw it because every fixture pins
    # `BRAIN_EMBEDDER=hash`, whose vectors are plain Python floats. One
    # conversion here, at the ONE place a similarity enters this module.
    return float(cosine(vecs[0], vecs[1]))


# -- CUR-01: the curated-coverage metric --------------------------------------
#: Notes carrying REAL supersession frontmatter — a link that actually landed,
#: through `core.supersede`, on disk. Membership of a DETECTED family is
#: deliberately NOT counted: a vault of Draft/Final pairs would read ~90%
#: "covered" on day one with not one link written, and the number would then
#: report success for a proposal queue nobody ever drained. The two are
#: reported side by side and never added together.
_COVERAGE_SQL = (
    "SELECT COUNT(*), SUM(CASE WHEN COALESCE(superseded_by, '') <> '' "
    "OR COALESCE(previous_version, '') <> '' "
    "OR LOWER(COALESCE(is_latest_version, '')) = 'false' THEN 1 ELSE 0 END) "
    "FROM notes")


def coverage(conn: Any) -> dict[str, Any]:
    """How much of the vault the CURRENCY layer can actually see, per run.

    ``notes`` is every indexed note; ``linked`` is how many carry supersession
    frontmatter (``superseded_by``, ``previous_version``, or a retired
    ``is_latest_version``); ``ratio`` is the two divided. Pure — one COUNT.
    """
    total, linked = conn.execute(_COVERAGE_SQL).fetchone()
    total = int(total or 0)
    linked = int(linked or 0)
    return {"notes": total, "linked": linked,
            "ratio": round(linked / total, 4) if total else 0.0}


# -- the generator ------------------------------------------------------------
_SCOPE_SQL = (
    "SELECT id, title, path, zone, classification, content_hash, created, "
    "updated, document_date, effective_date, is_latest_version, superseded_by "
    "FROM notes WHERE zone = 'raw' OR type IN ('source', 'source-derived')")
_RECENT_SQL = (
    "SELECT COUNT(*) FROM notes "
    "WHERE (zone = 'raw' OR type IN ('source', 'source-derived')) "
    "AND MAX(COALESCE(created, ''), COALESCE(updated, '')) >= ?")


def _load(core: Any) -> list[NoteView]:
    cols = ("id", "title", "path", "zone", "classification", "content_hash",
            "created", "updated", "document_date", "effective_date",
            "is_latest_version", "superseded_by")
    out: list[NoteView] = []
    for row in core.index.conn.execute(_SCOPE_SQL).fetchall():
        rec = dict(zip(cols, row))
        path = Path(str(rec["path"]))
        if not path.is_absolute():
            path = Path(core.vault) / path
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, body = frontmatter.parse_text(text)
        rec["path"] = str(path)
        # The hash of what is ON DISK NOW, not the index column: this fold runs
        # BEFORE the umbrella's sync, so the indexed hash can lag the file. A
        # lagging hash would become a proposal precondition that fails at apply
        # time for no real reason.
        rec["content_hash"] = sha256_text(text)
        out.append(NoteView(row=rec, meta=meta, body=body))
    return out


def generate(core: Any, *, cutoff: str, exclude: Iterable[str] = (),
             threshold: float | None = None) -> dict[str, Any]:
    """Nominate version-link candidates over the in-scope corpus.

    ``cutoff`` is an ISO date: only notes committed on or after it are
    considered as SUCCESSORS (predecessors may be arbitrarily old). ``exclude``
    is the set of :func:`pair_key` values already decided (proposed, rejected,
    applied or declined) — never re-asked.

    Pure: reads the index + note files, writes nothing.
    """
    threshold = min_similarity() if threshold is None else threshold
    report: dict[str, Any] = {"candidates": [], "ambiguous": [],
                              "pairs_examined": 0, "truncated": False}
    # ponytail: cheap early-out. This fold fires hourly and `_load` reads every
    # in-scope note's frontmatter; with nothing newly committed there is no
    # successor side and therefore nothing to deduce, so most runs cost one
    # COUNT(*). Drop this only if provenance ever becomes an indexed column.
    if not core.index.conn.execute(_RECENT_SQL, (cutoff,)).fetchone()[0]:
        return report

    excluded = set(exclude)
    notes = _load(core)
    by_id = {n.id: n for n in notes}

    # Group by the two HOST-VERIFIED email links, plus (CUR-01) by name stem
    # for the notes that have NEITHER. A note with a verified email link joins
    # only its email groups — see `analyze` rule 2 for why it must not also
    # fall back to the weaker name join.
    from .index import _boilerplate_patterns, _matches_boilerplate_pattern

    boilerplate = _boilerplate_patterns()
    groups: dict[tuple[str, str], list[str]] = {}
    for n in notes:
        if n.email_claimed:
            for field in ("conversation_id", "sender"):
                val = n.prov.get(field)
                if val:
                    groups.setdefault((field, val), []).append(n.id)
            continue
        # A recurring generated artifact (`daily-*`, `*-transcript`) shares a
        # stem with every other instance of itself by template design — the
        # same reason DDP-01 refuses to auto-dedup them. Never a name family.
        if _matches_boilerplate_pattern(n.id, boilerplate):
            continue
        for stem in n.stems:
            groups.setdefault(("stem", stem), []).append(n.id)

    cache: dict[str, list[float]] = {}
    seen: set[str] = set(excluded)
    proposed_ids: set[str] = set()

    for new in sorted(notes, key=lambda n: (n.commit_date, n.id), reverse=True):
        if new.commit_date < cutoff or new.retired:
            continue
        if new.email_claimed:
            keys = [("conversation_id", new.prov.get("conversation_id") or ""),
                    ("sender", new.prov.get("sender") or "")]
        else:
            keys = [("stem", s) for s in sorted(new.stems)]
        partners: list[str] = []
        for key_ in keys:
            for pid in groups.get(key_, ()):
                if pid != new.id and pid not in partners:
                    partners.append(pid)
        # NEAREST preceding version first (by valid date, then commit date):
        # in a thread carrying v1/v2/v3, v3 supersedes v2, not v1. Also what
        # the partner cap should keep — a busy sender must not turn one fold
        # into an O(n^2) sweep.
        partners.sort(key=lambda pid: (by_id[pid].valid_date,
                                       by_id[pid].commit_date, pid), reverse=True)
        for pid in partners[:MAX_PARTNERS]:
            old = by_id[pid]
            key = pair_key(old.id, new.id)
            if key in seen:
                continue
            if old.retired or old.id == new.id:
                continue
            # Leave the two AUTOMATIC tiers untouched.
            fam_old, fam_new = version_family_key(old.id), version_family_key(new.id)
            if fam_old and fam_new and fam_old[0] == fam_new[0]:
                continue                       # auto_version_chains owns it
            if old.body_sha and old.body_sha == new.body_sha:
                continue                       # auto_dedup_tier1 owns it
            # DDP-01's trust guard on the NAME class: an untrusted draft and a
            # trusted note are never one family. (The email class's join is
            # itself host-verified, so it keeps its existing behaviour.)
            if not (old.email_claimed or new.email_claimed) and (
                    old.untrusted != new.untrusted):
                continue
            # Orient by date: the later-dated side is the candidate successor.
            a, b = (old, new) if old.valid_date <= new.valid_date else (new, old)
            # ONE proposal per note per run, and never a second predecessor for
            # a note that already has one. A thread carrying v1/v2/v3 otherwise
            # nominates three overlapping pairs, and accepting them all would
            # point two predecessors at one successor — a corrupt chain. The
            # supersede preconditions would catch it (applying the first pair
            # moves the successor's hash, so the rest decline), but a batch that
            # asks the owner three contradictory questions is already wrong.
            if a.id in proposed_ids or b.id in proposed_ids or b.has_predecessor:
                continue
            if report["pairs_examined"] >= MAX_PAIRS:
                report["truncated"] = True
                break
            report["pairs_examined"] += 1
            seen.add(key)
            sim = _similarity(core.index, cache, a, b)
            verdict = analyze(a, b, similarity=sim, threshold=threshold)
            if verdict["verdict"] == "propose":
                report["candidates"].append({
                    "old_id": a.id, "new_id": b.id, "pair_key": key,
                    "old_title": a.title, "new_title": b.title,
                    "old_sha256": a.content_hash, "new_sha256": b.content_hash,
                    "old_classification": a.classification,
                    "new_classification": b.classification,
                    "signals": verdict["signals"],
                })
                proposed_ids.update({a.id, b.id})
            elif verdict["verdict"] == "ambiguous":
                report["ambiguous"].append({
                    "old_id": a.id, "new_id": b.id, "pair_key": key,
                    "reason": verdict["reason"], "signals": verdict["signals"],
                })
        if report["truncated"]:
            break
    return report

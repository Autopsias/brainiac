"""CON-01 — the vault LANGUAGE CENSUS: which languages this vault actually holds.

Derived data, no owner ritual (the self-organizing-vault ruling, AGENTS.md §4
rule 4): the census is recomputed from the indexed note bodies whenever the
index's content fingerprint changes, cached in the index ``meta`` table, and
surfaced by ``brain status --json``. Nothing asks the owner to declare
anything.

**Why it exists.** AGENTS.md §5's variant contract is CONDITIONAL: an agent
issues cross-language query variants only when the vault holds more than one
language, and it must know WHICH other languages without guessing. The census
answers exactly that, and its ordered ``vault_languages`` list is also the
selection policy behind the fan-out cap — top-N by prevalence above a stated
threshold, remainder reported as dropped, never silently truncated.

**The classifier** is the stopword-profile method the s09 stratum audit used
by hand (``_evidence/eval-power/s09-pt-stratum-audit.md`` §3), formalized:
count each profile's discriminative function words over the whole body and
take the strict winner. s09's own rule that "ambiguous words belong to
neither" is enforced MECHANICALLY here — a word appearing in more than one
profile counts for none — so Portuguese and Spanish (which share `de`, `que`,
`para`, `como`, …) stay separable, and an owner-added profile cannot poison
the built-ins. Too little signal, or a tie, is honestly ``unknown``; nothing
is rounded into English.

Measured against the audit (2026-08-09, reference vault, 2,579 notes): 53 of
the 54 resolvable gold documents get s09's verdict, including all 22
``cross_lingual_pt_en`` golds it read as English. The single divergence is a
Portuguese-framed note whose body is a verbatim SPANISH email — s09's two-way
PT/EN tally had no Spanish bucket to put it in.

**That agreement was re-measured after the 2026-08-17 profile repair** (see
``score`` and the ``pt`` profile below), because a claim about the classifier
cannot outlive a change to the classifier. Over the 92 cross-lingual gold
documents that resolve in the reference vault today, 89 keep their verdict
and 3 move EN -> PT. All three are Portuguese-titled slide decks that read
as English only because ALL-CAPS slide headings were counted as English
function words:
one of them scored ``en`` 150 / ``pt`` 113 before, and ``en`` 37 / ``pt`` 279
once shouted short tokens stop counting. The audit was right about the method
and wrong about those three documents; the divergence is an improvement, not
a regression, and it is recorded here rather than quietly absorbed.

**Profiles are DATA, not code.** ``BUILTIN_PROFILES`` ships English,
Portuguese and Spanish (the languages the reference deployment holds). A vault
in any other language extends the set WITHOUT a code change by pointing
``$BRAIN_LANGUAGE_PROFILES`` at a JSON file of the same shape; entries merge
over (and may override) the built-ins. **The honest scope statement, because
the gap matters:** a language with no profile — built-in or configured — is
classified ``unknown``, never becomes a vault language, and so never earns a
query variant. "Any language" is a property of the MECHANISM (nothing in the
retrieval path is PT/ES-specific); the CENSUS only knows the languages it has
profiles for.

``ponytail``: ``brain.chunk.detect_language`` keeps its own copy of these word
lists on purpose. That one picks a CHUNK's contextual-prefix language, which
is part of the EMBED INPUT — changing it changes every vector on the next
rebuild and moves every retrieval measurement in flight. This module is
note-level, read-only, and affects nothing that was embedded.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

#: Discriminative function words per language. Small on purpose: this is a
#: census, not a language-ID library. Words shared by two profiles are dropped
#: from both at load time (see ``_prepared``), so overlap here is harmless.
BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "en": {
        "name": "English",
        "stopwords": [
            "the", "and", "of", "to", "in", "is", "are", "for", "with", "that",
            "this", "as", "be", "on", "by", "an", "we", "it", "from", "will",
        ],
    },
    "pt": {
        "name": "Portuguese",
        # Portuguese function words are SHORT — `o`, `os`, `um`, `em`, `da`,
        # `na`, `ao` — and the profile shipped without a single one of them
        # until 2026-08-17. Spanish kept its articles (`el`, `los`, `las`,
        # `del`); Portuguese had none to keep, and the disjointness rule then
        # took `de`/`que`/`para`/`como` as well. What survived was 16 words,
        # none of them an article, so ordinary Portuguese prose scored ZERO:
        # a whole board-minute paragraph classified `unknown`, never counted
        # toward `classified`, and `pt` could not reach
        # MIN_NOTES_PER_LANGUAGE. A vault could fill up with Portuguese and
        # keep reporting itself monolingual English, which silently switches
        # OFF §5's variant contract — the BM25 leg those queries most need.
        "stopwords": [
            # articles, and the preposition contractions built on them
            "o", "os", "um", "uma", "da", "das", "dos", "na", "nas", "ao",
            "aos", "à", "às", "num", "numa", "pelo", "pela", "pelos", "pelas",
            "neste", "nesta", "nesse", "nessa", "deste", "desta", "desse",
            "dessa",
            # prepositions and conjunctions Spanish spells differently
            "de", "que", "em", "para", "com", "sem", "até", "sob", "após",
            "ou", "mas", "pois", "porém", "embora", "porque", "então",
            # copula and auxiliaries
            "é", "são", "foi", "foram", "tem", "têm", "ter", "tinha", "havia",
            "há", "pode", "podem", "deve", "devem", "seja", "sejam", "está",
            # determiners and pronouns
            "seu", "sua", "seus", "suas", "nosso", "nossa", "nós", "qual",
            "quais", "outro", "outra", "outros", "outras", "qualquer",
            "mesmo", "mesma", "isso",
            # adverbs
            "não", "mais", "muito", "ainda", "assim", "apenas", "bem",
            "como", "depois", "onde", "quando", "sempre", "sobre", "também",
            "já", "será", "através",
        ],
    },
    "es": {
        "name": "Spanish",
        # deliberately WITHOUT "no": it is a common English word that the
        # English profile does not list, so it would score Spanish on English
        # prose (the disjointness rule only sees cross-PROFILE collisions).
        # For the same reason this profile takes no NEW one- or two-letter
        # word. Spanish `y` measured 14,111 hits across 196 unambiguously
        # English notes in the reference vault — `overflow-y`, `translateY`
        # — enough to outscore English on its own decks. Spanish already has
        # its articles; it does not need the short-word risk to read Spanish.
        "stopwords": [
            "de", "que", "una", "para", "con", "como", "más", "está",
            "son", "también", "ya", "nosotros", "sobre", "será", "fue", "pero",
            "porque", "el", "la", "los", "las", "del", "esto", "un", "su",
            "sus", "hasta", "sin", "hay", "tiene", "tener", "cuando", "donde",
            "cual", "muy", "mucho", "bien", "siempre", "entonces", "aunque",
            "además", "esa", "ese",
        ],
    },
}

PROFILES_ENV = "BRAIN_LANGUAGE_PROFILES"
MIN_SHARE_ENV = "BRAIN_LANGUAGE_MIN_SHARE"
MAX_LANGUAGES_ENV = "BRAIN_LANGUAGE_MAX"

#: A language must hold at least this SHARE of the classified notes to count
#: as a vault language (and so to earn a query variant).
DEFAULT_MIN_SHARE = 0.02
#: …and at least this many notes, so one note in a three-note vault cannot
#: become a "language".
MIN_NOTES_PER_LANGUAGE = 3
#: Deterministic cap on how many languages the contract asks an agent to
#: translate into: top-N by prevalence, ties broken by language code ascending,
#: the remainder reported as ``dropped_by_cap``.
DEFAULT_MAX_LANGUAGES = 3
#: A body needs at least this many function-word hits before we name a
#: language at all. Below it (or on a tie) the note is ``unknown``.
MIN_SIGNAL_HITS = 3

UNKNOWN = "unknown"

_WORD = re.compile(r"[A-Za-zÀ-ÿ]+")


# -- profiles ----------------------------------------------------------------

def _validate(code: str, spec: Any) -> dict[str, Any] | None:
    """Return a normalized profile, or None if the entry is unusable.

    Trust boundary: the override file is owner-supplied JSON. A malformed
    entry is DROPPED rather than guessed at — a census built on a half-read
    profile is worse than one that says ``unknown``.
    """
    if not isinstance(code, str) or not code.strip() or not isinstance(spec, dict):
        return None
    words = spec.get("stopwords")
    if not isinstance(words, list) or not words:
        return None
    stop = {str(w).strip().lower() for w in words if str(w).strip()}
    if not stop:
        return None
    return {"name": str(spec.get("name") or code), "stopwords": sorted(stop)}


@lru_cache(maxsize=8)
def _read_override(path: str, mtime: float) -> str:  # noqa: ARG001
    """The override file's text, cached on (path, mtime) so a whole-vault
    census does not re-read it once per note."""
    return Path(path).read_text(encoding="utf-8")


def load_profiles() -> dict[str, dict[str, Any]]:
    """The built-in profiles, with ``$BRAIN_LANGUAGE_PROFILES`` merged over them.

    The override file is ``{"<code>": {"name": …, "stopwords": [...]}, …}``
    (or the same mapping under a top-level ``"profiles"`` key). A malformed
    entry is dropped; a missing/unreadable/invalid file leaves the built-ins
    untouched — extending the census can never break it.
    """
    profiles = {c: dict(p) for c, p in BUILTIN_PROFILES.items()}
    raw_path = os.environ.get(PROFILES_ENV, "").strip()
    if not raw_path:
        return profiles
    try:
        p = Path(raw_path).expanduser()
        data = json.loads(_read_override(str(p), p.stat().st_mtime))
    except (OSError, ValueError):
        return profiles
    if isinstance(data, dict) and isinstance(data.get("profiles"), dict):
        data = data["profiles"]
    if not isinstance(data, dict):
        return profiles
    for code, spec in data.items():
        norm = _validate(code, spec)
        if norm:
            profiles[str(code).strip().lower()] = norm
    return profiles


def profiles_fingerprint(profiles: dict[str, dict[str, Any]]) -> str:
    """Stable digest of the profile set — a census cached under a DIFFERENT
    profile set must be recomputed, never reused."""
    blob = json.dumps(profiles, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=8)
def _compile(serialized: str) -> tuple[tuple[str, frozenset[str]], ...]:
    """(code, discriminative-words) pairs, with ambiguous words removed.

    s09's rule, mechanized: a function word claimed by more than one profile
    discriminates nothing and is dropped from all of them.
    """
    profiles = json.loads(serialized)
    seen: Counter[str] = Counter()
    for spec in profiles.values():
        seen.update(set(spec["stopwords"]))
    return tuple(
        (code, frozenset(w for w in spec["stopwords"] if seen[w] == 1))
        for code, spec in sorted(profiles.items())
    )


def _prepared(profiles: dict[str, dict[str, Any]]):
    return _compile(json.dumps(profiles, sort_keys=True, ensure_ascii=False))


# -- classification ----------------------------------------------------------

def score(text: str, profiles: dict[str, dict[str, Any]] | None = None) -> dict[str, int]:
    """Per-language discriminative function-word hit counts for ``text``.

    A one- or two-letter token is DISCARDED when it is all-uppercase.
    Measured on 1,833 unambiguously-English notes in the reference vault, the
    whole short-token surface there is acronyms and locale codes, not prose:
    ``OS`` (470 hits), ``N/A`` -> ``N``/``A``, ``UN``, ``LA``, and ``EN``/
    ``ES`` from ``(EN summary)`` and ``lang="en"``. What this classifies is
    Markdown carrying CSS, code and acronyms, so the tokenizer sees all of
    it, and Portuguese cannot be detected without its short articles — so
    admit them and drop only the SHOUTED forms they collide with.

    The test is ``isupper()``, not ``islower()``, and the difference is
    load-bearing: a sentence-initial ``In``/``El`` is Title case, not
    shouting, and rejecting it cost three low-signal English notes their
    classification and Spanish its leading ``El``. Single letters cannot be
    told apart this way (``"O".isupper()`` is true either way), so a
    sentence-initial ``O`` is still lost; no measured case needed it.
    """
    prepared = _prepared(profiles if profiles is not None else load_profiles())
    words = [w.lower() for w in _WORD.findall(text)
             if len(w) > 2 or not w.isupper()]
    return {code: sum(1 for w in words if w in stop) for code, stop in prepared}


def classify(text: str, profiles: dict[str, dict[str, Any]] | None = None) -> str:
    """The note's dominant language code, or ``"unknown"``.

    ``unknown`` is a real answer, not a failure: too little signal
    (< ``MIN_SIGNAL_HITS``), a tie between the top two, or a language nobody
    supplied a profile for all land here rather than being rounded into
    English.
    """
    scores = score(text, profiles)
    if not scores:
        return UNKNOWN
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    top_code, top_score = ranked[0]
    if top_score < MIN_SIGNAL_HITS:
        return UNKNOWN
    if len(ranked) > 1 and ranked[1][1] == top_score:
        return UNKNOWN
    return top_code


# -- census ------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default
    return v if 0.0 <= v <= 1.0 else default


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default
    return v if v >= 1 else default


def select_languages(
    counts: dict[str, int], *, cap: int | None = None, min_share: float | None = None,
) -> dict[str, Any]:
    """Apply the prevalence policy to per-language note counts.

    Returns ``{"selected", "dropped_by_cap", "below_threshold", "min_share",
    "cap"}``. Selection is top-N by prevalence above ``min_share`` (and at
    least ``MIN_NOTES_PER_LANGUAGE`` notes), ties broken by language code
    ascending — deterministic, so two runs over the same vault never disagree
    about which languages an agent should translate into. ``unknown`` is never
    a vault language and never counts toward the denominator.
    """
    cap = cap if cap is not None else _env_int(MAX_LANGUAGES_ENV, DEFAULT_MAX_LANGUAGES)
    min_share = min_share if min_share is not None else _env_float(MIN_SHARE_ENV, DEFAULT_MIN_SHARE)
    classified = sum(n for c, n in counts.items() if c != UNKNOWN)
    ranked = sorted(
        ((c, n) for c, n in counts.items() if c != UNKNOWN and n > 0),
        key=lambda kv: (-kv[1], kv[0]),
    )
    eligible, below = [], []
    for code, n in ranked:
        share = (n / classified) if classified else 0.0
        if n >= MIN_NOTES_PER_LANGUAGE and share >= min_share:
            eligible.append(code)
        else:
            below.append(code)
    return {
        "selected": eligible[:cap],
        "dropped_by_cap": eligible[cap:],
        "below_threshold": below,
        "min_share": min_share,
        "cap": cap,
    }


def census(
    texts: Iterable[tuple[str, str]], *, profiles: dict[str, dict[str, Any]] | None = None,
    detail: bool = False,
) -> dict[str, Any]:
    """Classify ``(note_id, body)`` pairs and aggregate the vault census.

    ``detail=True`` also returns ``by_note`` (id -> language) — what a triage
    or translation pass needs; the cached/status form omits it.
    """
    profiles = profiles if profiles is not None else load_profiles()
    counts: dict[str, int] = {}
    by_note: dict[str, str] = {}
    total = 0
    for note_id, body in texts:
        total += 1
        lang = classify(body or "", profiles)
        counts[lang] = counts.get(lang, 0) + 1
        if detail:
            by_note[note_id] = lang
    policy = select_languages(counts)
    classified = sum(n for c, n in counts.items() if c != UNKNOWN)
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    out: dict[str, Any] = {
        "method": "stopword-profile/1",
        "notes": total,
        "classified": classified,
        "counts": dict(ordered),
        "shares": {
            c: round(n / classified, 4) for c, n in ordered
            if c != UNKNOWN and classified
        },
        "vault_languages": policy["selected"],
        "dropped_by_cap": policy["dropped_by_cap"],
        "below_threshold": policy["below_threshold"],
        "min_share": policy["min_share"],
        "cap": policy["cap"],
        "multilingual": len(policy["selected"]) > 1,
        "profiles": sorted(profiles),
        "profiles_fingerprint": profiles_fingerprint(profiles),
    }
    if detail:
        out["by_note"] = by_note
    return out


def variant_languages(census_block: dict[str, Any] | None, query_language: str) -> list[str]:
    """The languages a query should ALSO be issued in, per the §5 contract.

    Every vault language except the one the question is already in. Empty on a
    single-language vault (the contract's stated exemption) and on an absent
    census — fail quiet, never fabricate a target language.
    """
    langs = list((census_block or {}).get("vault_languages") or [])
    if len(langs) < 2:
        return []
    return [c for c in langs if c != query_language]

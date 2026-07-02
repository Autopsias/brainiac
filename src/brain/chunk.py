"""Block/section chunking + Anthropic-style in-language contextual prefix (IDX-02).

Two ideas, both lifting retrieval recall:

1. **Chunk, don't embed whole notes.** A 4 KB note embedded as one vector blurs
   every section together; a query that matches one paragraph competes against
   the whole-note average. We split the body at Markdown section/block level
   (headings first, then paragraph blocks, with a soft size budget) so each
   chunk is a coherent unit.

2. **Contextual prefix (Anthropic "Contextual Retrieval").** Each chunk is
   prepended — BEFORE embedding — with a short blurb situating it inside its
   note ("From note 'X' …; section: Y"). This restores the context a bare chunk
   loses and sharply cuts misses. The blurb is written **in the note's own
   language** (a Portuguese note gets a Portuguese blurb) so it does not pollute
   the chunk's language; the cross-lingual bridge happens at *query* time in
   Arctic-embed's shared multilingual vector space.

The canonical task prefix (``query:`` / ``passage:``) is a model control token
and is applied by the embedder (``brain.embed``), OUTSIDE this contextual prefix
— and is never translated. The contextual prefix here is content, not a token.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Soft target / hard ceiling for a chunk, in characters. Blocks are merged up to
# the target and a single oversized block is split at the ceiling.
TARGET_CHARS = 900
MAX_CHARS = 1400

# Dual-granularity threshold (RET-03) — DISABLED BY DEFAULT (= 0) after the S10
# A/B falsified it for this corpus + model. The hypothesis: a short curated note
# split per-section scatters its cross-lingual signal across competing vectors, so
# indexing it as ONE whole-note chunk should restore the edge Smart Connections
# (whole-note, same e5-small) has on monolingual PT. EMPIRICAL RESULT (full e5-small
# re-index, 2026-06-28): it did NOT help and slightly REGRESSED — monolingual_pt
# 0.653 → 0.611, overall 0.573 → 0.553 vs the chunk index at the same zone weight.
# Mashing a note's sections into one 450-token vector DILUTES the specific matching
# section rather than concentrating it (the opposite of the literature's claim for
# small models), and the corpus had few multi-section short notes anyway (only ~824
# of 83k chunks merged). Kept as an env-gated capability for other corpora, but the
# DEFAULT IS 0 (pure section chunking) because it is the better config here.
# Set BRAIN_WHOLENOTE_MAX_CHARS=<chars> to re-enable. Evidence:
# docs/operations/s10-agentic-retrieval-analysis.md.
WHOLE_NOTE_MAX_CHARS = 0


def _whole_note_max() -> int:
    try:
        return int(os.environ.get("BRAIN_WHOLENOTE_MAX_CHARS", "") or WHOLE_NOTE_MAX_CHARS)
    except ValueError:
        return WHOLE_NOTE_MAX_CHARS

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass
class Chunk:
    ordinal: int
    heading: str       # nearest enclosing heading text ("" if none / preamble)
    text: str          # the raw chunk body (no prefixes)
    lang: str          # detected language of the chunk: "pt" | "es" | "en"

    def embed_input(self, title: str, zone: str, doc_context: str = "") -> str:
        """The exact string handed to the embedder for this chunk: the
        in-language contextual prefix + a blank line + the chunk text.

        ``doc_context`` (UPG-04, optional) is an LLM-generated ≤1-sentence
        document-level summary that situates this chunk within its note. When
        non-empty it is prepended (after the template prefix, before the chunk
        text) so the embedding sees the note's overall meaning alongside the
        chunk's specific content. Empty string = the S10 template-only path.
        """
        prefix = contextual_prefix(title, zone, self.heading, self.lang)
        if doc_context:
            prefix = prefix + " " + doc_context
        return prefix + "\n\n" + self.text


# --- language detection (lightweight, dependency-free) ---------------------
# Stopword sets chosen to be discriminative between EN / PT / ES. Not a full
# language ID — good enough to pick the contextual-prefix language.
_PT = {
    "de", "que", "não", "uma", "para", "com", "como", "mais", "está", "são",
    "também", "já", "nós", "sobre", "será", "foi", "ção", "às", "então", "porque",
}
_ES = {
    "de", "que", "no", "una", "para", "con", "como", "más", "está", "son",
    "también", "ya", "nosotros", "sobre", "será", "fue", "ción", "pero", "porque",
    "el", "los", "las", "una",
}
_EN = {
    "the", "and", "of", "to", "in", "is", "are", "for", "with", "that", "this",
    "as", "be", "on", "by", "an", "we", "it", "from", "will",
}
_WORD = re.compile(r"[A-Za-zÀ-ÿ]+")


def detect_language(text: str) -> str:
    """Return 'pt', 'es', or 'en' by discriminative-stopword frequency.

    Defaults to 'en' on a tie or when there is too little signal. Accent marks
    (ç, ã, õ, á, é) strongly bias toward PT/ES.
    """
    words = [w.lower() for w in _WORD.findall(text)]
    if not words:
        return "en"
    scores = {
        "pt": sum(1 for w in words if w in _PT),
        "es": sum(1 for w in words if w in _ES),
        "en": sum(1 for w in words if w in _EN),
    }
    # Portuguese-specific orthography (ã, õ, ç) is a strong PT signal.
    if re.search(r"[ãõç]", text):
        scores["pt"] += 3
    # Spanish-specific (ñ, ¿, ¡)
    if re.search(r"[ñ¿¡]", text):
        scores["es"] += 3
    best = max(scores, key=lambda k: scores[k])
    # Require the winner to actually beat English by a margin, else default EN
    # (avoids flipping an English chunk to PT on a couple of shared tokens).
    if best != "en" and scores[best] <= scores["en"]:
        return "en"
    return best


# --- in-language contextual prefix -----------------------------------------
# Per-language templates. {title}=note title, {zone}=para zone, {heading}=section.
_TEMPLATES = {
    "en": "Context: from the note '{title}' (zone: {zone}).{section}",
    "pt": "Contexto: da nota '{title}' (zona: {zone}).{section}",
    "es": "Contexto: de la nota '{title}' (zona: {zone}).{section}",
}
_SECTION = {
    "en": " Section: {heading}.",
    "pt": " Secção: {heading}.",
    "es": " Sección: {heading}.",
}


def contextual_prefix(title: str, zone: str, heading: str, lang: str) -> str:
    lang = lang if lang in _TEMPLATES else "en"
    section = _SECTION[lang].format(heading=heading) if heading else ""
    return _TEMPLATES[lang].format(title=title or "(untitled)", zone=zone or "brain", section=section)


# --- the chunker ------------------------------------------------------------
def _split_blocks(body: str) -> list[tuple[str, str]]:
    """Walk the body line by line, tracking the nearest heading, and group
    contiguous non-heading lines into blocks. Returns [(heading, block_text)]."""
    blocks: list[tuple[str, str]] = []
    cur_heading = ""
    buf: list[str] = []

    def flush() -> None:
        text = "\n".join(buf).strip()
        if text:
            blocks.append((cur_heading, text))
        buf.clear()

    for line in body.splitlines():
        m = _HEADING.match(line)
        if m:
            flush()
            cur_heading = m.group(2).strip()
        elif line.strip() == "" and buf:
            # paragraph boundary inside a section
            flush()
        else:
            buf.append(line)
    flush()
    return blocks


def _pack(heading: str, text: str, ordinal_start: int, lang_of) -> list[Chunk]:
    """Split one oversized block at the char ceiling into sub-chunks."""
    out: list[Chunk] = []
    o = ordinal_start
    if len(text) <= MAX_CHARS:
        out.append(Chunk(o, heading, text, lang_of(text)))
        return out
    # greedy word-wrap into <= MAX_CHARS slices
    words = text.split()
    cur: list[str] = []
    size = 0
    for w in words:
        if size + len(w) + 1 > MAX_CHARS and cur:
            piece = " ".join(cur)
            out.append(Chunk(o, heading, piece, lang_of(piece)))
            o += 1
            cur, size = [], 0
        cur.append(w)
        size += len(w) + 1
    if cur:
        piece = " ".join(cur)
        out.append(Chunk(o, heading, piece, lang_of(piece)))
    return out


def chunk_text(body: str, *, lang_of=detect_language) -> list[Chunk]:
    """Chunk a note body at section/block level with a soft size budget.

    Strategy: split into heading-scoped blocks, then greedily merge adjacent
    blocks under the same heading up to TARGET_CHARS, splitting any single block
    over MAX_CHARS. Each chunk's language is detected on its own text.

    Dual-granularity (RET-03): a short note (whole body ≤ ``_whole_note_max()``)
    is returned as ONE whole-note chunk — its sections are NOT split — so its
    cross-lingual signal stays concentrated in a single vector. See
    ``WHOLE_NOTE_MAX_CHARS``.
    """
    whole_max = _whole_note_max()
    stripped = body.strip()
    if whole_max > 0 and stripped and len(stripped) <= whole_max:
        return [Chunk(0, "", stripped, lang_of(stripped))]
    blocks = _split_blocks(body)
    if not blocks:
        return []
    chunks: list[Chunk] = []
    ordinal = 0
    pending_heading = blocks[0][0]
    pending_text = ""

    def emit(heading: str, text: str) -> None:
        nonlocal ordinal
        for ch in _pack(heading, text, ordinal, lang_of):
            ch.ordinal = ordinal
            chunks.append(ch)
            ordinal += 1

    for heading, text in blocks:
        if heading != pending_heading:
            if pending_text:
                emit(pending_heading, pending_text)
            pending_heading, pending_text = heading, text
            continue
        if not pending_text:
            pending_text = text
        elif len(pending_text) + len(text) + 2 <= TARGET_CHARS:
            pending_text = pending_text + "\n\n" + text
        else:
            emit(pending_heading, pending_text)
            pending_text = text
    if pending_text:
        emit(pending_heading, pending_text)
    return chunks

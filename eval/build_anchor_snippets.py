#!/usr/bin/env python3
"""s01 follow-on — regenerate REAL relevant-passage snippets for the 15 ANCHOR
queries only (Ricardo's decision: anchors-only, fixed console).

Root cause fixed here: ``build_pt_candidates.py::snippet_for`` grabbed the
note's FIRST LINE (a header, e.g. ``[[Contoso]] - [[Atlas]] Project`` or
``**Open original:** [[...docx]]``) — there was nothing to judge relevance on.
This script instead, for each anchor query x candidate note:

  1. Reads the candidate's full text from the source vault (canonical
     ``note_id`` == source-vault-relative path, per H16/H17).
  2. Chunks it with the SAME section/block chunker the brain index uses
     (``brain.chunk.chunk_text``) and embeds each chunk with the exact same
     ``embed_input`` (in-language contextual prefix) the index build uses, via
     the REAL embedder (``brain.embed.get_embedder`` — multilingual-e5-small
     ONNX; refuses to run under ``BRAIN_REQUIRE_REAL_EMBEDDER=1`` if only the
     non-semantic HashEmbedder is available).
  3. Embeds the query once (``is_query=True``) and cosine-scores it against
     every chunk of the note, keeping the BEST-scoring chunk as the shown
     snippet (trimmed to ~300-500 chars at a sentence boundary where
     possible).
  4. Falls back to a lexical-overlap sliding window over the raw body if the
     note has no chunks (e.g. an empty file) or fails to load.
  5. Also captures the note's full body (frontmatter stripped, capped) so the
     console can render it on "expand" without another file read (self-
     contained / offline).

Notes are cached per note_id (chunked + embedded once) since several anchor
queries share overlapping candidate notes.

Run with the REAL embedder (never the silent HashEmbedder):
  BRAIN_REQUIRE_REAL_EMBEDDER=1 BRAIN_MODEL_CACHE=.fastembed_cache \\
    .venv-embed/bin/python eval/build_anchor_snippets.py \\
    --golden _evidence/s01/pt-golden-set.json \\
    --candidates _evidence/s01/qrels_candidates.json \\
    --source-vault /path/to/your-vault \\
    --out _evidence/s01/anchor_candidates_v2.json
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))

SNIPPET_TARGET_CHARS = 450
FULL_TEXT_MAX_CHARS = 80_000  # generous cap so even long transcripts fit; console truncates visually if needed
LEXICAL_WINDOW = 420
LEXICAL_STEP = 180


def strip_frontmatter(text: str) -> str:
    from brain import frontmatter

    fm = frontmatter.split(text)
    return fm[1].lstrip("\n") if fm else text


def zone_of(note_id: str) -> str:
    return note_id.split("/", 1)[0] if "/" in note_id else ""


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def trim_snippet(text: str, target: int = SNIPPET_TARGET_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= target:
        return text
    cut = text[:target]
    for sep in (". ", "? ", "! ", "; ", "— "):
        idx = cut.rfind(sep)
        if idx > target * 0.5:
            return cut[: idx + 1].strip()
    return cut.rstrip() + "…"


# A chunk that is JUST an "Open original: [[file.pdf]]" placeholder line (the
# root-cause pattern from the v1 console — see module docstring) sometimes
# scores deceptively high (the wikilink filename text itself can share
# vocabulary with the query), but it is never the actual answer content.
# Reject it outright as a snippet candidate rather than trusting cosine rank
# alone — the exact defect this rebuild exists to fix.
_BOILERPLATE_RE = re.compile(r"^\*\*open original:?\*\*\s*\[\[.*\]\]\s*$", re.IGNORECASE)


def is_boilerplate(text: str) -> bool:
    return bool(_BOILERPLATE_RE.match(" ".join(text.split())))


_WORD = re.compile(r"\w+", re.UNICODE)


def best_lexical_window(body: str, query: str, win: int = LEXICAL_WINDOW, step: int = LEXICAL_STEP) -> str:
    toks = {t.lower() for t in _WORD.findall(query) if len(t) > 2}
    if not body:
        return ""
    if not toks:
        return body[:win]
    n = len(body)
    if n <= win:
        return body
    best_i, best_score = 0, -1
    i = 0
    while i < n - win:
        window_l = body[i : i + win].lower()
        score = sum(window_l.count(t) for t in toks)
        if score > best_score:
            best_score, best_i = score, i
        i += step
    return body[best_i : best_i + win]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--golden", required=True)
    ap.add_argument("--candidates", required=True, help="qrels_candidates.json (all 102 queries; anchors filtered here)")
    ap.add_argument("--source-vault", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--full-text-max-chars", type=int, default=FULL_TEXT_MAX_CHARS)
    ap.add_argument("--all", action="store_true",
                    help="process ALL queries (not just the 15 anchors) — used by the "
                         "dual-model adjudication follow-on (all_candidates_snippets.json)")
    args = ap.parse_args()

    from brain.chunk import chunk_text
    from brain.embed import get_embedder

    embedder = get_embedder("auto")
    print(
        f"embedder: {type(embedder).__name__} model={getattr(embedder, 'model_id', '?')}",
        file=sys.stderr,
    )

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    anchor_ids = {q["id"] for q in golden["queries"] if q.get("anchor")}

    cands_all = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    if args.all:
        anchors = list(cands_all)
        print(f"--all: processing every query ({len(anchors)})", file=sys.stderr)
    else:
        anchors = [c for c in cands_all if c.get("anchor") and c["qid"] in anchor_ids]
        if len(anchors) != len(anchor_ids):
            print(
                f"WARNING: golden has {len(anchor_ids)} anchors but candidates file "
                f"matched {len(anchors)} — check qid alignment",
                file=sys.stderr,
            )

    src_root = Path(args.source_vault)
    note_cache: dict[str, tuple[str | None, list[tuple[str, list[float]]]]] = {}

    def load_note(note_id: str) -> tuple[str | None, list[tuple[str, list[float]]]]:
        if note_id in note_cache:
            return note_cache[note_id]
        fp = src_root / note_id
        if not fp.is_file():
            note_cache[note_id] = (None, [])
            return note_cache[note_id]
        raw = fp.read_text(encoding="utf-8", errors="replace")
        body = strip_frontmatter(raw)
        title = Path(note_id).stem
        zone = zone_of(note_id)
        chunks = chunk_text(body)
        pairs: list[tuple[str, list[float]]] = []
        if chunks:
            texts = [ch.embed_input(title, zone) for ch in chunks]
            if hasattr(embedder, "embed_batch"):
                vecs = embedder.embed_batch(texts, is_query=False)
            else:
                vecs = [embedder.embed(t, is_query=False) for t in texts]
            pairs = list(zip([ch.text for ch in chunks], vecs))
        note_cache[note_id] = (body, pairs)
        return note_cache[note_id]

    out = []
    n_chunk_scored = 0
    n_lexical_fallback = 0
    n_missing = 0
    for q in anchors:
        qid = q["qid"]
        text = q["query"]
        qvec = embedder.embed(text, is_query=True)
        new_cands = []
        for cand in q["candidates"]:
            nid = cand["note_id"]
            body, chunk_pairs = load_note(nid)
            if body is None:
                snippet = "(note not found in source vault — cannot extract a passage; verify note_id)"
                full_text = ""
                match_score = None
                n_missing += 1
            elif chunk_pairs:
                scored = sorted(
                    ((cosine(qvec, v), t) for t, v in chunk_pairs), key=lambda x: -x[0]
                )
                # Prefer the best-scoring chunk that (a) has enough content to
                # judge (>=80 chars stripped) and (b) is not a bare "Open
                # original" placeholder line (see is_boilerplate) — a
                # top-ranked chunk that is just a wikilink/TOC line is
                # technically the argmax but useless to a human adjudicator,
                # and the wikilink filename text can itself score deceptively
                # high. Searches the FULL ranked list (not just the top few) —
                # falls back to the top-2 chunks concatenated only if truly
                # nothing in the note clears the bar.
                MIN_CONTENT_CHARS = 80
                best_score, best_chunk = scored[0]
                pick = next(
                    (
                        (s, t)
                        for s, t in scored
                        if len(" ".join(t.split())) >= MIN_CONTENT_CHARS and not is_boilerplate(t)
                    ),
                    None,
                )
                if pick is not None:
                    best_score, best_chunk = pick
                elif len(scored) > 1:
                    best_chunk = best_chunk + "\n\n" + scored[1][1]
                snippet = trim_snippet(best_chunk)
                full_text = body[: args.full_text_max_chars]
                match_score = round(float(best_score), 4)
                n_chunk_scored += 1
            else:
                snippet = trim_snippet(best_lexical_window(body, text))
                full_text = body[: args.full_text_max_chars]
                match_score = None
                n_lexical_fallback += 1
            new_cands.append(
                {
                    "note_id": nid,
                    "title": cand.get("title", Path(nid).stem),
                    "snippet": snippet,
                    "full_text": full_text,
                    "machine": bool(cand.get("machine", False)),
                    "match_score": match_score,
                }
            )
        out.append(
            {
                "qid": qid,
                "query": text,
                "lang": q["lang"],
                "qclass": q["qclass"],
                "anchor": bool(q.get("anchor", False)),
                "candidates": new_cands,
            }
        )

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    n_cands = sum(len(a["candidates"]) for a in out)
    print(
        f"anchors: {len(out)}  candidates: {n_cands}  "
        f"chunk-scored: {n_chunk_scored}  lexical-fallback: {n_lexical_fallback}  "
        f"missing-file: {n_missing}"
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""BLK-01 — the quality gate for a BULK RELINKING batch, run over the WHOLE
batch rather than a sample.

Why this is a script and not a review habit: an agent told to drive an
unreachable-document count toward zero converges on the cheapest note that
survives a skim, and this vault has MEASURED that filler links are worse than
none (4,353 graphify-inferred edges rescued zero floor labels and cost one,
through PageRank dilution). So the quality contract is declared up front, in
code, with thresholds fixed BEFORE the batch is written, and every note is
checked — no sampling, no tolerated defect rate to argue about afterwards.

A batch note is a ``brain/`` note carrying ``bulk_link_batch: <id>`` in its
frontmatter (the same note-level stamp ``graph.bulk_linked_ids`` reads, so what
is checked here is exactly what can be excluded from the graph later).

The six checks, and what each one is actually defending against:

1. STAMPED       — type ``source-derived``, a ``source:`` frontmatter anchor,
                   and the batch stamp. Without the stamp the note cannot be
                   isolated, and the counterfactual arm dies.
2. SUBSTANCE     — the body clears a minimum length and carries at least
                   ``MIN_PROPOSITIONS`` sentences bearing a figure, date or
                   quantity that does NOT appear in the note's own title.
                   This is the anti-stub check: a note that restates its title
                   has no such sentence.
3. NOT-A-TITLE-  — at least ``MIN_NOVEL_TOKEN_RATIO`` of the body's DISTINCT
   PARAPHRASE      content vocabulary (unique tokens, function words removed)
                   is absent from the title.

                   CORRECTION, 2026-08-11, disclosed because it was made after
                   the first batch was measured: this ratio was originally
                   computed over every body token INCLUDING repeats and
                   function words, which is not what "content tokens" means and
                   is not what the check is for. A long title containing "the",
                   "for" or "and" then scored against every occurrence of those
                   words in the body, and three notes carrying 11-15
                   figure-bearing propositions each landed at 81-85% purely on
                   article frequency. The threshold was NOT moved; the metric
                   was corrected to the distinct-vocabulary basis it always
                   claimed, and the known-positive probe (a stub restating its
                   own title) still fails it at 44%. The frequency-basis number
                   is still reported, ungated, as ``token_frequency_ratio`` so
                   nothing is hidden by the correction.
4. RELATIONS     — every wikilink to a cited raw source sits on a line that
                   also states a relation (an em-dash clause). A bare wikilink
                   is exactly the edge that dilutes PageRank without carrying
                   meaning.
5. NO-TEMPLATE   — pairwise 5-gram Jaccard between any two batch bodies stays
                   at or below ``MAX_PAIR_SIMILARITY``. Catches a batch written
                   from one template with the nouns swapped.
6. TIER          — the note's classification is at least the maximum of the
                   classifications of the sources it cites. A derived note that
                   summarises Restricted substance at Internal is a fresh
                   cross-tier exposure, and one no filename-twin metric can see.

Read-only. Exit 0 when every check passes, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# -- the pre-registered thresholds (fix these BEFORE writing a batch) --------
MIN_BODY_CHARS = 1200
MIN_PROPOSITIONS = 5
MIN_NOVEL_TOKEN_RATIO = 0.85
MAX_PAIR_SIMILARITY = 0.15

TIERS = ["Public", "Internal", "Confidential", "Restricted", "MNPI"]
_WORD = re.compile(r"[0-9A-Za-zÀ-ÿ][0-9A-Za-zÀ-ÿ'’-]*")
_FIGURE = re.compile(r"\d")
_LINK = re.compile(r"\[\[([^\]\|#\n\r]+)(?:#[^\]\|\n\r]+)?(?:\|.+?)?\]\]")


def _tokens(text: str) -> list[str]:
    return [m.group(0).lower() for m in _WORD.finditer(text or "")]


def _function_words() -> frozenset[str]:
    """The census stopword profiles, reused rather than re-listed — plus the
    short English articles/prepositions the census deliberately omits (it is a
    language-ID census, not a stopword list, so it only carries DISCRIMINATIVE
    words)."""
    from brain.language import load_profiles

    words = {w for spec in load_profiles().values() for w in spec["stopwords"]}
    words |= {"a", "at", "or", "not", "its", "it", "if", "so", "no", "but",
              "was", "were", "has", "have", "had", "can", "cannot", "do",
              "does", "did", "than", "then", "there", "their", "them", "they",
              "these", "those", "what", "which", "who", "whom", "whose", "when",
              "where", "why", "how", "all", "any", "both", "each", "more",
              "most", "other", "some", "such", "only", "own", "same", "too",
              "very", "into", "over", "under", "out", "up", "down", "about",
              "after", "before", "between", "through", "during", "against",
              "one", "two", "three", "s", "t", "per", "also", "still", "while",
              "because", "been", "being", "would", "could", "should", "may",
              "might", "must", "shall", "here", "now", "yet", "own", "is"}
    return frozenset(words)


def _unwrap(body: str) -> list[str]:
    """Bullet lines, with wrapped continuations folded back onto their bullet —
    otherwise a relation that ran onto the next line reads as a bare link."""
    out: list[str] = []
    for raw in (body or "").splitlines():
        if raw.startswith(("  ", "\t")) and out and out[-1].lstrip().startswith(("-", "*")):
            out[-1] = out[-1].rstrip() + " " + raw.strip()
        else:
            out.append(raw)
    return out


def _sentences(body: str) -> list[str]:
    prose = "\n".join(ln for ln in _unwrap(body) if not ln.lstrip().startswith("#"))
    return [s.strip() for s in re.split(r"(?<=[.!?;])\s+|\n{2,}|\n\|", prose) if s.strip()]


def _shingles(body: str, n: int = 5) -> set[tuple[str, ...]]:
    toks = _tokens(body)
    return {tuple(toks[i:i + n]) for i in range(max(0, len(toks) - n + 1))}




def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--batch", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    res = check_batch(Path(args.vault).expanduser(), args.batch)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(f"batch {res['batch']}: {res['notes']} note(s), "
              f"max pairwise similarity {res['max_pair_similarity']:.2%}")
        for r in res["rows"]:
            print(f"  {r['id']}: {r['body_chars']}B, {r['propositions']} propositions, "
                  f"{r['novel_token_ratio']:.2%} novel vocabulary "
                  f"({r['token_frequency_ratio']:.2%} by token frequency), "
                  f"{len(r['cited_sources'])} cited source(s), "
                  f"{r['classification']} (>= {r['min_required_tier']})")
        for f in res["failures"]:
            print(f"  FAIL {f}")
        print("OK" if res["ok"] else f"FAILED ({len(res['failures'])})")
    return 0 if res["ok"] else 1


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.modules.setdefault("tools.bulk_link_check", sys.modules[__name__])
from tools.bulk_link_batch import check_batch  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

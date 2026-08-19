"""The grounding-overlap detector of `cos_model_answer` — fold, shingles, the unique-block subtraction (batch-2 drain).

Moved verbatim out of `cos_model_answer`; every name is re-imported by the
parent (and `cos_judge`'s `_answer_mod()` reads them off the parent module), so
`cma.shingles`, `cma.block_shingles`, `cma.USE_SHINGLE_W` and friends keep
their module path.
"""
from __future__ import annotations

import re
import unicodedata

#: D14's overlap rule: shingle width 5 tokens — the same width `brain`'s own
#: document-identity primitive uses (ENF-03), so the engine keeps ONE notion of
#: "the same text". A field of fewer than 5 tokens yields no shingles and CANNOT
#: BE JUDGED; that limit is stated rather than papered over, and such a field is
#: too short to carry a meaningful quotation.
SHINGLE_W = 5


#: THE DOTLESS-I PAIR, which no Unicode normalization form folds (review
#: 2026-08-15). `casefold()` maps `İ` to `i` + U+0307 but leaves Turkish `ı`
#: its own letter, so `the board ınfo details now` shares NO five-token shingle
#: with its plain spelling — a visually identical quotation that walks past the
#: refusal. It is translated explicitly; the combining-mark strip below then
#: closes the `İ` half and every accent confusable with it.
_DOTLESS_I = str.maketrans({"ı": "i", "ȷ": "j"})


def _fold(text: str) -> str:
    """One folding, used on BOTH sides of every comparison.

    NFKC, NOT NFC — a five-token grounding phrase re-rendered in FULLWIDTH
    characters is visually the same text and tokenizes differently under NFC, so
    `overlap_hit` returned False on a verbatim quotation. Then casefold, then the
    dotless-i translation, then NFKD with every combining mark dropped.

    THE MARK STRIP WIDENS THE DETECTOR, deliberately and in the fail-closed
    direction: `résumé` and `resume` now share a shingle, so a row that would
    have passed can now be REFUSED. A refusal costs one verdict and is visible in
    `refused_grounding_overlap`; a missed quotation is MNPI at rest in a sink. It
    never widens the other way — nothing that was refused becomes accepted.
    """
    s = unicodedata.normalize("NFKC", str(text or "")).casefold()
    s = s.translate(_DOTLESS_I)
    return "".join(ch for ch in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(ch))


def _norm_tokens(text: str) -> list[str]:
    """Fold, then every run of non-alphanumerics to one space, strip.
    BOTH SIDES are normalized identically or the comparison means nothing."""
    # `\W` is unicode-aware for `str` patterns, so an accented or CJK token
    # survives; `_` joins the separator class because it is not alphanumeric.
    return [t for t in re.split(r"[\W_]+", _fold(text)) if t]


def shingles(text: str, w: int = SHINGLE_W) -> set[tuple[str, ...]]:
    toks = _norm_tokens(text)
    return {tuple(toks[i:i + w]) for i in range(len(toks) - w + 1)}


def block_shingles(block_text: str, own_row_text: str,
                   w: int = SHINGLE_W) -> set[tuple[str, ...]]:
    """The shingles UNIQUE to this conversation's grounding block.

    Step 4 of the rule, and it is what stops the whole thing firing on a verdict
    legitimately quoting the subject line: subtract everything the block shares
    with the conversation's OWN batch row (its `subject`, `sender` and, for
    staging, its `text`). Only what is unique to the vault block counts.

    `w` is `SHINGLE_W` (5) for the REFUSAL rule and nothing else changes it. The
    counter-direction signal (`USE_SHINGLE_W`, below) passes 2 — one function,
    one tokenizer, one subtraction, two widths.
    """
    return shingles(block_text, w) - shingles(own_row_text, w)


#: THE WIDTH THE *USE* SIGNAL READS AT, and it is 2 rather than 1 on measured
#: grounds, not taste. At width 1 the "unique to the block" set still contains
#: function words the row's own text happened not to use — a verdict reading
#: "confirm the tender position by Friday" matched a block on the word `by`, so
#: a row that ignored the vault entirely scored as having used it (probed:
#: `test_the_run_facts_count_the_rows_that_USED_their_block_not_only_delivery`).
#: A two-token run is the shortest thing that carries a phrase rather than a
#: word. It stays well under `SHINGLE_W`, because this is a floor on USE, not
#: the refusal rule's evidence of QUOTATION.
USE_SHINGLE_W = 2


def overlap_hit(field_text: str, uniq: set[tuple[str, ...]]) -> bool:
    """THRESHOLD ONE. If ANY shingle unique to the block occurs in the field, the
    row is refused — there is no percentage to calibrate and no free parameter to
    drift. A five-word verbatim run out of host-written vault prose the model was
    never asked to reproduce is not coincidence, and the legitimate quoting path
    is the mail BODY, which the subtraction above has already excluded."""
    return bool(uniq) and bool(shingles(field_text) & uniq)

"""Literal-ASCII label validation for `cos_ground.extract_domain` (D7b rule 6).

The domain extractor refuses ~0 real rows rather than parsing them wrongly, and
this module holds the one refusal cluster that is about the DOMAIN'S SHAPE
rather than the `From` string's shape: the literal-ASCII-label rule and its
punctuation guards. The extractor itself stays in :mod:`cos_ground` (its
callers, `cos_driver_page.js` semantics and the tests name it there); this
module imports nothing from it — the domain string arrives as a parameter.
"""
from __future__ import annotations

_LITERAL_ASCII = "abcdefghijklmnopqrstuvwxyz0123456789.-"


def literal_ascii_labels(domain: str) -> bool:
    """Rule 6 of the eight: literal ASCII labels only, correctly punctuated.

    Returns whether the whole cluster of rule-6 checks passes — the caller
    (D7b rule 5's NFC+casefold step having already run) refuses the domain when
    it does not.
    """
    # 6. Literal ASCII labels only. This closes the punycode/homograph hole in
    #    BOTH directions: a Unicode homograph domain is refused outright, and an
    #    `xn--…` label is compared LITERALLY, so it can only ever equal a tenant
    #    domain an owner wrote in that same literal form.
    if any(ch not in _LITERAL_ASCII for ch in domain):
        return False
    if domain.startswith((".", "-")) or domain.endswith((".", "-")) or ".." in domain:
        return False
    for label in domain.split("."):
        if not label or label.startswith("-") or label.endswith("-"):
            return False
    return True

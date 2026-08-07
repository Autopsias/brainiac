"""Email provenance (PRV-01/PRV-02) + the ONE host-side secret scrub.

**Schema: FLAT DOTTED frontmatter keys, never a nested mapping.** ``provenance``
is not a mapping anywhere in this codebase — ``provenance.trust: untrusted`` is
written literally (``capture.enforce``, ``core._stamp_draft_frontmatter``, the
drain's ``frontmatter.set_keys``) and read literally
(``maintenance._is_untrusted``). Converting it to a nested block would make the
drain's untrusted-input detection silently read drained VM notes as TRUSTED, so
the email keys are added as SIBLINGS of the same shape:

    provenance.trust: untrusted            # pre-existing (capture stamp)
    provenance.sender: "Alice <a@x.com>"   # who sent it
    provenance.sent: 2026-07-05            # ISO date (or full ISO datetime)
    provenance.conversation_id: "<abc@x>"  # the thread it came from
    provenance.subject: "Q3 pricing"       # the subject line
    provenance.verified: true              # HOST-parsed from the original

**Claimed vs verified (the trust split).** Everything a VM writes — a
``cos-propose`` candidate's frontmatter, an ingest-manifest line — is a CLAIM.
It rides along, but it never carries ``provenance.verified``: that key is set
ONLY by the host ingest pipeline, and only from metadata parsed out of the
archived original itself (.eml headers). Downstream authority decisions (version
deduction) may count verified provenance only.

**One scrub routine.** ``scrub()`` is the single redaction function every
serialization surface routes through — proposal claim, ingest-manifest claim,
the provenance store, batch/report/ledger rendering. A secret planted in a
sender or subject must not reach any downstream artifact.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# -- the flat dotted key contract --------------------------------------------
TRUST_KEY = "provenance.trust"
VERIFIED_KEY = "provenance.verified"
SENDER_KEY = "provenance.sender"
SENT_KEY = "provenance.sent"
CONVERSATION_KEY = "provenance.conversation_id"
SUBJECT_KEY = "provenance.subject"

#: short field names, as used in manifest lines / the provenance store / kwargs
FIELDS: tuple[str, ...] = ("sender", "sent", "conversation_id", "subject")
#: the frontmatter key for each field, in the same order
CLAIM_KEYS: tuple[str, ...] = (SENDER_KEY, SENT_KEY, CONVERSATION_KEY, SUBJECT_KEY)
_KEY_BY_FIELD: dict[str, str] = dict(zip(FIELDS, CLAIM_KEYS))
#: every recognised dotted provenance key (validator + OKF profile share this)
KEYS: tuple[str, ...] = CLAIM_KEYS + (TRUST_KEY, VERIFIED_KEY)

#: keys ONLY the host may assert. Anything parsed out of a VM-WRITABLE file
#: (a proposal drop read raw off the drop dir, an ingest-manifest line) has
#: these stripped unconditionally at the parse — never checked per use site.
HOST_ONLY_KEYS: tuple[str, ...] = (VERIFIED_KEY,)


class HostOnlyKeyResidue(ValueError):
    """A document still resolves a host-only key after sanitization.

    Raised by :func:`without_host_only_text` when no line-based strip can
    clear the key — an anchored flow mapping merged with ``<<`` spells it on
    no single line yet resolves it document-wide. The boundary fails CLOSED:
    the caller rejects the candidate rather than promoting bytes that would be
    signed carrying forged host-verified provenance.
    """

#: a single provenance value is metadata, not content — cap it hard so a
#: hostile 10 MB "subject" can never bloat signed, immutable frontmatter.
MAX_VALUE_CHARS = 512

# -- secret scrub (trust boundary: VM drops + email text are attacker-influenced;
# a proposal must never smuggle credentials into the signed vault, a report, or
# a ledger) -------------------------------------------------------------------
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer-ish-key", re.compile(r"\b(?:sk|ghp|gho|glpat)[-_][A-Za-z0-9_-]{20,}\b")),
    ("kv-secret", re.compile(
        r"(?im)^\s*(?:api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*\S{8,}")),
)

REDACTED = "[redacted:secret]"


def secret_findings(text: str) -> list[str]:
    """Names of secret patterns present in ``text`` (empty = clean)."""
    return [name for name, pat in SECRET_PATTERNS if pat.search(text)]


def scrub(value: Any) -> Any:
    """THE host-side scrub. Redact every known secret pattern, recursively.

    One routine, every serialization surface (item 7): proposal claim,
    ingest-manifest claim, provenance store write, evidence records, batch
    question rendering, report/ledger generation. Strings are redacted in
    place; mappings/sequences are walked; anything else is returned unchanged.
    """
    if isinstance(value, str):
        out = value
        for _name, pat in SECRET_PATTERNS:
            out = pat.sub(REDACTED, out)
        return out
    if isinstance(value, Mapping):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(scrub(v) for v in value)
    return value


# -- value hygiene ------------------------------------------------------------
def sanitize_value(value: Any) -> str | None:
    """One provenance value, safe to serialise: scrubbed, control-chars and
    newlines collapsed to spaces, length-capped. ``None`` if nothing is left."""
    if value is None or isinstance(value, (dict, list, tuple)):
        return None
    # Scrub BEFORE collapsing whitespace: the kv-secret pattern is
    # line-anchored, so folding an embedded newline into a space first would
    # hide `\nsecret: hunter2000` from it.
    text = scrub(str(value))
    text = "".join(" " if ch < " " or ch == "\x7f" else ch for ch in text)
    text = " ".join(text.split())
    if len(text) > MAX_VALUE_CHARS:
        text = text[:MAX_VALUE_CHARS].rstrip() + "…"
    return text or None


def without_host_only(meta: Mapping[str, Any] | None,
                      *, keys: tuple[str, ...] = HOST_ONLY_KEYS) -> dict[str, Any]:
    """``meta`` with every ``keys`` entry removed (default ``HOST_ONLY_KEYS``).

    R5 (2026-07-30 review, HIGH): ``cos_propose`` strips ``provenance.verified``
    on the way in, but the proposal DROP DIRECTORY is the VM-writable surface
    the trust split exists to distrust — a VM that writes a drop file directly
    never goes through that API. The host therefore strips host-only keys ONCE,
    where it parses the untrusted file, so no downstream use site can be the
    one that forgot. A forged ``provenance.verified`` would otherwise mint a
    shared evidence lineage key and let one attacker-chosen conversation mark
    later independent REJECT verdicts as already-counted.
    """
    if not isinstance(meta, Mapping):
        return {}
    return {k: v for k, v in meta.items() if k not in keys}


def without_host_only_text(text: str, *,
                           keys: tuple[str, ...] = HOST_ONLY_KEYS) -> str:
    """``text`` with every ``keys`` frontmatter LINE removed.

    ``keys`` defaults to ``HOST_ONLY_KEYS``; ``cos.claim_drops`` widens it with
    the producer-version stamps the HOST now derives for itself (STA-01), which
    a VM may therefore no longer assert into bytes that later get signed.

    The mapping strip above protects ROUTING decisions; this protects the
    FILE. ``cos.claim_drops`` promotes the claimed copy into ``pending/`` and
    ``consume_answers`` moves it into the signed drain WITHOUT re-running
    ``capture.enforce`` — so an in-memory strip alone would still sign a VM's
    forged ``provenance.verified`` into vault frontmatter, where version
    deduction counts verified provenance as authority. Byte-identical when no
    host-only key is present: the honest path is untouched.
    """
    from . import frontmatter as fm

    split = fm.split(text)
    if split is None:
        return text
    head, body = split
    lines = head.splitlines()

    def rebuild(keep: list[str]) -> str:
        return "---\n" + "\n".join(keep) + "\n---" + body

    # Pass 1 — drop TOP-LEVEL lines that parse to a host-only key. Indentation
    # matters: a line nested inside another key's multi-line value is that
    # VALUE's text, not a key, and stripping it silently empties an honest
    # note's field.
    kept = [ln for ln in lines
            if ln[:1] in (" ", "\t") or not _is_host_only_line(ln, keys)]
    out = text if len(kept) == len(lines) else rebuild(kept)
    if not _carries_host_only(out, keys):
        return out

    # Pass 2 — the key survived a whole-document parse anyway (a YAML merge key
    # pulling it in from an anchor is the live example). Escalate to every
    # depth. Only a document actively smuggling the key reaches here, so
    # over-stripping its nested lines is the right trade; the honest path
    # already returned above, byte-identical.
    out = rebuild([ln for ln in lines if not _is_host_only_line(ln, keys)])
    if _carries_host_only(out, keys):
        # FAIL CLOSED. No line-based strip can cover every YAML construct that
        # resolves to a top-level key — `x: &a {provenance.verified: true}`
        # with `<<: *a` spells it on NO line yet resolves it in the document.
        # A best-effort sanitizer on a trust boundary is not a boundary, and
        # the next step signs these bytes, so refuse the document instead of
        # returning one still carrying the forgery. Callers turn this into a
        # claim-rejection, which is what a deliberate forgery deserves.
        raise HostOnlyKeyResidue(
            "frontmatter still resolves a host-only provenance key after "
            "sanitization (anchor/merge/flow construct) — refusing it")
    return out


def _is_host_only_line(line: str, keys: tuple[str, ...] = HOST_ONLY_KEYS) -> bool:
    """True when THIS frontmatter line parses to a host-only key.

    Asking the project's OWN parser is what makes this exact. Comparing the
    raw text before the colon let ``"provenance.verified": true`` and its
    single-quoted twin through the strip while still parsing as the protected
    key — and the drain signs these bytes without re-running
    ``capture.enforce``, so the forgery reached committed frontmatter.
    """
    from . import frontmatter as fm

    meta, _ = fm.parse_text(f"---\n{line}\n---\n")
    return any(k in meta for k in keys)


def _carries_host_only(text: str, keys: tuple[str, ...] = HOST_ONLY_KEYS) -> bool:
    """True when a WHOLE-document parse still yields a host-only key — the
    only check that actually decides whether the file is safe to promote."""
    from . import frontmatter as fm

    meta, _ = fm.parse_text(text)
    return any(k in meta for k in keys)


def claim_from(source: Mapping[str, Any] | None) -> dict[str, str]:
    """Read the four provenance FIELDS out of a manifest line / sidecar / kwargs
    mapping (short names) and return them sanitized, keyed by short name."""
    if not isinstance(source, Mapping):
        return {}
    out: dict[str, str] = {}
    for field in FIELDS:
        val = sanitize_value(source.get(field))
        if val is not None:
            out[field] = val
    return out


def frontmatter_keys(fields: Mapping[str, Any] | None, *,
                     verified: bool = False) -> dict[str, Any]:
    """Turn short-named provenance fields into the flat dotted frontmatter keys.

    ``verified=True`` is the HOST's assertion that these values were parsed from
    the archived original itself — never pass a VM-supplied flag through here.
    """
    claim = claim_from(fields)
    out: dict[str, Any] = {_KEY_BY_FIELD[f]: v for f, v in claim.items()}
    if out and verified:
        out[VERIFIED_KEY] = True
    return out


# -- where a swept attachment's claimed provenance now travels ----------------
# It used to be a plain JSON store at ``<vault>/.brain/ingest-provenance.json``,
# keyed by content sha, written at accept time and read by the ingest drain an
# hour later. Both the payload (``vault/inbox/``) and that store sat on the
# Cowork mount, so ONE write substituted both: swap the bytes, restate the
# hash, and the drain signed the attacker's file carrying the owner's claim.
# INT-04 moved the record into the Ed25519-signed, off-mount ACCEPTANCE ANCHOR
# (``cos.stage_attachment_anchor``), which the drain verifies against the exact
# buffer it is about to sign. There is deliberately no second, unsigned copy —
# a forgeable channel that is merely "also checked" is still a forgeable
# channel.


# -- classification of email-derived material (HARDENED:codex-2) --------------
def email_classification(
    vault: Any = None, *,
    proposed: str | None = None,
    verified_texts: Iterable[str] = (),
    category: str | None = None,
    overlay_dir: Any = None,
) -> tuple[str, list[str]]:
    """The tier an EMAIL-DERIVED source lands at, and why.

    Email-derived material defaults to **MNPI**: a swept attachment or an .eml
    body is unlabelled third-party content, and the old ``Internal`` ingest
    default was a DOWNGRADE path that surfaced it to the VM egress cap. The
    final tier is the MAX across:

    - the MNPI default,
    - any explicitly ``proposed`` classification,
    - the ingest category's ``min_tier`` FLOOR (docs/cos-ingest-taxonomy.md).

    **UNVERIFIED PROVENANCE NEVER LOWERS THE TIER** (R1, 2026-07-30 review,
    CRITICAL). Lowering is driven exclusively by ``verified_texts`` — text the
    HOST itself parsed out of the archived original (``provenance.verified``,
    earned only by the ``.eml`` handler). A subject/sender/filename a VM
    authored is a CLAIM: passing it here would let a producer plant a mapped
    keyword in its own ``provenance.subject``, bind Restricted material to a
    graduated ``Internal`` evidence key and reach the auto lane. The parameter
    is named for the rule so every caller — this lane, the attachment lane and
    any future one — inherits it instead of re-deciding at the call site.
    With no verified text there is simply no lowering input, and ``proposed``
    plus the category floor can still only RAISE the MNPI default.
    """
    from .classification import RANK, TIERS

    reasons: list[str] = []
    from . import overlay as ov

    mapped: str | None = None
    haystack = " ".join(t for t in verified_texts
                        if isinstance(t, str) and t.strip())
    if haystack:
        mapped, term = ov.match_keyword_tier(haystack, vault=vault, explicit=overlay_dir)
        if mapped:
            reasons.append(f"overlay keyword {term!r} -> {mapped}")
    tier = mapped or "MNPI"
    if mapped is None:
        reasons.append(
            "email-derived default (no overlay keyword mapping matched)"
            if haystack else
            "email-derived default (no HOST-VERIFIED text; a VM-claimed "
            "subject/sender/filename never lowers the tier)")

    if isinstance(proposed, str) and proposed in RANK and RANK[proposed] > RANK[tier]:
        tier = proposed
        reasons.append(f"proposed classification {proposed} is higher")

    if category:
        floor = ov.category_min_tier(category, vault=vault, explicit=overlay_dir)
        if floor and RANK[floor] > RANK[tier]:
            tier = floor
            reasons.append(f"category {category!r} min_tier floor {floor}")

    assert tier in TIERS
    return tier, reasons

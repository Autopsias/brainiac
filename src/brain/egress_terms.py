"""Outbound term guard (SEC-07) — refuse a protected term leaving in a tool call.

**The gap this closes.** The classification gate protects the READ side: it
decides what a caller may see. It says nothing about what the model then does
with it. ``AGENTS.md``'s retrieval discipline rule 4 names the consequence —
"the model's own web-search tool is an *ungated outbound channel*, and the
query string itself is the leak" — and until this module that rule was PROSE,
enforced by the model's compliance. A hostile document can carry an instruction
that puts a codename into a search query; nobody reviews a query string, and by
the time the results come back the term has already reached a third party.

**Why the terms are not in this repo, and never will be.** A shipped list of
one owner's counterparties is useless to every other owner and a disclosure in
itself. The terms come from the vault's own **overlay** — ``overlay/keywords/``,
the per-owner decoder ring whose rows are ``| Term | Expansion | Classification |``
— read through :func:`brain.overlay.match_keyword_tier`. The engine ships the
RULE; the vault supplies the DATA. That is the same split the overlay exists for
(``overlay/README.md``), and those rows already drive ingest classification
(``provenance.py``) and the COS grounding lane (``cos_ground.py``), so they stay
current as a side effect of work the owner already does rather than as a second
list to curate.

**An UNREADABLE decoder ring refuses, on every tool.** A ring file this code
cannot read or parse is a check that could not RUN, which is the opposite of a
check that found nothing — and until 2026-09-03 the two were indistinguishable,
so one ``chmod 000`` allowed a call the readable ring blocked. ``refused_for``
is ``"ring-unreadable"``, it does not wait for ``--strict``, and the message
names the file.

**An empty decoder ring is REPORTED, never a quiet pass.** A vault that maps no
terms cannot have a term caught, and a check whose all-clear means "nothing was
looked at" is worse than no check. :func:`check` always returns
``mapped_terms``, every caller surfaces it, and ``--strict`` turns an empty ring
into a refusal for a deployment that wants fail-closed.

**What this does NOT do.** It matches DECLARED terms, on word boundaries, case
insensitively. It does not catch a paraphrase, a codename nobody wrote down, or
a fact the model restates in its own words. It raises the bar for the terms an
owner has declared. It does not close the channel, and no caller should describe
it as though it did.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import classification as cls

#: Terms at or above this tier refuse. `Internal` is deliberately NOT the
#: default: an owner's whole vault is Internal by default, so defaulting there
#: would refuse almost every query and the guard would be switched off within a
#: day. `Confidential` is the first tier that means "this one is actually
#: sensitive".
DEFAULT_MIN_TIER = "Confidential"

#: Exit code for a refused text. Distinct from 3 (VM refusal), 4 and
#: 5 (SEC-06 record failure) so a hook can tell them apart.
EXIT_REFUSED = 6

MIN_TIER_ENV_VAR = "BRAIN_EGRESS_TERM_MIN_TIER"


def min_tier() -> str:
    """The configured refusal threshold, fail-CLOSED and CLAMPED.

    A typo must never silently widen what may leave: the only reason to set
    this var is to change the threshold deliberately, so an unreadable value
    falls to the most protective tier rather than the permissive default.
    Same rule, and same reasoning, as ``mcp_verbs._egress_ceiling_tier``.

    AND IT MAY ONLY EVER LOWER THE BAR (s06 round 3, C-1's class). This is an
    environment variable, and Claude Code lets COMMITTED PROJECT SETTINGS define
    a session's environment — so a *valid* value was as good an escape hatch as
    an invalid one: ``BRAIN_EGRESS_TERM_MIN_TIER=MNPI`` from a file in the repo
    let every Confidential and Restricted term leave, and nothing said so. The
    legitimate use — an owner who wants a STRICTER guard than the default —
    still works, because tightening is not an escape. Raising the bar above
    :data:`DEFAULT_MIN_TIER` is refused and the default applies. The shipped
    hook also scrubs the variable before it calls the engine; this clamp is what
    protects every OTHER caller of ``check-egress``.
    """
    raw = os.environ.get(MIN_TIER_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_MIN_TIER
    if raw not in cls.RANK:
        return cls.TIERS[0]
    return raw if cls.RANK[raw] <= cls.RANK[DEFAULT_MIN_TIER] else DEFAULT_MIN_TIER


def _bar(threshold: str | None) -> str:
    """The tier at or above which a term refuses. Fail-closed on a typo."""
    if threshold is None:
        return min_tier()
    if threshold in cls.RANK:
        return threshold
    return cls.TIERS[0]  # fail-closed on a typo, as `min_tier` does


def _primary_ring(
    vault: Any, overlay_dir: Any, ring_errors: list[str],
) -> tuple[dict[str, str], dict[str, str], bool]:
    """This vault's merged ring, its hand-written half, and whether it resolved.

    NO VAULT IS NOT A FAILED CHECK (s06 round 1, H-4). Resolving the overlay
    re-resolves the vault, and outside one that RAISES — so the PreToolUse
    guard, whose CWD is wherever the session happens to sit, got exit 3 on
    every call and refused every tool call on the host, including the edit
    that removes the hook. No vault means no decoder ring, which means no
    authority to refuse: the same state as no engine on PATH, which this
    design already accepts as an allow. It is REPORTED, never a quiet pass,
    and `--strict` does not convert it into a refusal — a `--strict` refusal
    is about an EMPTY ring on a real vault, which is a vault the owner can
    fix by filling `overlay/keywords/`. "There is no vault here" is not.

    The MERGED ring, never `resolve_keyword_tiers` alone — that one is
    ingest's input and stays exactly what the owner typed. See
    `overlay_generated`'s docstring for why the two must not be joined.

    A DEGRADED RING IS NOT AN EMPTY ONE (s06 round 2, H-1). `chmod 000` on a
    single ring file used to be indistinguishable from a vault that maps no
    terms — and "maps no terms" only refuses under `--strict`, which the hook
    passes on WebSearch/WebFetch alone. So one unreadable file switched the
    guard off for Bash, Write, Edit and every MCP call, silently. A file this
    code could not READ is a check that could not RUN, and a check that could
    not run has no authority to allow: it refuses on every tool, `--strict`
    or not, and names the file. Unreadable files land in ``ring_errors``.
    """
    from . import config as _config
    from . import overlay as ov

    try:
        mapped = ov.resolve_egress_keyword_tiers(vault, overlay_dir, ring_errors)
        hand = ov.resolve_keyword_tiers(vault, overlay_dir)
        return mapped, hand, True
    except _config.VaultNotFoundError:
        return {}, {}, False


def _merge_extra_rings(
    mapped: dict[str, str],
    extra_vaults: list[str] | None,
    *,
    ring_errors: list[str],
    from_vault: dict[str, str],
    vault_resolved: bool,
) -> bool:
    """Fold every OTHER vault this session entered into ``mapped``, in place.

    Same rules as the primary ring: an unreadable file lands in
    ``ring_errors`` and REFUSES, and a vault that resolves to nothing
    contributes nothing rather than raising. A term claimed by two vaults
    at different tiers keeps the HIGHER one — the ring answers "may this
    leave", and the protective answer is the safe one to be wrong about.

    ``from_vault`` records which EXTRA vault contributed a term, so a refusal
    can name the ring the owner would go and edit (s06 round 7, Claude). An
    absent key means the primary ring.

    A VAULT THAT HAS VANISHED REFUSES (s06 round 7, Claude). These names
    come from the session's own accumulated set, so each one WAS resolvable
    when the guard wrote it down. One that no longer resolves is a ring
    that could not be read, not a vault that was never there — the same
    epistemic state as the `chmod 000` case, and it gets the same answer.
    Without this the union weakened silently over exactly the long-lived
    session the accumulation exists for.

    Returns the updated ``vault_resolved``.
    """
    from . import config as _config
    from . import overlay as ov

    gone = "(a vault this session entered no longer resolves)"
    for other in extra_vaults or []:
        if not Path(other).is_dir():
            ring_errors.append(f"{other} {gone}")
            continue
        try:
            ring = ov.resolve_egress_keyword_tiers(other, None, ring_errors)
        except _config.VaultNotFoundError:
            ring_errors.append(f"{other} {gone}")
            continue
        for term_, tier_ in ring.items():
            prior = mapped.get(term_)
            if prior is None or cls.RANK[tier_] > cls.RANK[prior]:
                mapped[term_] = tier_
                from_vault[term_] = str(other)
                vault_resolved = True
    return vault_resolved


def _refusal_reason(
    *, refused: bool, degraded: bool, strict: bool, empty_ring: bool,
) -> str | None:
    """Why this string may not leave, or ``None`` when it may."""
    if refused:
        return "term"
    if degraded:
        return "ring-unreadable"
    if strict and empty_ring:
        return "no-mapped-terms"
    return None


def check(
    text: str,
    *,
    vault: str | os.PathLike[str] | None = None,
    overlay_dir: str | os.PathLike[str] | None = None,
    threshold: str | None = None,
    strict: bool = False,
    extra_vaults: list[str] | None = None,
) -> dict[str, Any]:
    """Judge one outbound string against this vault's overlay keyword tiers.

    Returns ``allowed`` plus everything a caller needs to explain itself:
    the matched ``term`` and its ``tier``, the ``threshold`` applied, and
    ``mapped_terms`` — the size of the decoder ring that was actually
    consulted, so a zero is visible rather than indistinguishable from a pass.

    ``extra_vaults`` MERGES more rings into the one consulted. One caller needs
    it and the reason is latency, not taste (s06 round 6): the outbound guard
    must judge a string against every vault the SESSION has entered, because a
    `cd` does not empty the conversation. Doing that as one process call per
    vault measured 0.96s for one vault and 2.19s for two, against a 5-second
    hook budget that FAILS OPEN when it is exceeded — the four registered
    vaults on the reference host would have disarmed the guard by being slow.
    Merging the rings costs one call for any N; re-measured at 0.79s for two.

    The four helpers above carry the reasoning behind each step; this function
    is the order they run in.
    """
    from . import overlay as ov

    bar = _bar(threshold)
    ring_errors: list[str] = []
    mapped, hand, vault_resolved = _primary_ring(vault, overlay_dir, ring_errors)
    primary_mapped = len(mapped)
    from_vault: dict[str, str] = {}
    vault_resolved = _merge_extra_rings(
        mapped, extra_vaults, ring_errors=ring_errors,
        from_vault=from_vault, vault_resolved=vault_resolved)

    tier, term = (None, None)
    if mapped and text:
        tier, term = ov.match_keyword_tier(text, vault, overlay_dir, tiers=mapped)

    refused = bool(tier) and cls.RANK[tier] >= cls.RANK[bar]
    why = _refusal_reason(
        refused=refused, degraded=bool(ring_errors), strict=strict,
        empty_ring=vault_resolved and not mapped)
    return {
        "allowed": why is None,
        "refused_for": why,
        "term": term if refused else None,
        "tier": tier if refused else None,
        "threshold": bar,
        "mapped_terms": len(mapped),
        "hand_terms": len(hand),
        # PRIMARY-RING count, deliberately: a merged term belongs to another
        # vault's ring, and reporting it as this vault's generated term sent the
        # owner to the wrong file (s06 round 7, Claude).
        "generated_terms": primary_mapped - len(hand),
        "merged_terms": len(mapped) - primary_mapped,
        "term_vault": from_vault.get(term) if refused else None,
        "strict": strict,
        "vault_resolved": vault_resolved,
        "ring_errors": ring_errors,
    }


def explain(result: Any) -> str:
    """One line a hook can print, saying WHY — including on the allow path.

    The empty-decoder-ring case gets its own sentence on purpose: a caller
    reading only "allowed" would report a vault that protects nothing as a
    vault that found nothing.

    Takes ``Any``, not ``dict``, on purpose: this used to raise
    ``AttributeError: 'str' object has no attribute 'get'`` when handed an
    already-rendered string, which turns an explanation helper into a crash on
    the one path that exists to explain things. A non-mapping is echoed back.
    """
    if not isinstance(result, dict):
        return str(result)
    if result.get("refused_for") == "ring-unreadable":
        files = ", ".join(str(e) for e in (result.get("ring_errors") or []))
        return ("REFUSED: this vault's decoder ring could NOT BE READ, so nothing "
                f"could be checked against it — {files}. An unreadable ring is not "
                "an empty one: fix the file (permissions, or its frontmatter) and "
                "re-run. This refuses on every tool, --strict or not.")
    if result.get("refused_for") == "term":
        whose = result.get("term_vault")
        ring = f"{whose}'s overlay keywords" if whose else "this vault's overlay keywords"
        return (f"REFUSED: '{result['term']}' is classified {result['tier']} in "
                f"{ring} (threshold {result['threshold']}). "
                f"The query string itself is an egress event — rephrase without the term, "
                f"or stay in the vault.")
    if result.get("refused_for") == "no-mapped-terms":
        return ("REFUSED (--strict): this vault's overlay maps NO keyword tiers, so "
                "nothing could be checked. Fill overlay/keywords/ or drop --strict.")
    if not result.get("vault_resolved", True):
        return ("allowed — NO VAULT resolvable from here, so there is no decoder ring "
                "and nothing to check against. Not a failed check and not evidence the "
                "text is safe: pin the vault with $BRAIN_VAULT to get a real answer.")
    if not result.get("mapped_terms"):
        return ("allowed — but this vault's overlay maps NO keyword tiers, so no term "
                "could have been caught. This is not evidence the text is safe.")
    gen = result.get("generated_terms") or 0
    merged = result.get("merged_terms") or 0
    derived = f" ({gen} derived from this vault's own notes)" if gen else ""
    other = f", {merged} from other vaults this session entered" if merged else ""
    return (f"allowed — {result['mapped_terms']} mapped term(s) checked at "
            f"threshold {result['threshold']}{derived}{other}")


def add_check_egress_arguments(sp: Any) -> None:
    """Attach `check-egress` arguments to a subparser."""
    sp.add_argument("text", nargs="?", help="the outbound string; omit to read stdin")
    sp.add_argument("--json", action="store_true")
    sp.add_argument(
        "--min-tier",
        help="refuse at or above this tier (default: $BRAIN_EGRESS_TERM_MIN_TIER, "
        "else Confidential). An unrecognised value fails CLOSED.",
    )
    sp.add_argument(
        "--strict",
        action="store_true",
        help="also refuse when the overlay maps NO terms — for a deployment that "
        "will not accept an all-clear produced by an empty decoder ring",
    )
    sp.add_argument(
        "--extra-vault",
        action="append",
        default=[],
        metavar="PATH",
        help="merge another vault's decoder ring into this check (repeatable). "
        "The outbound guard passes every vault the SESSION has entered, "
        "because leaving a vault does not empty the conversation. One "
        "call for any number of rings: the hook budget is 5 seconds and "
        "exceeding it FAILS OPEN, so a call per vault does not scale.",
    )


def is_direct_check_egress(argv: list[str]) -> bool:
    """True when argv invokes `check-egress` directly without help/version flags."""
    if any(tok in ("-h", "--help", "--version") for tok in argv):
        return False
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--vault", "--role"):
            i += 2
            continue
        if tok.startswith(("--vault=", "--role=")):
            i += 1
            continue
        if tok.startswith("-"):
            return False
        return tok == "check-egress"
    return False


def _read_opt(argv: list[str], i: int, name: str) -> tuple[bool, str | None, int]:
    tok = argv[i]
    if tok == name and i + 1 < len(argv):
        return True, argv[i + 1], i + 2
    prefix = name + "="
    if tok.startswith(prefix):
        return True, tok[len(prefix):], i + 1
    return False, None, i


def _parse_direct_check_egress_argparse(argv: list[str]) -> Any:
    import argparse

    parser = argparse.ArgumentParser(prog="brain", add_help=False)
    parser.add_argument("--vault", default=None)
    parser.add_argument("--role", default=None, choices=("host", "vm"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("check-egress", add_help=False)
    add_check_egress_arguments(sp)
    return parser.parse_args(argv)


def _parse_direct_header(argv: list[str], idx: int, opts: dict[str, Any]) -> bool:
    i = 0
    while i < idx:
        for flag, key in (("--vault", "vault"), ("--role", "role")):
            hit, val, i = _read_opt(argv, i, flag)
            if hit:
                opts[key] = val
                break
        else:
            return False
    return opts["role"] in (None, "host", "vm")


def parse_direct_check_egress(argv: list[str]) -> Any:
    """Parse a direct `check-egress` invocation without importing full `cli_cmds`."""
    from types import SimpleNamespace

    opts: dict[str, Any] = {
        "vault": None, "role": None, "cmd": "check-egress",
        "text": None, "json": False, "min_tier": None, "strict": False, "extra_vault": [],
    }
    try:
        idx = argv.index("check-egress")
    except ValueError:
        return _parse_direct_check_egress_argparse(argv)
    if not _parse_direct_header(argv, idx, opts):
        return _parse_direct_check_egress_argparse(argv)
    i = idx + 1
    end_opts = False
    while i < len(argv):
        tok = argv[i]
        if not end_opts and tok == "--":
            end_opts = True
            i += 1
        elif not end_opts and tok in ("--json", "--strict"):
            opts[tok[2:]] = True
            i += 1
        elif not end_opts and tok.startswith(("--min-tier", "--extra-vault")):
            hit, val, i = _read_opt(argv, i, "--min-tier" if tok.startswith("--min-tier") else "--extra-vault")
            if not hit:
                return _parse_direct_check_egress_argparse(argv)
            if tok.startswith("--min-tier"):
                opts["min_tier"] = val
            else:
                opts["extra_vault"].append(val)
        elif (not end_opts and tok.startswith("-")) or opts["text"] is not None:
            return _parse_direct_check_egress_argparse(argv)
        else:
            opts["text"] = tok
            i += 1
    return SimpleNamespace(**opts)


def run_cli_args(args: Any, cfg: Any, emit: Any) -> int:
    """Execute `brain check-egress` from parsed CLI args."""
    text = args.text
    if text is None:
        import sys as _sys

        text = "" if _sys.stdin.isatty() else _sys.stdin.read()

    try:
        vault = cfg.vault_root(args.vault)
    except Exception:  # fail-open: no resolvable vault; the egress check runs without one
        vault = None

    result = check(
        text,
        vault=vault,
        threshold=args.min_tier,
        strict=args.strict,
        extra_vaults=[
            v for v in getattr(args, "extra_vault", []) or [] if v and v != str(vault)
        ],
    )
    result["explanation"] = explain(result)
    if args.json:
        emit(result, True)
    else:
        emit(None, False, result["explanation"])
    return 0 if result["allowed"] else EXIT_REFUSED

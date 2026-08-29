"""Which original a request means, how sensitive it really is, and the record
that AUTHORISES disclosing it (closed-stacks S05, DESK-05).

This module is the DECISION half of document disclosure, and only that half. It
opens nothing, copies nothing, and puts nothing on any mount. It answers two
questions and writes one record:

1. **Which file?** A reference resolves to exactly one regular file inside the
   immutable ``vault/raw/originals/`` zone, or it is refused.
2. **How sensitive is it really?** The MAXIMUM tier over EVERY note that cites
   the file in its ``origin:`` frontmatter — never "the note that owns it",
   which is not a function.

   **Measured on the live corpus 2026-08-28, and the measurement does not say
   what the requirement assumed.** Over 1,676 files under ``raw/originals/``
   and 3,046 scanned notes: 1,168 originals are cited, by 1,168 notes — a
   strict 1:1. **ZERO originals have more than one owning note today**, and
   zero have an unlabelled one; 507 (30%) have NO owner at all and 1 is the
   ``.DS_Store``. So the ``max()`` is not an observed frequency, it is
   PROSPECTIVE INSURANCE: nothing in the substrate constrains an original to
   one citing note, the rule costs one ``max()``, and the day a second note
   cites an already-cited file the alternative would silently pick whichever
   the scan reached first. The ORPHAN case, by contrast, is the common one and
   is what most refusals will be.

Then, and only then, it writes an AUTHORISATION record through the SEC-06
wrapper and confirms the wrapper said the line reached disk. If that record
cannot be written, the caller gets an exception instead of a path.

**The record says AUTHORISED, not DELIVERED.** ``cmd`` is
:data:`AUTHORIZE_CMD` ( ``originals:authorize`` ) precisely so a later reader
cannot mistake it for evidence that a document changed hands. Nothing in this
module delivers anything; whoever acts on the decision does that, and a
delivery that never happens still leaves this record behind.

**WHAT THE RECORD PROVES, AND WHAT IT CANNOT.** It proves that an
authorisation happened, at a given time, at a given tier, on this host. It
does **not name the document**, and it cannot: ``read_log`` deliberately
stores no note ids, titles or paths (``read_log`` module docstring — "a log
that has to be protected like the vault is a log nobody keeps"), and its
``record()`` takes no identity argument. The owner's ruling directs this
module through that wrapper, so this is a stated limit, not an oversight: an
incident responder gets "N authorisations last week, these at Restricted",
never "these three files". Naming the document needs a store allowed to hold
paths, which is a new decision and not a quiet extension of the read-log.
For the same reason the tally this module submits carries ``surfaced: 0`` —
no NOTE crossed the gate on this call, and counting one would inflate the
``notes_surfaced`` total ``read_log.status()`` reports to the folds. And a row
in the log is not proof a caller was given anything: see :func:`authorize` on
the fsync residual — the log may over-report a refused authorisation, never
under-report a granted one.

**No per-caller ceiling exists, and none is invented here** (s01 JOB 3, quoted
in full by :data:`CEILING_DETAIL`): the broker cannot tell two callers apart,
so :func:`authorize` refuses on the resolution's own grounds and records the
resolved tier rather than comparing it against a caller that does not exist.
Under the owner's 2026-08-27 ruling disclosure happens by the shipped fallback
``brain project --dest <path> --max-tier <tier>``, run by the host operator —
which is why the resolved tier matters: it is what tells that operator which
``--max-tier`` is correct. ``brain project`` copies NOTES; it does not copy
files out of ``raw/originals/`` at all (:func:`brain.projection.project_workspace`
iterates ``notes.scan_vault``, and that scan skips the originals zone). This
module states a tier; it does not claim any command delivers the bytes.

**THIS COMMAND'S OUTPUT IS NOT EGRESS-GATED, AND THAT IS THE DECISION.** The
CLI verb prints the path, tier and owning note ids without routing them through
``egress.apply_gate`` or consulting ``$BRAIN_DEFAULT_MAX_TIER``, because the
identity of ONE document IS the answer here — a gate that withheld it would
leave the verb with nothing to return — and because there is no per-caller
ceiling to refuse against. What DOES bound the verb is the role gate: it is
absent from ``cli.VM_ALLOWED``, so ``role=vm`` refuses it before a vault is
opened. An unstated bypass in a security-adjacent surface is how exemptions
become holes, so if that reasoning stops holding, change the code rather than
quietly widening it.

**Full argument, census and residuals:**
``docs/operations/closed-stacks-s05-disclosure-decision-evidence.md``.

**What this is NOT: containment.** Same boundary :mod:`brain.classification`
draws for the egress filter — any file-capable process on the host can read
``vault/raw/originals/`` directly and never come near this module.
:func:`resolve` is the tier COMPUTATION and deliberately records nothing; it
tells a caller what it could already have worked out from files it can already
read. :func:`authorize` is the one that claims a document may be disclosed,
and it is the only one that returns after a confirmed record.
"""
from __future__ import annotations

import os
import stat
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import classification as cls
from . import read_log

#: The zone, relative to the vault root. Anything resolving outside it is
#: refused — including a path that only *looks* like it is inside one.
ORIGINALS_REL = ("raw", "originals")

#: The EXPLICIT non-document rule, and it is an allowlist because deny-by-default
#: is the posture everywhere else in this engine.
#:
#: Measured on the live corpus 2026-08-28 (``find vault/raw/originals -type f``,
#: 1,676 files, 821 MB): md 1038, docx 178, html 144, pdf 139, pptx 59, png 55,
#: xlsx 38, txt 12, csv 4, jpeg 3, eml 3, zip 2 — twelve document formats
#: summing to 1,675 — plus ONE file that is not a document in any format:
#: ``vault/raw/originals/.DS_Store``, macOS Finder metadata. An earlier count
#: read that as a thirteenth "format".
#:
#: Excluding it by SUFFIX rather than by "the census had one more file than the
#: formats explain" is the whole point: a count that happens to match is not a
#: rule, and the next stray ``Thumbs.db`` or ``.localized`` would pass one.
#: A thirteenth real format is a one-line change here and refuses until it is
#: made — a refusal names this set, so the fix is obvious. Note ``.zip`` is in
#: the set because the corpus has two: an archive whose contents this module
#: cannot classify beyond its owning notes' tier, exactly like every other
#: format here.
DOCUMENT_SUFFIXES = frozenset({
    ".md", ".docx", ".html", ".pdf", ".pptx", ".png",
    ".xlsx", ".txt", ".csv", ".jpeg", ".eml", ".zip",
})

#: The ``cmd`` this module writes into the SEC-06 log. Names the EVENT, not an
#: outcome: an authorisation, never a delivery.
AUTHORIZE_CMD = "originals:authorize"

# Refusal reasons. Stable strings — the CLI prints them and tests assert them.
REFUSED_MISSING = "missing"
REFUSED_SYMLINK = "symlink"
REFUSED_OUTSIDE_ORIGINALS = "outside_originals"
REFUSED_NOT_REGULAR = "not_regular"
REFUSED_NOT_A_DOCUMENT = "not_a_document"
REFUSED_NO_OWNER = "no_owner"
REFUSED_UNLABELLED_OWNER = "unlabelled_owner"
REFUSED_AMBIGUOUS_SPELLING = "ambiguous_owner_spelling"
REFUSED_NOT_RECORDED = "not_recorded"

#: There is no per-caller ceiling to test a resolved tier against (see the
#: module docstring). Recorded in the result, in these words, so close-03 can
#: state which case held instead of re-deriving it.
CEILING_CASE = "no_per_caller_ceiling"
CEILING_DETAIL = (
    "the Cowork leg has no per-caller ceiling (s01 JOB 3: the broker cannot "
    "distinguish two sessions); the only tier bound is the server-side role "
    "gate plus $BRAIN_MAX_EGRESS_TIER, identical for every caller. Disclosure is "
    "the shipped fallback `brain project --dest <path> --max-tier <tier>`, and "
    "this resolved tier is what tells the operator which --max-tier is correct."
)


class DisclosureRefused(RuntimeError):
    """This original may not be disclosed, and the reason is machine-readable."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class AuthorizationNotRecorded(DisclosureRefused):
    """The decision could not be written down, so it does not count as made.

    Raised INSTEAD of returning a resolution — on the wrapper's ``False``-shaped
    outcomes and on an exception from the wrapper alike. DESK-05's requirement is
    "if it cannot write that down, you do not get the document", so the caller
    gets this and no path.
    """

    def __init__(self, outcome: str, detail: str) -> None:
        super().__init__(REFUSED_NOT_RECORDED, detail)
        self.outcome = outcome


@dataclass(frozen=True)
class Owner:
    """One note that cites the original in its ``origin:`` frontmatter."""

    id: str
    path: str
    classification: str  # RAW frontmatter value; "" when the key is absent
    #: Did this note's ``origin:`` match the resolved path EXACTLY, or only
    #: after case-folding? Not in ``to_dict``, and deliberately: :func:`resolve`
    #: refuses on any inexact match, so a Resolution that reaches a caller never
    #: contains one. The field exists to carry that fact out of the ONE vault
    #: scan `owners_of` can afford, not to be reported.
    exact_spelling: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "path": self.path, "classification": self.classification}


@dataclass(frozen=True)
class Resolution:
    """One original, its owners, and the tier the maximum over them produced."""

    path: Path
    rel: str
    tier: str
    owners: tuple[Owner, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "rel": self.rel,
            "tier": self.tier,
            "owners": [o.to_dict() for o in self.owners],
        }


@dataclass(frozen=True)
class Authorization:
    """A resolution whose authorisation record is CONFIRMED on disk."""

    resolution: Resolution
    outcome: str
    ceiling_case: str = CEILING_CASE

    def to_dict(self) -> dict[str, Any]:
        return {
            "authorized": True,
            "event": AUTHORIZE_CMD,
            "record_outcome": self.outcome,
            "ceiling_case": self.ceiling_case,
            "ceiling_detail": CEILING_DETAIL,
            **self.resolution.to_dict(),
        }


def _norm(value: object) -> str:
    """One comparison key for two spellings of the same vault-relative path.

    NFC because macOS ``readdir`` hands back NFD while the ``origin:`` value was
    written from whatever the ingesting process held — a decomposed "ã" on one
    side and a composed one on the other compare unequal, and the file would
    read as an orphan and be refused.
    """
    text = str(value or "").strip().strip("\"'").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return unicodedata.normalize("NFC", text.strip("/"))


def owners_of(vault: str | os.PathLike[str], rel: str) -> list[Owner]:
    """Every note whose ``origin:`` frontmatter names this original.

    OWNERSHIP IS THE ``origin:`` KEY, and nothing else. That key is written by
    the ingestion pipeline (``brain.ingest.pipeline._meta``) as the archived
    original's vault-relative path, so it is the structural, machine-written
    citation. A note that merely MENTIONS the document in its body is not
    counted — a known ceiling, stated rather than hidden: a Restricted note that
    quotes an Internal source without carrying its ``origin:`` will not raise the
    resolved tier here.

    ponytail: one O(vault) scan per call. This is an operator-paced decision, not
    a per-request hot path; if it ever becomes one, build the origin index once
    and pass it in rather than caching it behind this function's back.
    """
    from .notes import scan_vault  # local: notes imports are not free

    root = Path(vault)
    key = _norm(rel)
    fold = key.casefold()
    found: list[Owner] = []
    for note in scan_vault(root):
        # Matched case-INSENSITIVELY on purpose, and every inexact match is
        # flagged rather than silently kept or silently dropped. Dropping is
        # fail-OPEN: on a case-insensitive filesystem (macOS, the host this
        # runs on) `Report.pdf` and `report.pdf` are ONE file, so an owner that
        # spelled its `origin:` the other way would vanish from the max() and
        # the tier could come out LOWER than the document's real sensitivity.
        # Keeping it is fail-open the other way on a case-SENSITIVE filesystem,
        # where the two really are different files. `resolve` refuses on the
        # difference instead of picking a side.
        origin = _norm(note.meta.get("origin"))
        if origin != key and origin.casefold() != fold:
            continue
        try:
            note_rel = note.path.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover - scan_vault yields under root
            note_rel = note.path.as_posix()
        found.append(
            Owner(id=note.id, path=note_rel, classification=note.classification,
                  exact_spelling=(origin == key))
        )
    return found


def resolve(vault: str | os.PathLike[str], ref: str | os.PathLike[str]) -> Resolution:
    """Resolve a reference to ONE original and to its real sensitivity.

    Refuses (``DisclosureRefused``) rather than guessing: a reference that leaves
    the originals zone, a symlink as its FINAL component, a non-regular file, a
    non-document suffix, a file NO note owns, a file whose owner carries no
    recognised classification, and a file whose owners spelled its path in more
    than one case. The orphan and the unlabelled owner are separate reasons on
    purpose — an orphan whose owning note was deleted or renamed after ingestion
    is not the same finding as a note somebody forgot to label — and both deny.

    **The symlink rule is about the FINAL component, and the sentence has to say
    so** (measured 2026-08-28): ``candidate.is_symlink()`` sees only the last
    element, and ``resolve(strict=True)`` then follows every PARENT link. So
    ``raw/originals/linkdir/doc.pdf``, where ``linkdir`` is a directory symlink,
    is not refused — it resolves to the link's target. That is safe, and it is
    safe for a reason worth writing down rather than assuming: ``rel``, the
    owner lookup and therefore the tier are all computed from the RESOLVED real
    path, so what gets authorised is the true file at the true file's own tier,
    and a parent link whose target leaves the zone is refused by the containment
    check (both directions measured). What the final-component rule buys is that
    the resolved tier can never describe a different file from the one a caller
    would open by that exact name.

    **Refusal details name the caller's OWN reference, never the resolved real
    path.** A refusal happens before any record exists, so it must not hand back
    a canonicalised path the caller did not already have — following a symlink
    and reporting where it landed is exactly that. TWO refusals deliberately
    name the owning note ids instead — ``unlabelled_owner`` and
    ``ambiguous_owner_spelling`` — because in both the note that must be fixed
    IS the finding, and a note id is not a path. No other refusal names
    anything the caller did not supply.

    Records NOTHING. See the module docstring: this is the computation, and
    :func:`authorize` is the claim.
    """
    given = str(ref)
    root = Path(vault)
    vault_real = root.resolve()
    originals_real = vault_real.joinpath(*ORIGINALS_REL)

    candidate = Path(ref)
    if not candidate.is_absolute():
        candidate = root / candidate
    # lstat, not stat: a symlink is refused for BEING one, before anything
    # follows it. The immutable zone holds files, not redirections, and a
    # redirection is how a resolved tier ends up describing a different file
    # from the one that would be disclosed.
    if candidate.is_symlink():
        raise DisclosureRefused(
            REFUSED_SYMLINK,
            f"{given} is a symlink; originals are archived files, not links",
        )
    try:
        real = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise DisclosureRefused(REFUSED_MISSING, f"no such file: {given}") from None
    if not real.is_relative_to(originals_real):
        raise DisclosureRefused(
            REFUSED_OUTSIDE_ORIGINALS,
            f"{given} does not resolve inside the vault's raw/originals zone",
        )
    if not stat.S_ISREG(os.lstat(real).st_mode):
        raise DisclosureRefused(REFUSED_NOT_REGULAR, f"{given} is not a regular file")
    if real.suffix.lower() not in DOCUMENT_SUFFIXES:
        raise DisclosureRefused(
            REFUSED_NOT_A_DOCUMENT,
            f"{given} is not one of the document formats this zone holds "
            f"({', '.join(sorted(DOCUMENT_SUFFIXES))})",
        )

    rel = real.relative_to(vault_real).as_posix()
    owners = owners_of(root, rel)
    if not owners:
        raise DisclosureRefused(
            REFUSED_NO_OWNER,
            f"no note cites {given} in its origin: frontmatter — an orphan's "
            "sensitivity is unknown, so it is denied",
        )
    inexact = [o for o in owners if not o.exact_spelling]
    if inexact:
        raise DisclosureRefused(
            REFUSED_AMBIGUOUS_SPELLING,
            f"owning note(s) {', '.join(o.id for o in inexact)} cite this file "
            "with different letter case — on a case-insensitive filesystem that "
            "is one file spelled two ways, on a case-sensitive one it is two "
            "files; refusing rather than guessing which",
        )
    unlabelled = [o for o in owners if cls.is_default_denied(o.classification)]
    if unlabelled:
        raise DisclosureRefused(
            REFUSED_UNLABELLED_OWNER,
            f"owning note(s) {', '.join(o.id for o in unlabelled)} carry no "
            "recognised classification — default-deny",
        )
    tier = max(owners, key=lambda o: cls.rank(o.classification)).classification
    return Resolution(path=real, rel=rel, tier=tier, owners=tuple(owners))


def authorize(
    vault: str | os.PathLike[str],
    ref: str | os.PathLike[str],
    *,
    role: str | None = None,
) -> Authorization:
    """Resolve, write the AUTHORISATION record, confirm it, and only then return.

    The order is the requirement. Nothing the resolution DERIVED — the canonical
    path, the tier, the owning note ids — leaves this function until
    ``read_log`` says the line is on disk. On every other outcome the resolution
    is discarded and :class:`AuthorizationNotRecorded` is raised, carrying only
    the caller's own reference and the outcome word. (Said precisely because the
    first cut of this docstring said "nothing about the resolved original", and
    the exception it described quoted ``resolution.rel``.)

    **The converse does NOT hold, and that is a residual, not a guarantee.** A
    ``failed`` outcome does not prove nothing was written: ``read_log``'s append
    writes the line and THEN calls ``os.fsync``, and an ``OSError`` from either
    is caught and reported as ``False``, so a row whose bytes already landed can
    accompany a refusal. The error runs one way only — the log may OVER-report
    an authorisation that was refused, never under-report one that was granted —
    which is the safe direction for a record whose purpose is that no
    authorisation goes unwritten. Making it exact would need a commit marker
    inside ``read_log``, which is s02's shipped wrapper and a decision of its
    own.

    **Confirmed BY VALUE, not by absence of an exception.**
    ``read_log.record_from_tally`` returns ``False`` WITHOUT raising for three
    unrelated situations, so this calls the wrapper's oracle
    (``read_log.outcome_from_tally``, of which ``record_from_tally`` is the
    boolean projection: it is ``True`` exactly when the outcome is ``written``)
    and keeps which one happened:

    * ``written`` — the only outcome that authorises anything.
    * ``failed`` — no securable log dir, an OSError, a lock timeout. Refuse.
    * ``disabled`` — ``BRAIN_READ_LOG=0``, or ``role=vm``. **Refuse**, and this
      is a DELIBERATE divergence from :func:`brain.mcp_mediation.mediate`, which
      lets ``disabled`` succeed. A read that goes unlogged because the operator
      switched logging off is an operator decision; an authorisation that goes
      unlogged is the thing DESK-05 exists to prevent. On ``role=vm`` it is also
      the right answer for a second reason: a VM-written record is not evidence,
      and the VM leg does not authorise disclosures.
    * ``nothing_to_record`` — unreachable here, because this call passes a tally
      with ``gates: 1`` rather than an accumulated one. Handled anyway, as a
      refusal: if that ever changes, the safe reading is that nothing was
      written.
    """
    from . import config

    given = str(ref)
    resolution = resolve(vault, ref)
    caller_role = role or config.role()
    started = time.perf_counter()
    # A tally built here, not taken from `egress.take_tally()`: this is not a
    # gated read, it is one authorisation for one document, and consuming the
    # ambient tally would both steal counts from a read in flight and report
    # numbers that describe something else. `max_tier` carries the RESOLVED tier
    # because for this event the cap and the content coincide — exactly one
    # document, at exactly that tier, is what was authorised.
    #
    # `surfaced: 0` is the honest count and it is load-bearing: no NOTE crossed
    # the egress gate on this call, and `read_log.status()` SUMS `surfaced` into
    # the `notes_surfaced` figure the maintenance folds read. A `1` here would
    # inflate that total by one per authorisation and, at volume, could tip the
    # `bulk` flag on a call that surfaced nothing. `gates: 1` is what makes the
    # row get written at all (`outcome_from_tally` returns `nothing_to_record`
    # on a falsy `gates`), and `cmd` is what identifies the event.
    tally = {"gates": 1, "surfaced": 0, "withheld": 0}
    try:
        outcome = read_log.outcome_from_tally(
            vault=vault,
            role=caller_role,
            cmd=AUTHORIZE_CMD,
            max_tier=resolution.tier,
            tally=tally,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )
    except Exception as exc:  # the wrapper is fail-closed; so is its caller
        # NAMES NOTHING THE RESOLUTION FOUND — not the canonical path, not the
        # tier, not the owners. This is the near-miss path: the resolution
        # SUCCEEDED and only the record failed, so a message quoting `rel` or
        # `tier` here would hand over the decision the missing record was
        # supposed to gate. The caller's own reference is what it gets back.
        raise AuthorizationNotRecorded(
            "raised",
            f"the authorisation record for {given} could not be written "
            f"({type(exc).__name__}); refusing to authorise it",
        ) from exc
    if outcome != read_log.RECORD_WRITTEN:
        raise AuthorizationNotRecorded(
            outcome,
            f"the authorisation record for {given} was not written "
            f"(outcome: {outcome}); refusing to authorise it",
        )
    return Authorization(resolution=resolution, outcome=outcome)

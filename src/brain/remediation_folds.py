"""FIX-01/FIX-02 — the first two AUTOMATIC repair branches, and their umbrella.

``remediation`` decides that ``invariant:unsigned_notes`` and
``invariant:unguarded_ingests`` are ``auto``; ``remediation_routing`` will
suppress those findings the moment a LIVE branch can prove it covered them.
Until this module existed nothing produced that proof, so every ``auto``
finding bannered exactly as before. This is the half that fills the seam.

**One umbrella fold, not two date-gated branches.** The adversarial-review
scheduler ruling: ``maintenance_escalation._next_trigger`` understands only
``daily``/``graphify``/weekday names, so adding ``sign_repair`` to
``_ALL_BRANCHES`` raises ``KeyError`` on the second scheduling pass — and a
date-only scheduler cannot express "hourly" anyway. So ``remediation_fold``
rides the existing hourly daily flow like every other fold, inside the maintain
umbrella's own lock, and each SUB-BRANCH carries its cadence, liveness,
convergence, shadow countdown and escalation in the HOST-PRIVATE state
:mod:`brain.remediation_state` owns. The only key written into the
mount-visible ``maintain-state.json`` is ``_remediation`` — ``_``-prefixed, so
no consumer that walks branch rows can mint a phantom branch out of it.

**Every repair is additive or raising, and that is enforced, not promised.**
:func:`audited_write` is the ONE write surface both branches use. It refuses a
target that does not already exist (so nothing is created, moved or renamed),
a body whose bytes changed (so no repair ever edits prose), a lowered
classification, and any change to the supersession keys (so nothing is
retired). There is no unlink, rename or replace anywhere in this module, and
``test_remediation_folds`` asserts that over the source.

It also refuses to be aimed somewhere else. A one-time "is this a symlink?"
check is a time-of-check/time-of-use race, because ``write_note`` resolves the
path a second time and a mount-writable directory can change underneath: the
write would land on ANOTHER note while the chain recorded this one's path. So
the file is opened once with ``O_NOFOLLOW``, its ``(st_dev, st_ino)`` recorded
and its bytes read from that descriptor, no path component anywhere may be a
symlink, and AFTER the write the name is re-opened ``O_NOFOLLOW`` and its bytes
compared to what was signed. A write that cannot be confirmed at the name it
was signed for raises, which stops the batch and reports.

**NEITHER branch acts on a claim the mount can write.** The vault is on the
VirtioFS mount, so every byte of a note — its body, its ``classification``, its
``classification_guard`` stamp — is a VM-writable CLAIM until the audit chain
covers it. Both branches therefore start from
``remediation_branches.chain_provenance``, and neither has a rule the other
lacks (adversarial review 2026-08-21: the first
cut applied this to ``sign_repair`` only, and that asymmetry was the defect).

*sign_repair* signs a note only when the chain ALREADY carries a
``write``/``ingest`` entry whose ``content_sha256`` equals the file's current
bytes — the note whose path-keyed chain entry was lost to a rename (the
measured 2026-08-18 failure), or whose path was deleted and restored. Matching
the hash is necessary and NOT sufficient, because bytes are copyable: the
entry's PATH must also be gone from the vault or be this very path (a MOVE, not
a duplicate), the note's ``id`` must be claimed by no other file, and a note
carrying a retired claim (``is_latest_version: false``/``superseded_by``) is
never resurrected at a new path. Everything else is a TAMPER exception: an
owner question, never an automatic signature. That is also why "content does
not match what the vault holds" needs no separate rule — a drifted note hashes
to bytes the chain never signed, so it lands in the same place.

*reguard* re-guards a source only when the chain's latest entry FOR THAT PATH
covers the file's current bytes. Without that rule its whole population is a
mutable frontmatter key: anyone who can write the mount could stamp
``classification_guard: unavailable`` on a note they had just downgraded, and
the branch would re-stamp the downgrade — with the host's signature on it,
since the only-raises floor is the file's own ``classification`` claim. With
it, both the stamp that selects the target and the tier the guard may not go
below are facts this host signed. A source whose bytes drifted since signing is
NOT a repair target; it is drift, and drift is an owner matter.

**reguard re-runs ENF-04, it does not re-implement it.**
``brain.ingest.tierguard`` is imported and driven; the only-raises rule, the
undecided band failing closed to the higher tier, the ENF-01 sub-floor refusal
and the per-leg accounting are all the guard's, unchanged. The branch's job is
to find the sources admitted while the guard could not run, ask the guard
again, and stamp the verdict through the audited path.

That it writes into ``raw/`` at all needs saying out loud, because AGENTS.md §2
calls that zone immutable. The immutability rule is about CONTENT — "a note
that needs to change is a ``brain/`` note, not a raw edit" — and the source
body here is never touched: the only keys that move are the guard's own verdict
fields, which ENF-04 stamps on exactly these notes at admission time, plus the
classification when the guard raises. Raising a low copy's tier on a ``raw/``
source through ``core.write_note`` is also the ACCEPT action the plan's frozen
disposition table already prescribes for the cross-tier findings. Both are
enforced rather than promised: :func:`audited_write` refuses a changed body and
refuses a lowered tier.

**Shadow acceptance is four runs, not three.** Both branches ship
``shadow_nights=3`` in the registry: three report-only runs that write nothing
and record what they WOULD have done, then live writes on the fourth. A run
that found no targets does not advance the countdown — a shadow night that
proves nothing is not evidence — and the countdown advances only when every
intended action falls in :data:`EXPECTED_WRITE_CLASSES`. Promotion is
automatic; there is no operator ritual.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import frontmatter as fm
from .classification import rank as tier_rank

#: The detector's own identity, compared against the coverage a branch
#: recorded (``remediation_state.binding_matches``). It changes when the way a
#: target set is DERIVED changes, so coverage recorded by an older definition
#: can never suppress a finding about a newer one.
DETECTOR_GENERATION = "remediation_folds/v1"

SIGN_REPAIR = "sign_repair"
REGUARD = "reguard"
KEY_UNSIGNED = "invariant:unsigned_notes"
KEY_UNGUARDED = "invariant:unguarded_ingests"

#: The unsigned notes this branch may NOT sign: no host record covers their
#: bytes. Declared in the registry as ``owner`` — a signature is the one thing
#: automation must never grant on trust.
TAMPER_KEY = "tamper:unsigned-note"

#: A write that did not land at the path it was signed for. Its own key, not
#: folded into ``TAMPER_KEY``: that one is about a note nobody signed, this one
#: is about the vault being raced while the fold held the writer lock.
REDIRECT_KEY = "tamper:redirected-write"

#: FIX-03 — the extraction-retry branch. It serves FOUR registry keys (one
#: sub-floor invariant plus three mechanical quarantine reasons), which is why
#: it does not fit the sign_repair/reguard one-branch-one-key shape: see
#: ``BranchOutcome.by_key``/``exhausted`` below.
EXTRACT_RETRY = "extract_retry"
KEY_SUBFLOOR = "invariant:subfloor_families"
#: How many times ``extract_retry`` will re-drop the SAME content-hash before
#: giving up on it — matches the registry's ``escalate_after`` for every key
#: this branch owns, so "exhausted" and "would have escalated the branch" are
#: the same number by construction.
EXTRACT_RETRY_MAX_ATTEMPTS = 3
#: One retry copy into ``inbox/`` per target per CALENDAR DAY — re-offering the
#: same bytes twice in one day collides on the ingest note id
#: (``{today}-{stem}``, quarantined as ``note_id_collision``) rather than
#: retrying anything (measured in ``tests/test_ingest_retry_failed_extraction.py``).
RETRY_COPY_TO_INBOX = "retry_copy_to_inbox"

#: How many targets one run may touch. Bounded so a 2,000-note backlog cannot
#: turn one hourly fold into an hour of signing; the remainder is reported and
#: picked up next run.
BATCH_ENV = "BRAIN_REMEDIATION_BATCH"
DEFAULT_BATCH = 20

#: The kill switch. This is the first fold that mints signatures and rewrites
#: classifications with nobody in the loop, and every other risky subsystem
#: here has one (``BRAIN_RERANK_DISABLED``, ``BRAIN_EXACT_LEG_ENABLED``,
#: ``BRAIN_INGEST_TIER_GUARD_DISABLED``). Shadow bounds the first three runs;
#: this is what bounds run 400. Disabled publishes NO target signatures either,
#: so a switched-off branch suppresses nothing — it cannot be used to silence
#: the findings it is no longer fixing.
DISABLED_ENV = "BRAIN_REMEDIATION_DISABLED"

#: The write classes a shadow run is allowed to have intended. A shadow night
#: whose record contains anything else does NOT advance promotion — the point
#: of shadow mode is to see the intended actions before they are real, so an
#: unrecognised one must stop the promotion rather than ride it.
#:
#: Frozen here rather than in the plan's design-freeze because this session's
#: sandbox forbids editing that file; a constant a test asserts against is a
#: stronger binding than prose either way.
SIGN_EXISTING_BYTES = "sign_existing_bytes"
RESTAMP_GUARD_VERDICT = "restamp_guard_verdict"
RAISE_CLASSIFICATION = "raise_classification"
EXPECTED_WRITE_CLASSES = frozenset(
    {SIGN_EXISTING_BYTES, RESTAMP_GUARD_VERDICT, RAISE_CLASSIFICATION})

#: Frontmatter keys a repair may never change: changing any of them retires,
#: un-retires or re-parents a note, and no branch may do that.
SUPERSESSION_KEYS = ("is_latest_version", "superseded_by", "superseded_date",
                     "previous_version", "replaces")

#: How many days of heal history the thrashing guard keeps. Long enough to see
#: ``remediation_state.THRASH_NIGHTS`` in a row, short enough that the state
#: file cannot grow without bound.
HEAL_STREAK_RETENTION_DAYS = 7


class ReversibilityError(RuntimeError):
    """A repair tried to do something no branch is allowed to do."""


class RedirectedWrite(ReversibilityError):
    """The bytes at the signed path are not the bytes that were signed.

    A SEPARATE class because it means something different from every other
    refusal here. The others are per-target: this note is a symlink, that one
    would lower a tier, so skip it and carry on. This one says a write landed
    somewhere else while the fold held the writer lock — the vault is being
    raced RIGHT NOW, and the next target in the batch would be racing it too.
    So it stops the batch and becomes an owner-class exception, which is what
    this module's docstring has always promised."""


@dataclass
class Intent:
    """One thing a branch would do (shadow) or did (live)."""

    target: str
    write_class: str
    detail: str = ""
    content: str = ""

    def record(self) -> dict[str, str]:
        return {"target": self.target, "write_class": self.write_class,
                "detail": self.detail}


@dataclass
class BranchOutcome:
    """What one sub-branch did this run — the s02 convergence contract."""

    branch: str
    key: str
    mode: str = "live"
    targets: list[str] = field(default_factory=list)
    healed: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    intents: list[dict[str, str]] = field(default_factory=list)
    available: bool = True
    error: str = ""
    #: Targets the DETECTOR could not enumerate (its scan cap). They are part
    #: of the defect population and must be part of ``remaining``, or a
    #: shrinking sample over an unenumerated backlog reads as convergence.
    unenumerated: int = 0
    #: Set when a write landed somewhere other than the path it was signed
    #: for. Stops the batch; becomes an owner-class tamper exception.
    redirected: str = ""
    #: FIX-03 only. ``sign_repair``/``reguard`` own exactly one registry key,
    #: so ``self.key``/``self.targets`` are enough. ``extract_retry`` owns
    #: FOUR (one sub-floor invariant, three mechanical quarantine reasons);
    #: ``by_key`` is ``{registry key: the subset of self.targets it covers}``,
    #: read by the fold to publish ONE ``target_signatures`` entry per key
    #: (and to record per-key convergence in the SAME host-private row) rather
    #: than one for the branch as a whole. Empty for every other branch, which
    #: falls back to ``{self.key: self.targets}``.
    by_key: dict[str, list[str]] = field(default_factory=dict)
    #: FIX-03 only, and the s01-review fix it exists for: a target that has
    #: been retried to ``EXTRACT_RETRY_MAX_ATTEMPTS`` and still fails is
    #: EXCLUDED from ``self.targets`` (so it can never make the whole branch
    #: read as non-converging — the finding was "one encrypted PDF forever",
    #: never "the branch is broken") and instead becomes an owner-class
    #: exception here, keyed by its own ``quarantine-exhausted:*`` registry
    #: key. ``{owner key: [target paths]}``.
    exhausted: dict[str, list[str]] = field(default_factory=dict)
    #: SPD-01 — this run's OWN measured spend. Every mechanical branch
    #: (``sign_repair``, ``reguard``, ``extract_retry``) never sets this, so
    #: its zero is a MEASURED zero (design-freeze (d), rule 2), not an
    #: absence. Only a model-backed branch (``synthesis_retry``) ever reports
    #: a nonzero figure, and only for its OWN re-fire — never the whole
    #: night's synthesis spend, which is already recorded separately as
    #: ``synthesis_cost_usd`` on the health record. No cap is enforced
    #: anywhere on this figure (owner ruling: unlimited spend, trend-alerted).
    cost_usd: float = 0.0
    tokens: int = 0

    @property
    def repairing(self) -> bool:
        """Whether this run was ALLOWED TO WRITE.

        The single notion of "this run is evidence, not a rehearsal". A shadow
        run has by definition repaired nothing, so nothing it reports may
        reach a suppression decision — see audit B (2026-08-21) and
        ``remediation_state.binding_matches``."""
        return self.mode == "live"

    @property
    def remaining(self) -> int:
        """Everything not healed THIS RUN — a skipped target counts.

        A branch that skips every target must not read healthy: it converges
        by fixing things, and ``unchanged_runs`` escalates it when it does
        not."""
        return len(self.targets) - len(self.healed) + self.unenumerated

    @property
    def enumerated(self) -> bool:
        """Whether the detector saw the WHOLE population this run.

        A target signature is a claim about a complete target set, so a run
        that could not enumerate everything publishes none and the finding
        banners (review item 10)."""
        return self.available and not self.unenumerated

    def report(self) -> dict[str, Any]:
        return {"branch": self.branch, "key": self.key, "mode": self.mode,
                "available": self.available, "targets": len(self.targets),
                "healed": len(self.healed), "skipped": len(self.skipped),
                "remaining": self.remaining, "unenumerated": self.unenumerated,
                "intents": self.intents, "cost_usd": self.cost_usd,
                "tokens": self.tokens,
                **({"redirected": self.redirected} if self.redirected else {}),
                "skipped_detail": self.skipped[:10],
                **({"error": self.error} if self.error else {})}


def disabled() -> bool:
    """Whether the whole remediation lane is switched off for this host."""
    return os.environ.get(DISABLED_ENV, "").strip() not in ("", "0", "false", "False")


def batch_cap() -> int:
    try:
        return max(1, int(os.environ.get(BATCH_ENV, "").strip() or DEFAULT_BATCH))
    except ValueError:
        return DEFAULT_BATCH


def target_set_hash(targets: list[str]) -> str:
    """sha256 over the sorted target ids — the s02 binding's ``hash`` half."""
    return hashlib.sha256(
        "\n".join(sorted(targets)).encode("utf-8")).hexdigest()


def signature(targets: list[str]) -> dict[str, str]:
    return {"generation": DETECTOR_GENERATION, "hash": target_set_hash(targets)}


# ---------------------------------------------------------------------------
# The ONE write surface. Everything both branches write goes through here.
# ---------------------------------------------------------------------------
def read_nofollow(target: Path) -> tuple[str, tuple[int, int]]:
    """``(text, (st_dev, st_ino))`` read through a NO-FOLLOW descriptor.

    Never opens a symlink, and never re-resolves the name between the check and
    the read — the bytes returned are the bytes of the inode reported."""
    import stat as _stat

    fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not _stat.S_ISREG(info.st_mode):
            raise ReversibilityError(f"{target.name}: not a regular file")
        chunks = []
        while True:
            block = os.read(fd, 1 << 20)
            if not block:
                break
            chunks.append(block)
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8"), (info.st_dev, info.st_ino)


def audited_write(
    core: Any, rel_path: str, new_text: str, reason: str, *, write_class: str,
) -> dict[str, Any]:
    """Write ``new_text`` at ``rel_path`` through the audited host path.

    Refuses, BEFORE anything is signed, every irreversible shape the plan's
    reversibility invariant forbids: creating a path (so nothing is moved or
    renamed into place), changing the body, lowering a classification, or
    touching a supersession key. It never deletes, moves or renames — those
    verbs do not appear in this module at all.

    And it refuses to be REDIRECTED. See the module docstring: a one-time
    symlink check races ``write_note``'s second resolve, so the pre-image is
    read through an ``O_NOFOLLOW`` descriptor, no path component may be a
    symlink, and the written bytes are confirmed at the same name afterwards."""
    if write_class not in EXPECTED_WRITE_CLASSES:
        raise ReversibilityError(f"{rel_path}: unknown write class {write_class!r}")
    vault = Path(core.vault)
    target = vault / rel_path
    if not target.is_file():
        raise ReversibilityError(
            f"{rel_path}: a repair only ever rewrites a file that already "
            "exists — it never creates, moves or renames one")
    if target.resolve() != (vault.resolve() / rel_path):
        # Catches a symlinked leaf AND a symlinked parent directory, which a
        # leaf-only `is_symlink()` check misses entirely.
        raise ReversibilityError(
            f"{rel_path}: resolves elsewhere — a repair never follows a link")
    old_text, before = read_nofollow(target)
    _refuse_irreversible(rel_path, old_text, new_text)
    result = core.write_note(rel_path, new_text, reason=reason)
    _confirm_written(vault, rel_path, new_text, before)
    return result


def _confirm_written(
    vault: Path, rel_path: str, new_text: str, before: tuple[int, int],
) -> None:
    """Prove the signed bytes are the bytes at the name that was signed for.

    ``os.replace`` swaps the NAME, so a fresh inode is expected and normal; a
    name that now refuses an ``O_NOFOLLOW`` open, or holds different bytes, or
    still holds the pre-image inode, means the write went somewhere else.

    ANY failure to re-read is a redirect, not just ``OSError``. ``read_nofollow``
    also raises ``ReversibilityError`` (the name is no longer a regular file)
    and ``UnicodeDecodeError`` (it no longer holds text) — and both of those
    fell through ``apply_intents``' generic handler as an ordinary per-target
    skip, leaving ``outcome.redirected`` unset so the owner-class exception
    never fired. A security control that degrades to a skip is not a control
    (llm-review, final). The rule is simply: if the bytes cannot be CONFIRMED
    at the name they were signed for, treat it as a redirect."""
    try:
        written, after = read_nofollow(vault / rel_path)
    except Exception as exc:  # noqa: BLE001 — unconfirmable IS the finding
        raise RedirectedWrite(
            f"{rel_path}: signed, but the path could not be re-read without "
            f"following a link ({exc}) — the write may have been redirected")
    # A same-inode result is only evidence on a platform that reports inodes;
    # `os.replace` always mints a new one, so equality means the name was not
    # replaced at all. Windows can report 0, which is not a comparison.
    same_inode = after == before and before[1] != 0
    if written != new_text or same_inode:
        raise RedirectedWrite(
            f"{rel_path}: signed, but the bytes at that path are not the bytes "
            "that were signed — the write was redirected")


def _refuse_irreversible(rel_path: str, old_text: str, new_text: str) -> None:
    old_meta, old_body = fm.parse_text(old_text)
    new_meta, new_body = fm.parse_text(new_text)
    if old_body != new_body:
        raise ReversibilityError(
            f"{rel_path}: a repair may not change the note's body")
    old_tier = str(old_meta.get("classification") or "")
    new_tier = str(new_meta.get("classification") or "")
    if tier_rank(new_tier) < tier_rank(old_tier):
        raise ReversibilityError(
            f"{rel_path}: a repair only ever RAISES a classification "
            f"({old_tier!r} -> {new_tier!r} lowers it)")
    for key in SUPERSESSION_KEYS:
        if old_meta.get(key) != new_meta.get(key):
            raise ReversibilityError(
                f"{rel_path}: a repair may not change {key!r} — retiring, "
                "un-retiring or re-parenting a note is never automatic")

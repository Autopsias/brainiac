"""FIX-01/FIX-02/FIX-03 — the repair branches' DETECTORS and their provenance
rules.

:mod:`brain.remediation_folds` holds the shared contract — the one audited
write surface, the outcome shape, the shadow-mode constants. This module holds
the audit-chain provenance reader (:class:`ChainProvenance`) and the DETECTORS
that decide what each branch may repair. Read that module's docstring first:
the rule the FIX-01/FIX-02 planners obey ("neither branch acts on a claim the
mount can write") is stated there, and every refusal here is one clause of it.
FIX-03's ``extract_retry`` is a different shape — its writes land in
``inbox/``, the drop zone, never in the audited ``vault/brain``/``vault/raw``
zone — so it does not go through ``audited_write`` at all; see
``plan_extract_retry``'s own docstring.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import frontmatter as fm
from .remediation_extract_retry import (
    _attempt_entry,
    _retry_already_resolved,
    plan_extract_retry,
    quarantine_retry_targets,
    subfloor_retry_targets,
)
from .remediation_folds import (
    BranchOutcome, Intent, KEY_UNGUARDED, KEY_UNSIGNED, RAISE_CLASSIFICATION,
    REGUARD, RESTAMP_GUARD_VERDICT, SIGN_EXISTING_BYTES, SIGN_REPAIR,
)

# ---------------------------------------------------------------------------
# fix-01 — sign the orphans, and ONLY the ones this host already signed.
# ---------------------------------------------------------------------------
#: How many unsigned notes one detector pass will enumerate. Far above any
#: plausible backlog; a population beyond it is reported as truncated rather
#: than silently shrinking the branch's own remaining count.
UNSIGNED_SCAN_CAP = 5000


@dataclass(frozen=True)
class ChainProvenance:
    """What the audit chain says this host has signed, and where.

    BOTH branches read this, and neither may act on anything else: every byte
    under ``vault/`` is on the VM-writable mount, so a frontmatter key is a
    CLAIM until an entry here covers it.

    * ``by_hash`` — ``{content_sha256: the FIRST path it was signed at}``. It
      proves the host signed those exact bytes once; it does NOT prove they
      belong at the path they are sitting at now, which is why
      :func:`plan_sign_repair` binds the path as well.
    * ``latest_by_path`` — ``{path: the content_sha256 of its most recent
      surviving write}``, with ``delete``/``write_failed`` popping the path
      exactly as ``AuditChain.content_drift`` does. This is what says "this
      file has not been edited since the host signed it"."""

    by_hash: dict[str, str]
    latest_by_path: dict[str, str]

    def covers_current(self, rel: str, digest: str) -> bool:
        """Whether the chain's latest entry for ``rel`` is these exact bytes."""
        return bool(digest) and self.latest_by_path.get(rel) == digest


def chain_provenance(chain_path: Path) -> ChainProvenance:
    """Read both provenance views in ONE pass over the chain.

    The ``delete``/``write_failed`` handling mirrors
    ``AuditChain.content_drift`` deliberately: two definitions of "what is the
    signed state of this path" would eventually disagree, and the one that
    disagreed quietly would be this one."""
    import json

    by_hash: dict[str, str] = {}
    latest: dict[str, str] = {}
    try:
        text = chain_path.read_text(encoding="utf-8")
    except OSError:
        return ChainProvenance(by_hash, latest)
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            continue
        verb, path = row.get("verb"), row["path"]
        digest = str(row.get("content_sha256") or "")
        if verb in ("write", "ingest") and digest:
            by_hash.setdefault(digest, path)
            latest[path] = digest
        elif verb in ("delete", "write_failed"):
            latest.pop(path, None)
    return ChainProvenance(by_hash, latest)


def plan_sign_repair(core: Any, cap: int) -> tuple[BranchOutcome, list[Intent],
                                                   list[str]]:
    """Targets, intended signatures, and the notes that may NOT be signed."""
    from .invariant_unsigned_notes import unsigned_notes

    out = BranchOutcome(SIGN_REPAIR, KEY_UNSIGNED)
    # The SAME chain file on both sides by construction: the population the
    # metric reports and the provenance the branch matches against must never
    # be two different chains, or a repair would attest to something the
    # invariant never measured.
    chain_path = Path(core.audit.log_path)
    metric = unsigned_notes(core.index.conn, Path(core.vault),
                            cap=UNSIGNED_SCAN_CAP, chain_path=chain_path)
    if not metric.get("available"):
        out.available = False
        out.error = str(metric.get("error") or "unsigned_notes unavailable")
        return out, [], []
    out.targets = [str(p) for p in (metric.get("sample") or [])]
    out.unenumerated = max(0, int(metric.get("value") or 0) - len(out.targets))
    if out.unenumerated:
        # Counted into `remaining`, not merely noted: a SHRINKING sample over a
        # population the detector never enumerated would otherwise report the
        # branch converging and suppress the finding (review item 10).
        out.skipped.append({"target": "*", "reason": f"{out.unenumerated} more "
                            f"beyond the {UNSIGNED_SCAN_CAP}-note scan cap"})
    provenance = chain_provenance(chain_path)
    # Built LAZILY: it is a full frontmatter walk of brain/ + raw/, and the
    # normal hourly run has no targets at all.
    ids: dict[str, int] | None = None
    intents, tamper = [], []
    for rel in out.targets:
        try:
            text = (Path(core.vault) / rel).read_text(encoding="utf-8")
        except OSError as exc:
            out.skipped.append({"target": rel, "reason": f"unreadable: {exc}"})
            continue
        if ids is None:
            ids = _id_owners(core)
        origin, refusal = _sign_provenance(core, rel, text, provenance, ids)
        if refusal is not None:
            tamper.append(rel)
            out.skipped.append({"target": rel, "reason": refusal})
            continue
        if len(intents) < cap:
            intents.append(Intent(rel, SIGN_EXISTING_BYTES,
                                  f"sign in place; bytes already signed at {origin}",
                                  text))
    return out, intents, tamper


def _id_owners(core: Any) -> dict[str, int]:
    """``{note id: how many files under vault/ claim it}``.

    A duplicate id is what a resurrected COPY produces, and the index cannot
    answer it (an unsigned note may not be indexed yet), so the files are
    counted directly."""
    from . import frontmatter as _fm

    owners: dict[str, int] = {}
    for zone in ("brain", "raw"):
        for path in (Path(core.vault) / zone).rglob("*.md"):
            try:
                meta, _body = _fm.parse_text(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            nid = str(meta.get("id") or "").strip()
            if nid:
                owners[nid] = owners.get(nid, 0) + 1
    return owners


def _sign_provenance(
    core: Any, rel: str, text: str, provenance: ChainProvenance,
    ids: dict[str, int],
) -> tuple[str, str | None]:
    """``(the path these bytes were signed at, refusal reason or None)``.

    A hash match alone is NOT provenance for THIS path, because bytes are
    copyable. Three further rules, each closing a named attack (adversarial
    review 2026-08-21):

    1. the entry's path must be gone from the vault, or be this very path —
       a MOVE or a restore, never a second copy of a live note;
    2. no other file may claim this note's ``id`` — otherwise a copy is signed
       into existence beside its original;
    3. a note carrying a RETIRED claim is never signed at a new path — that is
       version resurrection: a v1 body, superseded months ago, reappearing as
       a live note whose frontmatter still says it was superseded."""
    from . import frontmatter as _fm
    from .notes import sha256_text

    origin = provenance.by_hash.get(sha256_text(text))
    if origin is None:
        return "", ("no host record covers these bytes — TAMPER exception, "
                    "never auto")
    if origin != rel and (Path(core.vault) / origin).exists():
        return origin, (f"these bytes are signed at {origin}, which still "
                        "exists — this is a COPY, not an interrupted move")
    try:
        meta, _body = _fm.parse_text(text)
    except ValueError as exc:
        return origin, f"unparseable frontmatter: {exc}"
    nid = str(meta.get("id") or "").strip()
    if nid and ids.get(nid, 0) > 1:
        return origin, (f"id {nid!r} is claimed by {ids[nid]} files — signing "
                        "would confirm a duplicate id")
    retired = (str(meta.get("is_latest_version")).lower() == "false"
               or bool(meta.get("superseded_by")))
    if retired and origin != rel:
        return origin, ("this note carries a RETIRED claim and its signed path "
                        f"was {origin} — a superseded version is never "
                        "resurrected at a new path")
    return origin, None


# ---------------------------------------------------------------------------
# fix-02 — re-run ENF-04 over the sources admitted while it could not run.
# ---------------------------------------------------------------------------
def unguarded_targets(vault: Path) -> list[str]:
    """``raw/`` sources stamped admitted-while-the-guard-was-down.

    IMPORTED from the metric, never restated: ``invariants.ingest_guard``
    reports the population behind its own ``value`` as ``unguarded``, so the
    walk, the frontmatter key, the status and the pre-2026-08-17 back-compat
    rule all have exactly ONE definition. ``test_remediation_folds`` pins the
    two to the same number, and a hand-copy is what AGENTS.md legislates
    against for precisely this reason."""
    from . import invariants as _inv

    return list(_inv.ingest_guard(Path(vault))["unguarded"])


def _corpus_is_empty(core: Any) -> bool:
    """Whether the index holds no notes at all.

    A ``no_corpus`` verdict means "the corpus held nothing comparable", which
    is an honest and FINAL answer over a real corpus and a meaningless one over
    an empty index. Restamping on the second permanently retires a source from
    the unguarded population without any cross-tier comparison ever having
    happened (review item 5)."""
    try:
        return int(core.index.conn.execute(
            "SELECT count(*) FROM notes").fetchone()[0]) == 0
    except Exception:  # noqa: BLE001 — an unreadable index is treated as empty
        return True


def plan_reguard(core: Any, cap: int) -> tuple[BranchOutcome, list[Intent], list[str]]:
    """Ask the ENF-04 guard again about each unguarded source.

    Only sources whose CURRENT bytes the audit chain already covers are
    targets — see the module docstring: the ``unavailable`` stamp selects the
    work and the file's ``classification`` is the floor the guard may not go
    below, and both are mount-writable claims until the chain says otherwise.

    ponytail: the detector re-parses every ``raw/`` frontmatter each hourly
    fold, inside ``invariants.ingest_guard``'s own walk (~1-3 s on the
    2,600-note reference vault). If that ever matters, thread ONE parsed walk
    through both from the maintain run; do not add a second cache here."""
    from .ingest import tierguard
    from .notes import sha256_text

    out = BranchOutcome(REGUARD, KEY_UNGUARDED)
    out.targets = unguarded_targets(Path(core.vault))
    if not out.targets:
        return out, [], []
    provenance = chain_provenance(Path(core.audit.log_path))
    empty_corpus = _corpus_is_empty(core)
    guard = tierguard.guard_for(core)
    intents: list[Intent] = []
    for rel in out.targets[:cap]:
        try:
            text = (Path(core.vault) / rel).read_text(encoding="utf-8")
        except OSError as exc:
            out.skipped.append({"target": rel, "reason": f"unreadable: {exc}"})
            continue
        if not provenance.covers_current(rel, sha256_text(text)):
            out.skipped.append({"target": rel, "reason": (
                "the audit chain does not cover these bytes — the guard stamp "
                "and the classification floor are unverified claims, so this "
                "is drift for the owner, never an automatic re-guard")})
            continue
        intent = _reguard_one(guard, rel, text, out, empty_corpus=empty_corpus)
        if intent is not None:
            intents.append(intent)
    return out, intents, []


def _reguard_one(
    guard: Any, rel: str, text: str, out: BranchOutcome, *, empty_corpus: bool,
) -> Intent | None:
    """One source's verdict, or ``None`` when the guard still cannot judge it."""
    from .ingest.tierguard import (
        GUARD_KEY, GUARD_LEG_KEY, GUARD_REASON_KEY, NO_CORPUS, UNAVAILABLE,
    )

    try:
        meta, body = fm.parse_text(text)
    except ValueError as exc:
        out.skipped.append({"target": rel, "reason": f"unreadable: {exc}"})
        return None
    declared = str(meta.get("classification") or "")
    verdict = guard.verdict(body, declared)
    if verdict.status == UNAVAILABLE:
        out.skipped.append({"target": rel, "reason": "the guard still cannot "
                            f"run: {verdict.reason}"})
        return None
    if verdict.status == NO_CORPUS and empty_corpus:
        out.skipped.append({"target": rel, "reason": (
            "the index holds no notes at all, so 'no comparable document' is "
            "not a verdict — the marker stays for a run with a real corpus")})
        return None
    # The stale `unavailable` reason/leg must go, or the new verdict would be
    # read against the old explanation.
    stamped = fm.set_keys(
        fm.drop_keys(text, (GUARD_LEG_KEY, GUARD_REASON_KEY)),
        {**verdict.frontmatter(), "classification": verdict.tier})
    raised = verdict.tier != declared
    write_class = RAISE_CLASSIFICATION if raised else RESTAMP_GUARD_VERDICT
    detail = (f"{GUARD_KEY}: {UNAVAILABLE} -> {verdict.status}"
              + (f"; {declared} -> {verdict.tier}" if raised else ""))
    return Intent(rel, write_class, detail, stamped)

"""SUP-01 — declared supersession (2026-09-18; drop lane reworked F12/F14).

A third tier beside DDP-01 (sha-identical) and VER-01 (``-vN`` families).
CUR-01 stays as it was: what the ENGINE deduces is proposed, never applied.
What a caller DECLARES is also a proposal the owner answers — the declaration
is staged through ``cos.stage_declared_version_link`` and retired only after
the owner accepts it, through the same audited ``core.supersede``.

Three surfaces, one path:

* the drop lane — a sidecar ``<filename>.supersedes`` beside a payload in
  ``inbox/_deliverables/``, or ``replaces:`` in a dropped ``.md``'s own
  frontmatter. The drop folder is on the mount a Cowork VM can write, so the
  host cannot tell the owner's file from a planted one: it is STAGED, never
  applied;
* the broker verb ``supersede`` — :func:`guarded`;
* the broker's ``capture`` with ``replaces:`` — :func:`guarded` again.

**The declaration is stamped on the successor as ``replaces:``** — already the
documented alias of ``previous_version`` (AGENTS.md §2). That makes the failure
countable FROM THE CORPUS: :func:`declared_failed` reads every live note that
says it replaces something, and asks whether that something is retired under
it. It self-heals — a hand ``brain supersede`` brings it back to zero — and a
run report nobody read cannot hide it.

STATED LIMIT: ``replaces:`` is a scalar link key (``tools/validate.py``), so a
sidecar naming SEVERAL ids stamps only the first. Every id is applied, and
every failure is reported on the run it happens (maintain result, ``brain
alerts``, the exceptions page); only the first can keep counting afterwards.

**Why the broker may hold a host-only verb.** ``supersede`` stays a HOST-broker
privilege: the broker IS the host — a host process holding the audit key, which
Cowork reaches over Claude Desktop's channel. A ``role=vm`` core is still
refused, by ``mcp_adapter.dispatch`` and again by ``core._require_host``. What
the broker adds is that its CALLER is a model session that may have read
attacker-controlled text, so :func:`guarded` applies a pair only when the vault
itself says the two notes are related, and otherwise stages it as a CUR-01
proposal for the nightly owner question.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import _optional

SIDECAR_SUFFIX = ".supersedes"
FAILED = "supersede_declared_failed"
PENDING = "supersede_declared"

# The sidecar sits in the mount-writable drop lane (INT-04): read it capped, and
# keep only the first ids. A real declaration is a handful of short slugs.
MAX_SIDECAR_BYTES = 64 * 1024
MAX_SIDECAR_IDS = 100

_WIKILINK = re.compile(r"^\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]$")


# ---------------------------------------------------------------------------
# reading a declaration
# ---------------------------------------------------------------------------

def parse_ids(value: Any, limit: int | None = None) -> list[str]:
    """Bare ids or ``[[wikilinks]]`` (``[[raw/<id>]]``, ``[[id|alias]]``), one
    per line. Order kept, duplicates dropped, ``#`` lines ignored; stops at
    ``limit`` ids when one is given."""
    lines = value if isinstance(value, (list, tuple)) else str(value or "").splitlines()
    out: list[str] = []
    for raw in lines:
        text = str(raw).strip().strip("\"'")
        if not text or text.startswith("#"):
            continue
        match = _WIKILINK.match(text)
        ident = (match.group(1) if match else text).strip()
        ident = ident.rsplit("/", 1)[-1] if match else ident
        if ident and ident not in out:
            out.append(ident)
            if limit is not None and len(out) >= limit:
                break
    return out


def sidecar_for(payload: Path) -> Path:
    return payload.with_name(payload.name + SIDECAR_SUFFIX)


def declared_ids(record: Any) -> tuple[list[str], str]:
    """``(old ids, where they were declared)`` for one drop-lane claim.

    The sidecar still sits in the drop folder — the claim moved only the
    payload — so ``record.path`` finds it. A dropped ``.md`` may declare the
    same thing in its own leading frontmatter."""
    from . import draft_drain
    from . import frontmatter as fm

    sidecar = sidecar_for(record.path)
    ids: list[str] = []
    where = ""
    if sidecar.is_file() and not sidecar.is_symlink():
        try:
            ids = parse_ids(draft_drain._read_text_nofollow(sidecar, MAX_SIDECAR_BYTES),
                            limit=MAX_SIDECAR_IDS)
        except (OSError, UnicodeDecodeError, draft_drain._UnsafeDraft):
            ids = []
        where = f"inbox/_deliverables/{sidecar.name}"
    markdown = getattr(record.result, "markdown", "") if record.result else ""
    if record.path.suffix.lower() in (".md", ".markdown") and markdown:
        meta, _ = fm.parse_text(markdown)
        extra = [i for i in parse_ids(meta.get("replaces")) if i not in ids]
        if extra:
            ids += extra
            where = where or f"replaces: in {record.orig_name}"
    return ids, where


def stamp(record: Any) -> None:
    """Put the declaration on the raw source BEFORE it is signed (``raw/`` is
    immutable, so this is the one chance). Only a path-safe id is stamped."""
    from .notes import safe_slug

    for ident in declared_ids(record)[0]:
        try:
            record.meta["replaces"] = safe_slug(ident)
        except ValueError:
            continue
        return


def note_declaration(record: Any, entry: dict[str, Any]) -> None:
    """Queue the declaration for :func:`apply_pending`, which runs AFTER the
    index reconcile — staging resolves both ids through the index, and the
    note signed a moment ago is not in it yet."""
    ids, where = declared_ids(record)
    if not ids:
        return
    from .ingest import deliverables as DLV

    record.drain.report.setdefault(PENDING, []).append({
        "new_id": record.slug, "old_ids": ids, "declared_in": where,
        "anchor_id": DLV.anchor_id(record.slug),
        "anchored": not str(entry.get("deliverable_anchor", "")).startswith("deferred:"),
        "sidecar": str(sidecar_for(record.path)),
    })


# ---------------------------------------------------------------------------
# applying it
# ---------------------------------------------------------------------------

def _note(core: Any, note_id: str) -> tuple[dict[str, Any], str] | None:
    from . import frontmatter as fm

    row = core.index.get(note_id)
    if not row:
        return None
    path = Path(row["path"])
    path = path if path.is_absolute() else Path(core.vault) / path
    try:
        return fm.parse_text(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _meta(core: Any, note_id: str) -> dict[str, Any] | None:
    note = _note(core, note_id)
    return note[0] if note else None


def _link(value: Any) -> str:
    from .notes import _bitemporal_link

    return _bitemporal_link(value if isinstance(value, str) or value is None else str(value))


def sides(core: Any, old_id: str, new_id: str) -> dict[str, Any]:
    """Both sides' version frontmatter, as it is on disk now."""
    keys = ("is_latest_version", "superseded_by", "previous_version")
    return {side: {k: (meta or {}).get(k) for k in keys}
            for side, meta in (("old", _meta(core, old_id)), ("new", _meta(core, new_id)))}


def _pairs(core: Any, decl: dict[str, Any]) -> list[tuple[str, str]]:
    """The raw pair, plus the ANCHOR pair when the old side is itself a
    drop-lane deliverable — otherwise the shelf keeps showing both versions,
    because the shelf census reads anchors, not raw sources."""
    from .ingest import deliverables as DLV

    out: list[tuple[str, str]] = []
    for old_id in decl["old_ids"]:
        row = core.index.get(old_id)
        brain_zone = bool(row) and str(row.get("zone") or "") != "raw"
        out.append((old_id, decl["anchor_id"] if brain_zone else decl["new_id"]))
        old_anchor = DLV.anchor_id(old_id)
        if row and not brain_zone and core.index.get(old_anchor):
            out.append((old_anchor, decl["anchor_id"]))
    return out


def _vm_may_read(core: Any, note_id: str) -> bool:
    """Is this note within the tier a Cowork VM may read? The drop folder is
    VM-writable, so the id it names counts as the VM naming it. The tier is the
    LOWER of the signed ceiling and ``Internal``: that file sits on the mount
    (A-13), so a raised one must not widen what the drop lane may name. The
    ``ClassificationFilter`` is the gate ``egress.apply_gate`` wraps, minus its
    read-record tally, which a drain must not write."""
    from . import classification, vm_ceiling

    tier = classification.clamp_to(vm_ceiling.resolved_ceiling(core.vault)[0],
                                   classification.VM_DEFAULT_MAX_TIER)
    row = core.get(note_id)
    return bool(row) and classification.ClassificationFilter(tier).allows(
        row.get("classification"))


def _stage(core: Any, decl: dict[str, Any], old_id: str, new_id: str) -> str | None:
    """Stage ONE declared pair as an owner question. The error text, or None."""
    if old_id == new_id:
        return "a note may not supersede itself"
    # F12/F14: first, with the unknown-note text, so a note above the VM tier is
    # not told apart from an absent one and its title never reaches the question.
    if not _vm_may_read(core, old_id):
        return f"unknown or unreadable note {old_id!r}"
    for note_id in (old_id, new_id):
        meta = _meta(core, note_id)
        if meta is None:
            return f"unknown or unreadable note {note_id!r}"
        if (str(meta.get("is_latest_version", "")).strip().lower() == "false"
                or _link(meta.get("superseded_by"))):
            return f"note {note_id!r} has already been superseded"
    from . import host_hooks as _host_hooks

    hooks = _host_hooks.providers_with(
        _host_hooks.load_core_hooks(core), "handle_declared_supersede",
    )
    if hooks:
        for provider in hooks:
            staged = _host_hooks.call_hook_method(
                provider, "handle_declared_supersede",
                core, old_id, new_id,
                f"declared in the drop lane: {decl['declared_in']}", "drop lane",
                default=None,
            )
            if isinstance(staged, dict):
                if staged.get("already_decided"):
                    return "already ruled on or waiting for the owner - not re-asked"
                decl["staged"].append({"old_id": old_id, "new_id": new_id,
                                       "proposal": staged.get("proposal")})
                return None
        return "this build has no proposal queue to stage the declaration in"
    if not _optional.cos_available():
        return "this build has no proposal queue to stage the declaration in"
    from . import cos

    staged = cos.stage_declared_version_link(
        core, old_id, new_id, declared_by="drop lane",
        reason=f"declared in the drop lane: {decl['declared_in']}")
    if staged["already_decided"]:
        return "already ruled on or waiting for the owner - not re-asked"
    decl["staged"].append({"old_id": old_id, "new_id": new_id,
                           "proposal": staged["proposal"]})
    return None


def apply_pending(core: Any, ingest_report: dict[str, Any]) -> None:
    """Stage every declaration this drain queued — NOTHING is retired here. The
    caller (``core.sync``) holds the writer lock and has just reconciled the
    index.

    Never raises: the ingest already succeeded, and a declaration that cannot
    be staged is REPORTED under ``supersede_declared_failed``, never rolled
    into an ingest failure."""
    for decl in ingest_report.get(PENDING) or []:
        decl["staged"] = []
        if not decl.get("anchored"):
            # Nothing re-queues a deferred declaration, so its sidecar is
            # consumed here too (review 2026-09-29) — left behind, it
            # declared a supersession for the NEXT file of that name.
            _fail(ingest_report, decl, decl["old_ids"][0],
                  "anchor deferred — the deliverable did not finish landing")
        for old_id, new_id in (_pairs(core, decl) if decl.get("anchored") else []):
            if _link((_meta(core, old_id) or {}).get("superseded_by")) == new_id:
                continue  # a re-drop, or a recovered run: already done
            try:
                error = _stage(core, decl, old_id, new_id)
            except Exception as exc:  # noqa: BLE001 — reported, never raised
                error = f"{type(exc).__name__}: {exc}"
            if error:
                _fail(ingest_report, decl, old_id, error, new_id=new_id)
        # Consumed either way: the outcome is in the report, and a failed
        # declaration keeps counting through the stamped `replaces:`.
        Path(decl["sidecar"]).unlink(missing_ok=True)


def _fail(report: dict[str, Any], decl: dict[str, Any], old_id: str, error: str,
          *, new_id: str | None = None) -> None:
    report.setdefault(FAILED, []).append({
        "old_id": old_id, "new_id": new_id or decl["new_id"],
        "declared_in": decl["declared_in"], "error": error})


def findings(ingest_report: dict[str, Any], vault: Path) -> list[dict[str, Any]]:
    """One ``action_required`` item for the run's failed declarations.

    A COUNT only. This text is persisted into ``.brain/notify-sent/current.json``,
    which a Cowork VM session can read, and a note id here is a document title —
    the same rule the quarantine and declassification banners follow."""
    from .maintenance_outcomes import action_required_item

    failed = ingest_report.get(FAILED) or []
    if not failed:
        return []
    item = action_required_item(
        f"{len(failed)} owner-declared supersession(s) could NOT be applied "
        "(ids withheld — a note id is a document title)",
        "the new version was ingested, and the version it declares it replaces "
        "is STILL LIVE in search: the id was unknown or above the Cowork read "
        "tier, already superseded, the same note, or a pair already ruled on",
        "run `brain health-report` (row: owner-declared supersessions not "
        "applied) for the ids, then `brain supersede <old> <new>` by hand",
        str(vault / "inbox" / "_deliverables"),
    )
    item["notify_key"] = FAILED
    return [item]


# ---------------------------------------------------------------------------
# the metric (WAT-01, AGENTS.md §4 rule 6)
# ---------------------------------------------------------------------------

def declared_failed(conn: Any, *, cap: int = 10) -> dict[str, Any]:
    """Live notes whose ``replaces:`` names a note NOT retired under them.

    Keyed on ``replaces:`` alone, never the ``previous_version`` it aliases:
    measured 2026-09-18 on the reference vault, 127 of 2,055
    ``previous_version`` links have no reciprocal (date-id daily notes the
    chain folds stamped one-sided), and 0 of 2 ``replaces:`` do. The wider key
    would open at 127 and measure a different defect."""
    rows = conn.execute(
        "SELECT n.id, json_extract(n.frontmatter, '$.replaces'), o.superseded_by "
        "FROM notes n LEFT JOIN notes o "
        "  ON o.id = json_extract(n.frontmatter, '$.replaces') "
        "  OR '[[' || o.id || ']]' = json_extract(n.frontmatter, '$.replaces') "
        "WHERE json_extract(n.frontmatter, '$.replaces') IS NOT NULL "
        "  AND n.is_latest_version != 'false'").fetchall()
    from .ingest import deliverables as DLV

    # A brain-zone old side is retired under the new source's ANCHOR, not the
    # source itself (`_pairs`), and that is a success (review 2026-09-29).
    failed = sorted(f"{r[0]} -> {r[1]}" for r in rows
                    if _link(r[2]) not in (str(r[0]), DLV.anchor_id(str(r[0]))))
    return {"value": len(failed), "population": len(rows),
            "declared": len(rows), "sample": failed[:cap]}


# ---------------------------------------------------------------------------
# the broker path
# ---------------------------------------------------------------------------

def related(core: Any, old_id: str, new_id: str) -> str | None:
    """WHY the vault itself says ``new_id`` is a version of ``old_id``, or None.

    Two legs: a TRUSTED successor (or its anchor) declaring ``replaces:`` /
    ``previous_version``, or a name family. A body ``[[old_id]]`` is NOT
    evidence (F32): here a wikilink is a citation, so any trusted note citing a
    victim would retire it, and the deliverable anchor links only its project
    and its own source, never a predecessor.

    An UNTRUSTED successor (``status: draft`` / ``provenance.trust: untrusted``
    — everything the broker's own ``capture`` writes) is never related by its
    own say-so: its ``replaces:``, its links and its title were all chosen by
    the caller asking for the retirement. That is DDP-01's trust guard, and
    without it the guard below is one ``capture`` call away from empty."""
    from . import versionlink as vl
    from .ingest import deliverables as DLV

    def untrusted(meta: dict[str, Any]) -> bool:
        return (str(meta.get("status", "")).strip().casefold() == "draft"
                or str(meta.get("provenance.trust", "")).strip().casefold() == "untrusted")

    if untrusted(_meta(core, new_id) or {}):
        return None
    for holder in (new_id, DLV.anchor_id(new_id)):
        meta = _meta(core, holder)
        # The `<new_id>-deliverable` anchor is a plain id any caller can mint
        # through `capture`: it is evidence only while it is itself trusted.
        if meta is None or untrusted(meta):
            continue
        if old_id in (_link(meta.get("replaces")), _link(meta.get("previous_version"))):
            return f"{holder} declares replaces: {old_id}"
    views = {v.id: v for v in vl._load(core) if v.id in (old_id, new_id)}
    if len(views) == 2 and views[old_id].stems & views[new_id].stems:
        return "name family: " + sorted(views[old_id].stems & views[new_id].stems)[0]
    return None


def dropzone_source(meta: dict[str, Any]) -> bool:
    """True for a note the drop lane signed (its ``raw/originals/`` source, or a
    deliverable anchor). Its text, links and file name were written in ``inbox/``,
    which a Cowork session can write, so it never relates a pair a caller names.
    ``origin`` and ``provenance.produced_by`` are host-written; the drop lane
    cannot omit either."""
    from .ingest import deliverables as DLV

    return (str(meta.get("origin", "")).startswith("raw/originals/")
            or str(meta.get("provenance.produced_by", "")) == DLV.PRODUCED_BY)


def _refused(why: str, **extra: Any) -> dict[str, Any]:
    return {"applied": False, "proposed": False, "refused": why, **extra}


def guarded(core: Any, old_id: str, new_id: str, *, reason: str, max_tier: str,
            declared_by: str, undo: bool = False) -> dict[str, Any]:
    """The broker's ``supersede``/``unsupersede``. Guards IN ORDER; a guard
    that fails RETURNS a refusal (the ``capture`` tool's shape), it does not
    raise — the read record below must be flushed either way.

    1. host only (raises — a vm core never reaches a guard);
    2. both ids exist AND are VISIBLE at the caller's tier, through the one
       egress gate, in one recorded call. ONE refusal for "absent" and "above
       your tier", naming neither id, so the verb is not an existence oracle
       and nobody retires a note they cannot read;
    3. ``new_id`` is not itself retired; 4. a reason;
    5. ``unsupersede`` is REFUSED here, naming the host command;
    6. a DROP-LANE successor is REFUSED the same way (``dropzone_source``);
    7. relatedness — else the pair is STAGED as a CUR-01 proposal."""
    from . import egress

    core._require_host("supersede notes through the broker")
    notes = [n for n in (core.get(old_id), core.get(new_id)) if n]
    surfaced, _report = egress.apply_gate(notes, max_tier)
    if old_id == new_id or {str(n.get("id")) for n in surfaced} != {old_id, new_id}:
        return _refused("not-found: both ids must name distinct notes you can read")
    new_meta = _meta(core, new_id) or {}
    if str(new_meta.get("is_latest_version", "")).strip().lower() == "false":
        return _refused(f"new-is-retired: {new_id}")
    if not str(reason or "").strip():
        return _refused("reason-required")
    try:
        if undo:
            # NO BROKER UNDO (owner, 2026-09-29). Relatedness cannot guard it —
            # `supersede` stamps `previous_version`, so every linked pair reads
            # related — and a session that read injected text could otherwise
            # bring an owner-retired version back into search. The owner's
            # terminal keeps the verb.
            return _refused("undo-needs-the-owner: run `brain unsupersede "
                            f"{old_id} {new_id} --reason ...` on the host")
        if dropzone_source(new_meta):
            # Same reason as the untrusted-draft rule in `related`: a session
            # writes `inbox/`, so the successor's links, `replaces:` and name
            # are the caller's. Not staged either — `cos._version_apply`
            # would refuse it, and the owner's standing answer must not meet it.
            return _refused(f"drop-lane-successor: {new_id} came in through the "
                            "inbox, which a session can write, so it cannot relate "
                            f"the pair; run `brain supersede {old_id} {new_id} "
                            "--reason ...` on the host",
                            **sides(core, old_id, new_id))
        why = related(core, old_id, new_id)
        if why is None:
            from . import host_hooks as _host_hooks

            hooks = _host_hooks.providers_with(
                _host_hooks.load_core_hooks(core), "handle_declared_supersede",
            )
            if hooks:
                for provider in hooks:
                    staged = _host_hooks.call_hook_method(
                        provider, "handle_declared_supersede",
                        core, old_id, new_id, reason, declared_by,
                        default=None,
                    )
                    if isinstance(staged, dict):
                        return {"applied": False,
                                "proposed": not staged.get("already_decided"),
                                **staged, **sides(core, old_id, new_id)}
            elif _optional.cos_available():
                from . import cos

                staged = cos.stage_declared_version_link(
                    core, old_id, new_id, reason=reason, declared_by=declared_by)
                return {"applied": False, "proposed": not staged["already_decided"],
                        **staged, **sides(core, old_id, new_id)}
            # ADR 0013: no COS, no proposal queue to stage an unrelated pair
            # into. Refuse, and name the declaration that makes them related.
            return _refused("unrelated: this build has no proposal queue to stage "
                            "the pair; relate the notes first (a trusted new note "
                            f"declaring `replaces: {old_id}`)",
                            **sides(core, old_id, new_id))
        res = core.supersede(old_id, new_id, reason=f"{declared_by} ({why}): {reason}")
    except ValueError as exc:  # the core's own chain invariants, named
        return _refused(str(exc), **sides(core, old_id, new_id))
    extra = {"already_done": True} if res.get("already_done") else {}
    return {"applied": True, "proposed": False, "related": why, **extra,
            **sides(core, old_id, new_id)}

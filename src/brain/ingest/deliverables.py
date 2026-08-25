"""The ``inbox/_deliverables/`` drop lane (DLV-09).

Dropping a finished output in one folder is the whole capture gesture. The lane
ingests the payload exactly as the ordinary drop zone does, and ADDITIONALLY
writes one brain-zone anchor note carrying ``deliverable: true``, the project
taken from the subfolder name, and ``source: [[raw/<id>]]``.

Three properties this module exists to hold, each one a hardening finding:

* **Payload and anchor land together or not at all.** A raw source with no
  anchor is invisible to the shelf; a stranded anchor points at nothing. The
  archived original, the signed raw note and the anchor are three separate
  ordered stages, so a HOST-PRIVATE journal spans them and the NEXT run
  finishes what a kill interrupted. ``raw/`` is immutable, so recovery is
  FORWARD only — finish the anchor, never roll the archived original back. The
  journal is host-private for the supersede journal's reason: it is replayed
  through the audited ``write_note``, so a VM-writable one is an unsigned host
  write command.

* **The lane declares its own classification, and it governs ADMISSION.**
  ``pipeline._meta`` declares ``Internal`` for every drop-zone ingest and
  ``tierguard`` only ever RAISES against an existing higher-tier twin — so a
  UNIQUE synthesis would be admitted at ``Internal`` whatever any downstream
  note recorded, and the raw source is independently retrievable, so an
  Internal-capped reader (the Cowork VM) reaches it. The declared tier is read
  from a ``.classification`` control file and applied to the raw source AND the
  anchor BEFORE the guard runs. No file, or an unreadable one, means **MNPI**.

* **A byte-identical twin already sitting LOWER is refused, never quietly
  deduplicated.** The ordinary duplicate path files the drop aside and leaves
  the existing low copy exactly where it is — reachable at its low tier.

STATED LIMIT, because it is real and unguarded: the journal opens at
``tierguard_stage``, so a crash in the narrow window between ``_claim`` and
that stage leaves the payload in ``inbox/_processing/``, and the stale-claim
sweep returns it to the inbox ROOT — where the next run ingests it as an
ordinary source at the drop zone's ``Internal``, with no marker and no anchor.
Nothing is written in that window, so nothing is corrupted and no payload is
lost; what is lost is the drop's declared tier and its project. Re-dropping the
file into ``_deliverables/<project>/`` is the whole repair. Threading the
origin through the claim would close it, and is not worth the coupling until a
real crash lands there.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import classification as CLS

if TYPE_CHECKING:  # the stage record; a runtime import here would be a cycle
    from .pipeline_stages import ClaimRecord

#: The drop folder, and one optional level of ``<project>/`` beneath it.
DELIVERABLES_DIRNAME = "_deliverables"
#: The per-folder tier declaration. Most specific wins; absent/unreadable ⇒ MNPI.
CLASSIFICATION_FILENAME = ".classification"
#: DLV-09(d) — the AUTOMATIC half of the marker, stamped on every note this lane
#: creates and independent of ``deliverable:``. The marker is the judgment step;
#: this stamp is unconditional, which is what makes "produced but unmarked" a
#: countable set rather than an unknowable one.
PRODUCED_BY = "inbox-deliverables"
#: Fails to the most restrictive tier, NOT to the drop zone's ``Internal``.
DEFAULT_CLASSIFICATION = "MNPI"

_ANCHOR_SUBTREE = "brain/resources"


# ---------------------------------------------------------------------------
# scan + declared classification
# ---------------------------------------------------------------------------

def scan(inbox: Path) -> dict[Path, str | None]:
    """Payloads under ``_deliverables/``, each mapped to its project (or None).

    ONE level of nesting: ``_deliverables/<file>`` carries no project,
    ``_deliverables/<project>/<file>`` takes the folder name. Anything deeper is
    simply not seen — the premise is a folder an owner can scan in Finder, and a
    recursive walk here would silently ingest whole trees a drag-and-drop
    dropped by accident.
    """
    root = inbox / DELIVERABLES_DIRNAME
    if not root.is_dir() or root.is_symlink():
        return {}
    found: dict[Path, str | None] = {}
    for entry in sorted(root.iterdir()):
        if entry.name.startswith("."):
            continue
        if _ingestable(entry):
            found[entry] = None
        elif entry.is_dir() and not entry.is_symlink():
            for child in sorted(entry.iterdir()):
                if not child.name.startswith(".") and _ingestable(child):
                    found[child] = entry.name
    return found


def _ingestable(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def classification_for(path: Path, inbox: Path) -> str:
    """The tier THIS drop is admitted at — the most specific declaration wins.

    The file's own folder is consulted first, then ``_deliverables/`` itself.
    The FIRST control file that exists decides, even when its contents are
    unreadable or not a known tier: falling through a broken project-level
    declaration to a permissive root-level one would silently downgrade the drop
    it was written to protect (EXC-01 — a bad label is the ABSENCE of one, never
    an assertion of a lower tier).
    """
    for directory in (path.parent, inbox / DELIVERABLES_DIRNAME):
        control = directory / CLASSIFICATION_FILENAME
        if not control.is_file():
            continue
        try:
            declared = control.read_text(encoding="utf-8").strip()
        except OSError:
            return DEFAULT_CLASSIFICATION
        return declared if declared in CLS.TIERS else DEFAULT_CLASSIFICATION
    return DEFAULT_CLASSIFICATION


# ---------------------------------------------------------------------------
# the stage hooks — the whole lane lives here, `pipeline_stages` only calls it
# ---------------------------------------------------------------------------

def declare_tier(record: "ClaimRecord") -> None:
    """Put the LANE's declared tier on the frontmatter BEFORE the guard runs.

    ``pipeline._meta`` declares ``Internal`` for every drop-zone ingest and the
    guard only ever RAISES against an existing higher-tier twin, so a UNIQUE
    synthesis would otherwise be admitted at ``Internal`` however sensitive it
    is — and its raw source is independently retrievable, so an Internal-capped
    reader reaches it whatever the anchor says.
    """
    record.meta["classification"] = record.requested_classification
    record.meta["provenance.produced_by"] = PRODUCED_BY


def open_journal(record: "ClaimRecord") -> None:
    """Record the intent BEFORE the first of the three writes (archived
    original, signed raw note, anchor), so a kill anywhere across them leaves a
    forward-recovery record. ``anchor_for`` closes it on success; an entry whose
    raw note never landed is dropped by ``recover``."""
    journal_open(record.drain.vault, record.slug, _payload(record))


def anchor_for(record: "ClaimRecord", entry: dict[str, Any]) -> None:
    """Write the brain-zone anchor beside the raw source it marks.

    A failure here is DEFERRED, never raised: the raw note already landed and
    saying otherwise would misreport the run. The journal entry survives, so the
    next drain finishes the anchor — or reports the payload unanchored.
    """
    try:
        entry["deliverable_anchor"] = write_anchor(record.drain.core, _payload(record))
    except Exception as exc:  # noqa: BLE001 — reported, and retried next run
        entry["deliverable_anchor"] = f"deferred:{type(exc).__name__}: {exc}"
        return
    entry["project"] = record.project
    journal_close(record.drain.vault, record.slug)


def _payload(record: "ClaimRecord") -> dict[str, Any]:
    return {
        "slug": record.slug,
        "title": record.orig_name,
        "project": record.project,
        "classification": record.classification,
        "created": record.drain.today,
    }


# ---------------------------------------------------------------------------
# the cross-tier refusal
# ---------------------------------------------------------------------------

def low_twin_tier(vault: Path, existing_id: str, declared: str) -> str | None:
    """The existing note's tier when a byte-identical twin sits BELOW
    ``declared`` — the cross-tier conflict, named. ``None`` means no conflict.

    ONE definition, because two callers reach the same situation by different
    routes: the drain below, which quarantines the drop, and the absorption
    driver's already-present branch, which never drains at all and so would
    otherwise report the leak as a success.

    An existing note carrying NO recognised classification is a conflict too (a
    missing label is not an assertion that the twin is already high enough) and
    is reported as ``""`` — a conflict, not an absence of one.
    """
    from . import pipeline as facade

    existing = facade._existing_note_classification(vault, existing_id)
    if existing in CLS.TIERS and CLS.rank(existing) >= CLS.rank(declared):
        return None
    return existing or ""


def refuse_low_twin(record: "ClaimRecord", existing_id: str) -> "ClaimRecord | None":
    """Refuse a drop whose byte-identical twin sits BELOW its declared tier.

    ``None`` means "not a conflict — handle it as the ordinary duplicate it is".

    The ordinary duplicate path files the drop aside and leaves the existing
    note untouched, which keeps the low copy independently retrievable at its
    low tier — the exact leak the declared classification exists to prevent.
    Raising an already-signed note's tier is an audited owner act
    (``brain write``), never something a drop-zone scan performs by itself, so
    the lane refuses and names both sides instead.

    An existing note carrying NO recognised classification is a conflict too: a
    missing label is not an assertion that the twin is already high enough.
    """
    from . import pipeline as facade

    assert record.claimed is not None
    existing = low_twin_tier(
        record.drain.vault, existing_id, record.requested_classification)
    if existing is None:
        return None
    reason = "deliverable_tier_conflict"
    facade._quarantine(record.claimed, record.drain.quarantine_dir, reason, [
        f"identical content is already ingested as raw/{existing_id}.md at "
        f"{existing or '(no classification)'}, below this drop's declared "
        f"{record.requested_classification}. Ingesting now would file this drop "
        f"aside and leave the lower copy independently retrievable. Raise the "
        f"existing note through the audited path first, then re-drop.",
    ])
    record.append("quarantined", {
        "file": record.claimed.name,
        "reason": reason,
        "existing_id": existing_id,
        "existing_classification": existing,
        "declared_classification": record.requested_classification,
    })
    record.terminal = True
    return record


# ---------------------------------------------------------------------------
# the anchor note
# ---------------------------------------------------------------------------

def anchor_id(slug: str) -> str:
    return f"{slug}-deliverable"


def anchor_rel(slug: str) -> str:
    return f"{_ANCHOR_SUBTREE}/{anchor_id(slug)}.md"


# Extensions a produced document actually arrives with. Deliberately a closed
# list: a title like "Exec Review v4.1" has a dot-suffix too, and stripping
# ".1" from it would be worse than the filename it replaced.
_DOC_SUFFIXES = frozenset({
    ".md", ".txt", ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".html", ".htm", ".csv", ".rtf", ".odt",
})


def display_title(raw: str, slug: str) -> str:
    """A readable title for an anchor whose only name is a source FILENAME.

    The drop lane and the absorption driver both title an anchor from the file
    it came from, so the shelf README rendered `NORTHWIND2210_Rollout_Memo.docx` in
    the same column as written sentences. This turns the filename into prose
    WITHOUT inventing anything: the extension goes, `_` becomes a space, runs of
    whitespace collapse. Nothing else — the identifier in `NORTHWIND2210` is the part
    an owner searches for, so case and digits are left exactly as they are.

    A title that is not filename-shaped is returned untouched, so the 76 notes
    that carry a written title keep it.
    """
    text = (raw or "").strip()
    if not text:
        return slug
    stem, dot, suffix = text.rpartition(".")
    if dot and stem and f".{suffix.lower()}" in _DOC_SUFFIXES:
        text = stem
    text = " ".join(text.replace("_", " ").split())
    return text or slug


def anchor_text(entry: dict[str, Any]) -> str:
    """The anchor note's bytes.

    The BODY carries a bare ``[[<raw-id>]]`` citation, not the ``[[raw/<id>]]``
    form — that one belongs in ``source:`` frontmatter and creates no graph edge
    (BAK-04), so a body using it would leave the source counted as unlinked.
    """
    from .. import frontmatter as fm

    slug = str(entry["slug"])
    # ONE derivation for both the frontmatter title and the body heading — they
    # were separate expressions, and fixing only the first left the filename
    # rendering as the note's H1.
    title = display_title(str(entry.get("title") or ""), slug)
    project = entry.get("project")
    lines = [
        "---",
        f"id: {fm.yaml_scalar(anchor_id(slug))}",
        f"title: {fm.yaml_scalar(title)}",
        # Orthogonal to `type:`, never a member of its vocabulary — `type` is
        # single-valued and load-bearing (`type: decision` IS the decision
        # layer), so a produced document keeps whatever type it really is.
        "type: note",
        f"classification: {fm.yaml_scalar(entry['classification'])}",
        f"created: {fm.yaml_scalar(entry['created'])}",
        f"updated: {fm.yaml_scalar(entry['created'])}",
        "deliverable: true",
    ]
    if project:
        lines.append(f"project: {fm.yaml_scalar(project)}")
    lines.append(f"source: {fm.yaml_scalar(f'[[raw/{slug}]]')}")
    lines.append(f"provenance.produced_by: {fm.yaml_scalar(PRODUCED_BY)}")
    lines.append("---")
    body = [
        "",
        f"# {title}",
        "",
        "A final output produced from vault content — the payload is the",
        "immutable archived original; this note carries the marker and any",
        "commentary the output needs.",
        "",
    ]
    if project:
        body.append(f"Project: [[{project}]]")
    # ponytail: a drop with no `<project>/` subfolder links only the raw source,
    # which is outside the brain-zone knowledge graph — so it registers as a
    # graph orphan (GRH-01). Using the project subfolder is the fix; inventing a
    # link target here would trade an orphan for a dangling link.
    body.append(f"Source: [[{slug}]]")
    body.append("")
    return "\n".join(lines) + "\n".join(body)


def write_anchor(core: Any, entry: dict[str, Any]) -> str:
    """Commit the anchor through the audited host write path. Returns its path."""
    rel = anchor_rel(str(entry["slug"]))
    core.write_note(
        rel,
        anchor_text(entry),
        reason=(
            f"deliverable anchor for raw/{entry['slug']}.md "
            f"(dropped in inbox/{DELIVERABLES_DIRNAME}/)"
        ),
        subtree=_ANCHOR_SUBTREE,
    )
    return rel


# ---------------------------------------------------------------------------
# the host-private recovery journal
# ---------------------------------------------------------------------------

def journal_path(vault: Any) -> Path:
    """OFF THE MOUNT, and for the supersede journal's reason: this file is
    replayed through the audited ``write_note``, so a VM-writable one would be
    an unsigned host write command (it names an id, a tier and a project the
    host would then sign). Falls back to the app-data base exactly as
    ``host_lock_dir`` does, which is host-controlled by construction."""
    from .. import config, config_hostpaths as hp

    try:
        directory = hp.proven_off_mount(
            hp.host_private_base() / "deliverables", vault,
            what="deliverable anchor journal store",
        )
    except hp.HostPathUnsafe:
        directory = hp.proven_off_mount(
            config._app_data_base() / "deliverables", vault,
            what="deliverable anchor journal store (app-data fallback)",
        )
    return directory / f"pending-{config.vault_slug8(vault)}.json"


def _load(vault: Any) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(journal_path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store(vault: Any, pending: dict[str, dict[str, Any]]) -> None:
    from .. import config

    path = journal_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    config.secure_file_permissions(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(pending, indent=2, sort_keys=True), encoding="utf-8")
    config.secure_file_permissions(tmp, 0o600)
    tmp.replace(path)


def journal_open(vault: Any, slug: str, entry: dict[str, Any]) -> None:
    """Record the intent BEFORE the first of the three writes."""
    pending = _load(vault)
    pending[slug] = entry
    _store(vault, pending)


def journal_close(vault: Any, slug: str) -> None:
    pending = _load(vault)
    if pending.pop(slug, None) is not None:
        _store(vault, pending)


def recover(core: Any, report: dict[str, Any]) -> None:
    """Finish any anchor a kill interrupted, or report the payload unanchored.

    Forward only. Three outcomes per journal entry:

    * the raw note never landed, or was not written by THIS lane (a re-drop
      deduplicated against an older ordinary ingest, an id collision) — nothing
      to finish, the entry is dropped;
    * the anchor already exists — the entry is dropped;
    * otherwise the anchor is written now, or the payload is reported
      ``anchored: false`` with the reason and the entry is KEPT for the next run.
    """
    from .. import frontmatter as fm

    vault = core.vault
    pending = _load(vault)
    if not pending:
        return
    done: list[str] = []
    for slug, entry in sorted(pending.items()):
        if not _lane_wrote(fm, vault / "raw" / f"{slug}.md"):
            done.append(slug)
            continue
        if (vault / anchor_rel(slug)).is_file():
            done.append(slug)
            continue
        try:
            rel = write_anchor(core, entry)
        except Exception as exc:  # noqa: BLE001 — reported, never swallowed
            report.setdefault("deliverables", []).append({
                "id": slug,
                "anchored": False,
                "reason": f"anchor_write_failed:{type(exc).__name__}: {exc}",
            })
            continue
        done.append(slug)
        report.setdefault("deliverables", []).append({
            "id": slug, "note": rel, "anchored": True, "recovered": True,
        })
    if done:
        pending = _load(vault)
        for slug in done:
            pending.pop(slug, None)
        _store(vault, pending)


def _lane_wrote(fm: Any, note: Path) -> bool:
    """Whether ``note`` is a raw source THIS lane signed — read from its own
    ``provenance.produced_by`` stamp rather than from the journal's say-so, so a
    journal entry can never make the recovery pass anchor somebody else's note."""
    try:
        meta, _ = fm.parse_text(note.read_text(encoding="utf-8"))
    except OSError:
        return False
    return str(meta.get("provenance.produced_by") or "") == PRODUCED_BY

"""The ``note_body_dump`` detector: note-shaped Markdown ANYWHERE.

Split out of :mod:`brain.cowork_leak_scan` on 2026-08-30, along the same seam
:mod:`brain.cowork_leak_databases` was taken on 2026-08-29 and for the same
reason: one artefact class, one content sniff, one file. The parent module keeps
the walk and the six other classes. Nothing here knows what to DO about a hit --
:mod:`brain.cowork_staging` refuses, :mod:`brain.doctor_mount_leak` reports.
"""
from __future__ import annotations

import stat
from pathlib import Path

from .cowork_leak_artifact import Artifact


_NOTE_SUFFIXES = (".md", ".markdown", ".txt")
_FRONTMATTER_MARK = b"---"
_FRONTMATTER_BYTES = 4096

# The ADR-0010 generated shelf. Its own header declares "Highest classification
# here: MNPI" and its payload is the archived ORIGINAL each note was made from,
# so it is a leak of the same data by a shorter route --- and it carries no note
# frontmatter at all, so `_scan_note_dumps` cannot see it. Detected by name
# because ADR-0010 is what chooses that name and that location.
def _is_note_file(path: Path) -> bool:
    """True if ``path`` opens with VAULT NOTE frontmatter, not merely Markdown.

    The discriminator is ``id:`` plus ``classification:`` or ``type:``. That is
    what separates a note body from the Markdown a workspace legitimately holds
    --- ``SKILL.md`` frontmatter carries ``name:``/``description:``, a README
    carries none.

    A TEMPLATE is not a note body, and it is told apart by its ``id`` VALUE.
    ``templates/decision.md`` opens ``id: "{{id}}"`` --- correct note shape,
    zero note content. MEASURED 2026-08-29 on the cut-over workspace: the
    engine's own ``_assets/templates/`` ships 9 such files, so without this the
    scan reports a ``note_body_dump`` on EVERY Cowork workspace by construction.
    A permanent row is a row an operator learns to ignore, which is the same as
    not having one. The discriminator is the placeholder ``{{`` in the id --- a
    fact about the file's CONTENT, like the ``__init__.py`` rule in
    :func:`_zone_yields_bodies`, not a path or a name someone can choose. A
    leaked note always carries a real id.

    STATED LIMIT, and it is a real one: the frontmatter must OPEN the file.
    A note body with arbitrary bytes prepended before its ``---`` is not
    recognised. That is a deliberate transform rather than the drift this check
    exists to catch (a reinstall, a restage, a manual copy), and recognising it
    would mean scanning whole file bodies on every ``brain doctor``.
    """
    try:
        # REGULAR FILES ONLY, and this MUST precede the open(). `os.walk`
        # lists FIFOs and device nodes under `filenames`, and `open("rb")` on a
        # FIFO with no writer blocks forever: nothing raises, so the `except
        # OSError` below never fires, and the budget is checked between
        # directories so `ScanBudgetExceeded` never fires either. `brain doctor`
        # HANGS rather than failing. Reachable since the suffix stopped gating.
        # `lstat`: symlinks belong to `_scan_escaping_symlinks`. Full rationale
        # and the timing proof: tests/test_cowork_leak_scan_note_dumps.py
        # ::test_a_fifo_in_the_workspace_does_not_hang_the_scan
        if not stat.S_ISREG(path.lstat().st_mode):
            return False
        with path.open("rb") as fh:
            # The cheap rejection first, on CONTENT rather than on the name: a
            # file that does not open with `---` cannot carry frontmatter, and
            # three bytes settle it. This is what lets the suffix stop being a
            # gate without paying 4 KiB for every file in the workspace.
            if fh.read(len(_FRONTMATTER_MARK)) != _FRONTMATTER_MARK:
                return False
            fh.seek(0)
            head = fh.read(_FRONTMATTER_BYTES).decode("utf-8", "replace")
    except OSError:
        return False
    end = head.find("\n---", 3)
    body = head[3:end] if end != -1 else head[3:]
    fields: dict[str, str] = {}
    for line in body.splitlines():
        if ":" not in line or line[:1].isspace():
            continue
        key, value = line.split(":", 1)
        fields.setdefault(key.strip(), value.strip())
    if "id" not in fields or not (set(fields) & {"classification", "type"}):
        return False
    return "{{" not in fields["id"]


def _scan_note_dumps(root: Path, here: Path, filenames: list[str],
                     covered: set[Path]) -> list[Artifact]:
    """(6) a directory of note-shaped Markdown ANYWHERE under the workspace.

    Proves the leak from the FRONTMATTER, not from a directory name, which is
    the whole point: ``_scan_note_zones`` can only see a folder someone called
    ``brain`` or ``raw``, and a copy is rarely so considerate. MEASURED on the
    live reference mount 2026-08-29, this is what stands between the cutover and
    a green check over 46 note-shaped files in ``<workspace>/migration/``.

    ``covered`` holds the note zones this scan has ALREADY REPORTED, and a
    directory at or under one of them is skipped --- reporting ``vault/brain``
    seven more times, once per subdirectory, is a row an operator learns to
    ignore. The dedupe is on what was reported, never on a NAME.

    That distinction is the whole fix. Until 2026-08-29 this skipped any
    directory called ``brain``/``raw`` or sitting under one, on the theory that
    :func:`_scan_note_zones` owned them. It does not: that check declines a
    Python package (see :func:`_zone_yields_bodies`), and the staged engine
    lives at ``<workspace>/vault/.brain/engine/brain`` in EVERY Cowork
    workspace by construction. So the exemption handed ownership to a check
    that refuses it, and left a permanent blind region shaped exactly like the
    exemption. PROVED live on the cut-over workspace: a Restricted note at
    ``vault/.brain/engine/brain/leak.md`` scanned as 0 artefacts while the
    identical note one directory over scanned as 1.
    """
    if here in covered or any(parent in covered for parent in here.parents):
        return []
    # EVERY regular file is a candidate; the suffix only decides the ORDER.
    # Filtering on the suffix here was the same blind spot as filtering on it
    # inside `_is_note_file` -- `note.md.bak` never reached the content test.
    cands = sorted(filenames,
                   key=lambda f: (Path(f).suffix.lower() not in _NOTE_SUFFIXES, f))
    if not cands:
        return []
    seen = 0
    hit = None
    for f in cands:
        seen += 1
        if _is_note_file(here / f):
            hit = f
            break
    if hit is None:
        return []
    # `seen`, not `len(cands)`: the loop STOPS at the first hit, so on a
    # directory of 6000 files whose first candidate matches, the old string told
    # the operator 6000 files were inspected when one was. This row is evidence
    # an operator acts on; a count in it is a measurement claim like any other.
    return [Artifact("note_body_dump", here,
                     f"{seen} of {len(cands)} file(s) inspected; {hit!r} carries vault note "
                     "frontmatter (id + classification/type), readable by path, "
                     "by glob and by content search")]

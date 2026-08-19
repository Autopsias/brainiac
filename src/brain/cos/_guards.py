"""COS path-guard operations."""
from __future__ import annotations

from ._shared import *  # noqa: F401,F403

# ===========================================================================
# INT-01 / R3 — MOUNT-RESIDENT DATA THAT BECOMES A FILESYSTEM PATH
# ===========================================================================
# One defect CLASS produced a finding in FOUR consecutive review rounds, at a
# different site each time: a value the VM can write is used as (or to build)
# a path, and the host then opens / moves / renames / unlinks it. Guarding the
# sites a reviewer happened to report is what failed twice. This is the
# ENUMERATION the guards are derived from.
#
# WHAT THE TEST BINDS: ``tests/test_cos_pathguard.py`` fails if a `brain.cos`
# function (any module of this package, module-level parses included) parses
# JSON off disk (``json.loads``/``json.load``/a ``JSONDecoder``, ``_read_jsonl``
# or any other ``_read_*`` helper) and is not classified in its CENSUS; a
# GUARDED classification is checked by TAINT, not by a source grep — a parsed
# field may not reach a path expression except through a guard. Both checks
# are themselves probed with known-positive fixtures, so a detector that
# stopped detecting fails instead of reporting clean. THE LIVE READER->GUARD
# REGISTRY IS THAT TEST'S ``CENSUS`` TABLE — the registry is not duplicated
# here, because two copies of it is how the last one drifted.
#
# WHAT IT STILL DOES NOT BIND (stated so nobody reads the census as stronger
# than it is): reader-hood does not propagate transitively past a ``_read_*``
# helper. Measured on the pre-split monolith, the full call-graph closure
# would have classified 81 functions instead of 40 — the extra 41 are
# orchestration (`hold_cancel`, `status_block`, `calibration_report`, …)
# whose "classification" would be "guarded by its callee", which is noise,
# not a property. The bound is the helper hop, and it is enforced: a helper
# that parses must be named ``_read_*`` (test), and every caller of one is a
# reader.
#
# CLOSED IN INT-04/INT-05 (was: five tracked gaps here):
#   - the ATTACHMENT LANE has its signed anchor (`stage_attachment_anchor`,
#     verified in the ingest drain against the buffer it is about to sign);
#   - ``_safe_meta_path`` is GONE. It was resolve-then-use and therefore
#     raceable; rather than reopen-with-an-inode-check around it, the surface
#     was removed — a mount-written path field is reduced to its last component
#     (`_safe_basename`) and joined onto a HOST-derived root, and the
#     attachment payload is derived from the guarded id + the real directory
#     entry (`_quarantine_payload`) instead of being read at all;
#   - the SINGLE-WRITER lock moved off the mount with the append locks
#     (``config.writer_lock_path`` -> ``config.host_lock_dir()``);
#   - the no-raw-write gate now inspects ``os.open`` FLAGS (and refuses a
#     non-literal mode), instead of exempting every ``os.open``;
#   - the shared-zone permission test drives ``write_run_manifest`` and asserts
#     the fchmod-before-replace ORDERING, so deleting the production ``mode=``
#     argument fails it.
#
# Everything under ``<vault>/.brain/`` is on the Cowork VirtioFS mount, so
# every sidecar/marker/manifest/ledger this package reads is
# attacker-writable. Two field classes matter, and each has exactly ONE guard:
#
#   ``id``-like   -> `_safe_meta_id`  : a bare slug, length-capped in ENCODED
#                                       BYTES (it becomes ``<dir>/<id>.<ext>``;
#                                       an over-long one raises ENAMETOOLONG at
#                                       the write and wedges apply/recovery)
#   ``path``-like -> NOT USED AS A PATH: reduced to `_safe_basename` and joined
#                                       onto a host-derived root (`_leaf_in`),
#                                       or ignored entirely in favour of the
#                                       real directory entry
#                                       (`_quarantine_payload`)
#   ``filename``  -> `_unique_dest`   : a bare filename (it is a move
#                                       DESTINATION joined onto an inbox root).
#                                       On the ATTACHMENT release path it is not
#                                       taken from the sidecar at all any more
#                                       (INT-04 round 3): the destination name
#                                       comes from the SIGNED batch row or hold
#                                       authorization, because the suffix picks
#                                       the ingest handler and an authorization
#                                       over bytes alone left that choice to the
#                                       mount.
#
# And no raw writes: EVERY write in this package goes through `_write_atomic`
# (unpredictable temp, O_CREAT|O_EXCL|O_NOFOLLOW, write-until-complete, cleanup
# on any failure). A predictable ``.tmp`` on the mount was found twice — first
# at ``batches.jsonl``, then at ``<run-id>.validity.json`` — so the rule is now
# the whole package, and `test_no_raw_write_remains_on_a_mount_path` enforces
# it across every module of this package plus ``tools/cos_mutate.py``.
# ===========================================================================
def _safe_meta_id(m: Any) -> str | None:
    """The sidecar's ``id``, ONLY if it is a bare slug — else ``None``.

    Every one of these sidecars lives under ``.brain/`` on the shared mount and
    every reader turns its ``id`` into a PATH (``<dir>/<id>.md``,
    ``<id>.refused.json``, …). A marker carrying
    ``id: "../../../../brain/resources/pwned"`` wrote a real attacker-named file
    inside the vault. Guarding the readers — not one call site — is what makes
    that true for the callers too, present and future."""
    if not isinstance(m, dict):
        return None
    try:
        return safe_slug(str(m.get("id") or ""))
    except ValueError:
        return None

def _safe_basename(value: Any) -> str | None:
    """The BARE FILENAME in ``value``, or ``None`` if there isn't one.

    The replacement for the old ``_safe_meta_path`` (INT-05). That guard took a
    mount-written path, resolved it, and checked the RESULT was inside an
    allowed root — a resolve-then-use check with a real window between them:
    rename the checked directory and substitute a symlink, and the move/unlink
    that followed acted on something else entirely. Narrowing that window is
    not a fix, so the surface is gone instead: a mount-written path field is
    now reduced to its last component and joined onto a root the HOST derives,
    which cannot name anything outside that root at any point in time. No
    resolve, no comparison, nothing to race.

    Separators are refused rather than stripped on both platforms' spellings,
    so ``a\\b`` cannot become a filename on POSIX either."""
    name = str(value or "")
    if not name or name in (".", ".."):
        return None
    if (os.sep in name or (os.altsep and os.altsep in name)
            or "/" in name or "\\" in name):
        return None
    if Path(name).name != name or Path(name).is_absolute():
        return None
    return name

def _move_dirent(src: Path, dest: Path) -> bool:
    """Move a DIRECTORY ENTRY, never following a symlink at the leaf.

    ``os.replace`` acts on the entry itself, so a link planted at a derived
    name travels as a link and is never dereferenced. Only the cross-device
    fallback (``shutil.move``, which copies through a link) could exfiltrate
    what such a link points at, so that fallback is refused for one — the
    quarantine, expired and inbox trees are on one filesystem in every
    supported layout, and a link there is not the payload anyway."""
    try:
        os.replace(src, dest)
        return True
    except OSError:
        if src.is_symlink():
            return False
        shutil.move(str(src), str(dest))
        return True

def _leaf_in(root: Path, value: Any) -> Path | None:
    """``root/<basename of value>``, but only if it is a regular file today.

    ``is_symlink`` is checked explicitly: ``is_file()`` FOLLOWS links, so a
    link planted at the derived name would otherwise pass as a file and hand a
    move/unlink someone else's inode."""
    name = _safe_basename(Path(str(value or "")).name)
    if name is None:
        return None
    p = Path(root) / name
    try:
        if p.is_symlink() or not p.is_file():
            return None
    except OSError:
        return None
    return p

def _read_receipt_pairs(d: Path) -> tuple[list[dict[str, Any]], int]:
    """Every READABLE ``<id>.json`` + ``<id>.md`` pair in ``d``, and how many
    pairs were UNREADABLE or INCOMPLETE.

    ONE SCANNER, TWO DIRECTORIES, AND IT COUNTS WHAT IT COULD NOT READ (review
    2026-08-13, round 5, H6). Both readers used to `continue` past a meta that
    would not parse, so a corrupt or half-written receipt was indistinguishable
    from a directory with nothing in it. That absence is what
    ``run_proposal_drops`` reports as a count, and what K2's
    ``check_candidate_stamps`` reads as "the HOST's own record agrees: zero
    drops" — so a run denying a drop it made passed the control by damaging the
    receipt. Unreadable evidence is not absence; it is unreadable evidence, and
    the number of it has to come back with the answer.

    Four ways a pair fails, all counted the same because the caller's decision
    is the same: the meta will not parse, it carries no usable id, its ``.md``
    half is missing, or its ``.json`` half is missing — BOTH directions of a
    partially-written or partially-deleted pair.

    SCAN THE UNION OF STEMS, not just ``*.json`` (review 2026-08-13, round 6,
    H-md). The producer writes the ``.md`` before the ``.json``, so a crash
    between the two atomic writes leaves a ``.md`` with NO ``.json`` — and
    iterating ``*.json`` alone never sees it, returning a clean ``0`` that reads
    as "the HOST's own record agrees: zero drops" and reopens H6's fail-open in
    that partial-write window. A missing half in either direction is an
    incomplete pair.
    """
    out: list[dict[str, Any]] = []
    malformed = 0
    if not d.is_dir():
        return out, 0
    stems = sorted({p.stem for p in d.glob("*.json")}
                   | {p.stem for p in d.glob("*.md")})
    for stem in stems:
        meta_path = d / f"{stem}.json"
        try:
            m = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A `.md`-only orphan (no `.json` to open → FileNotFoundError) or a
            # `.json` that will not parse — both are an unreadable/incomplete
            # pair, counted the same.
            malformed += 1
            continue
        nid = _safe_meta_id(m)
        # THE PAIR IS ONE STEM, not two files that happen to both exist (review
        # 2026-08-13, round 7). `a.json` carrying `{"id": "b"}` beside a real
        # `b.md` used to be ACCEPTED — the `.md` existence test asked about the
        # EMBEDDED id, so a receipt could claim another receipt's body and be
        # returned as usable to `_pending_metas`/`quarantined_claims` while its
        # own body was missing. A receipt names itself or it is malformed.
        if not nid or nid != stem or not (d / f"{nid}.md").exists():
            malformed += 1
            continue
        out.append({**m, "id": nid})
    return out, malformed

def _unique_dest(inbox: Path, filename: str) -> Path:
    """A free name for ``filename`` INSIDE ``inbox`` — never outside it.

    R4: ``attachment_metas`` guards the sidecar's ``id`` and its ``path``, but
    ``filename`` is a THIRD mount-controlled field, and `_accept_attachment`
    hands it straight to this join. An absolute or traversing filename whose
    destination did not already exist made the caller's ``shutil.move`` write
    outside ``vault/inbox``. The guard belongs HERE, at the join every caller
    routes through, not at the one call site a reviewer happened to reach.

    The bare-name rule itself is `_safe_basename` — ONE definition, shared with
    the lifecycle/anchor lanes, so the three of them cannot drift apart."""
    name = _safe_basename(filename)
    if name is None:
        raise ValueError(
            f"unsafe destination filename {str(filename)[:60]!r}: must be a "
            f"bare filename (no separators, no '.'/'..', not absolute)")
    filename = name
    dest = inbox / filename
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    for i in range(2, 1000):
        cand = inbox / f"{stem}-{i}{suffix}"
        if not cand.exists():
            return cand
    raise ValueError(f"cannot uniquify destination for {filename!r}")

__all__ = ['_safe_meta_id', '_safe_basename', '_move_dirent', '_leaf_in', '_read_receipt_pairs', '_unique_dest']

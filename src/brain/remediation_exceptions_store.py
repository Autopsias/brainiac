"""EXC-01 — where the cross-tier proposals live.

The durable half of :mod:`brain.remediation_exceptions`, split out at its own
section banner so the lane's *behaviour* and the lane's *storage* are readable
apart. Nothing here decides anything: it opens files, refuses records that name
another vault, and appends to the two append-only ledgers.

``config.index_dir()/remediation/exceptions/`` — inside the directory
:mod:`brain.remediation_state` already proves off the VM mount and binds to
this vault by resolved path. A proposal names two notes and their tiers, so it
is precisely the kind of record the VM must be unable to read or forge; a
record naming another vault is refused exactly as the branch state is.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from . import remediation_state as _rs
from .remediation_answers import unlabelled_ids

#: BUMPED (llm-review, s06 HIGH finding). Behaviour changed under this same
#: string once already: an earlier build of THIS feature normalized a pair's
#: tier fields before storing them, so a member with no classification was
#: written as the literal string ``"MNPI"`` — indistinguishable, once on disk,
#: from a note genuinely classified MNPI. `unanswerable_pair` reads the STORED
#: value, so that proposal survived `retire_unanswerable`'s tier check and an
#: ACCEPT on it raised a correctly-labelled note to MNPI on the strength of a
#: MISSING label. The store already carries this version field; the fix is to
#: make an on-disk mismatch REFUSE the stored tier rather than trust it — see
#: :func:`stale_pending` and ``remediation_exceptions.retire_stale_schema``.
SCHEMA = "remediation_exceptions/v2"

DIRNAME = "exceptions"
PENDING_DIRNAME = "pending"
LEDGER_FILENAME = "ledger.jsonl"
RUNS_FILENAME = "runs.jsonl"

#: The three ledger states that RETIRE a proposal. ``answered`` is the
#: OWNER's decision; ``unanswerable`` is the tier rule's; ``stale-schema`` is
#: a proposal an older format wrote whose stored tier this build refuses to
#: trust. Only ``answered`` counts as decided below — a refused pair comes
#: back the moment it is labelled (or re-detected under the current schema).
ANSWERED_STATE = "answered"
UNANSWERABLE_STATE = "unanswerable"
STALE_SCHEMA_STATE = "stale-schema"


def exceptions_dir(vault: str | os.PathLike[str]) -> Path:
    return _rs.remediation_dir(vault) / DIRNAME


#: Where the unlabelled note ids are listed for the OWNER. Host-private (under
#: the index dir, off the Cowork mount) and rewritten whole each run, same
#: posture as the exceptions token map: nothing outside the current run needs a
#: stale copy, and an id that stopped being unlabelled must stop being listed.
UNLABELLED_IDS_FILENAME = "unlabelled-note-ids.txt"


def write_unlabelled_ids(vault: Path, pairs: Mapping[str, Any]) -> str | None:
    """Write the unlabelled ids where the owner can read them; return the path
    for the finding text, or ``None`` when there is nothing to list or the
    write fails.

    Best-effort by design: a listing that cannot be written must not fail the
    run, and the finding then reports its count alone exactly as before.
    """
    ids = unlabelled_ids(pairs)
    try:
        # `exceptions_dir` is inside the try on purpose: resolving the
        # host-private store is itself a filesystem act, and an unreachable
        # store must cost this run its LISTING, never its finding.
        path = exceptions_dir(vault) / UNLABELLED_IDS_FILENAME
        if not ids:
            path.unlink(missing_ok=True)
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(ids) + "\n", encoding="utf-8")
    except OSError:
        return None
    return str(path)


def pair_key(a: str, b: str) -> str:
    """The identity of a PAIR, direction-free — the same two notes reported by
    two detectors are one exception, not two."""
    return "|".join(sorted((str(a), str(b))))


def _read_records(path: Path, vault: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Every well-formed record in a JSONL file that names THIS vault.

    A record naming another vault is refused for the reason
    ``remediation_state`` refuses one: ``index_dir`` is keyed on a file the VM
    can rewrite, so a foreign store must never be able to claim this vault's
    decisions."""
    binding = _rs.vault_binding(vault)
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get("schema_version") == SCHEMA
                and row.get("vault") == binding):
            out.append(row)
    return out


def _append(path: Path, vault: str | os.PathLike[str], row: Mapping[str, Any]) -> None:
    payload = {"schema_version": SCHEMA, "vault": _rs.vault_binding(vault), **dict(row)}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def _write_proposal(vault: str | os.PathLike[str], meta: Mapping[str, Any]) -> None:
    d = exceptions_dir(vault) / PENDING_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{meta['id']}.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(dict(meta), indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def read_pending(vault: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Every staged, undecided proposal — the FULL exception set, not the
    capped queue. This is what the exceptions page renders."""
    binding = _rs.vault_binding(vault)
    out: list[dict[str, Any]] = []
    try:
        files = sorted((exceptions_dir(vault) / PENDING_DIRNAME).glob("*.json"))
    except Exception:  # noqa: BLE001 — an unreachable store holds nothing
        return out
    for path in files:
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(meta, dict) and meta.get("schema_version") == SCHEMA
                and meta.get("vault") == binding and meta.get("id")):
            out.append(meta)
    return out


def stale_pending(vault: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Every pending file bound to this vault whose schema PREDATES ``SCHEMA``.

    ``read_pending`` already refuses to hand one of these to ``ask_pending``/
    ``accept_pair`` — an older format may have already NORMALIZED its tier
    fields before they were stored, and a stale ``"MNPI"`` is unrecoverable
    from the bytes alone (indistinguishable from a real one once written). So
    this is the ONLY way such a file is ever found again: not to trust it,
    but to garbage-collect it — left alone it would sit in ``pending/``
    forever, seen by nothing."""
    binding = _rs.vault_binding(vault)
    out: list[dict[str, Any]] = []
    try:
        files = sorted((exceptions_dir(vault) / PENDING_DIRNAME).glob("*.json"))
    except Exception:  # noqa: BLE001 — an unreachable store holds nothing
        return out
    for path in files:
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(meta, dict) and meta.get("vault") == binding
                and meta.get("id") and meta.get("pair_key")
                and meta.get("schema_version") != SCHEMA):
            out.append(meta)
    return out


def ledger(vault: str | os.PathLike[str]) -> list[dict[str, Any]]:
    return _read_records(exceptions_dir(vault) / LEDGER_FILENAME, vault)


def decided_pair_keys(vault: str | os.PathLike[str]) -> set[str]:
    """Pairs the owner has ANSWERED, and only those.

    Deliberately not the version-link definition, which counts an expired
    proposal as decided. Here an unanswered exposure is still an exposure —
    see the module docstring."""
    return {str(row.get("pair_key")) for row in ledger(vault)
            if row.get("state") == ANSWERED_STATE and row.get("pair_key")}


def _record(vault: str | os.PathLike[str], pair: str, state: str, **extra: Any) -> None:
    _append(exceptions_dir(vault) / LEDGER_FILENAME, vault,
            {"pair_key": pair, "state": state, **extra})


def retire(
    vault: str | os.PathLike[str], meta: Mapping[str, Any], state: str, **extra: Any,
) -> bool:
    """Remove ONE proposal from ``pending/`` and record why. ``False`` means it
    is STILL PENDING and nothing was recorded.

    The order is load-bearing and is the 2026-08-21 fix: recording the ledger
    row first left a proposal on disk that the ledger already called settled,
    and the lane re-asked an answered question every hour."""
    try:
        (exceptions_dir(vault) / PENDING_DIRNAME / f"{meta['id']}.json").unlink()
    except FileNotFoundError:
        pass        # already retired; the ledger row below is the record
    except OSError:
        return False
    _record(vault, str(meta["pair_key"]), state, id=meta["id"], **extra)
    return True

"""The vault gates and ledger readers of `cos_mutate` — kill switch, canary, stop files, windowing

Moved verbatim out of `cos_mutate` (batch-2 drain); every name is re-imported
by the parent so its `cos_mutate` module path is unchanged.
"""
from __future__ import annotations

import datetime as _dt

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cos_driver as drv                                     # noqa: E402
from cos_mutate_policy import (  # noqa: E402
    ABSENT_SKIP_FLOOR, ABSENT_SKIP_FRACTION, RECEIPT_KEYS)
import unicodedata


#: The mutation lane this module elects. E17 canaries are per-lane and a canary
#: for a different lane does NOT satisfy the gate.
MUTATION_LANE = "rest"
PRIMITIVE = {"archive": "rest-conversation-move",
             "categorize": "rest-categorize",
             "draft": "rest-create-draft"}



def absent_skip_cap(planned: int) -> int:
    """The ceiling on conclusive-absence skips for a plan of `planned` rows."""
    return max(ABSENT_SKIP_FLOOR, int(planned * ABSENT_SKIP_FRACTION))



def _fold(value: Any) -> str:
    """NFC + casefold, for the ONE comparison in the receipts rule.

    THE PAGE DERIVES THE FOLDER NAMES ITSELF and the ledger hard-codes them:
    `prepareArchive` computes `var source = "inbox"` (lowercase,
    `cos_mutate_page.js:1496-1497`) while `_undo_row` writes
    `"original_folder": "Inbox"`. Exact string equality matches NEITHER, so the
    rule as first written refused the archive lane's own receipts on the HAPPY
    PATH and would have stopped an applying night on its first archive. Both
    specified tests were known negatives, so neither could catch it: the rule
    had no known positive. It has one now
    (`test_a_real_archive_receipts_payload_passes_unchanged`).
    """
    return unicodedata.normalize("NFC", str(value)).casefold()


def receipts_shape_ok(receipts: Any, row: dict[str, Any]) -> bool:
    """`None`, or a flat mapping of known keys whose values are `bool`, `int`,
    `None`, or a string equal — NFC + casefold — to this row's own
    `original_folder` or `destination_folder`. That leaves no string a model
    could author."""
    if receipts is None:
        return True
    if not isinstance(receipts, dict):
        return False
    # An ABSENT folder is not a match target: `{""}` would let an
    # attacker-authored empty string through on a row that names no folder.
    folders = {_fold(v) for v in (row.get("original_folder"),
                                  row.get("destination_folder")) if v}
    for key, value in receipts.items():
        if key not in RECEIPT_KEYS:
            return False
        if isinstance(value, bool) or value is None or isinstance(value, int):
            continue
        if isinstance(value, str) and _fold(value) in folders:
            continue
        if key == "refused" and value == "shape":
            continue
        return False
    return True



def draft_form(value: Any) -> str:
    """`value` projected onto `DRAFT_FORMS`, else `unspecified`."""
    v = str(value or "").strip().casefold()
    return v if v in DRAFT_FORMS else "unspecified"


class MutationStop(drv.DriverStop):
    """A condition the mutation module refuses to run past."""


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _ts(dt: _dt.datetime | None = None) -> str:
    return (dt or _utcnow()).astimezone(_dt.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def short(value: str) -> str:
    return drv.short(value)


# ---------------------------------------------------------------------------
# preflight: the vault, the kill switch, the canary
# ---------------------------------------------------------------------------
def assert_vault(vault: Path) -> dict[str, Any]:
    """G0. Every COS artifact lives in the deployment's own vault, and this
    repository has no `vault/cos-ops/` at all — an unqualified relative path
    resolves to nothing here, and a `brain cos-*` command run with the wrong CWD
    writes a stray run into the wrong vault (measured: a phantom run-999
    manifest). So the root is ASSERTED, with the numbers it was asserted on."""
    from brain import cos                                        # noqa: PLC0415

    if not vault or not vault.is_dir():
        raise MutationStop(
            f"$BRAIN_VAULT / --vault names no directory ({vault!r}). Set it to "
            "the COS deployment's vault before anything else.")
    ops = cos.run_ops_dir(vault)
    if not ops.is_dir():
        raise MutationStop(f"{ops} does not exist — this is not a COS vault")
    ledgers = sorted(p.name for p in ops.glob("_cos_ingestion_ledger_*.jsonl"))
    if not ledgers:
        raise MutationStop(
            f"{ops} holds no ingestion ledger at all — a mutation pass has "
            "nothing to act on and this is almost certainly the wrong vault")
    return {
        "BRAIN_VAULT": str(vault),
        "raw_source_count_at_preflight": len(list((vault / "raw").rglob("*.md"))),
        "cos_ops_exists": True,
        "ingestion_ledgers_observed": ledgers[-4:],
        "asserted_before_any_browser_action": True,
    }


def kill_switch(vault: Path) -> dict[str, Any]:
    """`overlay/cos/auto-archive.md`. Absent ⇒ enabled; unparseable ⇒ DISABLED.

    The overlay is the owner's lever and it is read literally, never inferred.
    """
    path = vault / "overlay" / "cos" / "auto-archive.md"
    if not path.exists():
        return {"enabled": True, "source": str(path), "state": "absent-defaults-on"}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"enabled": False, "source": str(path),
                "state": f"unreadable ({exc}) — a kill switch that cannot be "
                         "read is OFF"}
    enabled = True
    cap = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("enabled:"):
            enabled = s.split(":", 1)[1].strip().lower() == "true"
        elif s.startswith("cap:"):
            try:
                cap = int(s.split(":", 1)[1].strip())
            except ValueError:
                cap = None
    return {"enabled": enabled, "cap": cap, "source": str(path), "state": "read"}


def stop_file(vault: Path, run_id: str) -> Path:
    from brain import cos                                        # noqa: PLC0415
    return cos.run_ops_dir(vault) / f"_cos_mutation_stop_{run_id}"


def stopped(vault: Path, run_id: str) -> bool:
    """The emergency brake, re-read BETWEEN every mutation. `touch` the file and
    the pass stops after the mutation in flight, not at the end of the batch."""
    return stop_file(vault, run_id).exists()


def canary_status(vault: Path, *, lane: str = MUTATION_LANE,
                  now: _dt.datetime | None = None) -> dict[str, Any]:
    """E17, evaluated rather than assumed.

    Three clauses, all of them binding: the record must be FOR THIS LANE, at
    most 30 days old, and carry per-step verification RECEIPTS — "a canary file
    lacking per-step verification receipts is a FAIL (a written file is not a
    run drill)". The live `rest` record fails the receipts clause today, which
    is why this returns a structure rather than a boolean.
    """
    from brain import cos                                        # noqa: PLC0415

    path = cos.run_ops_dir(vault) / "_cos_undo_canary.json"
    out = {"lane": lane, "path": str(path), "lane_match": False,
           "canary_tested_utc": None, "canary_age_days": None,
           "receipts_present": False, "idempotent_replay": None, "valid": False,
           "why": ""}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        out["why"] = f"no readable canary record at {path} ({exc})"
        return out
    # A legacy flat pre-v5.7 file reads as the `rest` lane's record.
    record = (doc.get("lanes") or {}).get(lane)
    if record is None and lane == "rest" and doc.get("tested"):
        record = doc
    if record is None:
        out["why"] = f"the canary file carries no record for the {lane!r} lane"
        return out
    out["lane_match"] = True
    out["canary_tested_utc"] = record.get("tested")
    out["idempotent_replay"] = record.get("idempotent_replay")
    receipts = record.get("receipts")
    out["receipts_present"] = bool(receipts) and isinstance(receipts, dict)
    try:
        tested = _dt.datetime.fromisoformat(
            str(record.get("tested")).replace("Z", "+00:00"))
        age = ((now or _utcnow()) - tested).total_seconds() / 86400.0
        out["canary_age_days"] = round(age, 2)
    except (TypeError, ValueError):
        out["why"] = "the canary record carries no parseable `tested` timestamp"
        return out
    reasons = []
    if out["canary_age_days"] > CANARY_MAX_AGE_DAYS:
        reasons.append(f"the drill is {out['canary_age_days']:.0f} days old "
                       f"(max {CANARY_MAX_AGE_DAYS})")
    if not out["receipts_present"]:
        reasons.append("the record carries no per-step verification receipts — "
                       "a written file is not a run drill")
    if str(record.get("idempotent_replay")) != "confirmed":
        reasons.append("idempotent_replay is not `confirmed`")
    out["valid"] = not reasons
    out["why"] = "; ".join(reasons) if reasons else "lane, age and receipts all hold"
    return out


# ---------------------------------------------------------------------------
# the plan: what the JUDGE decided, re-screened mechanically
# ---------------------------------------------------------------------------
def draft_signature(run_id: str, conv_id: str) -> str:
    """The machine signature embedded in every draft this run saves.

    It is what makes a lost `CreateItem` response reconcilable: the Drafts
    folder is joined on conversation id and the match is CONFIRMED by this
    string, so "did the server accept it" has an answer that does not depend on
    a response we never received.
    """
    return f"[cos:{run_id}:{short(conv_id)[:12]}]"


def _ledger_path(vault: Path, run_id: str) -> Path:
    from brain import cos                                        # noqa: PLC0415
    return cos.run_ops_dir(vault) / f"_cos_ingestion_ledger_{run_id}.jsonl"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()
            if x.strip()]


def _within_window(received: Any, since_days: int | None,
                   now: _dt.datetime) -> bool:
    """Is this thread inside the recency window? Unknown date ⇒ IN.

    `since_days is None` is the historic / "do it in all" mode — everything is
    in scope. A thread whose `received` cannot be parsed is kept rather than
    dropped: the per-lane guards already vetted it, and a scope filter should
    never be the thing that silently discards a real action on a parse slip.
    """
    if since_days is None:
        return True
    try:
        when = _dt.datetime.fromisoformat(str(received))
    except (TypeError, ValueError):
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return (now - when).total_seconds() <= since_days * 86400

#: A canary older than this is STALE and does not satisfy E17 — a canary from
#: before a Chrome update proves nothing about today's DOM.
CANARY_MAX_AGE_DAYS = 30

DRAFT_FORMS = ("standard", "acknowledge-late")

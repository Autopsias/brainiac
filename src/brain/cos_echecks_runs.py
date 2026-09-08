"""Run-state loading, joins, and the archive truth table behind the E-checks."""
from __future__ import annotations

import hashlib
import json
import re as re
import subprocess
from pathlib import Path
from typing import Any, Callable as Callable

from . import cos, cos_chips, cos_runverify

# ---------------------------------------------------------------------------
# the executing tree, and its capability digest
# ---------------------------------------------------------------------------
def _slice(text: str, begin: str, end: str) -> str | None:
    i = text.find(begin)
    if i < 0:
        return None
    j = text.find(end, i)
    return None if j < 0 else text[i:j + len(end)]


def capability_digest(tools: Path | None = None) -> str | None:
    """sha256 over the capability set the model leg runs under, or ``None``
    when the executing tree is not on disk to hash.

    THE BOUNDARY IS THE CAPABILITY SET, NOT THE FENCE (DOCTRINE §2.8). The
    `⟦UNTRUSTED DATA⟧` wrapper is a mitigation — LLMail-Inject solved
    Spotlighting, Prompt Shields, TaskTracker and an LLM judge, together — so
    what E10 asserts is that the tool grant, the mutation allowlist and the
    zero-send denylist are the ones the run began with, byte for byte.
    """
    tools = tools or cos_runverify.tools_dir()
    if tools is None:
        return None
    h = hashlib.sha256()
    for name, begin, end in CAPABILITY_BLOCKS:
        p = tools / name
        try:
            block = _slice(p.read_text(encoding="utf-8"), begin, end)
        except OSError:
            return None
        if block is None:
            return None
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(block.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def git_state(repo: Path | None = None) -> dict[str, Any]:
    """The commit that is about to run, and whether the tree was clean.

    THE RUN MANIFEST RECORDED NO GIT STATE AT ALL, so "MODEL_TOOLS were
    unchanged at the commit that actually ran" was unprovable after the fact:
    inspecting today's constants proves what exists while you verify, never
    what mutated the mailbox. One field and one flag close that; a claim
    written by hand into a handoff file cannot.
    """
    repo = repo or (cos_runverify.tools_dir() or Path.cwd()).parent
    def _git(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", "-C", str(repo), *args],
                                 capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    head = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    return {"commit": head,
            "clean": (status == "") if status is not None else None}


# ---------------------------------------------------------------------------
# the run's artifacts, read ONCE
# ---------------------------------------------------------------------------
def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_run(vault, run_id: str) -> dict[str, Any]:
    """Every artifact the ten checks read, gathered once and named honestly.

    An artifact that is absent is ``None`` here and the check that needs it
    says so in its own answer — never a silent zero, which is the shape that
    lets a check pass because its input was empty.
    """
    ops = cos.run_ops_dir(vault)
    ledger = cos_runverify.ledger_rows(vault, run_id)
    undo = cos_runverify._read_jsonl(ops / f"_cos_undo_ledger_{run_id}.jsonl")
    drafts = cos_runverify._read_jsonl(
        ops / f"_cos_drafts_pending_{run_id}.jsonl")
    binding = _json(cos.run_plan_binding_path(vault, run_id))
    plan = _json(Path(str(binding.get("plan")))) if isinstance(binding, dict) \
        and binding.get("plan") else None
    ev = Path(str(binding["plan"])).parent if isinstance(binding, dict) \
        and binding.get("plan") else None
    stamps = None
    for name in ("categories-bound.json", "categories.json"):
        raw = _json(ev / name) if ev else None
        if isinstance(raw, list):
            stamps = {str(r.get("conversation_id")): r.get("category")
                      for r in raw if isinstance(r, dict)}
            break
        if isinstance(raw, dict):
            stamps = {str(k): v for k, v in raw.items()}
            break
    return {
        "vault": Path(str(vault)),
        "category_stamps": stamps,
        "ops": ops,
        "ledger": ledger,
        "undo": undo,
        "drafts": drafts,
        "binding": binding,
        "plan": plan,
        "evidence_dir": ev,
        "apply": _json(ev / "apply.json") if ev else None,
        "dry_run": _json(ev / "dry-run.json") if ev else None,
        "judgment": _json(ev / "judgment.json") if ev else None,
        "sent_baseline": _json(ops / f"_cos_sent_baseline_{run_id}.json"),
        "grounding": _json(grounding_path(vault, run_id)),
        # THE DELIVERY JOIN (grounding design D2a). A separate artifact from the
        # declaration on purpose: the declaration is the fetcher's word about
        # itself, and this is what the composed prompt actually carried.
        "grounding_join": _json(ev / "grounding-join.json") if ev else None,
        "manifest": cos.run_manifest(vault, run_id) or {},
    }


def grounding_path(vault, run_id: str) -> Path:
    """Where the run DECLARES its grounding state (DOCTRINE §8.2 E10).

    A missing file is a FAIL, not an ungrounded night: an ungrounded night is a
    thing the run SAYS, never a thing an absent file implies.
    """
    return cos.run_ops_dir(vault) / f"_cos_grounding_{run_id}.json"


def declare_grounding(vault, run_id: str, *, state: str, reason: str = "",
                      required: list[str] | None = None,
                      covered: list[str] | None = None) -> Path:
    """Write that declaration. HOST-only, once per run, at launch."""
    if state not in ("grounded", "ungrounded"):
        raise EcheckError(f"grounding state {state!r} is neither `grounded` "
                          "nor `ungrounded` — an undeclared state is a FAIL")
    if state == "ungrounded" and not reason.strip():
        raise EcheckError("an UNGROUNDED night must say why — a bare state is "
                          "the absent file wearing a word")
    p = grounding_path(vault, run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"run_id": run_id, "state": state,
                             "reason": reason,
                             "required": sorted(required or []),
                             "covered": sorted(covered or [])},
                            indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# joins the checks share (and `tools/cos_s02b_verify.py` reuses)
# ---------------------------------------------------------------------------
def dispatched(undo: list[dict[str, Any]], verb: str | None = None
               ) -> list[dict[str, Any]]:
    """Undo-ledger rows that actually reached the mailbox.

    `dispatched` — not the verification WORD. The page half returns
    `verification: "verified-failed"` for a target that simply was not there,
    because it has one word for "no" (run 125), so the host discriminates on
    the flag.
    """
    return [r for r in undo if r.get("dispatched") is True
            and (verb is None or r.get("verb") == verb)]


def by_conversation(ledger: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["conversation_id"]: r for r in ledger if r.get("conversation_id")}


def in_scope(row: dict[str, Any]) -> bool:
    """THE ONE Phase-1.6 eligibility definition — `act`, plus `read` at ANY
    tier (owner ruling 2026-09-01: informational mail is ingested like every
    other email; only `noise` stages nothing).

    Shared by batching, grounding and E9's denominator. Three copies is how a
    denominator drifts (DOCTRINE §8.2 E9).
    """
    return row.get("verdict") in ("act", "read")


def archive_join(vault, run_id: str, run: dict[str, Any] | None = None
                 ) -> list[dict[str, Any]]:
    """Every ARCHIVED thread this run dispatched, joined to its verdict row.

    ONE join, used by E3, by the before/after truth table and by the s02b
    gate — never three artifact models of the same night.
    """
    run = run or load_run(vault, run_id)
    verdicts = by_conversation(run["ledger"])
    out = []
    for r in dispatched(run["undo"], "archive"):
        cid = r.get("conversation_id")
        v = verdicts.get(cid) or {}
        out.append({
            "conversation_id": cid,
            "digest": r.get("conversation_id_digest")
            or hashlib.sha256(str(cid).encode()).hexdigest()[:16],
            "in_ledger": cid in verdicts,
            "verdict": v.get("verdict"),
            "judged_tier": v.get("judged_tier"),
            "read_state": v.get("read_state"),
            "noise_signal": v.get("noise_signal"),
            "state": r.get("state"),
        })
    return out


def chip_join(vault, run_id: str, run: dict[str, Any] | None = None
              ) -> list[dict[str, Any]]:
    """Every CHIP this run dispatched, joined to the verdict that chose it."""
    run = run or load_run(vault, run_id)
    verdicts = by_conversation(run["ledger"])
    rows = dispatched(run["undo"], "categorize")
    # ONE VAULT SCAN FOR THE WHOLE JOIN, not one per row: `signed_ingest_notes`
    # hashes every note in the vault (~1.2s / 3,993 notes on the reference
    # host), so the per-row shape this replaced would have cost four minutes on
    # a 200-row night. Built only when a mark is actually present.
    #
    # AND IT ASKS THE CATCHING-UP QUESTION, not the same-night one (review
    # 2026-08-25). A candidate offered by a night's ingest bridge is signed by
    # a LATER maintenance drain, so the mark a plan legitimately dispatches
    # tonight is almost always for a candidate an EARLIER run offered. Judged
    # under `run_id` alone every one of those reads as unsigned and E4 raises
    # a false alarm on correct work — the planner and this check now call the
    # one definition, `cos.signed_ingested_catching_up`.
    signed_cids: set[str] | None = None
    if any(r.get("chip") == cos_chips.CHIP_INGESTED for r in rows):
        signed_cids = cos.signed_ingested_catching_up(
            vault, run_id, list(verdicts.values()))
    out = []
    for r in rows:
        cid = r.get("conversation_id")
        v = verdicts.get(cid) or {}
        want = cos_chips.chip_for(v.get("verdict"), v.get("judged_tier"))
        out.append({
            "conversation_id": cid,
            "digest": r.get("conversation_id_digest")
            or hashlib.sha256(str(cid).encode()).hexdigest()[:16],
            "in_ledger": cid in verdicts,
            "chip": r.get("chip"),
            "expected_chip": want,
            "verdict": v.get("verdict"),
            "judged_tier": v.get("judged_tier"),
            "before_image": r.get("before_image") or [],
            # The INGESTION MARK's authority (E4, FIX-03 2026-08-25). The mark
            # is not a priority write, so the (bucket, tier) matrix cannot
            # judge it; what makes it legitimate is that the vault SIGNED a
            # note for this thread's own candidate — a content-hash join of
            # this run's claims rows against signed notes, with the matched
            # note's provenance naming this conversation. ONE definition, the
            # engine's — the planner chooses the mark by the same call. (The
            # old authority was the bridge's DROP stamp, which said "offered",
            # not "signed".)
            "signed": (signed_cids is not None and cid in signed_cids),
            "verification": r.get("verification"),
            "state": r.get("state"),
        })
    return out


def archive_truth_table(ledger: list[dict[str, Any]]) -> dict[str, Any]:
    """BEFORE/AFTER over one run's real ledger: which rows the v7 widening
    newly makes archive-eligible, and which it takes away (never any).

    BEFORE is the shipped screen — a MODEL-claimed `auto_archive`, then the
    mechanical re-screens. AFTER adds the host's own
    `cos_judge.archive_eligibility`. A widening you cannot enumerate is a
    widening you cannot review, so this returns the rows, not a count.
    """
    judge = _cos_judge()
    if judge is None:
        raise EcheckError(
            "the mutation/judgment toolchain is not on disk beside the engine, "
            "so the AFTER column cannot be derived from the shipped rule — and "
            "a truth table with a guessed AFTER is worse than none")

    def screens(row: dict[str, Any]) -> str | None:
        if row.get("auto_archive") is not True:
            return "no archive claim"
        if row.get("judgment_pending"):
            return "no verdict"
        if row.get("read_state") != "read":
            return "unread — the shield"
        if p0_floor_refuses(row.get("verdict"), row.get("tier")):
            return "carries a P0 chip outside the `read` bucket"
        if p0_floor_refuses(row.get("verdict"), row.get("judged_tier")):
            return "judged P0 outside the `read` bucket — the blast floor"
        return None

    before, after = [], []
    for row in ledger:
        d = hashlib.sha256(str(row.get("conversation_id")).encode()).hexdigest()[:16]
        if screens(row) is None:
            before.append({"digest": d, "signal": row.get("noise_signal"),
                           "verdict": row.get("verdict"),
                           "judged_tier": row.get("judged_tier")})
        widened = dict(row)
        elig = judge.archive_eligibility(widened)
        if elig is not None:
            widened["auto_archive"], widened["noise_signal"] = elig
        if screens(widened) is None:
            after.append({"digest": d, "signal": widened.get("noise_signal"),
                          "verdict": widened.get("verdict"),
                          "judged_tier": widened.get("judged_tier")})
    b = {r["digest"] for r in before}
    a = {r["digest"] for r in after}
    return {"rows": len(ledger), "before": before, "after": after,
            "newly_archived": sorted(a - b), "no_longer_archived": sorted(b - a)}

# Parent-namespace binds, deferred past this module's own defs.
from .cos_echecks import (  # noqa: E402
    CAPABILITY_BLOCKS as CAPABILITY_BLOCKS,
    EcheckError as EcheckError,
    FAIL as FAIL,
    p0_floor_refuses as p0_floor_refuses,
    _cos_driver as _cos_driver,
    _cos_judge as _cos_judge,
)

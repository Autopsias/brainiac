#!/usr/bin/env python3
"""The voice-check LEG: build one scoring prompt per draft, score it, fold the scores into the ledger.

THREE MODES, AND THE SPLIT IS THE SAFETY STORY.

    python3 tools/cos_voice_cli.py --prompts --vault <v> --run-id <id> \
        --out <dir> [--redact]
    <the model leg> | python3 tools/cos_voice_cli.py --score --slot <dir>
    python3 tools/cos_voice_cli.py --fold --vault <v> --run-id <id> \
        --dir <dir> --out <voice-check.json>

`--prompts` writes HOST-AUTHORED prompts (the profile, the rubric, one draft) into
the run directory. `--score` reads the leg's stdout OFF A PIPE and never writes
it: what lands on disk is `{check_id: PASS|FAIL}` drawn from `cos_voice.CHECK_IDS`
and two closed words, so nothing model-authored is persisted on this path — the
same rule `cos_model_answer` enforces for the judgment leg, whose whole hardened
envelope reader is REUSED here rather than re-spelled. `--fold` is pure host
arithmetic over those numbers.

ONE DRAFT PER CALL, AND THE CONTROL IS ONE OF THEM. `cos_voice.NEGATIVE_CONTROL_TEXT`
gets its own slot every night. It is frozen and deliberately off-voice, so its
score cannot legitimately rise; when it does, `control_verdict` fails the leg and
the night's draft scores are reported as untrustworthy rather than banked.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import cos_voice                                                # noqa: E402
from cos_judge_night import _short                              # noqa: E402


def _pending_path(vault: Path, run_id: str) -> Path:
    from brain import cos                                       # noqa: PLC0415
    return cos.run_ops_dir(vault) / f"_cos_drafts_pending_{run_id}.jsonl"


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in
            path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(
        json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8")


def slots(vault: Path, run_id: str) -> list[dict[str, Any]]:
    """One scoring slot per drafted thread, plus the negative control.

    The control is LAST and always present — including on a night that drafted
    nothing, because the rubric is checked whether or not it was used. A draft
    row with no text is skipped: there is nothing to score, and a zero would
    read as a failing draft rather than an absent one.
    """
    out = [{"slot": _short(r["conversation_id"]),
            "conversation_id": r["conversation_id"],
            "kind": "draft", "text": r.get("text") or ""}
           for r in _rows(_pending_path(vault, run_id))
           if (r.get("text") or "").strip()]
    out.append({"slot": cos_voice.NEGATIVE_CONTROL_ID,
                "conversation_id": cos_voice.NEGATIVE_CONTROL_ID,
                "kind": "control", "text": cos_voice.NEGATIVE_CONTROL_TEXT})
    return out


def do_prompts(vault: Path, run_id: str, out_dir: Path, *,
               redact: bool = False) -> dict[str, Any]:
    state = cos_voice.profile_state(vault)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for slot in slots(vault, run_id):
        d = out_dir / slot["slot"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "prompt.txt").write_text(
            cos_voice.check_prompt(state, slot["text"],
                                   slot["conversation_id"], redact=redact),
            encoding="utf-8")
        # The slot's own identity, host-authored, so `--score` can bind the
        # answer to the draft it was asked about without re-reading the ledger.
        (d / "slot.json").write_text(json.dumps(
            {k: slot[k] for k in ("slot", "conversation_id", "kind")},
            ensure_ascii=False, sort_keys=True), encoding="utf-8")
        written.append(slot["slot"])
    return {"run_id": run_id, "slots": written,
            "drafts": len(written) - 1, "control": cos_voice.NEGATIVE_CONTROL_ID,
            "voice_profile": cos_voice.ledger_fields(state)}


def do_score(slot_dir: Path, source: str = "-") -> dict[str, Any]:
    """The leg's stdout → `<slot>/score.json`, host words only.

    A leg that answered about a DIFFERENT conversation scores nothing: the rows
    are filtered on this slot's own id before a single verdict is counted, so a
    mis-piped or injected answer reads as an unscored slot (which
    `control_verdict` and the fold both treat as a failure) rather than as a
    score for the wrong draft.
    """
    import cos_model_answer as cma                              # noqa: PLC0415

    meta = json.loads((slot_dir / "slot.json").read_text(encoding="utf-8"))
    rows, note = cma.answer_from_envelope(source)
    checks = {r["check"]: r["verdict"] for r in rows
              if isinstance(r, dict)
              and str(r.get("conversation_id")) == meta["conversation_id"]
              and isinstance(r.get("check"), str)
              and isinstance(r.get("verdict"), str)}
    score = dict(meta, **cos_voice.score_from_checks(checks), note=note,
                 rows_returned=len(rows))
    (slot_dir / "score.json").write_text(
        json.dumps(score, ensure_ascii=False, sort_keys=True, indent=1),
        encoding="utf-8")
    return score


def _read_scores(dir_: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for f in sorted(dir_.glob("*/score.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("slot"):
            out[str(d["slot"])] = d
    return out


def fold(vault: Path, run_id: str, dir_: Path) -> dict[str, Any]:
    """Every slot's score, the control's verdict, and the drafts ledger updated.

    THE DEGRADATION TRAVELS WITH THE SCORE. Each ledger row carries the profile
    facts it was drafted under as well as its score, so a row scored 24/27
    against NO profile can never be read later as a grounded 24/27.
    """
    state = cos_voice.profile_state(vault)
    scored = _read_scores(dir_)
    control = scored.get(cos_voice.NEGATIVE_CONTROL_ID) or {}
    verdict = cos_voice.control_verdict(control)

    path = _pending_path(vault, run_id)
    rows = _rows(path)
    graded = 0
    for r in rows:
        s = scored.get(_short(r["conversation_id"]))
        # WHAT IT WAS DRAFTED UNDER, NOT WHAT THE VAULT HOLDS NOW. `write_night`
        # already stamped the draft-time state; re-stamping it here would
        # rewrite the record if the owner edited the profile between the
        # judgment leg and this one. Only a row that never got one is filled in.
        r.setdefault("voice_profile", cos_voice.ledger_fields(state))
        r["voice_check"] = ({k: s[k] for k in
                             ("rubric", "total", "answered", "passed", "score")}
                            if s else None)
        graded += 1 if s else 0
    if rows:
        _write_rows(path, rows)

    drafts = [s for k, s in sorted(scored.items())
              if k != cos_voice.NEGATIVE_CONTROL_ID]
    values = [float(s.get("score") or 0.0) for s in drafts]
    return {
        "run_id": run_id, "rubric": cos_voice.RUBRIC_VERSION,
        "voice_profile": cos_voice.ledger_fields(state),
        "drafts_scored": len(drafts), "drafts_in_ledger": len(rows),
        "drafts_graded_in_ledger": graded,
        "mean_score": round(sum(values) / len(values), 4) if values else None,
        "min_score": min(values) if values else None,
        "negative_control": verdict,
        # THE LEG'S OWN VERDICT, and the control is what decides it. A night
        # whose control drifted reports `ok: false` however well its drafts
        # scored — an instrument that cannot fail measures nothing.
        "ok": bool(verdict["ok"]),
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--prompts", action="store_true",
                   help="write one scoring prompt per draft, plus the control")
    p.add_argument("--score", action="store_true",
                   help="read the leg's stdout on stdin, write <slot>/score.json")
    p.add_argument("--fold", action="store_true",
                   help="merge the slot scores, apply the control's ceiling, "
                        "and write the scores into the drafts ledger")
    p.add_argument("--vault", type=Path, default=None)
    p.add_argument("--run-id", default=None)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--dir", type=Path, default=None)
    p.add_argument("--slot", type=Path, default=None)
    p.add_argument("--envelope", default="-",
                   help="with --score: `-` (the pipe, and the production path) "
                        "or a captured envelope for a fixture")
    p.add_argument("--redact", action="store_true",
                   help="print the profile and the draft as lengths. Required "
                        "for any copy written where git can reach it — this "
                        "repository is a public-export source")
    args = p.parse_args(argv[1:])

    if args.prompts:
        if not (args.vault and args.run_id and args.out):
            print("--prompts needs --vault, --run-id and --out", file=sys.stderr)
            return 2
        print(json.dumps(do_prompts(args.vault, args.run_id, args.out,
                                    redact=args.redact), indent=2))
        return 0
    if args.score:
        if not args.slot:
            print("--score needs --slot", file=sys.stderr)
            return 2
        try:
            s = do_score(args.slot, args.envelope)
        except (OSError, ValueError) as exc:
            # A SENTENCE, NOT A TRACEBACK, and never the answer itself: this
            # line goes to `$LOG`, and every ValueError the envelope reader
            # raises is already host-authored (a digest and a count, no model
            # text). No `score.json` is written, so the slot reads as unscored.
            print(f"the voice leg for {args.slot.name} produced no usable "
                  f"score: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({k: s[k] for k in
                          ("slot", "kind", "answered", "passed", "score")}))
        return 0
    if args.fold:
        if not (args.vault and args.run_id and args.dir):
            print("--fold needs --vault, --run-id and --dir", file=sys.stderr)
            return 2
        out = fold(args.vault, args.run_id, args.dir)
        text = json.dumps(out, indent=2)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text + "\n", encoding="utf-8")
        print(text)
        # NONZERO ON A DRIFTED RUBRIC, and the nightly logs it rather than
        # dying: a voice score is a QUALITY signal on unsent text, never a
        # safety gate on a mailbox mutation. Killing a night over it would
        # trade a real archive lane for a report.
        return 0 if out["ok"] else 1
    p.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

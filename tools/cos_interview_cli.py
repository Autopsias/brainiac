#!/usr/bin/env python3
"""The interview phrasing LEG's parser (INT-01): the leg's stdout → the wording
of the open questions on the morning sheet.

    <the model leg> | python3 tools/cos_interview_cli.py --score --vault <v>

The prompt is host-authored by `brain interview --nightly --prompt-out`; this
side reads the leg's stdout OFF THE PIPE through the same hardened envelope
reader the judgment leg uses (`cos_model_answer.answer_from_envelope`), and
hands the rows to `interview.apply_phrasing`, which takes a rewording only when
it kept every option action and every note id. Nothing model-authored lands
anywhere but the question text the owner reads; the fixed actions never move.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--score", action="store_true",
                   help="read the leg's stdout on stdin, reword the open questions")
    p.add_argument("--vault", type=Path, default=None)
    p.add_argument("--envelope", default="-",
                   help="`-` (the pipe, and the production path) or a captured "
                        "envelope for a fixture")
    args = p.parse_args(argv[1:])
    if not args.score or not args.vault:
        p.print_help()
        return 2
    import cos_model_answer as cma                              # noqa: PLC0415
    from brain import interview                                 # noqa: PLC0415
    try:
        rows, note = cma.answer_from_envelope(args.envelope)
    except (OSError, ValueError) as exc:
        # A SENTENCE, NOT A TRACEBACK, and never the answer itself: this line
        # goes to `$LOG`. The host wording stands.
        print(f"the interview phrasing leg produced no usable wording: {exc}",
              file=sys.stderr)
        return 1
    out = interview.apply_phrasing(args.vault, rows)
    print(json.dumps({"phrased": len(out["phrased"]),
                      "kept_host_wording": len(out["kept_host_wording"]),
                      "note": note}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""Read deployed COS versions."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from tools import cos_deployed_version as _source


def _option(argv: list[str], name: str) -> str | None:
    value = next((arg.split("=", 1)[1] for arg in argv[1:]
                  if arg.startswith(f"--{name}=")), None)
    if value is None and f"--{name}" in argv:
        index = argv.index(f"--{name}")
        if index + 1 < len(argv):
            value = argv[index + 1]
    return value


def _parse_options(argv: list[str]) -> tuple[str | None, str | None, list[str]]:
    expect, lane = _option(argv, "expect"), _option(argv, "lane")
    if lane is not None and lane not in _source.LANES:
        print(f"usage: --lane must be one of {', '.join(_source.LANES)}", file=sys.stderr)
        return expect, lane, []
    args = [arg for arg in argv[1:]
            if not arg.startswith("--") and arg not in (expect, lane)]
    return expect, lane, args


def _render_report(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        _source._print_text(result)


def _check_report(result: dict, expect: str | None) -> int:
    if result["lane"] is None:
        print("\nREFUSING TO ANSWER: " + result["lane_reason"], file=sys.stderr)
        return 2
    if not result["lane_supported"]:
        print("\nUNSUPPORTED SURFACE: " + result["lane_unsupported_reason"],
              file=sys.stderr)
        return 2
    if not result["deployed"] and not result["run_reports"]:
        print("FAIL: no readback source available on this lane", file=sys.stderr)
        return 2
    if not expect:
        return 0
    if expect in result["versions_seen"]:
        print(f"\nOK: {expect!r} is reported by the {result['lane']} lane "
              "or by a run report")
        return 0
    elsewhere = any(entry.get("version") == expect for entry in result["other_surfaces"])
    extra = (f" ({expect!r} IS present on a non-executing surface — that "
             "surface does not run, so it does not count)" if elsewhere else "")
    print(f"\nMISMATCH: the {result['lane']} lane does not report {expect!r} — "
          f"counted: {result['versions_seen'] or '(none)'}{extra}. Do NOT move "
          "the calibration pin: guard 4 is a string equality and a pin "
          "ahead of the deployment silently freezes every gated phase.",
          file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    expect, lane, args = _parse_options(argv)
    if lane is not None and lane not in _source.LANES:
        return 2
    if not args:
        print("usage: cos_deployed_version.py [--json] [--lane LANE] "
              "[--expect VERSION] <vault>", file=sys.stderr)
        return 2
    vault = Path(args[0]).expanduser().resolve()
    if not (vault / "cos-ops").is_dir():
        print(f"FAIL: no cos-ops dir under {vault}", file=sys.stderr)
        return 2

    result = _source.report(vault, lane=lane)
    _render_report(result, "--json" in argv)
    return _check_report(result, expect)

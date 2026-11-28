#!/usr/bin/env python3
"""EV-04 — Agentic answer-grounding and trajectory evaluation harness.

Measures whether an agent using Brainiac's composable read/write tools actually
honors the substrate contract across five behavioral families:
  1. dossier_routing_tensions — routes decision-state questions to `dossier`,
     reports newer post-dating tensions without promoting proposals over decisions.
  2. egress_freshness_hints — respects `egress.hint` and `freshness`/`embedder`
     notices instead of falsely concluding the vault is empty or falling back to web.
  3. bare_id_body_linking — cites notes with `[[bare-id]]` in note bodies, never
     `[[raw/...]]` or `[[brain/...]]` path-prefixed wikilinks.
  4. create_safety_compliance — avoids duplicate creation on `do_not_create` and
     `unknown`, and never leaks concealed higher-tier collision metadata.
  5. cascade_navigation — composes multi-step `search/grep/bases-query ->
     graph-expand -> get` confirmation reads, scored via `eval/navigation_stats.py`.

Modes:
  --mode offline   Deterministic trajectory/answer verifier (zero external API
                   calls, CI- and pytest-safe). Uses `--trajectories` JSON or
                   each scenario's `reference_trajectory`.
  --mode live      Invokes local `claude -p` or `codex exec` against an
                   isolated fixture vault with fresh temp HOME,
                   BRAIN_APP_DATA_DIR, BRAIN_INDEX_DIR, and BRAIN_RUNTIME_DIR.
                   Refuses to launch if `--fixture-vault` points at a live vault.

Usage:
    python3 eval/agentic_eval.py --mode offline \\
        --out _evidence/eval/agentic-baseline-2026-11-25.json \\
        --trace-out _evidence/eval/navigation-trace-2026-11-25.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _agentic_checks as checks  # noqa: E402
import navigation_stats  # noqa: E402

SCHEMA_VERSION = "agentic-eval/1"
DEFAULT_SCENARIOS = (
    Path(__file__).resolve().parent / "fixtures" / "agentic-scenarios.json"
)
DEFAULT_MIN_PASS_RATE = 0.80
MIN_SCENARIOS = 15
REQUIRED_FAMILIES = frozenset(
    {
        "dossier_routing_tensions",
        "egress_freshness_hints",
        "bare_id_body_linking",
        "create_safety_compliance",
        "cascade_navigation",
    }
)
_LIVE_VAULT_DENY_NAMES = frozenset({".brainiac", "Obsidian", "Documents", "vault"})
_LIVE_TRACE_SHIM = """#!/usr/bin/env python3
import json, os, subprocess, sys

args = sys.argv[1:]
subcmd = next((a for a in args if not a.startswith("-")), "")
real_bin = os.environ.get("BRAIN_REAL_BIN", "")
if not real_bin:
    sys.stderr.write("brain shim: real brain binary not found on PATH\\n")
    rc = 127
else:
    rc = subprocess.call([real_bin, *args])

log_path = os.environ.get("BRAIN_EVAL_TRACE_LOG", "")
if log_path:
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"tool": subcmd, "argv": args, "rc": rc}) + "\\n")

raise SystemExit(rc)
"""


class FixtureIsolationError(RuntimeError):
    """Raised when --mode live is pointed at an unisolated or live vault."""


def _norm_path(p: Path) -> Path:
    real = os.path.realpath(str(p.expanduser()))
    norm = os.path.normcase(real)
    return Path(norm.casefold() if sys.platform in ("darwin", "win32") else norm)


def _samefile_overlap(a: Path, b: Path) -> bool:
    if not (a.exists() and b.exists()):
        return False
    if os.path.samefile(a, b):
        return True
    return any(anc.exists() and os.path.samefile(anc, b) for anc in a.parents) or any(
        anc.exists() and os.path.samefile(a, anc) for anc in b.parents
    )


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        _norm_path(child).relative_to(_norm_path(parent))
        return True
    except ValueError:
        return False


def _paths_overlap(a: Path, b: Path) -> bool:
    """Return True if two paths are equal or one is an ancestor of the other."""
    return (
        _norm_path(a) == _norm_path(b)
        or _is_relative_to(a, b)
        or _is_relative_to(b, a)
        or _samefile_overlap(a, b)
    )


def validate_live_fixture_isolation(
    fixture_vault: Path,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Enforce strict fixture isolation before launching any live agent CLI.

    Refuses if `fixture_vault` does not exist, equals/is inside/is an ancestor
    of the user's live `BRAIN_VAULT`, overlaps sensitive home directories, or
    is outside both the system temp dir and the repo's fixture directories
    (unless it explicitly carries an `.agentic-eval-fixture` sentinel file).
    """
    env_map = os.environ if env is None else env
    if not fixture_vault.exists() or not fixture_vault.is_dir():
        raise FixtureIsolationError(
            f"fixture vault does not exist or is not a directory: {fixture_vault}"
        )
    resolved = fixture_vault.expanduser().resolve(strict=False)

    live_vault = env_map.get("BRAIN_VAULT", "").strip()
    if live_vault:
        live_resolved = Path(live_vault).expanduser().resolve(strict=False)
        if _paths_overlap(resolved, live_resolved):
            raise FixtureIsolationError(
                f"refusing --mode live: fixture vault {resolved} overlaps "
                f"live BRAIN_VAULT ({live_resolved})"
            )

    home = Path.home().resolve(strict=False)
    for name in _LIVE_VAULT_DENY_NAMES:
        blocked = (home / name).resolve(strict=False)
        if _paths_overlap(resolved, blocked):
            raise FixtureIsolationError(
                f"refusing --mode live: fixture vault {resolved} overlaps "
                f"protected home path {blocked}"
            )

    repo_root = Path(__file__).resolve().parent.parent
    allowed_roots = [
        Path(tempfile.gettempdir()).resolve(strict=False),
        (repo_root / "eval" / "fixtures").resolve(strict=False),
        (repo_root / "tests" / "fixtures").resolve(strict=False),
    ]
    has_sentinel = (resolved / ".agentic-eval-fixture").exists()
    if not has_sentinel and not any(
        _is_relative_to(resolved, r) for r in allowed_roots
    ):
        raise FixtureIsolationError(
            f"refusing --mode live: {resolved} is outside temp/fixture roots "
            "and lacks an .agentic-eval-fixture marker"
        )
    return resolved


def build_isolated_live_env(
    fixture_vault: Path,
    scratch_root: Path,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Construct a sanitized environment for `--mode live` subprocesses."""
    source = os.environ if base_env is None else base_env
    env = {k: v for k, v in source.items() if not k.startswith("BRAIN_")}
    iso_home = scratch_root / "home"
    iso_app = scratch_root / "app-data"
    iso_idx = scratch_root / "index"
    iso_rt = scratch_root / "runtime"
    iso_bin = scratch_root / "bin"
    for d in (iso_home, iso_app, iso_idx, iso_rt, iso_bin):
        d.mkdir(parents=True, exist_ok=True)

    orig_path = env.get("PATH", "")
    real_brain = shutil.which("brain", path=orig_path) or ""
    shim_path = iso_bin / "brain"
    shim_path.write_text(_LIVE_TRACE_SHIM, encoding="utf-8")
    shim_path.chmod(0o755)

    trace_log = iso_rt / "brain-invocations.jsonl"
    env["HOME"] = str(iso_home)
    env["PATH"] = f"{iso_bin}{os.pathsep}{orig_path}" if orig_path else str(iso_bin)
    env["BRAIN_VAULT"] = str(fixture_vault)
    env["BRAIN_APP_DATA_DIR"] = str(iso_app)
    env["BRAIN_INDEX_DIR"] = str(iso_idx)
    env["BRAIN_RUNTIME_DIR"] = str(iso_rt)
    env["BRAIN_EVAL_TRACE_LOG"] = str(trace_log)
    if real_brain:
        env["BRAIN_REAL_BIN"] = real_brain
    return env


def evaluate_scenario(
    scenario: dict[str, Any],
    trajectory: dict[str, Any] | None,
) -> dict[str, Any]:
    """Score one scenario trajectory against its declarative expectations."""
    sid = scenario["id"]
    family = scenario["family"]
    dimension = scenario.get("dimension", "X")
    if not trajectory:
        return {
            "id": sid,
            "family": family,
            "dimension": dimension,
            "passed": False,
            "failures": ["no trajectory provided"],
        }
    exp = scenario.get("expectations", {})
    calls = list(trajectory.get("calls", []))
    answer = str(trajectory.get("answer", ""))
    failures: list[str] = []
    failures.extend(checks.check_tools(exp, calls))
    failures.extend(checks.check_answer_phrases(exp, answer, trajectory))
    failures.extend(checks.check_body_links(exp, trajectory))
    failures.extend(checks.check_create_safety(exp, trajectory))
    failures.extend(checks.check_cascade_confirmation(scenario, calls))
    failures.extend(checks.check_live_verification(scenario, trajectory))
    return {
        "id": sid,
        "family": family,
        "dimension": dimension,
        "passed": len(failures) == 0,
        "failures": failures,
    }


def build_navigation_trace(
    scenarios: list[dict[str, Any]],
    trajectories: Mapping[str, dict[str, Any]],
    run_id: str,
) -> dict[str, Any]:
    """Emit a trace JSON matching eval/navigation_stats.py's exact schema."""
    queries = []
    for sc in scenarios:
        sid = sc["id"]
        traj = trajectories.get(sid) or {}
        calls = [
            {
                "tool": str(c.get("tool", "")),
                "returned_ids": list(c.get("returned_ids", [])),
                **({"status": c["status"]} if "status" in c else {}),
            }
            for c in traj.get("calls", [])
        ]
        queries.append(
            {
                "qid": sid,
                "gold_ids": list(sc.get("gold_ids", [])),
                "calls": calls,
            }
        )
    return {"run": run_id, "queries": queries}


def _find_duplicate_ids(scenarios: list[dict[str, Any]]) -> tuple[int, list[str]]:
    seen: set[str] = set()
    dups: list[str] = []
    for sc in scenarios:
        sid = str(sc.get("id", ""))
        if sid in seen:
            if sid not in dups:
                dups.append(sid)
        else:
            seen.add(sid)
    return len(seen), dups


def evaluate_suite(
    scenarios_doc: dict[str, Any],
    trajectories: Mapping[str, dict[str, Any]],
    *,
    mode: str = "offline",
    run_id: str | None = None,
    min_pass_rate: float = DEFAULT_MIN_PASS_RATE,
    scenarios_ref: str = "eval/fixtures/agentic-scenarios.json",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Evaluate every scenario and return (scorecard, navigation_trace)."""
    if min_pass_rate <= 0.0 or min_pass_rate > 1.0:
        raise ValueError(f"min_pass_rate must be in (0.0, 1.0], got {min_pass_rate}")
    run_label = run_id or date.today().isoformat()
    scenarios = list(scenarios_doc.get("scenarios", []))
    n_distinct, duplicate_ids = _find_duplicate_ids(scenarios)
    results = [evaluate_scenario(sc, trajectories.get(sc["id"])) for sc in scenarios]

    per_family: dict[str, dict[str, Any]] = {}
    for res in results:
        slot = per_family.setdefault(res["family"], {"n": 0, "passed": 0, "failed": 0})
        slot["n"] += 1
        if res["passed"]:
            slot["passed"] += 1
        else:
            slot["failed"] += 1
    for slot in per_family.values():
        rate = round(slot["passed"] / slot["n"], 4) if slot["n"] else 0.0
        slot["pass_rate"] = rate
        slot["meets_bar"] = (
            slot["n"] > 0 and slot["passed"] > 0 and rate >= min_pass_rate
        )

    trace = build_navigation_trace(scenarios, trajectories, run_label)
    nav_stats = navigation_stats.compute_stats(trace)

    n_total = len(results)
    n_passed = sum(1 for r in results if r["passed"])
    overall = round(n_passed / n_total, 4) if n_total else 0.0
    required_families = REQUIRED_FAMILIES | set(scenarios_doc.get("families", []))
    missing_families = sorted(required_families - set(per_family))
    all_fam = (
        bool(per_family)
        and not missing_families
        and all(s["meets_bar"] for s in per_family.values())
    )
    passed = (
        n_total > 0
        and n_distinct >= MIN_SCENARIOS
        and not duplicate_ids
        and n_passed > 0
        and overall >= min_pass_rate
        and all_fam
    )

    scorecard = {
        "schema": SCHEMA_VERSION,
        "mode": mode,
        "run": run_label,
        "scenarios_file": scenarios_ref,
        "n_scenarios": n_distinct,
        "n_distinct_scenarios": n_distinct,
        "duplicate_ids": duplicate_ids,
        "n_passed": n_passed,
        "n_failed": n_total - n_passed,
        "overall_pass_rate": overall,
        "min_pass_rate": min_pass_rate,
        "per_family": per_family,
        "missing_families": missing_families,
        "navigation_stats": nav_stats,
        "all_families_pass": all_fam,
        "passed": passed,
        "results": results,
    }
    return scorecard, trace


def _parse_live_stdout(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and "calls" in parsed and "answer" in parsed:
            return parsed
        if isinstance(parsed, dict) and "result" in parsed:
            inner = json.loads(parsed["result"])
            if isinstance(inner, dict):
                return inner
    except (json.JSONDecodeError, TypeError):
        pass
    return {"calls": [], "answer": raw}


def _run_live_scenario(
    scenario: dict[str, Any],
    runner: str,
    env: dict[str, str],
    cwd: Path,
    timeout_s: int = 120,
) -> dict[str, Any]:
    """Invoke local claude -p or codex exec for one scenario in isolated env."""
    instruction = (
        "Answer using ONLY Brainiac CLI tools against $BRAIN_VAULT. "
        "Return one JSON object with keys: calls (list of {tool, returned_ids, status}), "
        "answer (string), action (optional string), draft_body (optional string).\n\n"
        f"Scenario {scenario['id']}: {scenario['prompt']}"
    )
    if runner == "claude":
        cmd = ["claude", "-p", instruction, "--output-format", "json"]
    elif runner == "codex":
        cmd = ["codex", "exec", instruction]
    else:
        raise ValueError(f"unsupported live runner: {runner!r}")

    trace_raw = env.get("BRAIN_EVAL_TRACE_LOG", "")
    trace_log = Path(trace_raw) if trace_raw else None
    start_line = (
        len(trace_log.read_text(encoding="utf-8").splitlines())
        if (trace_log is not None and trace_log.exists())
        else 0
    )

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(cwd),
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        entries = checks.read_trace_entries(trace_log, start_line)
        return {
            "calls": [{"tool": runner, "returned_ids": [], "status": "timeout"}],
            "answer": f"runner {runner} timed out after {timeout_s}s: {exc}",
            "live_execution": True,
            "observed_trace": entries,
            "observed_tools": [e for e in entries if e["rc"] == 0],
        }

    entries = checks.read_trace_entries(trace_log, start_line)
    ok_entries = [e for e in entries if e["rc"] == 0]
    if proc.returncode != 0:
        return {
            "calls": [
                {
                    "tool": runner,
                    "returned_ids": [],
                    "status": f"exit-{proc.returncode}",
                }
            ],
            "answer": proc.stderr.strip() or proc.stdout.strip(),
            "live_execution": True,
            "observed_trace": entries,
            "observed_tools": ok_entries,
        }
    result = _parse_live_stdout(proc.stdout.strip())
    result["live_execution"] = True
    result["observed_trace"] = entries
    result["observed_tools"] = ok_entries
    return result


def run_live_suite(
    scenarios_doc: dict[str, Any],
    *,
    fixture_vault: Path,
    runner: str = "claude",
    timeout_s: int = 120,
) -> dict[str, dict[str, Any]]:
    """Run all scenarios in `--mode live` inside a strictly isolated temp env."""
    resolved_vault = validate_live_fixture_isolation(fixture_vault)
    trajectories: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="brainiac-agentic-live-") as tmp:
        env = build_isolated_live_env(resolved_vault, Path(tmp))
        for sc in scenarios_doc.get("scenarios", []):
            trajectories[sc["id"]] = _run_live_scenario(
                sc, runner=runner, env=env, cwd=resolved_vault, timeout_s=timeout_s
            )
    return trajectories


def _load_offline_trajectories(
    scenarios_doc: dict[str, Any],
    trajectories_path: Path | None,
) -> dict[str, dict[str, Any]]:
    if trajectories_path is not None:
        raw = json.loads(trajectories_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "trajectories" in raw:
            return dict(raw["trajectories"])
        if isinstance(raw, dict):
            return raw
        raise ValueError(
            "trajectories JSON must be a mapping of scenario_id -> trajectory"
        )
    return {
        sc["id"]: sc["reference_trajectory"]
        for sc in scenarios_doc.get("scenarios", [])
        if "reference_trajectory" in sc
    }


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    ap.add_argument("--mode", choices=["offline", "live"], default="offline")
    ap.add_argument(
        "--trajectories",
        type=Path,
        default=None,
        help="Offline mode: optional JSON mapping scenario_id -> trajectory",
    )
    ap.add_argument(
        "--runner",
        choices=["claude", "codex"],
        default="claude",
        help="Live mode: local agent CLI to invoke",
    )
    ap.add_argument(
        "--fixture-vault",
        type=Path,
        default=None,
        help="Live mode: isolated fixture vault directory (live vaults refused)",
    )
    ap.add_argument("--out", type=Path, default=None, help="Write scorecard JSON")
    ap.add_argument(
        "--trace-out",
        type=Path,
        default=None,
        help="Write navigation_stats.py-compatible trace JSON",
    )
    ap.add_argument(
        "--run-id", default=None, help="Run label (default: today ISO date)"
    )
    ap.add_argument("--min-pass-rate", type=float, default=DEFAULT_MIN_PASS_RATE)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.min_pass_rate <= 0.0 or args.min_pass_rate > 1.0:
        print(
            f"ERROR: --min-pass-rate must be in (0.0, 1.0], got {args.min_pass_rate}",
            file=sys.stderr,
        )
        return 2
    if not args.scenarios.exists():
        print(f"ERROR: scenarios file not found: {args.scenarios}", file=sys.stderr)
        return 2
    scenarios_doc = json.loads(args.scenarios.read_text(encoding="utf-8"))

    if args.mode == "offline":
        trajectories = _load_offline_trajectories(scenarios_doc, args.trajectories)
    else:
        if args.fixture_vault is None:
            print("ERROR: --mode live requires --fixture-vault", file=sys.stderr)
            return 2
        try:
            trajectories = run_live_suite(
                scenarios_doc, fixture_vault=args.fixture_vault, runner=args.runner
            )
        except FixtureIsolationError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    scorecard, trace = evaluate_suite(
        scenarios_doc,
        trajectories,
        mode=args.mode,
        run_id=args.run_id,
        min_pass_rate=args.min_pass_rate,
        scenarios_ref=str(args.scenarios),
    )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(scorecard, indent=2) + "\n", encoding="utf-8")
    if args.trace_out:
        args.trace_out.parent.mkdir(parents=True, exist_ok=True)
        args.trace_out.write_text(json.dumps(trace, indent=2) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "schema": scorecard["schema"],
                "mode": scorecard["mode"],
                "n_scenarios": scorecard["n_scenarios"],
                "n_passed": scorecard["n_passed"],
                "overall_pass_rate": scorecard["overall_pass_rate"],
                "all_families_pass": scorecard["all_families_pass"],
                "passed": scorecard["passed"],
                "navigation_stats": scorecard["navigation_stats"],
            },
            indent=2,
        )
    )
    return 0 if scorecard["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

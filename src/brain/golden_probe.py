"""Golden-set regression probes over the `brain` CLI (WD-02).

GENERIC runner: the probes (queries + expected anchors) live in a per-vault
probes file OUTSIDE this repo — this module never embeds vault content and
takes the probes-file path as its one required argument. It shells out to
the installed `brain` CLI with ``--json`` and imports NOTHING from the
engine, so what it measures is exactly the gated surface an agent sees
(egress filter included).

Four probe classes, one per measured historical failure mode:

  decision_state  ``brain dossier`` must surface the decided claim in the
                  DECISION layer (catches egress starvation, decision-layer
                  crowding, uncaptured decisions).
  currency        the expected note's version-chain HEAD must be latest
                  (catches version-anchoring / recency poisoning).
  freshness       a note updated in the last N days must be reachable
                  through the gated read surface (catches sweep/ingest
                  death).
  tension         a decision with a known newer source must carry the
                  ``tensions`` flag (catches the proposal-promotion class).

Anchoring is by STABLE ANCHOR, not literal HEAD id: the self-organizing
folds (VER-01/PAR-01/supersede) legitimately retire ids, so when an anchor
id has been superseded the runner FOLLOWS the version chain to the current
HEAD instead of firing a false alarm; a decision can also be anchored on
its CLAIM text (``claim_any`` substrings) — when both are given and the
anchor id is gone, the runner falls through to the claim match before
declaring anything invalid. A MISSING anchor with no claim fallback is
probe-INVALID (loud) — never a silent pass — and deterministic invalidity
is distinguished from transient CLI failure so a scheduler retries only
the transient class.

Scoring: weighted mean over the VALID probes -> one 0-1 number; every
probe also emits pass/fail + a reason string. Weights are validated at
load (finite, non-negative) — a bad weight is config-invalid, never
scored; an all-zero weight total is unscoreable config, not a regression.

Exit codes (the s07 maintain-fold interface — disposition precedence
action_required > regression > transient > ok: a deterministic config
problem outranks everything; a real regression on the RESOLVED probes is
never masked by an unrelated transient sibling; transient backs off only
when nothing worse is known):

  0  ok               score >= threshold, nothing invalid or transient
  1  regression       resolved-probe score < threshold — a real retrieval
                      regression (fires even if a sibling probe is transient)
  2  action_required  deterministic config problem (malformed probes file,
                      missing/renamed anchor id, non-numeric/negative
                      weight or field, zero weight total, supersession
                      cycle/runaway): fix the probes file / vault; do NOT
                      retry before the next scheduled run
  3  transient        the brain CLI itself failed / emitted non-JSON —
                      bounded backoff is appropriate

The emitted JSON ALWAYS carries the same ``disposition``/``exit_code`` the
process exits with — no path crashes to a bare traceback/exit 1.

Usage:
  brain-golden-probe <probes.json> [--vault DIR] [--brain-cmd CMD]
                     [--threshold F] [--max-tier TIER] [-k N] [--timeout S]
  python -m brain.golden_probe <probes.json> ...
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import shlex
import subprocess
import sys
from typing import Any, Callable, Optional

Call = Callable[[list], tuple]  # (args) -> (returncode, stdout)

EXIT_OK, EXIT_REGRESSION, EXIT_ACTION_REQUIRED, EXIT_TRANSIENT = 0, 1, 2, 3
# The cycle check (`cur in seen`) is the real guard against a corrupt chain;
# this is only a safety net against a pathological NON-cyclic linear chain.
# 100 is well above any realistic supersession-family depth (a note the nightly
# folds revise a few times a year takes ~decades to approach it — finding [204]:
# a low ceiling of 10 false-alarmed a long, correctly-evolving family) while
# still bounding a runaway walk to ~100 sequential `get`s, not ~1000 (finding
# [228]: each hop is a subprocess round-trip).
MAX_CHAIN_HOPS = 100
# The fixed egress-tier vocabulary (hardcoded because this runner is
# deliberately stdlib-only / engine-decoupled). Kept in sync with
# classification.TIERS by hand — an unknown --max-tier is a config error, not a
# retryable transient (review finding [487]).
VALID_TIERS = ("Public", "Internal", "Confidential", "Restricted", "MNPI")

PROBE_CLASSES = ("decision_state", "currency", "freshness", "tension")
_REQUIRED_KEYS = {
    "decision_state": ("query",),
    "currency": ("anchor_id",),
    "freshness": ("max_age_days",),
    "tension": ("query", "anchor_id"),
}


class ProbeFail(Exception):
    """The probe ran deterministically and the expectation did NOT hold."""


class ProbeInvalid(Exception):
    """Deterministic CONFIG failure (missing anchor id, malformed probe) —
    action_required, never a silent pass, never retried before next run.

    ``kind`` distinguishes a RETIRED/renamed anchor id (``"missing_anchor"`` —
    legitimate vault evolution the stable-anchor claim fallback recovers from)
    from real chain CORRUPTION (``"chain_corrupt"`` — a cycle/runaway that must
    NOT be papered over by the claim fallback; review finding [245])."""

    def __init__(self, msg: str, *, kind: str = "config"):
        super().__init__(msg)
        self.kind = kind


class ProbeTransient(Exception):
    """The brain CLI itself failed (crash / timeout / non-JSON output)."""


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------
def make_subprocess_call(brain_cmd: str, vault: Optional[str],
                         timeout: float) -> Call:
    """Production ``call``: run the installed brain CLI, capture stdout only
    (warnings go to stderr). Resolved from PATH / an explicit --brain-cmd —
    never repo-relative, so a launchd run with no repo CWD works."""
    base = shlex.split(brain_cmd)
    if vault:
        base += ["--vault", vault]

    def call(args: list) -> tuple:
        proc = subprocess.run(  # noqa: S603 — operator-supplied command
            base + list(args), capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout

    return call


def _cli_json(call: Call, args: list) -> tuple:
    """One brain CLI invocation -> (rc, parsed-JSON). Any crash or
    unparseable output is TRANSIENT (retryable), by definition: the probe
    never got a deterministic answer."""
    try:
        rc, out = call(args)
    except Exception as exc:  # timeout, OSError, missing binary
        raise ProbeTransient(f"brain CLI failed to run ({args[0]}): {exc}")
    if not out.strip():
        # A `--json` command ALWAYS emits a JSON object; empty stdout is a CLI
        # malfunction, not a valid empty payload (review finding [152]: a
        # rc=0 empty response was read as `{}` and treated as a real note).
        raise ProbeTransient(f"empty output from `brain {args[0]}` (rc={rc})")
    try:
        payload = json.loads(out)
    except ValueError:
        raise ProbeTransient(
            f"non-JSON output from `brain {args[0]}` (rc={rc})")
    return rc, payload


def _coerce_num(value: Any, field: str, *, integer: bool = False,
                minimum: Optional[float] = None, maximum: Optional[float] = None,
                exclusive_min: bool = False) -> Any:
    """Guarded numeric parse. A non-numeric / non-finite / out-of-range
    value in the probes file is a deterministic CONFIG error (ProbeInvalid
    -> action_required/exit 2) — never an uncaught crash, never scored."""
    if isinstance(value, bool):
        raise ProbeInvalid(f"{field} must be a number, got {value!r}")
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ProbeInvalid(f"{field} must be a number, got {value!r}")
    if not math.isfinite(num):
        raise ProbeInvalid(f"{field} must be finite, got {value!r}")
    if minimum is not None and (num <= minimum if exclusive_min else num < minimum):
        rel = ">" if exclusive_min else ">="
        raise ProbeInvalid(f"{field} must be {rel} {minimum}, got {value!r}")
    if maximum is not None and num > maximum:
        raise ProbeInvalid(f"{field} must be <= {maximum}, got {value!r}")
    return int(num) if integer else num


def _tier_args(max_tier: Optional[str]) -> list:
    # Deliberately EMPTY by default: the probes must measure the surface an
    # agent actually gets (default egress cap included) — pinning a tier
    # would hide a regressed default cap (the round-1 starvation class).
    return ["--max-tier", max_tier] if max_tier else []


def _clean_link(raw: Any) -> str:
    """Normalize a supersession pointer: '[[id|display]]' / 'id.md' -> 'id'."""
    s = str(raw or "").strip().strip('"').strip("'")
    if s.startswith("[[") and s.endswith("]]"):
        s = s[2:-2]
    s = s.split("|", 1)[0].strip()
    if s.endswith(".md"):
        s = s[:-3]
    return s


def _get_note(call: Call, note_id: str, max_tier: Optional[str]) -> tuple:
    """-> (status, note) with status in ok|missing|withheld."""
    rc, payload = _cli_json(
        call, ["get", note_id, "--json"] + _tier_args(max_tier))
    if rc == 0:
        return "ok", payload
    if rc == 1 and payload.get("error") == "not_found":
        return "missing", None
    if rc == 2 and payload.get("error") == "withheld_by_egress_filter":
        return "withheld", None
    raise ProbeTransient(f"`brain get {note_id}` returned rc={rc}")


def _chain_head(call: Call, anchor_id: str,
                max_tier: Optional[str]) -> tuple:
    """Follow the supersession chain from ``anchor_id`` to its HEAD.

    A superseded anchor is CORRECT vault evolution (the folds retire ids
    nightly) — follow it, don't alarm. A missing ANCHOR is config-invalid;
    a broken link mid-chain or a withheld note is a real integrity FAIL; a
    cycle / runaway chain is deterministic and unevaluable -> invalid
    (action_required), never a retry.
    -> (head_id, head_note, hops)
    """
    seen: list = []
    cur = anchor_id
    # Bound = MAX_CHAIN_HOPS *links* (MAX_CHAIN_HOPS + 1 notes): the HEAD
    # of a legitimately MAX-hop family is always inspected; only a chain
    # STILL carrying superseded_by past the bound is a runaway.
    while len(seen) <= MAX_CHAIN_HOPS:
        if cur in seen:
            raise ProbeInvalid(
                f"supersession CYCLE at {cur} (chain: {seen}) — fix the "
                f"chain (or the probes file anchor) before re-running",
                kind="chain_corrupt")
        seen.append(cur)
        status, note = _get_note(call, cur, max_tier)
        if status == "missing":
            if cur == anchor_id:
                raise ProbeInvalid(
                    f"anchor id not in index: {anchor_id} — update the "
                    f"probes file (renamed/removed note?)",
                    kind="missing_anchor")
            raise ProbeFail(
                f"broken supersession chain: {seen[-2]} -> {cur} (missing)")
        if status == "withheld":
            raise ProbeFail(
                f"note {cur} withheld by egress filter — starvation? "
                f"(re-check the default --max-tier)")
        nxt = _clean_link(note.get("superseded_by"))
        if not nxt:
            return cur, note, len(seen) - 1
        cur = nxt
    raise ProbeInvalid(
        f"supersession chain from {anchor_id} still carries superseded_by "
        f"after {MAX_CHAIN_HOPS} hops — cycle/runaway; fix the chain",
        kind="chain_corrupt")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="brain-golden-probe",
        description="Golden-set retrieval regression probes over the brain "
                    "CLI (see module docstring for the exit-code contract).")
    parser.add_argument("probes_file", help="path to the per-vault probes "
                        "JSON (lives with the vault, NOT in this repo)")
    parser.add_argument("--vault", default=None,
                        help="vault dir (passed to `brain --vault`)")
    parser.add_argument("--brain-cmd", default="brain",
                        help="brain CLI command (default: `brain` from PATH; "
                             "e.g. `python -m brain.cli`)")
    # Numeric flags are parsed as STRINGS then guarded-coerced below, NOT via
    # argparse `type=float/int` — a bad value there raises argparse's own usage
    # error to stderr with NO JSON on stdout, breaking the "JSON always carries
    # the disposition" contract for a CLI config typo (review finding [542]).
    parser.add_argument("--threshold", default=None,
                        help="override the probes-file threshold (0, 1]")
    parser.add_argument("--max-tier", default=None,
                        help="pass an explicit egress cap through to every "
                             "read (default: none — measure the real default)")
    parser.add_argument("-k", default="12", help="dossier depth")
    parser.add_argument("--timeout", default="120", help="per-CLI-call timeout, seconds")
    args = parser.parse_args(argv)

    # Validate ALL CLI config to a JSON action_required (NOT argparse's own
    # exit-2 usage error, which prints no JSON). An unknown --max-tier would
    # make every `brain` call fail argparse and be misread as a retryable
    # transient (finding [487]); the numeric flags are range/type-checked here
    # via the same guarded coercion the probes file uses (findings [400]/[542]).
    def _emit_cli_error(detail: str) -> int:
        print(json.dumps({
            "error": "invalid_config", "detail": detail,
            "disposition": "action_required", "score": None,
            "exit_code": EXIT_ACTION_REQUIRED, "probes_file": args.probes_file,
        }, indent=2))
        return EXIT_ACTION_REQUIRED

    if args.max_tier is not None and args.max_tier not in VALID_TIERS:
        return _emit_cli_error(
            f"--max-tier {args.max_tier!r} is not one of {list(VALID_TIERS)}")
    try:
        threshold = (None if args.threshold is None else _coerce_num(
            args.threshold, "--threshold", minimum=0.0, exclusive_min=True, maximum=1.0))
        k = _coerce_num(args.k, "-k", integer=True, minimum=1)
        timeout = _coerce_num(args.timeout, "--timeout", minimum=0.0, exclusive_min=True)
    except ProbeInvalid as exc:
        return _emit_cli_error(str(exc))

    try:
        with open(args.probes_file, "r", encoding="utf-8") as fh:
            spec = json.load(fh)
        if not isinstance(spec, dict):
            raise ValueError("top level is not an object")
    except Exception as exc:  # missing file / bad JSON: deterministic config
        print(json.dumps({
            "error": "invalid_probes_file",
            "detail": f"{args.probes_file}: {exc}",
            "disposition": "action_required", "score": None,
            "exit_code": EXIT_ACTION_REQUIRED,
        }, indent=2))
        return EXIT_ACTION_REQUIRED

    call = make_subprocess_call(args.brain_cmd, args.vault, timeout)
    try:
        doc = run_probes(spec, call, threshold=threshold, k=k,
                         max_tier=args.max_tier)
    except Exception as exc:
        # Contract: NO path exits with a bare traceback/exit 1. Anything
        # that escapes run_probes still emits a JSON disposition — a
        # deterministic config error is action_required (2), everything
        # unexpected is transient (3): the run never got a deterministic
        # answer, so bounded backoff is the honest disposition.
        if isinstance(exc, ProbeInvalid):
            disposition, code = "action_required", EXIT_ACTION_REQUIRED
        else:
            disposition, code = "transient", EXIT_TRANSIENT
        doc = {
            "error": "unexpected_error",
            "detail": f"{type(exc).__name__}: {exc}",
            "disposition": disposition, "score": None,
            "exit_code": code,
        }
    doc["probes_file"] = args.probes_file
    print(json.dumps(doc, indent=2))
    return int(doc["exit_code"])


_golden_probe_cases = importlib.import_module("brain.golden_probe_cases")

_golden_probe_cases.configure(globals())
_claims_of = _golden_probe_cases._claims_of
_probe_decision_state = _golden_probe_cases._probe_decision_state
_probe_currency = _golden_probe_cases._probe_currency
_probe_freshness = _golden_probe_cases._probe_freshness
_probe_tension = _golden_probe_cases._probe_tension
_PROBE_FNS = _golden_probe_cases._PROBE_FNS
_validate_probe = _golden_probe_cases._validate_probe
run_probes = _golden_probe_cases.run_probes


if __name__ == "__main__":  # python -m brain.golden_probe
    sys.exit(main())

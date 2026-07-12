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
import datetime as _dt
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


# ---------------------------------------------------------------------------
# probe classes
# ---------------------------------------------------------------------------
def _claims_of(probe: dict) -> list:
    """Validated ``claim_any`` (lowercased). Must be a LIST of non-empty
    strings — a bare string would iterate per-character and match on a single
    common letter/space, blinding the gate (review finding [263]); an empty
    string is a substring of everything. Absent ⇒ ``[]``."""
    raw = probe.get("claim_any")
    if raw is None:
        return []
    if not isinstance(raw, list) or any(
            not isinstance(c, str) or not c.strip() for c in raw):
        raise ProbeInvalid(
            "claim_any must be a list of non-empty strings")
    return [c.strip().lower() for c in raw]


def _probe_decision_state(call: Call, probe: dict, *, k: int,
                          max_tier: Optional[str]) -> str:
    claims = _claims_of(probe)
    anchor = probe.get("anchor_id")
    if not claims and not anchor:
        raise ProbeInvalid(
            "decision_state probe needs anchor_id and/or claim_any")

    # Resolve the anchor (if any) to its live HEAD. A RETIRED/renamed anchor
    # (kind="missing_anchor") is legitimate vault evolution → drop to the
    # claim path; real chain CORRUPTION (kind="chain_corrupt") must NOT be
    # papered over → re-raise as action_required (review finding [245]).
    head_id = None  # set iff the anchor is LIVE (resolved to a HEAD)
    if anchor:
        try:
            head_id, _, _ = _chain_head(call, anchor, max_tier)
        except ProbeInvalid as exc:
            if not claims or getattr(exc, "kind", "config") != "missing_anchor":
                raise
            head_id = None

    rc, res = _cli_json(
        call, ["dossier", probe["query"], "--json", "-k", str(k)]
        + _tier_args(max_tier))
    if rc != 0:
        raise ProbeTransient(f"dossier returned rc={rc}")
    decisions = res.get("decisions") or []
    withheld = (res.get("egress") or {}).get("withheld", 0)
    starve = (f"; {withheld} note(s) withheld by egress — starvation?"
              if withheld else "")

    # A LIVE anchor is the PRIMARY, most specific expectation: it MUST be in
    # the decision layer. If it resolved but is NOT surfaced, the specifically
    # anchored decision has been crowded out — that IS the decision-layer
    # crowding regression this probe class exists to catch (review finding
    # [293]); it must NOT be silently accepted just because a SIBLING decision
    # happens to carry the claim substring. Claims are the fallback ONLY when
    # there is no live anchor (retired, or none given).
    if head_id is not None:
        if any(d.get("id") == head_id for d in decisions):
            return f"decision layer surfaced anchor {head_id}"
        # KNOWN AUTHORING TRADEOFF (review finding [317]): "surfaced" means
        # "within the top-k decision layer". A decision that is genuinely
        # current but legitimately ranks BELOW -k for a broad query reads as a
        # (mild) crowding regression here. That is intentional — the anchored
        # decision SHOULD rank high for its own probe query — but it means a
        # decision_state probe must be authored with a query specific enough
        # (and a -k large enough) that its decision reliably sits in the top-k.
        # A too-broad probe is a probes-file tuning issue, not a vault fault.
        raise ProbeFail(
            f"anchor {head_id} resolved but is NOT in the top-{k} decision "
            f"layer ({len(decisions)} hit(s)) — crowded out?{starve}")

    # No live anchor: match the claim in title+snippet, then full bodies.
    for d in decisions:
        hay = (str(d.get("title", "")) + " " + str(d.get("snippet", ""))).lower()
        if any(c in hay for c in claims):
            return f"decision layer carries the claim (in {d.get('id')})"
    body_fetch_transient = False
    for d in decisions:  # fetch ALL surfaced decisions' bodies (the dossier
        # already caps their count) — a fixed [:N] slice below the cap would
        # skip a legitimate match at high -k and false-alarm (finding [301]).
        try:
            status, note = _get_note(call, d.get("id", ""), max_tier)
        except ProbeTransient:
            # A flaky secondary body fetch leaves the match UNDETERMINED — the
            # claim may be hidden behind it. If nothing else matches, report
            # transient (retry), never a false regression (finding [275]).
            body_fetch_transient = True
            continue
        if status == "ok" and any(
                c in str(note.get("body", "")).lower() for c in claims):
            return f"decision layer carries the claim (in {d.get('id')})"
    if body_fetch_transient:
        raise ProbeTransient(
            "a candidate decision's body fetch was transient; the claim "
            "match is undetermined — retry rather than assert a regression")
    raise ProbeFail(
        f"decision layer ({len(decisions)} hit(s)) lacks the expected "
        f"decision (claims={claims}){starve}")


def _probe_currency(call: Call, probe: dict, *, k: int,
                    max_tier: Optional[str]) -> str:
    head_id, head, hops = _chain_head(call, probe["anchor_id"], max_tier)
    latest = str(head.get("is_latest_version") or "").lower()
    if latest == "false":
        raise ProbeFail(
            f"version-chain HEAD {head_id} is retired "
            f"(is_latest_version: false) with no successor — stale HEAD")
    via = f" (followed {hops} supersession hop(s))" if hops else ""
    return f"chain HEAD {head_id} is current{via}"


def _probe_freshness(call: Call, probe: dict, *, k: int,
                     max_tier: Optional[str]) -> str:
    max_age = _coerce_num(probe["max_age_days"], "max_age_days",
                          integer=True, minimum=0)
    # -n 200 (not 20): `brain recent` limits BEFORE the egress gate, so at a
    # narrow --max-tier a small window could be entirely withheld while older
    # reachable notes exist, giving a false "sweep death" (review finding
    # [305]). A wide window makes that vanishingly unlikely; the host default
    # (full vault, no --max-tier) was never affected.
    rc, res = _cli_json(
        call, ["recent", "--json", "-n", "200"] + _tier_args(max_tier))
    if rc != 0:
        raise ProbeTransient(f"recent returned rc={rc}")
    items = res.get("results") or []
    if not items:
        raise ProbeFail("`brain recent` surfaced nothing — empty index or "
                        "total egress starvation")
    today_utc = _dt.datetime.now(_dt.timezone.utc).date()
    dated = []
    for it in items:
        try:
            d = _dt.date.fromisoformat(str(it.get("updated", ""))[:10])
        except ValueError:
            continue  # 'unknown' etc.
        # Ignore FUTURE-dated notes (review finding [388]): a clock-skew /
        # template-placeholder / typo'd future `updated` would otherwise
        # dominate max(), give a non-positive age, and always pass — masking a
        # genuinely dead sweep/ingest.
        if d <= today_utc:
            dated.append((d, it))
    if not dated:
        raise ProbeFail("no non-future parseable `updated` date among recent "
                        "notes (empty index, or all newest notes are "
                        "future-dated — check the clock)")
    newest_date, newest = max(dated, key=lambda t: t[0])
    # UTC 'today' vs the UTC-stamped `updated` date (review finding [365]):
    # local date.today() can be a day ahead just after local midnight in a
    # timezone behind UTC, false-failing a note updated 'today' in UTC.
    age = (today_utc - newest_date).days
    if age > max_age:
        raise ProbeFail(
            f"newest indexed note is {age}d old (> {max_age}d) — "
            f"sweep/ingest death? (newest: {newest.get('id')})")
    # No re-fetch via `get`: `recent` already applied the egress gate, so
    # the note surfacing at all IS the reachability evidence (per-subcommand
    # gate parity is the engine's own test suite, not this probe's job).
    return f"newest note {newest.get('id')} is {age}d old (within {max_age}d)"


def _probe_tension(call: Call, probe: dict, *, k: int,
                   max_tier: Optional[str]) -> str:
    head_id, _, _ = _chain_head(call, probe["anchor_id"], max_tier)
    rc, res = _cli_json(
        call, ["dossier", probe["query"], "--json", "-k", str(k)]
        + _tier_args(max_tier))
    if rc != 0:
        raise ProbeTransient(f"dossier returned rc={rc}")
    match = next((d for d in res.get("decisions") or []
                  if d.get("id") == head_id), None)
    if match is None:
        raise ProbeFail(
            f"decision {head_id} absent from the dossier decision layer — "
            f"cannot evaluate tensions")
    tensions = match.get("tensions") or []
    if not tensions:
        raise ProbeFail(
            f"decision {head_id} carries NO tension flag despite an "
            f"expected newer source — proposal-promotion guard is blind")
    # Normalize the expected id the SAME way the tension ids are normalized
    # (review finding [423]): a wikilink/`.md` form in the probes file
    # ('[[widget-memo]]') must match the cleaned tension id ('widget-memo'),
    # not false-fail a healthy vault that carries the tension.
    want = _clean_link(probe.get("expect_source_id")) if probe.get("expect_source_id") else None
    if want and all(_clean_link(t.get("id")) != want for t in tensions):
        raise ProbeFail(
            f"tension list on {head_id} lacks expected source {want} "
            f"(has: {[t.get('id') for t in tensions]})")
    return (f"decision {head_id} carries {len(tensions)} tension flag(s)")


_PROBE_FNS = {
    "decision_state": _probe_decision_state,
    "currency": _probe_currency,
    "freshness": _probe_freshness,
    "tension": _probe_tension,
}


# ---------------------------------------------------------------------------
# scoring + run
# ---------------------------------------------------------------------------
def _validate_probe(probe: Any) -> str:
    """-> probe class, or raise ProbeInvalid (deterministic config error)."""
    if not isinstance(probe, dict):
        raise ProbeInvalid("probe entry is not an object")
    cls = probe.get("class")
    if cls not in PROBE_CLASSES:
        raise ProbeInvalid(f"unknown probe class: {cls!r}")
    # presence, not truthiness: `max_age_days: 0` is a VALID value
    missing = [key for key in _REQUIRED_KEYS[cls]
               if key not in probe or probe[key] is None]
    if missing:
        raise ProbeInvalid(f"{cls} probe missing required key(s): {missing}")
    return cls


def run_probes(spec: dict, call: Call, *, threshold: Optional[float] = None,
               k: int = 12, max_tier: Optional[str] = None) -> dict:
    """Execute every probe in ``spec`` -> the result document (see module
    docstring for the disposition/exit-code contract)."""
    probes = spec.get("probes")
    if not isinstance(probes, list) or not probes:
        return {
            "error": "invalid_probes_file",
            "detail": "probes file has no `probes` list",
            "disposition": "action_required", "score": None,
            "probes": [], "exit_code": EXIT_ACTION_REQUIRED,
        }
    # Validate ALL run config HERE — run_probes is the s07 maintain-fold's
    # direct-call seam, so it must guard the injected threshold/k/max_tier as
    # tightly as main()'s CLI seam (findings [441]/[456]/[457]); otherwise a bad
    # injected value blinds/false-alarms the gate. threshold in (0,1] ([400]);
    # k >= 1; max_tier in VALID_TIERS ([487]).
    def _config_error(detail: str) -> dict:
        return {"error": "invalid_config", "detail": detail,
                "disposition": "action_required", "score": None,
                "probes": [], "exit_code": EXIT_ACTION_REQUIRED}
    if max_tier is not None and max_tier not in VALID_TIERS:
        return _config_error(f"max_tier {max_tier!r} is not one of {list(VALID_TIERS)}")
    try:
        threshold = _coerce_num(
            spec.get("threshold", 1.0) if threshold is None else threshold,
            "threshold", minimum=0.0, exclusive_min=True, maximum=1.0)
        k = _coerce_num(k, "k", integer=True, minimum=1)
    except ProbeInvalid as exc:
        return _config_error(str(exc))

    results = []
    weight_total = 0.0
    weight_passed = 0.0
    counts = {"pass": 0, "fail": 0, "invalid": 0, "transient": 0}
    for probe in probes:
        pid = probe.get("id", "?") if isinstance(probe, dict) else "?"
        weight = 1.0
        try:
            cls = _validate_probe(probe)
            # weight must be > 0 (review finding [522]): a weight-0 probe is
            # dropped from the score, so a weight-0 probe that FAILS would leave
            # a real regression invisible (self-certify healthy). A probe to be
            # de-emphasized should carry a small positive weight, or be removed
            # — weight 0 is a config error, surfaced as invalid, never silent.
            weight = _coerce_num(probe.get("weight", 1.0), "weight",
                                 minimum=0.0, exclusive_min=True)
            reason = _PROBE_FNS[cls](call, probe, k=k, max_tier=max_tier)
            status = "pass"
        except ProbeFail as exc:
            status, reason = "fail", str(exc)
        except ProbeInvalid as exc:
            status, reason = "invalid", str(exc)
        except ProbeTransient as exc:
            status, reason = "transient", str(exc)
        except Exception as exc:  # noqa: BLE001 — review finding [425]/[480]: an
            # unexpected error in ONE probe (a malformed CLI JSON shape raising
            # AttributeError/KeyError, a corrupt note) must be CONTAINED to that
            # probe, never escape and discard every other probe's verdict. It is
            # classified INVALID (action_required), NOT transient: an unexpected
            # shape is a DETERMINISTIC "fix the vault/probe" problem, and a
            # transient classification would retry-loop it forever and silently
            # blind the gate to that failure class ([480]). action_required
            # surfaces it AND the next scheduled run still re-runs it fresh.
            status, reason = "invalid", f"unexpected: {type(exc).__name__}: {exc}"
        cls_name = probe.get("class", "?") if isinstance(probe, dict) else "?"
        results.append({"id": pid, "class": cls_name, "status": status,
                        "weight": weight, "reason": reason})
        counts[status] += 1
        if status in ("pass", "fail"):  # only VALID probes enter the score
            weight_total += weight
            if status == "pass":
                weight_passed += weight

    score = (round(weight_passed / weight_total, 4)
             if weight_total > 0 else None)
    # Precedence, refined for finding [531]: a genuine REGRESSION on the
    # resolved probes is the most actionable signal for the maintain fold, so
    # it must NOT be masked by a per-probe `invalid` (an unconfigured/errored
    # sibling probe) — regression outranks a per-probe invalid. (A SPEC-level
    # config error — bad threshold/k/max_tier, no probes — already returned
    # action_required up front, before any probe ran.) Order:
    #   regression > per-probe invalid (action_required) > transient >
    #   nothing-resolved (action_required) > ok.
    if score is not None and score < threshold - 1e-9:
        disposition, exit_code = "regression", EXIT_REGRESSION
    elif counts["invalid"]:
        disposition, exit_code = "action_required", EXIT_ACTION_REQUIRED
    elif counts["transient"]:
        disposition, exit_code = "transient", EXIT_TRANSIENT
    elif score is None:  # nothing resolved to a pass/fail — nothing to score
        disposition, exit_code = "action_required", EXIT_ACTION_REQUIRED
    else:
        disposition, exit_code = "ok", EXIT_OK

    return {
        "score": score,
        "threshold": threshold,
        "disposition": disposition,
        "exit_code": exit_code,
        "counts": counts,
        "probes": results,
        "captured": _dt.datetime.now(_dt.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "baseline": spec.get("baseline"),
    }


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


if __name__ == "__main__":  # python -m brain.golden_probe
    sys.exit(main())

"""The self-eval check (moved VERBATIM, never reorganized in place)."""
from __future__ import annotations

from typing import Any

from . import cos

def check_self_eval(vault, run_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """(a) The run reported its self-eval, and reported ALL of it.

    The expected count is re-derived from THE RUN MANIFEST'S skill digest, not
    from whatever ``SKILL.md`` is deployed now — the deployed bundle changes
    between a run and its validation (that is the whole reason the manifest is
    frozen at launch), and counting against the wrong bundle is the same
    claim-time-vs-production-time error the manifest exists to prevent.
    """
    report = cos.run_ops_dir(vault) / f"_cos_nightly_{run_id}.md"
    try:
        text = report.read_text(encoding="utf-8")
    except OSError as exc:
        return _row("self_eval", INCONCLUSIVE,
                    f"the run report {report.name} is unreadable ({exc}) — the "
                    "host cannot tell whether the self-eval ran",
                    reexecuted=False)
    # AN OUTCOME DECIDES, NOT A PRINTED ID (DOCTRINE v7 §8.1 rule 3). This line
    # read `for n, _ in ...findall` until 2026-08-14 — it CAPTURED the verdict
    # token and threw it away — so a report whose every E-check said FAIL
    # produced the id set {1..10} and scored this control PASS. Probed, not
    # assumed (`_evidence/cosv7/s01-echeck-probe.txt`): that is exactly what
    # today's shipped verifier does. A checker that reads ids instead of
    # answers is the same defect one level up as a run that grades its own
    # homework.
    results: dict[int, set[str]] = {}
    for n, verdict in _REPORT_ECHECK_RE.findall(text):
        token = verdict.upper().replace("/", "")
        results.setdefault(int(n), set()).add("NA" if token == "NA" else token)
    found = set(results)

    expected, why = expected_check_count(manifest)
    if expected is None:
        return _row("self_eval", DEGRADED,
                    f"{len(found)} self-eval check result(s) in "
                    f"{report.name}, but the EXPECTED count is not derivable "
                    f"host-side: {why}. Presence alone proves a string was "
                    "printed, so this scores degraded, never valid",
                    reexecuted=False)
    if not found:
        return _row("self_eval", FAIL,
                    f"the run report {report.name} carries ZERO self-eval check "
                    f"results; the bundle the host froze for this run "
                    f"({manifest.get('bundle_version')}) defines {expected}. "
                    "The run skipped its entire self-eval — the exact run-59 "
                    "failure this validator exists to catch",
                    reexecuted=False)
    # The SET, not the count. Counting alone lets a run report E1-E28 plus an
    # invented E40 and score PASS with E29 never checked — the same
    # "the instrument cannot fail" shape this validator exists to catch, one
    # level up. Measured: run 60's report numbers its block E01-E29 with labels
    # that do not correspond to the doctrine's checks at all, and a count-only
    # test cannot see that. The ids are all this check can bind; it binds them.
    missing = sorted(set(range(1, expected + 1)) - found)
    if missing:
        return _row("self_eval", FAIL,
                    f"{report.name} reports {len(found)} of {expected} self-eval "
                    f"checks defined by the run's own bundle "
                    f"({manifest.get('bundle_version')}); "
                    f"missing E{', E'.join(str(m) for m in missing[:12])}"
                    + (" …" if len(missing) > 12 else ""),
                    reexecuted=False)

    # -- now the ANSWERS, which is the half that could not fail before -------
    conflicting = sorted(i for i, r in results.items() if len(r) > 1)
    if conflicting:
        return _row("self_eval", FAIL,
                    f"{report.name} reports TWO CONFLICTING results for "
                    f"E{', E'.join(str(i) for i in conflicting[:8])} "
                    + "; ".join(f"E{i}={sorted(results[i])}"
                                for i in conflicting[:4])
                    + ". One check, one outcome — a duplicated id is a FAIL, "
                      "because whichever line a reader happens to see decides "
                      "the night",
                    reexecuted=False)
    failed = sorted(i for i, r in results.items() if "FAIL" in r)
    if failed:
        return _row("self_eval", FAIL,
                    f"{report.name} reports E"
                    f"{', E'.join(str(i) for i in failed[:12])}"
                    + (" …" if len(failed) > 12 else "")
                    + f" as FAIL ({len(failed)} of {expected}). ANY FAIL fails "
                      "the self-eval: the checks are the run's own account of "
                      "what it did to the mailbox, and a night that reports a "
                      "breach has not passed by reporting it honestly",
                    reexecuted=False)

    # `N/A` IS CORROBORATED, NEVER BELIEVED. It is legal only against a
    # MACHINE-DERIVED zero denominator, so the machine derives them here rather
    # than reading the number the report printed beside its own claim.
    na = sorted(i for i, r in results.items() if "NA" in r)
    if na:
        try:
            from . import cos_echecks                      # noqa: PLC0415
            host = cos_echecks.denominators(vault, run_id)
        except Exception as exc:                           # noqa: BLE001
            return _row("self_eval", FAIL,
                        f"{report.name} answers E"
                        f"{', E'.join(str(i) for i in na[:8])} `N/A`, and the "
                        f"host could not re-derive their denominators to "
                        f"corroborate it ({type(exc).__name__}: "
                        f"{str(exc)[:120]}). An uncorroborated N/A is a check "
                        "that scored itself",
                        reexecuted=True)
        uncorroborated = {i: host.get(i) for i in na if host.get(i)}
        never = [i for i in na if i in cos_echecks.NEVER_NA]
        if never:
            return _row("self_eval", FAIL,
                        f"{report.name} answers E"
                        f"{', E'.join(str(i) for i in never)} `N/A`, which "
                        "those checks may never be — their denominators (the "
                        "sent baseline, the frozen capability digest) exist on "
                        "every run",
                        reexecuted=True)
        if uncorroborated:
            return _row("self_eval", FAIL,
                        f"{report.name} answers "
                        + ", ".join(f"E{i} `N/A` over a host-derived "
                                    f"denominator of {n}"
                                    for i, n in sorted(uncorroborated.items()))
                        + ". N/A is legal only against a MACHINE-DERIVED ZERO "
                          "denominator; on a non-zero one it is a FAIL",
                        reexecuted=True)
    return _row("self_eval", PASS,
                f"{len(found)} self-eval check result(s) reported and DECIDED, "
                f"against {why} — never against whatever SKILL.md is deployed "
                f"now. No FAIL, no duplicated or conflicting id"
                + (f", and {len(na)} N/A corroborated against a host-derived "
                   "zero denominator" if na else ""),
                reexecuted=bool(na))


#: The self-eval header every run report carries, e.g.

# Parent/metrics binds, deferred past this module's own defs.
from .cos_runverify import (  # noqa: E402
    DEGRADED as DEGRADED,
    FAIL as FAIL,
    INCONCLUSIVE as INCONCLUSIVE,
    PASS as PASS,
    _REPORT_ECHECK_RE as _REPORT_ECHECK_RE,
    _row as _row,
)
from .cos_runverify_metrics import expected_check_count as expected_check_count  # noqa: E402

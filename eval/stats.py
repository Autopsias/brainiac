#!/usr/bin/env python3
"""EF-04 (S03) — statistical rigor toolkit for the PT retrieval eval.

Every "parity" or "win" claim in this eval must carry a confidence interval
and a significance test, not a single point estimate that could be noise.
This module is the SHARED, TESTED home for the primitives; `eval/gate.py`
imports from here rather than keeping its own private copies.

Three primitives, kept DELIBERATELY SEPARATE (H19 — one primary regime):

  * bootstrap_ci             — DESCRIPTIVE effect-size interval ONLY. Useful
                                everywhere, but never itself the confirmatory
                                significance decision.
  * paired_permutation_test  — THE ONE primary significance regime for this
                                eval. Runs EXACTLY ONCE, on the LOCKED
                                held-out split (H37), by session s11b. On any
                                other fold (train / dev / adoption-validation)
                                its output is descriptive/informational only
                                — pass `fold_context="held-out"` to label it
                                PRIMARY; any other value prints an explicit
                                "not confirmatory" caveat. K-fold CV
                                out-of-fold numbers are descriptive-only for
                                the same reason (K different retrained
                                pipelines break exchangeability) — do not feed
                                pooled-fold deltas into this function and call
                                it a significance test.
  * benjamini_hochberg       — FDR control across a FAMILY of stratum-level
                                tests (H20). Apply whenever reporting >=2
                                language x class slice p-values in the same
                                breath — uncorrected multiplicity manufactures
                                false 'wins'.

Plus MDE / power (H20 "pre-register a minimum detectable effect + reported
power; the success gate is an effect-size threshold, NOT a bare 'CI lower
bound > 0'"):

  * minimum_detectable_effect — given n, an observed/assumed SD, alpha and a
                                 target power, the smallest true effect this
                                 sample size could reliably detect.
  * achieved_power            — given n, SD, alpha and an observed/assumed
                                 effect, the power actually achieved.

All functions are deterministic given a seed (numpy `Generator` instances,
never global RNG state) — same inputs + same seed => byte-identical output,
so re-runs match. No scipy dependency: `statistics.NormalDist` (stdlib,
3.8+) supplies the normal CDF/inverse-CDF for the power calculations.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np

_NORMAL = NormalDist()


# --------------------------------------------------------------------------
# 1. Bootstrap CI — descriptive effect-size interval ONLY (H19)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class BootstrapCI:
    mean: float
    ci_lower: float
    ci_upper: float
    n: int
    b: int
    alpha: float
    seed: int
    kind: str = "descriptive_effect_size_interval"

    def as_dict(self) -> dict:
        return {
            "mean": self.mean,
            "ci_lower": self.ci_lower,
            "ci_upper": self.ci_upper,
            "n": self.n,
            "b": self.b,
            "alpha": self.alpha,
            "seed": self.seed,
            "kind": self.kind,
        }


def bootstrap_ci(deltas, b: int = 10000, seed: int = 7, alpha: float = 0.05) -> BootstrapCI:
    """Two-sided (1-alpha) percentile bootstrap CI on the mean of `deltas`.

    DESCRIPTIVE ONLY (H19) — an effect-size interval, never itself a
    hypothesis test or a gate. Deterministic: identical (deltas, b, seed)
    always produces byte-identical output (a fresh `np.random.default_rng`
    is created per call — no shared/global RNG state).
    """
    arr = np.asarray(deltas, dtype=float)
    n = arr.size
    if n == 0:
        nan = float("nan")
        return BootstrapCI(nan, nan, nan, 0, b, alpha, seed)
    rng = np.random.default_rng(seed)
    resample_means = arr[rng.integers(0, n, size=(b, n))].mean(axis=1)
    lo = float(np.percentile(resample_means, 100 * alpha / 2))
    hi = float(np.percentile(resample_means, 100 * (1 - alpha / 2)))
    return BootstrapCI(float(arr.mean()), lo, hi, n, b, alpha, seed)


def bootstrap_ci_lower_one_sided(deltas, b: int = 10000, seed: int = 7, alpha: float = 0.05) -> float:
    """One-sided lower bound (the alpha percentile), for non-inferiority
    framing. Still descriptive (H19) — the *effect-size threshold* it is
    compared against is what makes a gate decision valid, not this number in
    isolation."""
    arr = np.asarray(deltas, dtype=float)
    if arr.size == 0:
        return float("nan")
    rng = np.random.default_rng(seed)
    n = arr.size
    means = arr[rng.integers(0, n, size=(b, n))].mean(axis=1)
    return float(np.percentile(means, 100 * alpha))


# --------------------------------------------------------------------------
# 2. Paired permutation (sign-flip / Fisher randomization) test — THE ONE
#    primary significance regime (H19), for the locked held-out split.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PermutationResult:
    observed_mean: float
    p_two_sided: float
    p_greater: float
    p_less: float
    n: int
    b: int
    seed: int
    exact: bool
    fold_context: str
    kind: str

    def as_dict(self) -> dict:
        return {
            "observed_mean_delta": self.observed_mean,
            "p_two_sided": self.p_two_sided,
            "p_one_sided_greater": self.p_greater,
            "p_one_sided_less": self.p_less,
            "n": self.n,
            "b": self.b,
            "seed": self.seed,
            "exact": self.exact,
            "fold_context": self.fold_context,
            "kind": self.kind,
        }

    @property
    def is_primary(self) -> bool:
        return self.fold_context == "held-out"

    def caveat(self) -> str:
        if self.is_primary:
            return "PRIMARY significance test (H19) — locked held-out split, single read."
        return ("NOT confirmatory (H19) — informational only. The primary "
                "significance regime runs exactly once, on the locked "
                "held-out split (session s11b).")


def paired_permutation_test(
    deltas,
    b: int = 10000,
    seed: int = 7,
    fold_context: str = "unknown",
) -> PermutationResult:
    """Paired sign-flip permutation test, H0: the per-query deltas are drawn
    from a distribution symmetric about 0 (Fisher randomization on the sign).

    `fold_context` must be one of "held-out", "non-held-out", "unknown" —
    it only changes labeling/caveats (`PermutationResult.is_primary` /
    `.caveat()`), never the computed statistics. Pass "held-out" ONLY when
    this really is the single locked held-out read (H37) — mislabeling this
    is exactly the multiplicity/p-hacking failure mode H19/H20 exist to
    prevent.

    Exact enumeration for n <= 20 (2**n <= 1,048,576 sign patterns) — no RNG,
    the p-value is an exact rational number. Monte Carlo (seeded, `b` draws)
    for n > 20. Either way: deterministic given (deltas, b, seed).
    """
    if fold_context not in ("held-out", "non-held-out", "unknown"):
        raise ValueError(f"fold_context must be held-out/non-held-out/unknown, got {fold_context!r}")
    arr = np.asarray(deltas, dtype=float)
    n = arr.size
    kind = "primary_significance_test" if fold_context == "held-out" else "descriptive_significance_test"
    if n == 0:
        nan = float("nan")
        return PermutationResult(nan, nan, nan, nan, 0, b, seed, False, fold_context, kind)

    obs = float(arr.mean())
    exact = n <= 20
    if exact:
        total = 1 << n
        bits = (np.arange(total, dtype=np.int64)[:, None] >> np.arange(n)[None, :]) & 1
        signs = bits.astype(np.float64) * 2 - 1  # 0/1 -> -1/+1
        perm_means = (signs * arr).mean(axis=1)
        b_eff = total
        p_greater = float((perm_means >= obs).mean())
        p_less = float((perm_means <= obs).mean())
        p_two_sided = float((np.abs(perm_means) >= abs(obs) - 1e-12).mean())
    else:
        rng = np.random.default_rng(seed)
        signs = rng.choice(np.array([-1.0, 1.0]), size=(b, n))
        perm_means = (signs * arr).mean(axis=1)
        b_eff = b
        # Monte Carlo +1 correction (Davison & Hinkley) — a permutation
        # p-value of exactly 0 is never reportable from a finite sample.
        p_greater = (np.sum(perm_means >= obs) + 1) / (b_eff + 1)
        p_less = (np.sum(perm_means <= obs) + 1) / (b_eff + 1)
        p_two_sided = (np.sum(np.abs(perm_means) >= abs(obs)) + 1) / (b_eff + 1)
        p_greater, p_less, p_two_sided = float(p_greater), float(p_less), float(p_two_sided)

    return PermutationResult(obs, p_two_sided, p_greater, p_less, n, b_eff, seed, exact,
                              fold_context, kind)


# --------------------------------------------------------------------------
# 3. Benjamini-Hochberg FDR correction — H20 family-wise correction across
#    every language x class slice test.
# --------------------------------------------------------------------------

def benjamini_hochberg(pvalues, alpha: float = 0.05) -> tuple[list[bool], list[float]]:
    """Benjamini-Hochberg step-up FDR procedure.

    Controls the FALSE DISCOVERY RATE across a FAMILY of `m` simultaneous
    tests (e.g. one paired-permutation p-value per language x class slice —
    H20: ">=10-15 of them" for this eval, run WITHOUT correction they
    manufacture false 'wins'). Returns `(rejected, adjusted_p)` in the
    ORIGINAL input order:

      rejected[i]    -- True iff H0 is rejected for test i at FDR level `alpha`
      adjusted_p[i]  -- the BH-adjusted p-value (q-value); monotone
                        non-decreasing when re-sorted by raw p, capped at 1.0

    Deterministic (no RNG) — a pure order-statistic computation.
    """
    p = np.asarray(pvalues, dtype=float)
    m = p.size
    if m == 0:
        return [], []
    order = np.argsort(p, kind="stable")
    ranked = p[order]
    ranks = np.arange(1, m + 1, dtype=float)
    raw_adj = ranked * m / ranks
    # step-up monotonicity: adjusted q-values must be non-decreasing as the
    # raw p-value increases, enforced from the largest p-value downward.
    adj_sorted = np.minimum.accumulate(raw_adj[::-1])[::-1]
    adj_sorted = np.clip(adj_sorted, 0.0, 1.0)
    adjusted = np.empty(m, dtype=float)
    adjusted[order] = adj_sorted
    rejected = adjusted <= alpha
    return [bool(x) for x in rejected], [float(x) for x in adjusted]


# --------------------------------------------------------------------------
# 4. Minimum detectable effect / achieved power (H20 pre-registration)
# --------------------------------------------------------------------------

def minimum_detectable_effect(n: int, sd: float, alpha: float = 0.05, power: float = 0.8) -> float:
    """The smallest TRUE mean effect that a paired two-sided test at level
    `alpha` with `n` paired observations and per-query-delta standard
    deviation `sd` would detect with the target `power`, under a normal
    approximation to the sampling distribution of the mean delta:

        MDE = SE * (z_(1-alpha/2) + z_power),   SE = sd / sqrt(n)

    This is the standard closed-form two-sided z-test MDE. Used to
    pre-register "what could we even have detected" alongside any observed
    delta (H20) — a bare 'CI lower bound > 0' is not a success criterion;
    an effect-size threshold vetted against the MDE is.
    """
    if n <= 0 or sd < 0:
        return float("nan")
    if sd == 0:
        return 0.0
    se = sd / math.sqrt(n)
    z_alpha2 = _NORMAL.inv_cdf(1 - alpha / 2)
    z_power = _NORMAL.inv_cdf(power)
    return float(se * (z_alpha2 + z_power))


def achieved_power(n: int, sd: float, effect: float, alpha: float = 0.05) -> float:
    """The power actually achieved for a true effect of size `effect` given
    `n` paired observations, per-query-delta SD `sd`, and two-sided level
    `alpha` (normal approximation; inverse of `minimum_detectable_effect`).
    """
    if n <= 0 or sd < 0:
        return float("nan")
    if sd == 0:
        return 1.0 if effect != 0 else float(alpha)
    se = sd / math.sqrt(n)
    z_alpha2 = _NORMAL.inv_cdf(1 - alpha / 2)
    ncp = effect / se
    return float(_NORMAL.cdf(ncp - z_alpha2) + _NORMAL.cdf(-ncp - z_alpha2))


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deltas-json", help="path to a JSON list of per-query deltas")
    ap.add_argument("--bootstrap", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--fold-context", default="unknown",
                    choices=["held-out", "non-held-out", "unknown"])
    ap.add_argument("--target-power", type=float, default=0.8)
    args = ap.parse_args()

    if not args.deltas_json:
        ap.error("--deltas-json is required for CLI use; import the module for library use")

    from pathlib import Path
    deltas = json.loads(Path(args.deltas_json).read_text(encoding="utf-8"))

    ci = bootstrap_ci(deltas, b=args.bootstrap, seed=args.seed, alpha=args.alpha)
    perm = paired_permutation_test(deltas, b=args.bootstrap, seed=args.seed,
                                    fold_context=args.fold_context)
    sd = float(np.std(np.asarray(deltas, dtype=float), ddof=1)) if len(deltas) > 1 else 0.0
    mde = minimum_detectable_effect(len(deltas), sd, alpha=args.alpha, power=args.target_power)
    pw = achieved_power(len(deltas), sd, ci.mean, alpha=args.alpha)

    print(json.dumps({
        "bootstrap_ci": ci.as_dict(),
        "paired_permutation_test": perm.as_dict(),
        "caveat": perm.caveat(),
        "sd": sd,
        "minimum_detectable_effect": {
            "n": len(deltas), "sd": sd, "alpha": args.alpha,
            "target_power": args.target_power, "mde": mde,
        },
        "achieved_power_for_observed_effect": pw,
    }, indent=2))

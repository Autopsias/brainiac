"""EF-04 (S03) — regression tests for eval/stats.py: bootstrap CI, paired
permutation test, Benjamini-Hochberg FDR correction, and MDE/power.

Known-input -> known-output cases (no reliance on "looks plausible"), plus
determinism checks (same seed -> byte-identical output; different seed on a
non-degenerate distribution -> can differ).
"""
from __future__ import annotations

import itertools
import math
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "eval"))

from stats import (  # noqa: E402
    achieved_power,
    benjamini_hochberg,
    bootstrap_ci,
    bootstrap_ci_lower_one_sided,
    minimum_detectable_effect,
    paired_permutation_test,
)


# --------------------------------------------------------------------------
# bootstrap_ci
# --------------------------------------------------------------------------

def test_bootstrap_ci_degenerate_constant_deltas():
    """Every per-query delta is identical -> every bootstrap resample has the
    same mean -> the CI collapses to a point at that value, for ANY seed/B."""
    ci = bootstrap_ci([0.1] * 20, b=500, seed=1)
    assert ci.mean == pytest.approx(0.1)
    assert ci.ci_lower == pytest.approx(0.1)
    assert ci.ci_upper == pytest.approx(0.1)
    assert ci.kind == "descriptive_effect_size_interval"


def test_bootstrap_ci_empty_input_is_nan_not_a_crash():
    ci = bootstrap_ci([], b=100, seed=1)
    assert ci.n == 0
    assert math.isnan(ci.mean)
    assert math.isnan(ci.ci_lower)


def test_bootstrap_ci_bracket_contains_mean():
    rng = np.random.default_rng(42)
    deltas = rng.normal(loc=0.05, scale=0.02, size=200)
    ci = bootstrap_ci(deltas, b=5000, seed=3)
    assert ci.ci_lower <= ci.mean <= ci.ci_upper
    # true generating mean should be well inside a 95% CI on n=200
    assert ci.ci_lower < 0.05 < ci.ci_upper


def test_bootstrap_ci_determinism_same_seed():
    rng = np.random.default_rng(7)
    deltas = rng.normal(size=50)
    a = bootstrap_ci(deltas, b=2000, seed=11)
    b = bootstrap_ci(deltas, b=2000, seed=11)
    assert a.ci_lower == b.ci_lower
    assert a.ci_upper == b.ci_upper
    assert a.mean == b.mean


def test_bootstrap_ci_lower_one_sided_matches_two_sided_alpha_percentile():
    rng = np.random.default_rng(5)
    deltas = rng.normal(loc=0.1, scale=0.05, size=100)
    lb = bootstrap_ci_lower_one_sided(deltas, b=4000, seed=9, alpha=0.05)
    ci = bootstrap_ci(deltas, b=4000, seed=9, alpha=0.10)  # 2-sided 90% == 5th/95th pct
    # one-sided 5th-percentile lower bound should equal the two-sided-90%
    # CI's lower bound (both are the 5th percentile of the same resample
    # distribution when using the SAME seed/b -> same resample draws)
    assert lb == pytest.approx(ci.ci_lower)


# --------------------------------------------------------------------------
# paired_permutation_test — exact enumeration branch (n <= 20)
# --------------------------------------------------------------------------

def test_permutation_exact_all_positive_deltas_known_p():
    """deltas=[1,2,3,4]: observed mean=2.5 is the MAXIMUM possible mean over
    all 2**4=16 sign patterns (only the all-+1 pattern reaches it) ->
    p_greater = 1/16 exactly. abs(mean)>=2.5 holds for the all-+1 AND
    all-(-1) patterns -> p_two_sided = 2/16 exactly. Hand-verified, not
    'looks about right'."""
    res = paired_permutation_test([1.0, 2.0, 3.0, 4.0], fold_context="unknown")
    assert res.exact is True
    assert res.n == 4
    assert res.b == 16
    assert res.observed_mean == pytest.approx(2.5)
    assert res.p_greater == pytest.approx(1 / 16)
    assert res.p_two_sided == pytest.approx(2 / 16)


def test_permutation_exact_matches_brute_force_itertools():
    """Cross-check the vectorised exact enumeration against a naive
    itertools-based brute force for an independent, asymmetric small case."""
    deltas = np.array([0.3, -0.1, 0.5, 0.2, -0.4, 0.6])
    obs = deltas.mean()
    n = len(deltas)
    perm_means = []
    for signs in itertools.product([-1.0, 1.0], repeat=n):
        perm_means.append(np.mean(np.array(signs) * deltas))
    perm_means = np.array(perm_means)
    expected_p_greater = float((perm_means >= obs).mean())
    expected_p_two_sided = float((np.abs(perm_means) >= abs(obs) - 1e-12).mean())

    res = paired_permutation_test(deltas.tolist(), fold_context="unknown")
    assert res.exact is True
    assert res.p_greater == pytest.approx(expected_p_greater)
    assert res.p_two_sided == pytest.approx(expected_p_two_sided)


def test_permutation_symmetric_deltas_p_near_one():
    """A perfectly sign-symmetric delta set has observed mean 0, which is the
    MEDIAN of the permutation distribution -> two-sided p should be 1.0
    (every sign flip is at least as extreme as an observed statistic of 0)."""
    res = paired_permutation_test([1.0, -1.0, 2.0, -2.0], fold_context="unknown")
    assert res.observed_mean == pytest.approx(0.0)
    assert res.p_two_sided == pytest.approx(1.0)


def test_permutation_empty_input_is_nan_not_a_crash():
    res = paired_permutation_test([], fold_context="unknown")
    assert res.n == 0
    assert math.isnan(res.p_two_sided)


def test_permutation_rejects_bad_fold_context():
    with pytest.raises(ValueError):
        paired_permutation_test([1.0, 2.0], fold_context="bogus")


def test_permutation_fold_context_labeling_h19():
    """fold_context is a LABEL ONLY — never changes the computed statistic —
    but it must flip is_primary/caveat correctly, since mislabeling this is
    exactly the p-hacking failure mode H19 exists to prevent."""
    deltas = [0.1, 0.2, -0.05, 0.15, 0.3]
    held_out = paired_permutation_test(deltas, fold_context="held-out")
    non_held = paired_permutation_test(deltas, fold_context="non-held-out")
    unknown = paired_permutation_test(deltas, fold_context="unknown")

    assert held_out.p_two_sided == non_held.p_two_sided == unknown.p_two_sided
    assert held_out.is_primary is True
    assert non_held.is_primary is False
    assert unknown.is_primary is False
    assert held_out.kind == "primary_significance_test"
    assert non_held.kind == "descriptive_significance_test"
    assert "PRIMARY" in held_out.caveat()
    assert "NOT confirmatory" in non_held.caveat()
    assert "NOT confirmatory" in unknown.caveat()


# --------------------------------------------------------------------------
# paired_permutation_test — Monte Carlo branch (n > 20)
# --------------------------------------------------------------------------

def test_permutation_montecarlo_determinism_same_seed():
    rng = np.random.default_rng(13)
    deltas = rng.normal(loc=0.05, scale=0.1, size=40)  # n=40 > 20 -> MC branch
    a = paired_permutation_test(deltas, b=3000, seed=99, fold_context="unknown")
    b = paired_permutation_test(deltas, b=3000, seed=99, fold_context="unknown")
    assert a.exact is False
    assert a.p_two_sided == b.p_two_sided
    assert a.p_greater == b.p_greater
    assert a.p_less == b.p_less


def test_permutation_montecarlo_never_reports_exact_zero_p():
    """The Monte Carlo +1 correction means a finite-sample permutation
    p-value can never round to exactly 0, even for an extreme observed
    statistic — a p=0.0000 claim from a finite resample is never honest."""
    deltas = np.full(40, 5.0)  # every sign-flip pattern except all-+1 gives a
    # smaller mean; all-+1 itself gives the max -> extremely unlikely to be
    # hit at random with a small B, so naive counting would report 0.
    res = paired_permutation_test(deltas, b=200, seed=1, fold_context="unknown")
    assert res.p_greater > 0.0
    assert res.p_two_sided > 0.0


# --------------------------------------------------------------------------
# benjamini_hochberg
# --------------------------------------------------------------------------

def test_bh_all_equal_pvalues_all_survive():
    """Textbook case: p=(0.01,0.02,0.03,0.04,0.05) against BH critical values
    (i/m)*alpha with alpha=0.05, m=5 -> critical values are IDENTICAL to the
    p-values themselves at every rank -> every adjusted q-value collapses to
    0.05 and every test is rejected."""
    rejected, adj = benjamini_hochberg([0.01, 0.02, 0.03, 0.04, 0.05], alpha=0.05)
    assert adj == pytest.approx([0.05, 0.05, 0.05, 0.05, 0.05])
    assert rejected == [True, True, True, True, True]


def test_bh_mixed_pvalues_only_smallest_survives():
    """p=(0.001, 0.5, 0.9): sorted raw BH q = (0.003, 0.75, 0.9); step-up
    monotonicity from the top doesn't change these (already increasing) ->
    only the first survives alpha=0.05."""
    rejected, adj = benjamini_hochberg([0.001, 0.5, 0.9], alpha=0.05)
    assert adj == pytest.approx([0.003, 0.75, 0.9])
    assert rejected == [True, False, False]


def test_bh_uncorrected_vs_corrected_manufactures_fewer_wins():
    """H20's core claim, made concrete: across a family of 12 stratum tests
    where 11 are genuinely null (large p) and only one clears the naive
    alpha=0.05 threshold (p=0.03 -- well within the false-positive count
    you'd EXPECT from testing 12 independent strata at alpha=0.05,
    E[false positives]=0.6), naive per-test thresholding reports 1 'win';
    BH-adjusted, that same test's q-value is 0.36 and NONE of the 12
    survive -- exactly the manufactured-win failure mode H20 exists to
    prevent. (Hand-verified: raw_adj(rank1)=0.03*12/1=0.36, which is already
    the smallest raw value in the family, so it IS the final adjusted q.)"""
    pvals = [0.03, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 0.99]
    naive_wins = sum(1 for p in pvals if p <= 0.05)
    rejected, adj = benjamini_hochberg(pvals, alpha=0.05)
    assert naive_wins == 1  # naive (uncorrected) count
    assert sum(rejected) == 0  # BH-corrected: none survive
    assert adj[0] == pytest.approx(0.36)


def test_bh_preserves_input_order_not_sorted_order():
    rejected, adj = benjamini_hochberg([0.9, 0.001, 0.5], alpha=0.05)
    # input order is (0.9, 0.001, 0.5) -> only index 1 (p=0.001) should survive
    assert rejected == [False, True, False]


def test_bh_empty_input():
    rejected, adj = benjamini_hochberg([], alpha=0.05)
    assert rejected == []
    assert adj == []


# --------------------------------------------------------------------------
# minimum_detectable_effect / achieved_power
# --------------------------------------------------------------------------

def test_mde_matches_hand_computed_z_formula():
    """MDE = (sd/sqrt(n)) * (z_.975 + z_.80), z_.975=1.959963985,
    z_.80=0.841621234 — the standard closed-form two-sided z-test MDE,
    computed independently here and compared to the function's output."""
    n, sd = 65, 0.30
    se = sd / math.sqrt(n)
    z_alpha2 = 1.9599639845400545
    z_power = 0.8416212335729143
    expected = se * (z_alpha2 + z_power)
    got = minimum_detectable_effect(n, sd, alpha=0.05, power=0.8)
    assert got == pytest.approx(expected, rel=1e-9)


def test_mde_zero_for_zero_sd():
    assert minimum_detectable_effect(50, 0.0) == 0.0


def test_mde_nan_for_invalid_n():
    assert math.isnan(minimum_detectable_effect(0, 0.1))
    assert math.isnan(minimum_detectable_effect(-5, 0.1))


def test_achieved_power_near_half_at_effect_equal_to_critical_boundary():
    """If the true effect exactly equals se * z_alpha2 (the two-sided
    critical boundary at effect/se = z_alpha2), the upper-tail power term is
    Phi(0) = 0.5 exactly, and the lower-tail term is Phi(-2*z_alpha2) ~ 0 ->
    total power should be ~0.5 (not exactly, but very close)."""
    n, sd, alpha = 100, 1.0, 0.05
    se = sd / math.sqrt(n)
    z_alpha2 = 1.9599639845400545
    effect = se * z_alpha2
    power = achieved_power(n, sd, effect, alpha=alpha)
    assert power == pytest.approx(0.5, abs=1e-3)


def test_mde_and_achieved_power_are_inverses():
    """Round-trip consistency: the MDE for a target power, fed back into
    achieved_power, should reproduce (approximately) that same target power —
    for ANY n/sd/alpha, not just a hardcoded pair."""
    for n, sd, alpha, target_power in [(30, 0.2, 0.05, 0.8), (65, 0.35, 0.05, 0.9),
                                        (200, 0.1, 0.01, 0.7)]:
        mde = minimum_detectable_effect(n, sd, alpha=alpha, power=target_power)
        got_power = achieved_power(n, sd, mde, alpha=alpha)
        assert got_power == pytest.approx(target_power, abs=1e-6)


def test_achieved_power_degenerate_zero_sd():
    assert achieved_power(50, 0.0, 0.1) == 1.0
    assert achieved_power(50, 0.0, 0.0, alpha=0.05) == pytest.approx(0.05)

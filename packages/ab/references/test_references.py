# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""sequentialist-ab against independent references: GrowthBook's sequential test and the mSPRT of Optimizely.

Run in CI's `references` job (Python 3.11, `pip install gbstats==0.8.0`);
skipped where gbstats is not installed.  HILLSTROM names the CSV of the
MineThatData e-mail experiment; without it, the part on that data is skipped.

What is compared, and what is expected:

* point estimates (difference of means) agree exactly: all three use the
  plain difference of the arms' means;
* decisions agree where the effect is clear (Hillstrom, visits);
* intervals differ by method, and the widths are printed: GrowthBook's
  confidence sequence is asymptotic (Waudby-Smith et al. 2023), ours is
  exact (betting, BoundedABTest) and wider;
* on data without any effect, monitored after every 500 units, our exact
  test rejects at most as often as alpha allows (checked with a binomial
  bound); the rejection rates of the references are printed.

mSPRT (Johari, Koomen, Pekelis and Walsh 2017, the "always valid p-values"
of Optimizely): for the difference of two proportions with a normal
mixture of variance tau², with V = p_A(1-p_A)/n_A + p_B(1-p_B)/n_B,

    Lambda = sqrt(V / (V + tau²)) exp(tau² d² / (2 V (V + tau²))),   reject when Lambda >= 1/alpha.
"""
import csv
import math
import os
import random
import unittest
from pathlib import Path

from sequential_analysis.procedures import BoundedABTest, TwoProportionTest

try:
    from gbstats.frequentist.tests import SequentialConfig, SequentialTwoSidedTTest
    from gbstats.models.statistics import ProportionStatistic, SampleMeanStatistic
except ImportError:  # the references are optional
    SequentialTwoSidedTTest = None

ALPHA = 0.05
TAU2 = 1e-4  # the mixing variance of the mSPRT: tuned for effects of about one percentage point


def msprt_rejects(n_a, s_a, n_b, s_b, alpha=ALPHA, tau2=TAU2):
    if min(n_a, n_b) < 2:
        return False
    p_a, p_b = s_a / n_a, s_b / n_b
    v = p_a * (1 - p_a) / n_a + p_b * (1 - p_b) / n_b
    if v <= 0:
        return False
    d = p_b - p_a
    log_lambda = 0.5 * math.log(v / (v + tau2)) + tau2 * d * d / (2 * v * (v + tau2))
    return log_lambda >= -math.log(alpha)


def growthbook(n_a, s_a, n_b, s_b, ss_a=None, ss_b=None, alpha=ALPHA):
    """GrowthBook's sequential two-sided test, absolute difference: (estimate, lower, upper, p-value)."""
    if ss_a is None:
        a, b = ProportionStatistic(n=n_a, sum=s_a), ProportionStatistic(n=n_b, sum=s_b)
    else:
        a = SampleMeanStatistic(n=n_a, sum=s_a, sum_squares=ss_a)
        b = SampleMeanStatistic(n=n_b, sum=s_b, sum_squares=ss_b)
    test = SequentialTwoSidedTTest(a, b, SequentialConfig(alpha=alpha, difference_type="absolute"))
    lower, upper = test.confidence_interval
    return test.point_estimate, lower, upper, test.p_value


def hillstrom_rows():
    path = os.environ.get("HILLSTROM")
    if not path or not Path(path).exists():
        return None
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


@unittest.skipIf(SequentialTwoSidedTTest is None, "gbstats is not installed")
class HillstromTest(unittest.TestCase):
    def test_estimates_decisions_and_widths(self):
        rows = hillstrom_rows()
        if rows is None:
            self.skipTest("set HILLSTROM to the CSV of the MineThatData e-mail experiment")
        order = list(range(len(rows)))
        random.Random(2008).shuffle(order)
        arms = {"No E-Mail": 0, "Mens E-Mail": 1, "Womens E-Mail": 2}
        lines = ["| metric | variant | estimate | GrowthBook interval | sequentialist interval | width ratio | "
                 "GrowthBook | mSPRT | sequentialist |", "|---|---|---|---|---|---|---|---|---|"]
        for metric, column, bounded in (("visit", "visit", False), ("purchase", "conversion", False),
                                        ("spend", "spend", True)):
            for treatment, name in ((1, "mens"), (2, "womens")):
                ours = BoundedABTest(0.0, 500.0 if bounded else 1.0, 0.5, ALPHA, arms=(treatment, 0))
                decide = None if bounded else TwoProportionTest(ALPHA, arms=(treatment, 0))
                state = ours.initial_state()
                dstate = decide.initial_state() if decide else None
                n = [0, 0]
                s = [0.0, 0.0]
                ss = [0.0, 0.0]
                for i in order:
                    arm = arms[rows[i]["segment"]]
                    if arm not in (0, treatment):
                        continue
                    y = float(rows[i][column])
                    k = 0 if arm == 0 else 1
                    n[k] += 1
                    s[k] += y
                    ss[k] += y * y
                    x = {"arm": arm, "y": y if bounded else int(y)}
                    state, out = ours.step(state, x, None)
                    if decide:
                        dstate, dout = decide.step(dstate, x, None)
                gb = growthbook(n[0], s[0], n[1], s[1], ss[0], ss[1]) if bounded else \
                    growthbook(n[0], s[0], n[1], s[1])
                estimate = out.means[0] - out.means[1]
                self.assertAlmostEqual(estimate, gb[0], places=12)
                ours_rejects = (dout.decision == "reject_h0") if decide else (out.decision == "reject_h0")
                ms = msprt_rejects(n[0], s[0], n[1], s[1]) if not bounded else None
                if metric == "visit":
                    self.assertTrue(ours_rejects)
                    self.assertTrue(gb[3] < ALPHA)
                    self.assertTrue(ms)
                ratio = (out.upper - out.lower) / (gb[2] - gb[1])
                lines.append(f"| {metric} | {name} | {estimate:.5f} | [{gb[1]:.5f}, {gb[2]:.5f}] | "
                             f"[{out.lower:.4f}, {out.upper:.4f}] | {ratio:.1f} | "
                             f"{'reject' if gb[3] < ALPHA else '—'} | "
                             f"{'—' if ms is None else 'reject' if ms else '—'} | "
                             f"{'reject' if ours_rejects else '—'} |")
        print("\nHillstrom, all 64,000 customers, alpha 0.05 per comparison:\n" + "\n".join(lines))


@unittest.skipIf(SequentialTwoSidedTTest is None, "gbstats is not installed")
class NoEffectTest(unittest.TestCase):
    def test_rejections_when_nothing_differs(self):
        runs, units, look = 60, 6000, 500
        counts = {"sequentialist": 0, "GrowthBook": 0, "mSPRT": 0}
        for r in range(runs):
            rnd = random.Random(r)
            test = TwoProportionTest(ALPHA)
            state = test.initial_state()
            n, s = [0, 0], [0, 0]
            hit = {k: False for k in counts}
            for i in range(1, units + 1):
                arm = rnd.randint(0, 1)
                y = int(rnd.random() < 0.1)
                n[arm] += 1
                s[arm] += y
                state, out = test.step(state, {"arm": arm, "y": y}, None)
                hit["sequentialist"] |= out.decision == "reject_h0"
                if i % look == 0 and min(n) > 1:
                    hit["GrowthBook"] |= growthbook(n[0], s[0], n[1], s[1])[3] < ALPHA
                    hit["mSPRT"] |= msprt_rejects(n[0], s[0], n[1], s[1])
            for k in counts:
                counts[k] += hit[k]
        print(f"\nNo effect, {runs} runs of {units} units, looked at every {look} (references) or every unit "
              f"(sequentialist): runs with a rejection {counts}; alpha {ALPHA}")
        # exact at level 0.05: more than 9 rejections in 60 runs has probability below 0.01
        self.assertLessEqual(counts["sequentialist"], 9)


if __name__ == "__main__":
    unittest.main()

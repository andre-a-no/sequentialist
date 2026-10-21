# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Plans: their operating characteristic against closed formulas, their risks, their execution, their specs."""
import json
import math
import random
import unittest
from pathlib import Path

from sequentialist.core import SpecError, load_spec, parse_spec
from sequentialist.quality import PlanSpec, SchemeSpec, characteristic, compile_plan, design, verify
from sequentialist.quality.plans import parameter

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def plan(**changes):
    data = {"kind": "quality.plan/1", "name": "p", "characteristic": {"type": "attributes"}, "aql": 0.01, "lq": 0.05,
            "method": {"type": "single", "sample": 50, "acceptance": 1}}
    data.update(changes)
    return parse_spec(data, PlanSpec)


def binomial_accept(n, c, p):
    return sum(math.comb(n, k) * p**k * (1 - p) ** (n - k) for k in range(c + 1))


def poisson_accept(n, c, rate):
    mean = n * rate
    return sum(math.exp(-mean) * mean**k / math.factorial(k) for k in range(c + 1))


class SinglePlanTest(unittest.TestCase):
    """Single sampling has a closed form: an independent reference for the operating characteristic."""

    def test_attributes_against_the_binomial(self):
        for n, c in ((20, 0), (50, 1), (132, 3), (200, 7)):
            designed = design(plan(method={"type": "single", "sample": n, "acceptance": c}))
            for q, point in zip((0.001, 0.01, 0.03, 0.08), characteristic(designed, [0.001, 0.01, 0.03, 0.08])):
                with self.subTest(n=n, c=c, q=q):
                    self.assertAlmostEqual(point.accept, binomial_accept(n, c, q), places=13)
                    self.assertTrue(point.exact)

    def test_nonconformities_against_the_poisson(self):
        designed = design(plan(characteristic={"type": "nonconformities"}, aql=0.02, lq=0.1,
                               method={"type": "single", "sample": 80, "acceptance": 3}))
        for rate, point in zip((0.01, 0.05, 0.1), characteristic(designed, [0.01, 0.05, 0.1])):
            self.assertAlmostEqual(point.accept, poisson_accept(80, 3, rate), places=12)

    def test_curtailment_changes_the_items_not_the_decision(self):
        full = design(plan(method={"type": "single", "sample": 132, "acceptance": 3, "curtailed": False}))
        cut = design(plan(method={"type": "single", "sample": 132, "acceptance": 3}))
        for a, b in zip(characteristic(full, [0.01, 0.05]), characteristic(cut, [0.01, 0.05])):
            self.assertAlmostEqual(a.accept, b.accept, places=13)
            self.assertAlmostEqual(a.asn, 132.0, places=10)
            self.assertLess(b.asn, a.asn)


class SequentialPlanTest(unittest.TestCase):
    def test_risks_are_met_exactly_with_fewer_items(self):
        sequential = load_spec(EXAMPLES / "normal.designed.json", PlanSpec)
        single = load_spec(EXAMPLES / "single.designed.json", PlanSpec)
        oc = sequential.data["design"]["oc"]
        self.assertGreaterEqual(oc["aql"]["accept"], 0.95)
        self.assertLessEqual(oc["lq"]["accept"], 0.10)
        self.assertLessEqual(len(sequential.data["design"]["continuation"]), 300)
        self.assertTrue(sequential.data["design"]["exact"])
        self.assertEqual(verify(sequential), [])
        # the same risks as the single plan of 132 items, with fewer items on average
        for q in (0.005, 0.01, 0.025, 0.05):
            seq, sgl = characteristic(sequential, [q])[0], characteristic(single, [q])[0]
            self.assertLess(seq.asn, sgl.asn)

    def test_a_changed_boundary_is_found(self):
        designed = json.loads((EXAMPLES / "normal.designed.json").read_text())
        designed["design"]["continuation"][100][1] += 1.0
        problems = verify(parse_spec(designed, PlanSpec))
        self.assertTrue(problems)
        self.assertIn("boundaries were changed", problems[0])

    def test_variables(self):
        shaft = load_spec(EXAMPLES / "shaft.designed.json", PlanSpec)
        self.assertFalse(shaft.data["design"]["exact"])  # computed on a lattice
        self.assertEqual(verify(shaft), [])
        upper = shaft.data["characteristic"]
        mean_at_aql = parameter(shaft, 0.01)
        self.assertAlmostEqual(mean_at_aql, upper["value"] - 2.3263478740408408 * upper["sigma"], places=9)
        lower = parse_spec({**shaft.to_json(), "characteristic": {**upper, "limit": "lower", "value": 24.95},
                            "design": None}, PlanSpec)
        self.assertAlmostEqual(parameter(lower, 0.01), 24.95 + 2.3263478740408408 * upper["sigma"], places=9)
        # executed on simulated measurements, the plan accepts as often as its characteristic says
        test = compile_plan(shaft)
        rnd = random.Random(3)
        runs = 2000
        accepted = 0
        for _ in range(runs):
            state = test.initial_state()
            while True:
                state, out = test.step(state, {"x": rnd.gauss(mean_at_aql, upper["sigma"])}, None)
                if out.decision is not None:
                    accepted += out.decision == "accept_h0"
                    break
        expected = shaft.data["design"]["oc"]["aql"]["accept"]
        self.assertLess(abs(accepted / runs - expected), 4 * math.sqrt(expected * (1 - expected) / runs))


class SpecTest(unittest.TestCase):
    def test_problems(self):
        cases = [
            ({"lq": 0.005}, "/lq", "worse"),
            ({"method": {"type": "single", "sample": 5, "acceptance": 5}}, "/method/acceptance", "smaller"),
            ({"characteristic": {"type": "variables", "limit": "upper", "value": 1, "sigma": 0.1}}, "/method",
             "single sampling by variables"),
            ({"aql": 0.5, "lq": 1.5}, "/lq", "below 1"),
        ]
        for change, path, text in cases:
            with self.subTest(path=path), self.assertRaises(SpecError) as raised:
                plan(**change)
            self.assertIn(text, dict(raised.exception.errors)[path])

    def test_schemes(self):
        scheme = json.loads((EXAMPLES / "scheme.json").read_text())
        load_spec(scheme, SchemeSpec)
        undesigned = json.loads((EXAMPLES / "tightened.plan.json").read_text())
        nonconformities = {**json.loads((EXAMPLES / "normal.designed.json").read_text()),
                           "characteristic": {"type": "nonconformities"}}
        cases = [
            ({"tightened": undesigned}, "/plans/tightened/design", "not designed"),
            ({"tightened": nonconformities}, "/plans", "same characteristic"),
            ({"tightened": None}, "/resubmission/plan", "no tightened plan"),
        ]
        for plans, path, text in cases:
            with self.subTest(path=path), self.assertRaises(SpecError) as raised:
                load_spec({**scheme, "plans": {**scheme["plans"], **plans}}, SchemeSpec)
            self.assertIn(text, dict(raised.exception.errors)[path])


if __name__ == "__main__":
    unittest.main()

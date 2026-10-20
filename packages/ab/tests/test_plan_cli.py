# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Planning, A/A on the real pipeline, and `sequentialist ab`."""
import contextlib
import io
import json
import random
import tempfile
import unittest
from pathlib import Path

from sequentialist.ab import Experiment, Unit, aa_check, plan
from sequentialist.core import Engine, FileStorage
from sequentialist.core.cli import main

from .helpers import make_spec, run, simulate


class PlanTest(unittest.TestCase):
    def test_conversion(self):
        spec = make_spec()
        small = plan(spec, baseline=0.1, units_per_day=2000, effect=0.01, runs=40, seed=1)
        large = plan(spec, baseline=0.1, units_per_day=2000, effect=0.04, runs=40, seed=1)
        self.assertEqual((large["procedure"], large["guarantee"], large["level"]), ("two_proportion_test", "exact",
                                                                                     0.05))
        self.assertLess(small["power"], large["power"])
        self.assertGreater(large["power"], 0.9)
        self.assertLess(large["days_to_decision"]["median"], large["max_days"])
        lo, hi = large["power_interval"]
        self.assertLessEqual(lo, large["power"])
        self.assertLessEqual(large["power"], hi + 1e-12)

    def test_bounded_and_planned(self):
        spec = make_spec(metrics=[{"name": "spend", "type": "bounded", "role": "primary", "event": "purchase",
                                   "value": "amount", "bounds": [0, 50], "mde": 5}])
        rnd = random.Random(0)
        values = [0.0] * 700 + [rnd.uniform(0, 50) for _ in range(300)]
        result = plan(spec, baseline=values, units_per_day=1000, runs=20)
        self.assertEqual(result["procedure"], "bounded_ab_test")
        planned = make_spec(monitoring={"mode": "planned", "looks": [0.5, 1.0], "max_units": 4000})
        result = plan(planned, baseline=0.1, units_per_day=2000, effect=0.03, runs=40)
        self.assertEqual((result["procedure"], result["guarantee"]), ("group_sequential_proportions", "asymptotic"))
        self.assertGreater(result["power"], 0.6)
        with self.assertRaises(ValueError):
            plan(spec, baseline=0.1, units_per_day=10)


class AATest(unittest.TestCase):
    def test_the_pipeline_on_units_without_effect(self):
        spec = make_spec(max_duration="2d")
        rnd = random.Random(5)
        units = [Unit(i * 60.0, [(3600.0, "purchase", {})] if rnd.random() < 0.1 else []) for i in range(1500)]
        result = aa_check(spec, units, runs=6, seed=2)
        self.assertEqual(result["runs"], 6)
        self.assertEqual(result["shipped"]["hits"], 0)
        self.assertEqual(result["sample_ratio_mismatch"]["hits"], 0)
        self.assertTrue(all(d.get("reason") in ("duration_limit", "no_variant_left") for d in result["decisions"]))
        lo, hi = result["shipped"]["interval"]
        self.assertEqual(lo, 0.0)
        self.assertGreater(hi, 0.0)  # six runs say little; the interval says how little


class CliTest(unittest.TestCase):
    def test_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = Path(tmp) / "spec.json"
            spec = make_spec()
            spec_path.write_text(json.dumps(spec.to_json()))
            storage = FileStorage(Path(tmp) / "store", sync="flush")
            experiment = Experiment.create(Engine(storage, clock="event"), spec)
            run(experiment, simulate(spec, 3000, rates={"A": 0.1, "B": 0.2}))
            experiment.close()
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["ab", "check", str(spec_path)]), 0)
                self.assertEqual(main(["ab", "client-config", str(spec_path)]), 0)
                self.assertEqual(main(["ab", "schema"]), 0)
                self.assertEqual(main(["ab", "report", f"files:{tmp}/store", "exp"]), 0)
                self.assertEqual(main(["ab", "plan", str(spec_path), "--baseline", "0.1", "--units-per-day", "500",
                                       "--runs", "10"]), 0)
                self.assertEqual(main(["audit", f"files:{tmp}/store", "exp"]), 0)
            text = out.getvalue()
            self.assertIn("multiplicity over primary and secondary metrics: e-BH", text)
            self.assertIn('"salt": "salt-1"', text)
            self.assertIn("**Ship B**", text)
            self.assertIn('"power"', text)


if __name__ == "__main__":
    unittest.main()

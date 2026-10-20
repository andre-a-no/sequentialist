# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""The experiment spec, and the assignment: the shared vectors, the split, the ramp, overrides."""
import json
import unittest
from collections import Counter
from pathlib import Path

from sequentialist.ab import Analysis, AssignmentConfig, ExperimentSpec, config, uniform
from sequentialist.core import SpecError, load_spec, parse_spec

from .helpers import make_spec

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class SpecTest(unittest.TestCase):
    def test_examples_are_valid(self):
        for path in sorted(EXAMPLES.glob("*.json")):
            with self.subTest(path=path.name):
                spec = load_spec(path, ExperimentSpec)
                Analysis(spec)

    def test_problems(self):
        base = make_spec().to_json()
        primary = base["metrics"][0]
        cases = [
            ({"variants": [{"name": "A"}, {"name": "B"}]}, "/variants", "exactly one variant must be the control"),
            ({"variants": [{"name": "A", "control": True}, {"name": "A"}]}, "/variants", "must differ"),
            ({"metrics": [{**primary, "role": "secondary"}]}, "/metrics", "exactly one metric must have role"),
            ({"metrics": [{**primary, "mde": None}]}, "/metrics/0/mde", "needs an mde"),
            ({"metrics": [primary, {"name": "e", "type": "conversion", "event": "e", "role": "guardrail"}]},
             "/metrics/1/margin", "needs a margin"),
            ({"metrics": [{**primary, "margin": 0.1}]}, "/metrics/0/margin", "guardrails only"),
            ({"overrides": {"qa": "Z"}}, "/overrides/qa", "unknown variant"),
            ({"traffic": [{"from": 10, "share": 0.1}, {"from": 5, "share": 0.5}]}, "/traffic", "must increase"),
            ({"monitoring": {"mode": "planned", "looks": [0.5, 0.9], "max_units": 100}}, "/monitoring/looks",
             "end at 1"),
            ({"metrics": [{"name": "r", "type": "ratio", "role": "primary", "mde": 0.1,
                           "numerator": {"event": "click"}, "denominator": {"event": "view"}}],
              "monitoring": {"mode": "planned", "looks": [1], "max_units": 100}}, "/monitoring", "conversion or"),
        ]
        for change, path, text in cases:
            with self.subTest(path=path, text=text):
                with self.assertRaises(SpecError) as raised:
                    parse_spec({**base, **change}, ExperimentSpec)
                self.assertIn(path, dict(raised.exception.errors))
                self.assertIn(text, dict(raised.exception.errors)[path])

    def test_schema_file_is_current(self):
        from sequentialist.core import json_schema

        stored = json.loads((ROOT / "schemas" / "ab.experiment-1.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, json.loads(json.dumps(json_schema(ExperimentSpec))))

    def test_analysis_levels(self):
        spec = make_spec(metrics=[
            {"name": "purchase", "type": "conversion", "role": "primary", "event": "purchase", "mde": 0.02},
            {"name": "revenue", "type": "bounded", "event": "purchase", "value": "amount", "bounds": [0, 100]},
            {"name": "ctr", "type": "ratio", "numerator": {"event": "click"}, "denominator": {"event": "view"}},
            {"name": "errors", "type": "rate", "role": "guardrail", "event": "error", "exposure": {"event": "view"},
             "direction": "decrease", "margin": 0.1},
        ], guardrail_alpha=0.1)
        a = Analysis(spec)
        self.assertEqual(a.multiplicity, "Bonferroni")
        levels = {p.name: (p.interval_level, p.interval_guarantee, p.decision_guarantee) for p in a.pairs}
        self.assertEqual(levels["purchase:B"], (0.05 / 3, "exact", "exact"))
        self.assertEqual(levels["ctr:B"], (0.05 / 3, "asymptotic", "asymptotic"))
        self.assertEqual(levels["errors:B"], (0.1, "exact", "exact"))


class AssignmentTest(unittest.TestCase):
    def test_shared_vectors(self):
        """The cases @sequentialist/ab-client is tested against, reproduced here exactly."""
        vectors = json.loads((ROOT / "vectors" / "assignment.json").read_text(encoding="utf-8"))
        for u in vectors["uniform"]:
            self.assertEqual(uniform(u["domain"], u["salt"], u["unit"]), u["value"])
        configs = [AssignmentConfig.from_json(c) for c in vectors["configs"]]
        for case in vectors["cases"]:
            got = configs[case["config"]].assign(case["unit"], case["time"]).to_json()
            self.assertEqual(got, {k: case[k] for k in ("variant", "probability", "reason")}, case)
        self.assertGreater(len(vectors["cases"]), 500)

    def test_split_follows_the_weights(self):
        cfg = AssignmentConfig("split", [("A", 1.0), ("B", 2.0), ("C", 1.0)])
        counts = Counter(cfg.assign(f"user-{i}").variant for i in range(40000))
        for name, share in (("A", 0.25), ("B", 0.5), ("C", 0.25)):
            self.assertAlmostEqual(counts[name] / 40000, share, delta=0.01)
        self.assertEqual(cfg.assign("user-1").probability, 0.25 if cfg.assign("user-1").variant != "B" else 0.5)

    def test_ramp_keeps_everyone_who_was_in(self):
        cfg = AssignmentConfig("ramp", [("A", 1.0), ("B", 1.0)], traffic=[(0.0, 0.1), (100.0, 0.5)])
        early = {f"u{i}" for i in range(5000) if cfg.assign(f"u{i}", 50.0).variant is not None}
        later = {f"u{i}" for i in range(5000) if cfg.assign(f"u{i}", 150.0).variant is not None}
        self.assertTrue(early <= later)
        self.assertAlmostEqual(len(early) / 5000, 0.1, delta=0.02)
        self.assertAlmostEqual(len(later) / 5000, 0.5, delta=0.03)
        for u in early:  # and in the same variant
            self.assertEqual(cfg.assign(u, 50.0).variant, cfg.assign(u, 150.0).variant)
        self.assertEqual(cfg.assign("u1", -1.0).reason, "traffic")

    def test_spec_config_overrides_and_window(self):
        spec = make_spec(overrides={"qa-user": "B"}, max_duration="7d", start="1970-01-02T00:00:00Z")
        cfg = config(spec)
        self.assertEqual((cfg.start, cfg.end), (86400.0, 86400.0 + 7 * 86400))
        self.assertEqual(cfg.assign("qa-user", 0.0).reason, "override")
        self.assertEqual(cfg.assign("someone", 0.0).reason, "inactive")
        self.assertEqual(cfg.assign("someone", 86400.0).reason, "hash")
        with self.assertRaises(ValueError):
            cfg.assign("someone")
        with self.assertRaises(ValueError):
            uniform("variant", "salt", "\ud800")


if __name__ == "__main__":
    unittest.main()

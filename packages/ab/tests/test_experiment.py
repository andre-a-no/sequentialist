# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Experiments end to end: decisions and their grounds, attribution, data quality, crashes, reports."""
import tempfile
import unittest
from pathlib import Path

from sequentialist.ab import Experiment, event, exposure
from sequentialist.core import Engine, FileStorage, MemoryStorage, verify_audit

from .helpers import DAY, make_spec, run, simulate

GUARDED = [
    {"name": "purchase", "type": "conversion", "role": "primary", "event": "purchase", "mde": 0.02},
    {"name": "revenue", "type": "bounded", "event": "purchase", "value": "amount", "bounds": [0, 200]},
    {"name": "error_share", "type": "ratio", "numerator": {"event": "error"}, "denominator": {"event": "page_view"},
     "direction": "decrease"},
    {"name": "errors", "type": "rate", "role": "guardrail", "event": "error", "exposure": {"event": "page_view"},
     "direction": "decrease", "margin": 0.3},
]


def open_experiment(spec, storage=None):
    return Experiment.create(Engine(storage or MemoryStorage(), clock="event"), spec)


class DecisionTest(unittest.TestCase):
    def test_ship_when_the_primary_improves_and_guardrails_hold(self):
        spec = make_spec(metrics=GUARDED)
        records = simulate(spec, 8000, rates={"A": 0.10, "B": 0.16}, spend={"A": 40, "B": 40},
                           errors={"A": 0.15, "B": 0.15})
        with open_experiment(spec) as experiment:
            decision = run(experiment, records)
            self.assertEqual((decision["decision"], decision["variant"], decision["reason"]),
                             ("ship", "B", "primary_success"))
            entry = next(e for e in experiment.task.audit() if e.decision == "ship")
            self.assertEqual(entry.basis["metric"], "purchase")
            self.assertEqual(entry.basis["multiplicity"], "Bonferroni")
            procedures = {e["procedure"]["name"]: e["guarantee"] for e in entry.evidence}
            self.assertEqual(procedures, {"two_proportion_test": "exact", "bounded_ab_test": "exact",
                                          "rate_ratio_test": "exact"})
            rows = {(r["metric"], r["variant"]): r for r in experiment.results()}
            self.assertEqual(rows[("purchase", "B")]["verdict"], "better")
            self.assertEqual(rows[("errors", "B")]["verdict"], "non-inferior")
            self.assertEqual(rows[("purchase", "B")]["level"], 0.05 / 3)  # Bonferroni over three metrics
            self.assertEqual((rows[("error_share", "B")]["interval_guarantee"], rows[("error_share", "B")]["scale"]),
                             ("asymptotic", "relative"))
            self.assertIsNotNone(rows[("error_share", "B")]["lower"])
            report = experiment.report().to_markdown()
            self.assertIn("**Ship B**", report)
            self.assertIn("*Guarantee: exact.*", report)
            self.assertIn("| errors | B |", report)

    def test_guardrail_harm_drops_the_variant(self):
        spec = make_spec(metrics=GUARDED)
        records = simulate(spec, 6000, rates={"A": 0.10, "B": 0.10}, spend={"A": 40, "B": 40},
                           errors={"A": 0.02, "B": 0.10})
        with open_experiment(spec) as experiment:
            decision = run(experiment, records)
            self.assertEqual((decision["decision"], decision["reason"]), ("stop", "no_variant_left"))
            drop = next(e for e in experiment.task.audit() if e.decision == "drop_variant")
            self.assertEqual((drop.basis["reason"], drop.basis["metric"]), ("harm", "errors"))
            self.assertGreater(drop.basis["interval"][0], 0.3)  # the whole interval beyond the margin

    def test_sample_ratio_mismatch_stops_everything(self):
        spec = make_spec(assignment="external")
        records = simulate(spec, 4000, rates={"A": 0.1, "B": 0.1}, assign=False, split={"A": 0.4, "B": 0.6})
        with open_experiment(spec) as experiment:
            decision = run(experiment, records)
            self.assertEqual((decision["decision"], decision["reason"]), ("stop", "sample_ratio_mismatch"))
            entry = next(e for e in experiment.task.audit() if e.kind == "decision")
            self.assertEqual(entry.evidence[0]["procedure"]["name"], "sample_ratio_mismatch")
            self.assertGreater(experiment.status()["counts"]["after_decision"], 0)

    def test_futility_when_the_mde_is_excluded(self):
        spec = make_spec(metrics=[{"name": "purchase", "type": "conversion", "role": "primary", "event": "purchase",
                                   "mde": 0.15}])
        records = simulate(spec, 6000, rates={"A": 0.3, "B": 0.3})
        with open_experiment(spec) as experiment:
            decision = run(experiment, records)
            self.assertEqual((decision["decision"], decision["reason"]), ("stop", "no_variant_left"))
            self.assertEqual(experiment.status()["variants"], {"B": "futility"})

    def test_duration_limit(self):
        spec = make_spec(max_duration="2d", assignment="external")
        records = simulate(spec, 1000, rates={"A": 0.1, "B": 0.1}, every=300.0, assign=False,
                           split={"A": 0.5, "B": 0.5})  # exposures go on past the end
        with open_experiment(spec) as experiment:
            run(experiment, records[:500])
            self.assertIsNone(experiment.decision)
            experiment.advance(3 * DAY - 1)
            self.assertIsNone(experiment.decision)  # the windows of the last exposures are still open
            experiment.ingest(records[500:])
            # decided when the end (2 days) plus the window (1 day) passed, before the exposure at that moment
            self.assertEqual((experiment.decision["reason"], experiment.decision["at"]), ("duration_limit", 3 * DAY))
            counts = experiment.status()["counts"]
            self.assertEqual((counts["exposures"], counts["after_end"]), (576, 288))
            self.assertEqual(counts["after_decision"], 1000 - 576 - 288)

    def test_three_variants_with_e_bh(self):
        spec = make_spec(variants=[{"name": "A", "control": True}, {"name": "B"}, {"name": "C"}],
                         metrics=[{"name": "purchase", "type": "conversion", "role": "primary", "event": "purchase",
                                   "mde": 0.02},
                                  {"name": "visit", "type": "conversion", "event": "visit"}])
        records = simulate(spec, 9000, rates={"A": 0.10, "B": 0.13, "C": 0.18})
        with open_experiment(spec) as experiment:
            self.assertEqual(experiment.handler.analysis.multiplicity, "e-BH")
            decision = run(experiment, records)
            self.assertEqual((decision["decision"], decision["variant"]), ("ship", "C"))
            entry = next(e for e in experiment.task.audit() if e.decision == "ship")
            self.assertEqual(entry.evidence[0]["procedure"]["name"], "multiple_tests")

    def test_planned_looks(self):
        spec = make_spec(monitoring={"mode": "planned", "looks": [0.5, 1.0], "max_units": 3000})
        records = simulate(spec, 6000, rates={"A": 0.10, "B": 0.15})
        with open_experiment(spec) as experiment:
            decision = run(experiment, records)
            self.assertEqual(decision["decision"], "ship")
            entry = next(e for e in experiment.task.audit() if e.decision == "ship")
            self.assertEqual(entry.basis["guarantee"], "asymptotic")
            self.assertEqual(entry.evidence[0]["procedure"]["name"], "group_sequential_proportions")
            self.assertIn(entry.evidence[0]["output"]["analysis"], (1, 2))

    def test_manual_stop_is_audited_with_actor_and_reason(self):
        spec = make_spec()
        with open_experiment(spec) as experiment:
            experiment.ingest(simulate(spec, 100, rates={"A": 0.1, "B": 0.1}))
            experiment.stop(actor="alice@example.com", reason="the release was rolled back")
            self.assertEqual(experiment.decision["reason"], "manual")
            action = next(e for e in experiment.task.audit() if e.kind == "action")
            self.assertEqual((action.actor, action.reason), ("alice@example.com", "the release was rolled back"))


class AttributionTest(unittest.TestCase):
    def test_windows_lateness_and_counters(self):
        spec = make_spec(metrics=[
            {"name": "purchase", "type": "conversion", "role": "primary", "event": "purchase", "mde": 0.02},
            {"name": "spend", "type": "bounded", "event": "purchase", "value": "amount", "bounds": [0, 100]},
        ], overrides={"qa": "B"}, lateness="1h")
        r = [
            exposure("u1", "A", 0, source="s", seq=0),
            event("u1", "purchase", 3600, source="s", seq=1, amount=250.0),   # in the window, clipped to 100
            event("u1", "purchase", DAY + 1, source="s", seq=2, amount=5.0),  # after the window
            exposure("u1", "A", 10, source="s", seq=3),                       # repeated
            exposure("u1", "B", 20, source="s", seq=4),                       # conflicting
            exposure("qa", "B", 30, source="s", seq=5),                       # override: QA
            event("ghost", "purchase", 40, source="s", seq=6),                # never exposed
            exposure("u2", "Z", 50, source="s", seq=7),                       # unknown variant
            exposure("u3", "B", 60, source="s", seq=8),
            event("u3", "purchase", 70, source="s", seq=9, amount="a lot"),   # bad value: conversion only
        ]
        r.sort(key=lambda x: (x.time, x.seq))
        with open_experiment(spec) as experiment:
            experiment.ingest(r)
            self.assertEqual(experiment.status()["open_units"], 2)  # the watermark is 1d + 1, less an hour
            experiment.advance(DAY + 60 + 3599)
            self.assertEqual(experiment.status()["open_units"], 1)  # u3's window ends at 1d + 60, lateness 1h
            experiment.advance(DAY + 60 + 3600)
            counts = experiment.status()["counts"]
            expected = {"exposures": 2, "repeat_exposures": 1, "conflicting_exposures": 1, "qa_exposures": 1,
                        "unknown_variant": 1, "events_outside_window": 1, "events_of_unknown_units": 1,
                        "bad_values": 1}
            self.assertEqual({k: counts[k] for k in expected}, expected)
            self.assertEqual(experiment.status()["clipped"], {"spend": 1})
            spend = experiment.task.run("cs:spend:B")
            self.assertEqual(spend.t, 2)
            out = experiment.task.last_output("cs:spend:B")
            self.assertEqual(out["means"], [0.0, 100.0])  # u3 converted, its amount was not a number; u1 clipped

    def test_assignment_checks_in_engine_mode(self):
        spec = make_spec()
        cfg = __import__("sequentialist.ab", fromlist=["config"]).config(spec)
        right = cfg.assign("u1", 0)
        wrong = "A" if right.variant == "B" else "B"
        with open_experiment(spec) as experiment:
            experiment.ingest([exposure("u1", right.variant, 0, source="s", seq=0, probability=right.probability),
                               exposure("u2", cfg.assign("u2", 1).variant, 1, source="s", seq=1, probability=0.9),
                               exposure("u3", wrong if cfg.assign("u3", 2).variant == right.variant else
                                        right.variant, 2, source="s", seq=2)])
            counts = experiment.status()["counts"]
            self.assertEqual((counts["probability_mismatch"], counts["assignment_mismatch"]), (1, 1))


class RecoveryTest(unittest.TestCase):
    def test_a_crash_changes_nothing(self):
        spec = make_spec(metrics=GUARDED)
        records = simulate(spec, 5000, rates={"A": 0.10, "B": 0.16}, spend={"A": 40, "B": 40},
                           errors={"A": 0.02, "B": 0.02})
        with open_experiment(spec) as whole:
            run(whole, records)
            expected = ([e.hash for e in whole.task.audit()], whole.handler.state(), whole.results())
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(FileStorage(Path(tmp), sync="flush"), clock="event", checkpoint_every=700)
            experiment = Experiment.create(engine, spec)
            half = len(records) // 2
            for i in range(0, half, 500):
                experiment.ingest(records[i:min(i + 500, half)])
            experiment.ingest(records[half - 300:half + 100])  # resent: duplicates
            experiment.task.closed = True  # the process dies
            experiment.task._lock.release()
            experiment = Experiment.open(engine, "exp")
            run(experiment, records[half + 100:])
            got = ([e.hash for e in experiment.task.audit()], experiment.handler.state(), experiment.results())
            self.assertEqual(got, expected)
            self.assertEqual(verify_audit(engine.storage.audit_read("exp"), "exp"), len(expected[0]))
            experiment.close()


if __name__ == "__main__":
    unittest.main()

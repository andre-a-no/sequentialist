# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Schemes: lots decided as their plans say, switching, rework and resubmission, supplier watch, crashes, records."""
import contextlib
import io
import json
import math
import random
import tempfile
import unittest
from pathlib import Path

from sequentialist.core import Engine, FileStorage, MemoryStorage, load_spec, verify_audit
from sequentialist.core.cli import main
from sequentialist.quality import Scheme, SchemeSpec, item, lot

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SCHEME = json.loads((EXAMPLES / "scheme.json").read_text(encoding="utf-8"))


def spec(**changes):
    return load_spec({**SCHEME, **changes}, SchemeSpec)


class Supplier:
    """Presents lots and inspects their items one at a time, as an inspection station does."""

    def __init__(self, scheme, seed=0):
        self.scheme, self.rnd, self.seq, self.time = scheme, random.Random(seed), 0, 1_800_000_000.0

    def _send(self, record):
        self.scheme.ingest([record])
        self.seq += 1
        self.time += 1.0

    def present(self, lot_id, p, size=10000):
        self._send(lot(lot_id, self.time, size=size, source="station", seq=self.seq))
        history = self.scheme.lot(lot_id)
        if history is None or history["status"] != "inspecting":
            return None if history is None else history["status"]
        while self.scheme.lot(lot_id)["status"] == "inspecting":
            self._send(item(lot_id, int(self.rnd.random() < p), self.time, source="station", seq=self.seq))
        return self.scheme.lot(lot_id)["status"]


def open_scheme(scheme_spec, storage=None):
    return Scheme.create(Engine(storage or MemoryStorage(), clock="event"), scheme_spec)


class AcceptanceTest(unittest.TestCase):
    def test_lots_are_accepted_as_often_as_the_plan_says(self):
        only_normal = spec(plans={"normal": SCHEME["plans"]["normal"]}, resubmission={"max": 0},
                           supplier_watch=None)
        oc = SCHEME["plans"]["normal"]["design"]["oc"]
        for quality, expected in ((0.01, oc["aql"]["accept"]), (0.05, oc["lq"]["accept"])):
            with self.subTest(quality=quality), open_scheme(only_normal) as scheme:
                supplier = Supplier(scheme, seed=int(quality * 1000))
                runs = 300
                accepted = sum(supplier.present(f"L{i}", quality) == "accepted" for i in range(runs))
                self.assertLess(abs(accepted / runs - expected), 4 * math.sqrt(expected * (1 - expected) / runs))
                self.assertEqual(scheme.severity, "normal")  # no tightened plan: no switching

    def test_the_decision_and_its_grounds_are_audited(self):
        with open_scheme(spec()) as scheme:
            Supplier(scheme).present("L-1", 0.0)
            entry = next(e for e in scheme.task.audit() if e.decision == "accept_lot")
            self.assertEqual((entry.basis["lot"], entry.basis["severity"], entry.basis["sum"]), ("L-1", "normal", 0))
            self.assertEqual(entry.evidence[0]["procedure"]["name"], "plan_test")
            self.assertEqual(entry.evidence[0]["guarantee"], "exact")
            self.assertGreaterEqual(entry.basis["accept_at_aql"], 0.95)
            self.assertEqual(scheme.task.segment("lot:normal"), 1)  # each lot is one segment of its plan's stream


class SwitchingTest(unittest.TestCase):
    def test_normal_tightened_discontinued_resumed_normal(self):
        with open_scheme(spec(supplier_watch=None)) as scheme:
            s = Supplier(scheme)
            self.assertEqual(s.present("A1", 0.4), "rejected")
            self.assertEqual(s.present("A2", 0.0), "accepted")
            self.assertEqual(scheme.severity, "normal")
            self.assertEqual(s.present("A3", 0.4), "rejected")  # 2 of the last 5
            self.assertEqual(scheme.severity, "tightened")
            for i in range(5):
                self.assertEqual(s.present(f"B{i}", 0.4), "rejected")
            self.assertTrue(scheme.handler.discontinued)
            self.assertIsNone(s.present("C1", 0.0))  # refused: never inspected
            self.assertEqual(scheme.status()["counts"]["lot_refused_discontinued"], 1)
            scheme.resume(actor="quality manager", reason="supplier corrective action 8D-117 verified")
            self.assertEqual((scheme.severity, scheme.handler.discontinued), ("tightened", False))
            for i in range(5):
                self.assertEqual(s.present(f"D{i}", 0.0), "accepted")
            self.assertEqual(scheme.severity, "normal")
            decisions = [(e.decision, e.basis.get("to")) for e in scheme.task.audit()
                         if e.decision in ("switch", "discontinue", "resume", "refuse_lot")]
            self.assertEqual(decisions, [("switch", "tightened"), ("discontinue", None), ("refuse_lot", None),
                                         ("resume", None), ("switch", "normal")])

    def test_reduced(self):
        scheme_spec = spec(plans={**SCHEME["plans"], "reduced": SCHEME["plans"]["normal"]},
                           switching={**SCHEME["switching"], "to_reduced": 3}, supplier_watch=None)
        with open_scheme(scheme_spec) as scheme:
            s = Supplier(scheme)
            for i in range(3):
                s.present(f"A{i}", 0.0)
            self.assertEqual(scheme.severity, "reduced")
            s.present("B", 0.4)
            self.assertEqual(scheme.severity, "normal")


class ResubmissionTest(unittest.TestCase):
    def test_rework_resubmit_concession(self):
        with open_scheme(spec(supplier_watch=None)) as scheme:
            s = Supplier(scheme)
            self.assertEqual(s.present("L1", 0.4), "rejected")
            self.assertEqual(s.present("L1", 0.0), "rejected")  # not allowed before a disposition
            self.assertEqual(scheme.status()["counts"]["resubmission_not_allowed"], 1)
            scheme.dispose("L1", "rework", actor="shift lead", reason="sorted and reworked")
            self.assertEqual(s.present("L1", 0.0), "accepted")
            history = scheme.lot("L1")
            self.assertEqual([p["severity"] for p in history["presentations"]], ["normal", "tightened"])
            self.assertEqual(scheme.severity, "normal")  # resubmissions do not count for switching
            self.assertEqual(s.present("L2", 0.4), "rejected")
            scheme.dispose("L2", "concession", actor="chief engineer", reason="deviation permit 42, non-critical use")
            self.assertEqual(scheme.lot("L2")["status"], "accepted_by_concession")
            scheme.dispose("L2", "scrap", actor="x", reason="y")  # refused: not rejected any more
            actions = [(e.decision, e.actor) for e in scheme.task.audit() if e.kind == "action"]
            self.assertEqual(actions, [("dispose", "shift lead"), ("dispose", "chief engineer"),
                                       ("dispose_refused", "x")])
            protocol = scheme.protocol("L1").to_markdown()
            self.assertIn("Presentation 2: tightened inspection", protocol)
            self.assertIn("*Guarantee: exact.*", protocol)
            self.assertIn("sorted and reworked", protocol)

    def test_data_quality(self):
        with open_scheme(spec(supplier_watch=None)) as scheme:
            s = Supplier(scheme)
            scheme.ingest([item("nowhere", 0, 1.0, source="station", seq=1000)])
            scheme.ingest([lot("L1", 2.0, source="station", seq=1001)])
            scheme.ingest([lot("L2", 3.0, source="station", seq=1002),  # while L1 is inspected
                           item("L1", 2, 4.0, source="station", seq=1003),  # not 0 or 1
                           item("L1", 0, 5.0, source="station", seq=1004)])
            counts = scheme.status()["counts"]
            self.assertEqual((counts["items_outside_inspection"], counts["lot_while_inspecting"],
                              counts["invalid_items"], counts["items"]), (1, 1, 1, 1))
            del s


class SupplierWatchTest(unittest.TestCase):
    def test_lasting_deterioration_raises_an_alarm(self):
        with open_scheme(spec()) as scheme:
            s = Supplier(scheme, seed=5)
            for i in range(8):
                s.present(f"good-{i}", 0.01)
            self.assertFalse([e for e in scheme.task.audit() if e.decision == "supplier_alarm"])
            for i in range(8):
                s.present(f"bad-{i}", 0.04)
            alarms = [e for e in scheme.task.audit() if e.decision == "supplier_alarm"]
            self.assertTrue(alarms)
            self.assertEqual(alarms[0].evidence[0]["procedure"]["name"], "cusum")
            self.assertTrue(alarms[0].basis["lot"].startswith("bad-"))


class RecoveryTest(unittest.TestCase):
    def test_a_crash_changes_nothing(self):
        def play(scheme, crash_after=None, engine=None):
            s = Supplier(scheme, seed=9)
            plan = [("A", 0.4), ("B", 0.01), ("C", 0.4), ("D", 0.02), ("E", 0.0), ("F", 0.05)]
            for k, (name, p) in enumerate(plan):
                if crash_after is not None and k == crash_after:
                    scheme.task.closed = True  # the process dies
                    scheme.task._lock.release()
                    scheme = Scheme.open(engine, "fasteners-supplier-a")
                    s.scheme = scheme
                s.present(name, p)
                if name == "A":
                    scheme.dispose("A", "rework", actor="lead", reason="rework")
            return scheme

        with open_scheme(spec()) as whole:
            play(whole)
            expected = ([e.hash for e in whole.task.audit()], whole.handler.state())
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(FileStorage(tmp, sync="flush"), clock="event", checkpoint_every=10**6)
            scheme = play(Scheme.create(engine, spec()), crash_after=3, engine=engine)
            self.assertEqual(([e.hash for e in scheme.task.audit()], scheme.handler.state()), expected)
            self.assertEqual(verify_audit(engine.storage.audit_read("fasteners-supplier-a")), len(expected[0]))
            scheme.close()


class CliTest(unittest.TestCase):
    def test_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = FileStorage(Path(tmp) / "store", sync="flush")
            scheme = Scheme.create(Engine(storage, clock="event"), spec())
            Supplier(scheme).present("L1", 0.4)
            scheme.close()
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["quality", "check", str(EXAMPLES / "scheme.json")]), 0)
                self.assertEqual(main(["quality", "oc", str(EXAMPLES / "normal.designed.json"), "--at", "0.01"]), 0)
                self.assertEqual(main(["quality", "design", str(EXAMPLES / "single.plan.json")]), 0)
                self.assertEqual(main(["quality", "schema", "scheme"]), 0)
                self.assertEqual(main(["quality", "protocol", f"files:{tmp}/store", "fasteners-supplier-a", "L1"]), 0)
                self.assertEqual(main(["quality", "report", f"files:{tmp}/store", "fasteners-supplier-a"]), 0)
            text = out.getvalue()
            self.assertIn("characteristic verified", text)
            self.assertIn("| 0.01 | 0.9500 |", text)
            self.assertIn("Inspection of lot L1", text)
            self.assertIn("Inspection scheme fasteners-supplier-a", text)


if __name__ == "__main__":
    unittest.main()

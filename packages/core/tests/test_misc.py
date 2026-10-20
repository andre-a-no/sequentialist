# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Storages in detail, reports, simulations, sinks, pseudonyms and the command line."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from sequentialist.core import (
    ConflictError,
    Engine,
    FileStorage,
    Finding,
    JsonlSink,
    MemoryStorage,
    Report,
    Section,
    SQLiteStorage,
    StorageError,
    Table,
    WebhookSink,
    open_storage,
    proportion,
    pseudonymize,
    run_simulations,
)
from sequentialist.core.cli import main

from .helpers import KIND, SPEC, Watch, records, values


class StorageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def each(self):
        sqlite = SQLiteStorage(self.dir / "db.sqlite", sync="flush")
        self.addCleanup(sqlite.close)
        return [MemoryStorage(), FileStorage(self.dir / "files", sync="flush"), sqlite]

    def test_versions_journal_audit_documents(self):
        for storage in self.each():
            with self.subTest(storage=type(storage).__name__):
                self.assertIsNone(storage.load("t"))
                self.assertEqual(storage.save("t", {"a": 1}, expected=0), 1)
                with self.assertRaises(ConflictError):
                    storage.save("t", {"a": 2}, expected=0)
                storage.journal_append("t", [{"r": 1}, {"r": 2}])
                storage.journal_append("t", [{"r": 3}])
                self.assertEqual(storage.journal_read("t"), [{"r": 1}, {"r": 2}, {"r": 3}])
                self.assertEqual(storage.save("t", {"a": 2, "π": "ü"}, expected=1, clear_journal=True), 2)
                self.assertEqual(storage.load("t"), (2, {"a": 2, "π": "ü"}))
                self.assertEqual(storage.journal_read("t"), [])
                storage.audit_append("t", {"n": 0})
                self.assertEqual(storage.audit_read("t"), [{"n": 0}])
                storage.put_document("t", "segments/mean/0", {"x": [1, 2]})
                storage.put_document("t", "report", {"y": 1})
                self.assertEqual(storage.documents("t"), ["report", "segments/mean/0"])
                self.assertEqual(storage.get_document("t", "segments/mean/0"), {"x": [1, 2]})
                self.assertIsNone(storage.get_document("t", "nothing"))
                self.assertEqual(storage.tasks(), ["t"])
                for bad in ("../x", "a b", ".hidden"):
                    with self.assertRaises(StorageError):
                        storage.load(bad)

    def test_torn_lines_are_ignored_and_cut(self):
        storage = FileStorage(self.dir / "files", sync="flush")
        storage.journal_append("t", [{"r": 1}])
        with open(self.dir / "files" / "t" / "journal.jsonl", "ab") as f:
            f.write(b'{"r": 2, "tor')
        self.assertEqual(storage.journal_read("t"), [{"r": 1}])
        storage.journal_append("t", [{"r": 3}])
        self.assertEqual(storage.journal_read("t"), [{"r": 1}, {"r": 3}])

    def test_open_storage(self):
        self.assertIsInstance(open_storage("memory:"), MemoryStorage)
        self.assertIsInstance(open_storage(f"files:{self.dir}"), FileStorage)
        open_storage(f"sqlite:{self.dir / 'x.db'}").close()
        with self.assertRaises(ValueError):
            open_storage("postgres://somewhere")


class ReportTest(unittest.TestCase):
    def test_every_finding_names_its_guarantee(self):
        with self.assertRaisesRegex(ValueError, "needs a guarantee"):
            Finding("B is better.", "probably")
        report = Report("Experiment", "exp", "ab" * 32, 1793491200.0, summary="Ship B.", sections=[
            Section("Results", findings=[Finding("B converts more than A.", "exact", {"difference": 0.0123}),
                                         Finding("Revenue per session is higher.", "asymptotic")],
                    tables=[Table(["metric", "estimate", "guarantee"],
                                  [["visit", 0.07, "exact"], ["x", None, "none"]])]),
        ])
        text = report.to_markdown()
        self.assertIn("- B converts more than A. *Guarantee: exact.*", text)
        self.assertIn("difference = 0.0123", text)
        self.assertIn("| x | — | none |", text)
        self.assertIn("**asymptotic**: they hold approximately", text)
        self.assertNotIn("**model**", text)
        self.assertEqual(json.loads(json.dumps(report.to_json()))["sections"][0]["findings"][0]["guarantee"], "exact")


class SimulateTest(unittest.TestCase):
    def test_runs_are_reproducible_and_counted_with_their_error(self):
        first = run_simulations(lambda rnd, i: rnd.random() < 0.05, 400, seed=3)
        self.assertEqual(first, run_simulations(lambda rnd, i: rnd.random() < 0.05, 400, seed=3))
        p = proportion(sum(first), len(first))
        lo, hi = p.interval
        self.assertLess(lo, p.rate)
        self.assertLess(p.rate, hi)
        self.assertLess(lo, 0.05)
        self.assertLess(0.05, hi)


class SinkAndPrivacyTest(unittest.TestCase):
    def test_sinks_get_entries_after_the_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            sent = []

            def post(url, body, headers, timeout):
                if not sent:
                    sent.append(None)
                    raise OSError("refused")  # retried
                sent.append(json.loads(body))

            hook = WebhookSink("http://hook", post=post, attempts=3)
            jsonl = JsonlSink(Path(tmp) / "entries.jsonl")
            task = Engine(MemoryStorage(), sinks=[hook, jsonl], clock="event").create("w", KIND, SPEC, Watch(SPEC))
            task.ingest(records(values()))
            task.close()
            hook.close()
            lines = [json.loads(x) for x in (Path(tmp) / "entries.jsonl").read_text().splitlines()]
            self.assertEqual(lines[0]["kind"], "created")
            decisions = [e for e in sent[1:]]
            self.assertTrue(decisions)
            self.assertEqual({d["kind"] for d in decisions}, {"decision"})

    def test_pseudonyms(self):
        key = b"k" * 32
        self.assertEqual(pseudonymize("user-1", key), pseudonymize("user-1", key))
        self.assertNotEqual(pseudonymize("user-1", key), pseudonymize("user-1", b"j" * 32))
        self.assertEqual(len(pseudonymize("user-1", key)), 64)
        with self.assertRaises(ValueError):
            pseudonymize("user-1", b"short")


class CliTest(unittest.TestCase):
    def test_tasks_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = FileStorage(tmp, sync="flush")
            task = Engine(storage, clock="event").create("w", KIND, SPEC, Watch(SPEC))
            task.ingest(records(values()))
            task.close()
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(main(["tasks", f"files:{tmp}"]), 0)
                self.assertEqual(main(["audit", f"files:{tmp}", "w"]), 0)
            self.assertEqual(out.getvalue().splitlines()[0], "w")
            self.assertIn("chain verified", err.getvalue())
            path = Path(tmp) / "w" / "audit.jsonl"
            path.write_text(path.read_text().replace('"alarm"', '"other"', 1))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                self.assertEqual(main(["audit", f"files:{tmp}", "w"]), 1)
            self.assertIn("BROKEN", err.getvalue())


if __name__ == "__main__":
    unittest.main()

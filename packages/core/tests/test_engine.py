# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""The engine: idempotent intake, crash recovery bit for bit, segments, commands, locks, shards."""
import json
import tempfile
import unittest
from pathlib import Path

from sequentialist.core import (
    AuditError,
    Engine,
    FileStorage,
    InvalidRecord,
    MemoryStorage,
    Record,
    SQLiteStorage,
    TaskError,
    TaskLocked,
    replay,
    shard_of,
    verify_audit,
)

from .helpers import KIND, SPEC, Watch, records, values


def crash(task):
    """The process dies: nothing more is written, the lock goes with the process."""
    task.closed = True
    task._lock.release()


def snapshot(task):
    """Everything that must be identical between an uninterrupted and a recovered task."""
    audit = [(e.n, e.kind, e.decision, e.basis, e.evidence, e.at, e.hash) for e in task.audit()]
    return {
        "handler": task.handler.state(),
        "digest": task.run("mean").history_digest,
        "t": task.run("mean").t,
        "segment": task.segment("mean"),
        "watermark": task.watermark,
        "output": task.last_output("mean"),
        "counters": dict(task.counters),
        "audit": audit,
        "documents": task.engine.storage.documents(task.task_id),
    }


class StorageCases:
    """Each test runs on every storage."""

    def storages(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        made = {
            "memory": MemoryStorage(),
            "files": FileStorage(Path(tmp.name) / "files", sync="flush"),
            "sqlite": SQLiteStorage(Path(tmp.name) / "tasks.db", sync="flush"),
        }
        for storage in made.values():
            self.addCleanup(storage.close)
        return made


class RecoveryTest(StorageCases, unittest.TestCase):
    def uninterrupted(self, data):
        engine = Engine(MemoryStorage(), clock="event")
        task = engine.create("w", KIND, SPEC, Watch(SPEC))
        for i in range(0, len(data), 50):
            task.ingest(data[i:i + 50])
        result = snapshot(task)
        task.close()
        return result

    def test_alarms_and_segments(self):
        data = records(values())
        expected = self.uninterrupted(data)
        self.assertGreaterEqual(expected["handler"]["alarms"], 2)
        self.assertEqual(expected["segment"], expected["handler"]["alarms"])
        self.assertEqual(len(expected["documents"]), expected["segment"])
        alarms = [a for a in expected["audit"] if a[1] == "decision"]
        self.assertGreater(alarms[0][3]["seen"], 300)  # no false alarm before the change
        self.assertEqual(alarms[0][4][0]["procedure"]["name"], "mean_e_detector")
        self.assertEqual(alarms[0][4][0]["guarantee"], "exact")

    def test_reopen_after_close_and_after_crash_is_the_uninterrupted_task(self):
        data = records(values())
        expected = self.uninterrupted(data)
        for cut in (40, 330, 420):  # before the change, inside a batch with an alarm, after
            for name, storage in self.storages().items():
                with self.subTest(storage=name, cut=cut):
                    engine = Engine(storage, clock="event", checkpoint_every=10**6)
                    task_id = "w"
                    task = engine.create(task_id, KIND, SPEC, Watch(SPEC))
                    for i in range(0, cut, 50):
                        task.ingest(data[i:min(i + 50, cut)])
                    crash(task)
                    task = engine.open(task_id, Watch)
                    for i in range(cut, len(data), 50):
                        task.ingest(data[i:i + 50])
                    task.close()
                    task = engine.open(task_id, Watch)
                    got = snapshot(task)
                    task.close()
                    self.assertEqual(got, {**expected, "documents": got["documents"]})
                    self.assertEqual(verify_audit(storage.audit_read(task_id), task_id), len(expected["audit"]))

    def test_crash_after_a_decision_before_its_checkpoint(self):
        """The audit entry is written, the checkpoint is not: the replay meets the entry and keeps it."""
        data = records(values())
        expected = self.uninterrupted(data)
        storage = MemoryStorage()
        engine = Engine(storage, clock="event", checkpoint_every=10**6)
        task = engine.create("w", KIND, SPEC, Watch(SPEC))
        original = task.checkpoint
        task.checkpoint = lambda: None  # the process dies before every checkpoint
        task.ingest(data)
        self.assertGreater(len(storage.audit_read("w")), 1)
        task.checkpoint = original
        crash(task)
        notified = []
        engine.sinks.append(notified.append)
        task = engine.open("w", Watch)
        self.assertEqual(snapshot(task), {**expected, "documents": snapshot(task)["documents"]})
        # the decisions were never announced before the crash; they are now, once
        self.assertEqual([e.n for e in notified if e.kind == "decision"],
                         [a[0] for a in expected["audit"] if a[1] == "decision"])
        task.close()

    def test_journal_left_after_a_checkpoint_is_harmless(self):
        """A crash between saving the state and emptying the journal: records and commands are not applied twice."""
        storage = MemoryStorage()
        engine = Engine(storage, clock="event")
        task = engine.create("w", KIND, SPEC, Watch(SPEC))
        data = records(values(50, 0))
        task.ingest(data[:20])
        save = storage.save

        def save_keeping_journal(task_id, state, *, expected, clear_journal=False):
            return save(task_id, state, expected=expected, clear_journal=False)

        storage.save = save_keeping_journal
        task.command("pause", actor="alice", reason="maintenance")
        task.ingest(data[20:30])  # ignored: paused
        task.checkpoint()
        storage.save = save
        crash(task)
        self.assertTrue(storage.journal_read("w"))
        task = engine.open("w", Watch)
        self.assertEqual(task.handler.state()["seen"], 20)
        self.assertEqual(task.counters["commands"], 1)
        self.assertEqual([e.kind for e in task.audit()].count("action"), 1)
        task.close()


class IntakeTest(unittest.TestCase):
    def setUp(self):
        self.storage = MemoryStorage()
        self.engine = Engine(self.storage, clock="event")
        self.task = self.engine.create("w", KIND, SPEC, Watch(SPEC))
        self.addCleanup(self.task.close)

    def test_duplicates_are_dropped(self):
        data = records(values(30, 0))
        first = self.task.ingest(data[:20])
        again = self.task.ingest(data[10:30] + data[25:30])
        self.assertEqual((first.accepted, first.duplicates), (20, 0))
        self.assertEqual((again.accepted, again.duplicates), (10, 15))
        self.assertEqual(self.task.run("mean").t, 30)
        self.assertEqual(self.task.counters["duplicates"], 15)

    def test_a_batch_with_an_invalid_record_is_refused_whole(self):
        bad = [Record("s", 0, {"x": 0.1}), Record("s", 1, {"x": float("nan")})]
        with self.assertRaisesRegex(InvalidRecord, "record 1"):
            self.task.ingest(bad)
        for record, text in ((Record("", 0, {}), "source"), (Record("s", -1, {}), "seq"),
                             (Record("s", 0, {}, float("inf")), "time"), (Record("s", 0, [1]), "data")):
            with self.subTest(text=text), self.assertRaisesRegex(InvalidRecord, text):
                self.task.ingest([record])
        self.assertEqual(self.task.counters["records"], 0)
        self.assertEqual(self.storage.journal_read("w"), [])

    def test_time_moves_by_records_and_by_advance(self):
        self.task.ingest(records(values(3, 0)))
        self.assertEqual(self.task.watermark, 1_000_002.0)
        self.task.advance(1_000_100.0)
        self.task.advance(5.0)  # backwards: no effect
        self.assertEqual(self.task.watermark, 1_000_100.0)
        self.assertEqual(self.task.handler.ticks, 4)

    def test_commands_need_an_actor_and_a_reason(self):
        with self.assertRaises(InvalidRecord):
            self.task.command("pause", actor="", reason="x")
        with self.assertRaisesRegex(TaskError, "unknown command 'explode'; this task accepts"):
            self.task.command("explode", actor="bob", reason="why not")
        self.assertIsNone(self.task.failed)  # refused before the journal: the task goes on
        self.assertEqual(self.storage.journal_read("w"), [])
        self.task.command("pause", actor="bob", reason="maintenance")


class LockAndShardTest(StorageCases, unittest.TestCase):
    def test_one_writer_per_task(self):
        for name, storage in self.storages().items():
            with self.subTest(storage=name):
                engine = Engine(storage)
                task = engine.create("w", KIND, SPEC, Watch(SPEC))
                with self.assertRaises(TaskLocked):
                    engine.open("w", Watch)
                with self.assertRaisesRegex(TaskError, "exists"):
                    task.close()
                    engine.create("w", KIND, SPEC, Watch(SPEC))
                engine.open("w", Watch).close()

    def test_shards(self):
        ids = [f"exp-{i}" for i in range(40)]
        owners = {i: shard_of(i, 3) for i in ids}
        self.assertEqual(set(owners.values()), {0, 1, 2})
        storage = MemoryStorage()
        for task_id, shard in owners.items():
            right = Engine(storage, shard=(shard, 3))
            wrong = Engine(storage, shard=((shard + 1) % 3, 3))
            with self.assertRaisesRegex(TaskError, "belongs to shard"):
                wrong.create(task_id, KIND, SPEC, Watch(SPEC))
            right.create(task_id, KIND, SPEC, Watch(SPEC)).close()

    def test_reopening_with_other_streams_is_refused(self):
        class Other(Watch):
            def streams(self):
                return {"other": super().streams()["mean"]}

        storage = MemoryStorage()
        Engine(storage).create("w", KIND, SPEC, Watch(SPEC)).close()
        with self.assertRaisesRegex(TaskError, "declares streams"):
            Engine(storage).open("w", Other)


class AuditTest(unittest.TestCase):
    def test_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = FileStorage(tmp, sync="flush")
            task = Engine(storage, clock="event").create("w", KIND, SPEC, Watch(SPEC))
            task.ingest(records(values()))
            task.command("pause", actor="alice", reason="end of test")
            task.close()
            path = Path(tmp) / "w" / "audit.jsonl"
            lines = path.read_text().splitlines()
            self.assertEqual(verify_audit([json.loads(x) for x in lines], "w"), len(lines))
            self.assertEqual(json.loads(lines[-1])["actor"], "alice")
            edited = [json.loads(x) for x in lines]
            edited[1]["basis"]["seen"] += 1
            with self.assertRaisesRegex(AuditError, "entry 1: its content does not match"):
                verify_audit(edited)
            with self.assertRaisesRegex(AuditError, "entry 1: numbered 2"):
                verify_audit([json.loads(x) for x in lines[:1] + lines[2:]])

    def test_a_handler_that_is_not_deterministic_is_caught_on_replay(self):
        class Flaky(Watch):
            calls = 0

            def on_record(self, record, ctx):
                Flaky.calls += 1
                if Flaky.calls == 700:
                    ctx.record("decision", "something else")
                else:
                    super().on_record(record, ctx)

        storage = MemoryStorage()
        engine = Engine(storage, clock="event", checkpoint_every=10**6)
        task = engine.create("w", KIND, SPEC, Flaky(SPEC))
        task.checkpoint = lambda: None
        task.ingest(records(values()))
        crash(task)
        with self.assertRaisesRegex(AuditError, "not deterministic"):
            engine.open("w", Flaky)

    def test_replays_of_the_same_data_give_the_same_audit_log(self):
        data = records(values())
        hashes = []
        for _ in range(2):
            task = replay(KIND, SPEC, Watch(SPEC), [*data[:400], 2_000_000.0, *data[400:]])
            hashes.append([e.hash for e in task.audit()])
            task.close()
        self.assertEqual(hashes[0], hashes[1])
        self.assertGreater(len(hashes[0]), 2)


if __name__ == "__main__":
    unittest.main()

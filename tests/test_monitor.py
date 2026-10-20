# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC (Kazan, Tatarstan, Russian Federation)
"""Monitors: alarms and segments, checkpoints, crash recovery from the journal."""
import json
import random
import tempfile
import unittest
from pathlib import Path

from sequentialist import MonitorConfig, MonitorError
from sequentialist.monitor import Monitor

DETECTOR = MonitorConfig(
    name="latency",
    detector="mean_e_detector",
    params={"m": 0.2, "lo": 0.0, "hi": 1.0, "arl": 1000, "direction": "increase", "statistic": "cusum"},
)


def latencies(seed: int = 4, before: int = 400, after: int = 400) -> list[float]:
    """Response times with mean 0.2 s, then 0.3 s (the monitoring tutorial of sequential-analysis)."""
    rnd = random.Random(seed)
    return ([min(1.0, rnd.gammavariate(2.0, 0.1)) for _ in range(before)]
            + [min(1.0, rnd.gammavariate(2.0, 0.15)) for _ in range(after)])


def feed(monitor: Monitor, values, batch: int = 50):
    alerts = []
    for i in range(0, len(values), batch):
        raised, _ = monitor.ingest({"value": v} for v in values[i:i + batch])
        alerts.extend(raised)
    return alerts


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def monitor(self, name="m", config=DETECTOR, **kwargs):
        kwargs.setdefault("sync", "flush")
        monitor = Monitor(config, self.dir / name, **kwargs)
        return monitor

    def test_alarms_after_the_change_and_rearms(self):
        monitor = self.monitor()
        alerts = feed(monitor, latencies())
        self.assertGreaterEqual(len(alerts), 1)
        self.assertGreater(alerts[0].seq, 400)  # no false alarm on the steady state
        self.assertEqual([a.segment for a in alerts], list(range(len(alerts))))
        self.assertEqual(monitor.segment, len(alerts))
        self.assertEqual(monitor.run.run_id, f"latency/{len(alerts)}")
        for a in alerts:
            self.assertIn("change_estimate", a.details)
        # the next segment names the run that alarmed before it
        self.assertEqual(monitor.run.provenance["initialized_from"]["rearmed_after"], alerts[-1].run_id)
        self.assertTrue((self.dir / "m" / "segments" / "0.json").exists())
        self.assertEqual([a["id"] for a in monitor.alerts()], [a.id for a in alerts])
        monitor.close()

    def test_restart_from_checkpoint_continues_the_same_run(self):
        values = latencies()
        whole = self.monitor("whole", checkpoint_every=10**9)
        expected = feed(whole, values)
        whole.close()

        first = self.monitor("split", checkpoint_every=70)
        alerts = feed(first, values[:450])
        first.close()
        second = self.monitor("split", checkpoint_every=70)
        alerts += feed(second, values[450:])
        self.assertEqual([(a.seq, a.id) for a in alerts], [(a.seq, a.id) for a in expected])
        self.assertEqual(second.run.history_digest, whole.run.history_digest)
        self.assertEqual(second.run.t, whole.run.t)
        second.close()

    def test_crash_is_recovered_from_the_journal(self):
        values = latencies()
        whole = self.monitor("whole")
        expected = feed(whole, values)
        whole.close()

        crashed = self.monitor("crash", checkpoint_every=10**9)
        before = feed(crashed, values[:430])
        # no close(): the process dies; the state on disk is the last checkpoint plus the journal
        crashed.journal.close()
        crashed.alert_log.close()
        recovered = self.monitor("crash", checkpoint_every=10**9)
        after = recovered.replayed + feed(recovered, values[430:])
        self.assertEqual([a.id for a in before + after], [a.id for a in expected])
        self.assertEqual(recovered.next_seq, len(values))
        self.assertEqual(recovered.run.history_digest, whole.run.history_digest)
        recovered.close()

    def test_alarm_in_the_journal_is_raised_on_recovery(self):
        values = latencies()
        whole = self.monitor("whole")
        expected = feed(whole, values[:500])
        whole.close()

        crashed = self.monitor("crash", checkpoint_every=10**9)
        before = feed(crashed, values[:430])
        # the process dies after the journal write of the next batch, before the run saw it
        crashed.journal.append([{"seq": 430 + i, "value": v} for i, v in enumerate(values[430:500])])
        crashed.journal.close()
        crashed.alert_log.close()
        recovered = self.monitor("crash", checkpoint_every=10**9)
        self.assertEqual([a.id for a in before + recovered.replayed], [a.id for a in expected])
        self.assertTrue(recovered.replayed)  # the alarm at seq 446 was raised by the replay
        self.assertEqual(recovered.next_seq, 500)
        self.assertEqual([a["id"] for a in recovered.alerts()], [a.id for a in expected])
        recovered.close()

    def test_crash_right_after_an_alarm_loses_no_values(self):
        class Crash(BaseException):
            pass

        class Dying(Monitor):
            def _alarm(self, *args):
                super()._alarm(*args)
                raise Crash  # after the alarm's checkpoint, with the rest of the batch unseen

        values = latencies()
        whole = self.monitor("whole")
        expected = feed(whole, values[:500])
        whole.close()

        dying = Dying(DETECTOR, self.dir / "crash", sync="flush", checkpoint_every=10**9)
        feed(dying, values[:400])
        with self.assertRaises(Crash):
            dying.ingest({"value": v} for v in values[400:500])  # the alarm is at seq 446
        dying.journal.close()
        dying.alert_log.close()
        recovered = self.monitor("crash", checkpoint_every=10**9)
        self.assertEqual(recovered.next_seq, 500)
        self.assertEqual(recovered.run.history_digest, whole.run.history_digest)
        self.assertEqual([a["id"] for a in recovered.alerts()], [a.id for a in expected])
        recovered.close()

    def test_torn_journal_line_is_dropped(self):
        monitor = self.monitor(checkpoint_every=10**9)
        feed(monitor, [0.2] * 10)
        monitor.journal.close()
        monitor.alert_log.close()
        with open(self.dir / "m" / "journal.jsonl", "ab") as f:
            f.write(b'{"seq": 10, "val')
        recovered = self.monitor(checkpoint_every=10**9)
        self.assertEqual(recovered.next_seq, 10)
        self.assertEqual(recovered.run.t, 10)
        recovered.close()

    def test_changed_configuration_is_refused(self):
        self.monitor().close()
        changed = MonitorConfig(name="latency", detector=DETECTOR.detector, params={**DETECTOR.params, "arl": 500})
        with self.assertRaisesRegex(MonitorError, "does not match the configuration"):
            self.monitor(config=changed)

    def test_without_rearm_the_monitor_stops(self):
        config = MonitorConfig(name="once", detector=DETECTOR.detector, params=DETECTOR.params, rearm=False)
        monitor = self.monitor(config=config)
        alerts = feed(monitor, latencies(), batch=800)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(monitor.status()["state"], "stopped")
        with self.assertRaisesRegex(MonitorError, "stopped"):
            monitor.ingest([{"value": 0.2}])
        monitor.close()
        again = self.monitor(config=config)
        self.assertTrue(again.stopped)
        again.close()

    def test_invalid_values_are_counted_and_skipped(self):
        monitor = self.monitor()
        _, counts = monitor.ingest([{"value": 0.2}, {"value": 7.0}, {"value": "fast"}, {"value": 0.3}])
        self.assertEqual(counts, {"accepted": 4, "invalid": 2})
        self.assertEqual(monitor.run.t, 2)
        status = monitor.status()
        self.assertEqual(status["invalid_skipped"], 2)
        self.assertEqual(status["next_seq"], 4)
        json.dumps(status)
        monitor.close()

    def test_event_time_is_kept_in_the_alert(self):
        monitor = self.monitor()
        values = latencies()
        alerts, _ = monitor.ingest({"value": v, "time": 1_760_000_000.0 + i} for i, v in enumerate(values))
        self.assertEqual(alerts[0].time, 1_760_000_000.0 + alerts[0].seq)
        monitor.close()


if __name__ == "__main__":
    unittest.main()

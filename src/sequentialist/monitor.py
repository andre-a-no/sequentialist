# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC (Kazan, Tatarstan, Russian Federation)
"""Monitor: one stream of values watched by one detector, across alarms and restarts.

A monitor's life is a series of *segments*.  Each segment is one run of
sequential-analysis, from a fresh detector to its alarm.  After the alarm the
monitor starts the next segment with the next observation (or stops, with
``rearm = false``).  The run of a segment is named ``<monitor>/<segment>``
and its provenance names the run before it.

Files in the monitor's directory:

    state.json      the segment, sequence counter and the run's checkpoint
    journal.jsonl   observations accepted since that checkpoint
    alerts.jsonl    every alert, in order (the durable record)
    segments/N.json the final checkpoint of every finished segment

Order of writes, and what a crash at any point leaves:

1. accepted observations are appended to the journal (and forced to disk)
   before the run sees them: none is lost;
2. an alarm is appended to the alert log, then the state is checkpointed,
   then the sinks are notified: an alarm is recorded exactly once, but a
   crash between the checkpoint and the notification loses the notification
   (the alert log still has it).  The journal is kept: the rest of the batch
   is in it, not yet seen by the run;
3. every `checkpoint_every` observations, once the run has seen them all,
   the state is written and the journal emptied.  On start the journal is
   replayed; observations the checkpoint already holds are dropped by
   delivery.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from sequential_analysis import (
    Delivery,
    IncompatibleCheckpoint,
    Observation,
    Run,
    checkpoint_id,
)

from .alerts import Alert
from .config import MonitorConfig
from .detectors import INPUT, alarm_fields
from .storage import JsonLines, read_json, write_json

logger = logging.getLogger("sequentialist")

STATE_FORMAT = "sequentialist.monitor/1"
RUNTIME = "sequentialist"


class MonitorError(RuntimeError):
    """The monitor cannot take observations (stopped or failed), or its state cannot be used."""


class Monitor:
    def __init__(
        self,
        config: MonitorConfig,
        directory: Path,
        *,
        checkpoint_every: int = 1000,
        sync: str = "fsync",
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.name = config.name
        self.directory = directory
        self.checkpoint_every = checkpoint_every
        self._clock = clock
        self._lock = threading.Lock()
        self.procedure = config.procedure()

        directory.mkdir(parents=True, exist_ok=True)
        (directory / "segments").mkdir(exist_ok=True)
        self._state_path = directory / "state.json"
        self.segment = 0
        self.next_seq = 0
        self.alarms = 0
        self.stopped = False
        self.failed: str | None = None
        self.last_output: Any = None
        self._since_checkpoint = 0
        self._invalid = 0  # values skipped as invalid since the service started

        if self._state_path.exists():
            self._restore(read_json(self._state_path))
        else:
            self.run = self._new_run(start=0, initialized_from="default")
        self.journal = JsonLines(directory / "journal.jsonl", sync)
        self.alert_log = JsonLines(directory / "alerts.jsonl", "fsync")
        self._alert_ids = {a["id"] for a in self.alert_log.read()}
        self.replayed = self._replay()

    # ------------------------------------------------------------ segments

    def _run_id(self, segment: int) -> str:
        return f"{self.name}/{segment}"

    def _new_run(self, start: int, initialized_from: Any) -> Run:
        run = Run(self.procedure, delivery=Delivery(start=start), run_id=self._run_id(self.segment),
                  initialized_from=initialized_from, clock=self._clock)
        run.start(runtime=RUNTIME)
        self._start = start
        return run

    def _restore(self, state: Mapping[str, Any]) -> None:
        if state.get("format") != STATE_FORMAT or state.get("monitor") != self.name:
            raise MonitorError(f"monitor {self.name!r}: {self._state_path} is not this monitor's state")
        self.segment, self.next_seq = state["segment"], state["next_seq"]
        self.alarms, self.stopped = state["alarms"], state["stopped"]
        try:
            self.run = Run.restore(self.procedure, state["checkpoint"], delivery=Delivery(start=state["start"]),
                                   clock=self._clock)
        except IncompatibleCheckpoint as error:
            raise MonitorError(
                f"monitor {self.name!r}: the saved run does not match the configuration ({error}). "
                f"Restore the old configuration, or move {self.directory} away to start this monitor afresh"
            ) from None
        self.run.start(runtime=RUNTIME)
        self._start = state["start"]

    def _rearm(self, after_seq: int) -> None:
        """End the segment that alarmed; the next begins with the observation after `after_seq`."""
        final = self.run.checkpoint()
        write_json(self.directory / "segments" / f"{self.segment}.json", final)
        previous = self.run.run_id
        self.run.close()
        self.segment += 1
        self.run = self._new_run(after_seq + 1, {"rearmed_after": previous, "checkpoint": checkpoint_id(final)})

    # ------------------------------------------------------------ observations

    def ingest(self, values: Iterable[Mapping[str, Any]]) -> tuple[list[Alert], dict[str, int]]:
        """Accept values, each ``{"value": v}`` or ``{"value": v, "time": t}``, in order.

        Returns the alerts raised and counts of the values accepted and of
        those the detector skipped as invalid (a value outside its contract).
        The caller notifies the sinks of the alerts.
        """
        with self._lock:
            if self.failed is not None:
                raise MonitorError(f"monitor {self.name!r} has failed: {self.failed}")
            if self.stopped:
                raise MonitorError(f"monitor {self.name!r} has stopped at its alarm (rearm = false)")
            records = []
            for i, item in enumerate(values):
                record = {"seq": self.next_seq + i, "value": item.get("value")}
                if item.get("time") is not None:
                    record["time"] = item["time"]
                records.append(record)
            self.journal.append(records)
            self.next_seq += len(records)
            invalid_before = self._invalid
            alerts: list[Alert] = []
            for record in records:
                if self.failed is not None or self.stopped:
                    break  # accepted, but no run will see the rest
                alerts.extend(self._process(record))
            self._since_checkpoint += len(records)
            if self.failed is None and self._since_checkpoint >= self.checkpoint_every:
                self._checkpoint()
            return alerts, {"accepted": len(records), "invalid": self._invalid - invalid_before}

    def _process(self, record: Mapping[str, Any]) -> list[Alert]:
        obs = Observation(INPUT, record["value"], source=self.name, seq=record["seq"], time=record.get("time"))
        invalid_before = self.run.counters["invalid_skipped"]
        try:
            events = self.run.offer(obs)
        except Exception as error:  # a numerical failure of the detector: the run is FAILED, nothing committed
            self.failed = f"{type(error).__name__}: {error}"
            logger.error("monitor %s failed at seq %d: %s", self.name, record["seq"], self.failed)
            return []
        self._invalid += self.run.counters["invalid_skipped"] - invalid_before
        alerts = []
        for event in events:
            self.last_output = event.output
            if event.terminal:
                alerts.append(self._alarm(event.output, event.t, record))
        return alerts

    def _alarm(self, output: Any, t: int, record: Mapping[str, Any]) -> Alert:
        alert = Alert(
            id=f"{self.run.run_id}@{t}",
            monitor=self.name,
            run_id=self.run.run_id,
            segment=self.segment,
            t=t,
            seq=record["seq"],
            time=record.get("time"),
            detector=self.procedure.name,
            details=alarm_fields(output),
            raised_at=self._clock(),
        )
        if alert.id not in self._alert_ids:
            self.alert_log.append([alert.to_json()])
            self._alert_ids.add(alert.id)
        self.alarms += 1
        if self.config.rearm:
            self._rearm(record["seq"])
        else:
            self.stopped = True
        self._checkpoint(clear_journal=False)  # an alarm is never replayed; the journal may hold unseen values
        return alert

    def _replay(self) -> list[Alert]:
        """Feed the journal to the restored run: the observations accepted after the last checkpoint."""
        alerts: list[Alert] = []
        count = 0
        for record in self.journal.read():
            self.next_seq = max(self.next_seq, record["seq"] + 1)
            if self.failed is None and not self.stopped:
                alerts.extend(self._process(record))
                count += 1
        if count:
            logger.info("monitor %s: replayed %d observations from the journal", self.name, count)
        if self.failed is None:
            self._checkpoint()
        return alerts

    # ------------------------------------------------------------ persistence

    def _checkpoint(self, clear_journal: bool = True) -> None:
        """Write the state; empty the journal only if the run has seen everything in it."""
        state = {
            "format": STATE_FORMAT,
            "monitor": self.name,
            "segment": self.segment,
            "start": self._start,
            "next_seq": self.next_seq,
            "alarms": self.alarms,
            "stopped": self.stopped,
            "checkpoint": self.run.checkpoint(),
        }
        write_json(self._state_path, state)
        if clear_journal:
            self.journal.clear()
            self._since_checkpoint = 0

    def close(self) -> None:
        with self._lock:
            if self.failed is None:
                self._checkpoint()
            self.journal.close()
            self.alert_log.close()

    # ------------------------------------------------------------ reading

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "description": self.config.description,
                "detector": self.procedure.identity(),
                "guarantee": self.procedure.guarantee,
                "state": "failed" if self.failed else "stopped" if self.stopped else "running",
                "error": self.failed,
                "segment": self.segment,
                "run_id": self.run.run_id,
                "t": self.run.t,
                "next_seq": self.next_seq,
                "alarms": self.alarms,
                "invalid_skipped": self.run.counters["invalid_skipped"],
                "last_output": None if self.last_output is None else self.procedure.encode_output(self.last_output),
            }

    def alerts(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.alert_log.read())[-limit:] if limit > 0 else []

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""A small task for the tests: watch the mean of values in [0, 1], alarm, restart, obey commands."""
import random

from sequential_analysis.procedures import MeanEDetector

from sequentialist.core import Handler, Record, Stream

KIND = "test.watch/1"
SPEC = {"m": 0.2, "arl": 200}


class Watch(Handler):
    commands = ("pause",)

    def __init__(self, spec):
        self.spec = spec
        self.alarms, self.seen, self.ticks, self.paused = 0, 0, 0, False

    def streams(self):
        return {"mean": Stream(MeanEDetector(m=self.spec["m"], arl=self.spec["arl"], direction="increase",
                                             statistic="cusum"))}

    def state(self):
        return {"alarms": self.alarms, "seen": self.seen, "ticks": self.ticks, "paused": self.paused}

    def restore(self, state):
        self.alarms, self.seen = state["alarms"], state["seen"]
        self.ticks, self.paused = state["ticks"], state["paused"]

    def on_record(self, record, ctx):
        if self.paused:
            return
        self.seen += 1
        event = ctx.step("mean", {"x": record.data["x"]})
        if event is not None and event.terminal:
            self.alarms += 1
            ctx.record("decision", "alarm", basis={"seen": self.seen}, evidence=["mean"])
            ctx.restart("mean", "alarm")

    def on_time(self, time, ctx):
        self.ticks += 1

    def on_command(self, command, ctx):
        if command.name == "pause":
            self.paused = True
            ctx.record("action", "paused", actor=command.actor, reason=command.reason)
        else:
            super().on_command(command, ctx)


def values(n_before=300, n_after=300, seed=1):
    rnd = random.Random(seed)
    return ([min(1.0, rnd.betavariate(2, 8)) for _ in range(n_before)]
            + [min(1.0, rnd.betavariate(5, 5)) for _ in range(n_after)])


def records(xs, source="sensor", start=0, t0=1_000_000.0):
    return [Record(source, start + i, {"x": x}, t0 + start + i) for i, x in enumerate(xs)]

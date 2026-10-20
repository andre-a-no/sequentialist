# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Replays: recorded data through the same engine and handler as live, in memory, on the data's own clock.

A replay re-analyses history (a finished experiment, last year's sensor
data, a backtest) with exactly the code that runs live.  The engine's
clock is the event time of the data, so two replays of the same records
give the same audit log, hashes included.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence, Union

from .engine import Engine, Handler, Record, Sink, Task
from .storage.memory import MemoryStorage

Input = Union[Record, float]  # a record, or a time to advance the watermark to


def replay(kind: str, spec: dict[str, Any], handler: Handler, inputs: Iterable[Input], *, task_id: str = "replay",
           batch: int = 1000, sinks: Sequence[Sink] = ()) -> Task:
    """Run `inputs` through a new task in memory; the task is returned open (close it when done)."""
    engine = Engine(MemoryStorage(), sinks=sinks, clock="event", checkpoint_every=10**9)
    task = engine.create(task_id, kind, spec, handler)
    pending: list[Record] = []
    for item in inputs:
        if isinstance(item, Record):
            pending.append(item)
            if len(pending) >= batch:
                task.ingest(pending)
                pending = []
        else:
            if pending:
                task.ingest(pending)
                pending = []
            task.advance(item)
    if pending:
        task.ingest(pending)
    return task

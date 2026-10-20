# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""The engine: tasks that take records, feed sequential-analysis runs, decide, and survive crashes.

A *task* (an experiment, a monitor, a trial) is described by a spec and
driven by a `Handler`, the domain logic of a library.  The handler declares
the task's *streams*: each stream is one `Run` of sequential-analysis.  The
engine gives the handler the records that arrive, the passing of time and
the commands of people, in one serial order; the handler feeds its streams
and records decisions in the audit log.

What the engine guarantees, for every storage:

* **Idempotent intake.**  A record is identified by its source and its
  sequence number in that source (an offset, a row id, a counter).  A
  record seen before is dropped, however often it is sent (`Delivery` of
  sequential-analysis keeps the positions; sequence numbers should be dense
  per source).
* **Nothing accepted is lost.**  A batch is written to the journal before
  any of it is processed.  The task is checkpointed every
  `checkpoint_every` records, after every decision and on `close`; opening
  the task restores the checkpoint and replays the journal.  The handler
  must be deterministic (a function of its state and its inputs), and then
  the reopened task is the uninterrupted one, bit for bit: same states,
  same history digests, same decisions.
* **Decisions are recorded once.**  An audit entry is written before the
  checkpoint that follows it, and a replay that reproduces it finds it
  already written (and checks that it is the same decision: a handler that
  is not deterministic is caught here).  Sinks are notified after the
  checkpoint; a crash between the two loses a notification, never an
  entry.
* **One writer per task**, by the storage's lock and by the version check
  on every save.

Time: records may carry an event time.  The *watermark* is the largest
event time seen (or set by `advance`); when it moves, the handler's
`on_time` runs before the record that moved it.  With ``clock="event"``
the engine's clock is the watermark, so a replay of recorded data gives
the same audit log, timestamps included.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time as _time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence, Union

from sequential_analysis import Delivery, Observation, OutputEvent, Procedure, Run
from sequential_analysis.topology import Topology

from .audit import GENESIS, AuditEntry, AuditError, Evidence
from .canonical import digest, is_plain
from .errors import InvalidRecord, StorageError, TaskError
from .storage.base import Storage, check_task_id

logger = logging.getLogger("sequentialist")

STATE_FORMAT = "sequentialist.task/1"
_MAX_EXACT = 2**53

Clock = Union[Callable[[], float], str]
Sink = Callable[[AuditEntry], None]


@dataclass(frozen=True)
class Stream:
    """One run of sequential-analysis inside a task."""

    procedure: Procedure
    topology: Callable[[], Topology] | None = None  # a factory: every segment needs a fresh topology
    seed: int | None = None  # for randomized procedures


@dataclass(frozen=True)
class Record:
    """One arriving fact: its identity (source, seq), its content and its event time."""

    source: str
    seq: int
    data: dict[str, Any]
    time: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {"source": self.source, "seq": self.seq, "data": self.data, "time": self.time}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Record:
        return cls(data["source"], data["seq"], data["data"], data.get("time"))


@dataclass(frozen=True)
class Command:
    """An action of a person on a task: stop an experiment, acknowledge an alarm."""

    name: str
    args: dict[str, Any]
    actor: str
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "args": self.args, "actor": self.actor, "reason": self.reason}


@dataclass
class IngestResult:
    accepted: int = 0
    duplicates: int = 0
    entries: list[AuditEntry] = field(default_factory=list)  # audit entries written by this call


class Handler(ABC):
    """The domain logic of one kind of task.  It must be deterministic: no wall clock, no unseeded randomness."""

    #: The commands `on_command` accepts.  Others are refused before they reach the journal: a command
    #: that fails in the handler would fail again on every replay.
    commands: Sequence[str] = ()

    @abstractmethod
    def streams(self) -> Mapping[str, Stream]:
        """The task's streams by name; built from the spec, the same every time the task is opened."""

    def state(self) -> Any:
        """The handler's own state as plain JSON (stored in every checkpoint)."""
        return None

    def restore(self, state: Any) -> None:  # noqa: B027 - optional
        """Take back what `state` returned."""

    @abstractmethod
    def on_record(self, record: Record, ctx: Context) -> None:
        """Process one new record."""

    def on_time(self, time: float, ctx: Context) -> None:  # noqa: B027 - optional
        """The watermark moved to `time`."""

    def on_command(self, command: Command, ctx: Context) -> None:
        raise TaskError(f"unknown command {command.name!r}")


class Context:
    """What a handler may do while it processes an input."""

    def __init__(self, task: Task):
        self._task = task

    @property
    def now(self) -> float | None:
        """The watermark: the latest event time seen (None before any)."""
        return self._task.watermark

    @property
    def task_id(self) -> str:
        return self._task.task_id

    def run(self, stream: str) -> Run:
        return self._task.run(stream)

    def last_output(self, stream: str) -> Any:
        return self._task.last_output(stream)

    def step(self, stream: str, x: Mapping[str, Any]) -> OutputEvent | None:
        """Give a stream one statistical input (bypassing its topology)."""
        event = self._task.run(stream).step(x)
        if event is not None:
            self._task._outputs[stream] = (True, event.output)
        return event

    def offer(self, stream: str, obs: Observation) -> list[OutputEvent]:
        """Give a stream one observation (through its delivery and topology)."""
        events = self._task.run(stream).offer(obs)
        if events:
            self._task._outputs[stream] = (True, events[-1].output)
        return events

    def evidence(self, stream: str) -> dict[str, Any]:
        return Evidence.of(stream, self._task.run(stream), self._task.last_output(stream)).to_json()

    def record(self, kind: str, decision: str | None = None, *, basis: Mapping[str, Any] | None = None,
               evidence: Sequence[str] = (), actor: str | None = None, reason: str | None = None) -> AuditEntry:
        """Write an audit entry; `evidence` names the streams the entry rests on."""
        return self._task._audit(kind, decision, dict(basis or {}), [self.evidence(s) for s in evidence],
                                 actor, reason)

    def restart(self, stream: str, reason: str) -> None:
        """End the stream's segment and begin a new run of the same procedure (after an alarm, a new window)."""
        self._task._restart(stream, reason)


class Task:
    """An open task: the only writer of its state until `close`."""

    def __init__(self, engine: Engine, task_id: str, kind: str, spec: dict[str, Any], handler: Handler, lock: Any):
        self.engine, self.task_id, self.kind, self.spec = engine, task_id, kind, spec
        self.spec_id = digest(spec)
        self.handler = handler
        self._lock = lock
        self._storage = engine.storage
        self._streams = dict(handler.streams())
        self._runs: dict[str, Run] = {}
        self._segments: dict[str, int] = {}
        self._outputs: dict[str, tuple[bool, Any]] = {}  # (raw?, output): encoded lazily, kept in checkpoints
        self.intake = Delivery("arrival", dedup=True)
        self.watermark: float | None = None
        self.counters = {"records": 0, "duplicates": 0, "ticks": 0, "commands": 0}
        self.created_at: float = 0.0
        self._version = 0
        self._audit_n = 0
        self._audit_last = ""
        self._stored_audit: list[AuditEntry] = []  # entries written after the last checkpoint, met again on replay
        self._since_checkpoint = 0
        self._pending: list[AuditEntry] = []  # written, not yet given to sinks
        self.failed: str | None = None
        self.closed = False
        self._ctx = Context(self)

    # ------------------------------------------------------------------ streams

    def streams(self) -> list[str]:
        return sorted(self._streams)

    def run(self, stream: str) -> Run:
        try:
            return self._runs[stream]
        except KeyError:
            raise TaskError(f"task {self.task_id!r} has no stream {stream!r}") from None

    def segment(self, stream: str) -> int:
        self.run(stream)
        return self._segments[stream]

    def last_output(self, stream: str) -> Any:
        """The stream's latest output as plain JSON (the procedure's encode_output), or None before its first."""
        run = self.run(stream)
        found = self._outputs.get(stream)
        if found is None:
            return None
        raw, output = found
        if raw:
            output = json.loads(json.dumps(run.procedure.encode_output(output)))
            self._outputs[stream] = (False, output)
        return output

    def _run_id(self, stream: str, segment: int) -> str:
        return f"{self.task_id}/{stream}/{segment}"

    def _new_run(self, name: str, segment: int, initialized_from: Any) -> Run:
        s = self._streams[name]
        run = Run(s.procedure, topology=s.topology() if s.topology else None, seed=s.seed,
                  run_id=self._run_id(name, segment), initialized_from=initialized_from, clock=self.engine._now(self))
        run.start(runtime="sequentialist")
        return run

    def _restart(self, name: str, reason: str) -> None:
        old = self.run(name)
        final = old.checkpoint()
        segment = self._segments[name]
        self._storage.put_document(self.task_id, f"segments/{name}/{segment}", final)
        self._audit("segment", None, {"stream": name, "ended": segment, "reason": reason},
                    [Evidence.of(name, old, self.last_output(name)).to_json()], None, None)
        old.close()
        self._segments[name] = segment + 1
        self._runs[name] = self._new_run(name, segment + 1, {"previous": old.run_id, "history_digest":
                                                            old.history_digest, "reason": reason})
        self._outputs.pop(name, None)

    # ------------------------------------------------------------------ audit

    def audit(self) -> list[AuditEntry]:
        return [AuditEntry.from_json(e) for e in self._storage.audit_read(self.task_id)]

    def _audit(self, kind: str, decision: str | None, basis: dict[str, Any], evidence: list[dict[str, Any]],
               actor: str | None, reason: str | None) -> AuditEntry:
        if not is_plain(basis):
            raise TaskError(f"audit basis must be plain JSON: {basis!r}")
        entry = AuditEntry(self._audit_n, self.task_id, self.spec_id, kind, decision, self.engine._time(self),
                           basis, tuple(evidence), actor, reason, prev=self._audit_last or GENESIS).sealed()
        stored = self._stored_audit[0] if self._stored_audit else None
        if stored is not None:  # a replay meets an entry it wrote before the crash
            if not stored.same_event(entry):
                raise AuditError(f"task {self.task_id!r}: replay produced a different audit entry {entry.n} than "
                                 f"the one stored ({stored.kind} {stored.decision} vs {entry.kind} "
                                 f"{entry.decision}); the handler is not deterministic")
            self._stored_audit.pop(0)
            entry = stored
        else:
            self._storage.audit_append(self.task_id, entry.to_json())
        self._audit_n += 1
        self._audit_last = entry.hash
        self._pending.append(entry)
        return entry

    # ------------------------------------------------------------------ inputs

    def ingest(self, records: Iterable[Record]) -> IngestResult:
        """Accept a batch of records: all of it or, if one is invalid, none (InvalidRecord)."""
        self._usable()
        batch = list(records)
        for i, r in enumerate(batch):
            problem = _record_problem(r)
            if problem:
                raise InvalidRecord(f"record {i} of the batch: {problem}; nothing of the batch was accepted")
        result = IngestResult()
        fresh: list[Record] = []
        keys = set()
        for r in batch:
            key = (r.source, r.seq)
            if key in keys or self.intake.delivered(_observation(r)):
                result.duplicates += 1
                continue
            keys.add(key)
            fresh.append(r)
        self.counters["duplicates"] += result.duplicates
        if fresh:
            self._storage.journal_append(self.task_id, [{"record": r.to_json()} for r in fresh])
        return self._process([("record", r) for r in fresh], result)

    def advance(self, time: float) -> IngestResult:
        """Move the watermark to `time` without a record (time passes with no data)."""
        self._usable()
        if type(time) not in (int, float) or not math.isfinite(time):
            raise InvalidRecord(f"time must be a finite number, got {time!r}")
        self._storage.journal_append(self.task_id, [{"tick": time}])
        return self._process([("tick", time)], IngestResult())

    def command(self, name: str, args: Mapping[str, Any] | None = None, *, actor: str, reason: str) -> IngestResult:
        """An action of a person; the handler records it in the audit log with the actor and the reason."""
        self._usable()
        command = Command(name, dict(args or {}), actor, reason)
        if name not in self.handler.commands:
            raise TaskError(f"unknown command {name!r}; this task accepts {sorted(self.handler.commands)}")
        if not (actor and reason and is_plain(command.to_json())):
            raise InvalidRecord("a command needs an actor, a reason and plain JSON arguments")
        # numbered, so that a replay never applies a command twice (records are deduplicated by their seq)
        self._storage.journal_append(self.task_id, [{"command": command.to_json(), "n": self.counters["commands"]}])
        return self._process([("command", command)], IngestResult())

    def _process(self, inputs: list[tuple[str, Any]], result: IngestResult) -> IngestResult:
        start = len(self._pending)
        try:
            for kind, item in inputs:
                self._apply(kind, item)
        except BaseException as error:
            self.failed = f"{type(error).__name__}: {error}"
            logger.error("task %s failed: %s", self.task_id, self.failed)
            raise
        result.accepted = sum(1 for kind, _ in inputs if kind == "record")
        result.entries = self._pending[start:]
        if len(self._pending) > start or self._since_checkpoint >= self.engine.checkpoint_every:
            self.checkpoint()
        return result

    def _apply(self, kind: str, item: Any) -> None:
        if kind == "record":
            record: Record = item
            self.intake.push(_observation(record))
            self.counters["records"] += 1
            self._since_checkpoint += 1
            if record.time is not None:
                self._move_watermark(record.time)
            self.handler.on_record(record, self._ctx)
        elif kind == "tick":
            self.counters["ticks"] += 1
            self._move_watermark(item)
        else:
            self.counters["commands"] += 1
            self.handler.on_command(item, self._ctx)

    def _move_watermark(self, time: float) -> None:
        if self.watermark is None or time > self.watermark:
            self.watermark = float(time)
            self.handler.on_time(self.watermark, self._ctx)

    def _usable(self) -> None:
        if self.closed:
            raise TaskError(f"task {self.task_id!r} is closed")
        if self.failed is not None:
            raise TaskError(f"task {self.task_id!r} has failed ({self.failed}); reopen it to replay, or inspect it")

    # ------------------------------------------------------------------ persistence

    def _state(self) -> dict[str, Any]:
        return {
            "format": STATE_FORMAT,
            "task_id": self.task_id,
            "kind": self.kind,
            "spec_id": self.spec_id,
            "spec": self.spec,
            "created_at": self.created_at,
            "intake": {"spec": self.intake.spec(), "state": self.intake.state()},
            "watermark": self.watermark,
            "counters": dict(self.counters),
            "streams": {name: {"segment": self._segments[name], "checkpoint": run.checkpoint()}
                        for name, run in sorted(self._runs.items())},
            "outputs": {name: self.last_output(name) for name in sorted(self._outputs)},
            "handler": self.handler.state(),
            "audit": {"n": self._audit_n, "last": self._audit_last},
        }

    def checkpoint(self) -> None:
        """Save the whole task and empty the journal; then hand new audit entries to the sinks."""
        if self.failed is not None:
            raise TaskError(f"task {self.task_id!r} has failed; a failed task is not checkpointed")
        state = self._state()
        if not is_plain(state):
            raise TaskError(f"task {self.task_id!r}: the handler's state is not plain JSON")
        self._version = self._storage.save(self.task_id, state, expected=self._version, clear_journal=True)
        self._since_checkpoint = 0
        pending, self._pending = self._pending, []
        self.engine._notify(pending)

    def close(self) -> None:
        if self.closed:
            return
        try:
            if self.failed is None:
                self.checkpoint()
        finally:
            self.closed = True
            self._lock.release()

    def __enter__(self) -> Task:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _observation(record: Record) -> Observation:
    return Observation("record", None, source=record.source, seq=record.seq)


def _record_problem(r: Any) -> str | None:
    if not isinstance(r, Record):
        return f"expected a Record, got {type(r).__name__}"
    if not isinstance(r.source, str) or not r.source or len(r.source) > 256 or not is_plain(r.source):
        return f"source {r.source!r} must be a non-empty string of at most 256 characters"
    if type(r.seq) is not int or not 0 <= r.seq <= _MAX_EXACT:
        return f"seq {r.seq!r} must be an integer from 0 to 2**53"
    if r.time is not None and (type(r.time) not in (int, float) or not math.isfinite(r.time)):
        return f"time {r.time!r} must be a finite number of seconds, or None"
    if not isinstance(r.data, dict) or not is_plain(r.data):
        return "data must be a JSON object of plain values (finite numbers, text, lists, objects)"
    return None


def shard_of(task_id: str, shards: int) -> int:
    """The shard that owns a task: a stable hash of its id, so every process agrees."""
    if type(shards) is not int or shards < 1:
        raise ValueError("shards must be a positive int")
    return int(hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16], 16) % shards


class Engine:
    """Opens tasks in a storage.  One engine per process and shard; a task is open in one engine at a time."""

    def __init__(self, storage: Storage, *, shard: tuple[int, int] = (0, 1), sinks: Sequence[Sink] = (),
                 checkpoint_every: int = 1000, clock: Clock = _time.time):
        index, count = shard
        if type(count) is not int or count < 1 or type(index) is not int or not 0 <= index < count:
            raise ValueError("shard must be (index, count) with 0 <= index < count")
        if type(checkpoint_every) is not int or checkpoint_every < 1:
            raise ValueError("checkpoint_every must be a positive int")
        if not (callable(clock) or clock == "event"):
            raise ValueError("clock must be a function returning seconds, or 'event'")
        self.storage, self.shard, self.sinks = storage, (index, count), list(sinks)
        self.checkpoint_every, self.clock = checkpoint_every, clock

    # the clock of the audit log and of the runs' provenance
    def _time(self, task: Task) -> float:
        if self.clock == "event":
            return task.watermark if task.watermark is not None else 0.0
        return float(self.clock())  # type: ignore[operator]

    def _now(self, task: Task) -> Callable[[], float]:
        return lambda: self._time(task)

    def _notify(self, entries: Sequence[AuditEntry]) -> None:
        for entry in entries:
            for sink in self.sinks:
                try:
                    sink(entry)
                except Exception:
                    logger.exception("sink %s failed on audit entry %d of task %s", type(sink).__name__, entry.n,
                                     entry.task_id)

    def _check_shard(self, task_id: str) -> None:
        check_task_id(task_id)
        if shard_of(task_id, self.shard[1]) != self.shard[0]:
            raise TaskError(f"task {task_id!r} belongs to shard {shard_of(task_id, self.shard[1])} of "
                            f"{self.shard[1]}, this engine serves shard {self.shard[0]}")

    def exists(self, task_id: str) -> bool:
        return self.storage.load(check_task_id(task_id)) is not None

    def stored(self, task_id: str) -> dict[str, Any]:
        """The kind and spec of a stored task, to build its handler before `open`."""
        loaded = self.storage.load(check_task_id(task_id))
        if loaded is None:
            raise StorageError(f"no task {task_id!r}")
        state = loaded[1]
        return {"kind": state["kind"], "spec": state["spec"], "spec_id": state["spec_id"]}

    def create(self, task_id: str, kind: str, spec: dict[str, Any], handler: Handler) -> Task:
        """A new task; TaskError if the id is taken."""
        self._check_shard(task_id)
        if not is_plain(spec):
            raise TaskError("the spec must be plain JSON")
        lock = self.storage.lock(task_id)
        try:
            if self.storage.load(task_id) is not None:
                raise TaskError(f"task {task_id!r} exists already; open it")
            task = Task(self, task_id, kind, spec, handler, lock)
            # a create that crashed before its first checkpoint may have left its "created" entry
            task._stored_audit = [AuditEntry.from_json(e) for e in self.storage.audit_read(task_id)]
            task.created_at = self._time(task)
            for name in sorted(task._streams):
                task._segments[name] = 0
                task._runs[name] = task._new_run(name, 0, "default")
            task._audit("created", None, {"kind": kind, "spec_id": task.spec_id}, [], None, None)
            if task._stored_audit:
                raise AuditError(f"task {task_id!r} has no state but an audit log; it is not a new task")
            task.checkpoint()
            return task
        except BaseException:
            lock.release()
            raise

    def open(self, task_id: str, factory: Callable[[dict[str, Any]], Handler]) -> Task:
        """Reopen a stored task: restore its checkpoint, replay its journal.  `factory(spec)` builds the handler."""
        self._check_shard(task_id)
        lock = self.storage.lock(task_id)
        try:
            loaded = self.storage.load(task_id)
            if loaded is None:
                raise StorageError(f"no task {task_id!r}")
            version, state = loaded
            if state.get("format") != STATE_FORMAT or state.get("task_id") != task_id:
                raise StorageError(f"task {task_id!r}: stored state has format {state.get('format')!r}")
            task = Task(self, task_id, state["kind"], state["spec"], factory(state["spec"]), lock)
            if task.spec_id != state["spec_id"]:
                raise StorageError(f"task {task_id!r}: the stored spec does not match its id")
            task._version = version
            task.created_at = state["created_at"]
            stored_streams = state["streams"]
            if sorted(stored_streams) != sorted(task._streams):
                raise TaskError(f"task {task_id!r}: the handler declares streams {sorted(task._streams)}, "
                                f"the stored task has {sorted(stored_streams)}")
            for name, saved in sorted(stored_streams.items()):
                s = task._streams[name]
                run = Run.restore(s.procedure, saved["checkpoint"], topology=s.topology() if s.topology else None,
                                  clock=self._now(task))
                run.start(runtime="sequentialist")
                task._runs[name], task._segments[name] = run, saved["segment"]
            task.intake.restore(state["intake"]["state"])
            task.watermark = state["watermark"]
            task.counters = dict(state["counters"])
            task._outputs = {name: (False, out) for name, out in state["outputs"].items()}
            task.handler.restore(state["handler"])
            task._audit_n, task._audit_last = state["audit"]["n"], state["audit"]["last"]
            stored_audit = self.storage.audit_read(task_id)
            task._stored_audit = [AuditEntry.from_json(e) for e in stored_audit[task._audit_n:]]
            journal = self.storage.journal_read(task_id)
            inputs: list[tuple[str, Any]] = []
            for entry in journal:
                if "record" in entry:
                    record = Record.from_json(entry["record"])
                    if not task.intake.delivered(_observation(record)):
                        inputs.append(("record", record))
                elif "tick" in entry:
                    inputs.append(("tick", entry["tick"]))
                elif entry["n"] >= task.counters["commands"]:
                    c = entry["command"]
                    inputs.append(("command", Command(c["name"], c["args"], c["actor"], c["reason"])))
            if inputs:
                logger.info("task %s: replaying %d journal entries", task_id, len(inputs))
            task._process(inputs, IngestResult())
            if task._stored_audit:
                raise AuditError(f"task {task_id!r}: {len(task._stored_audit)} audit entries were written after the "
                                 f"checkpoint and not reproduced by the replay")
            task.checkpoint()
            return task
        except BaseException:
            lock.release()
            raise

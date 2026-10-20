# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""An experiment as a task of the sequentialist engine: events in, decisions out.

Records (`exposure`, `event`) carry an event time and a unit pseudonym.

1. **Exposure.**  The first exposure of a unit fixes its variant and opens
   its attribution window [t, t + window).  It is counted at once in the
   sample ratio mismatch test.  Later exposures of the same unit change
   nothing (a different variant is counted as a conflict).  Units with a
   manual override (QA), exposures before the start, after the end of the
   experiment or after its decision are counted and left out.
2. **Events** of a unit inside its window add to its metrics.  Events
   outside any window are counted, not used.
3. **Closing.**  When the watermark (the latest event time, less the
   allowed `lateness`) passes the end of a unit's window, the unit's
   totals are fixed and given, once, to every analysis stream.
4. **Decisions**, after every closing:

   * stop, if the sample ratio mismatch test rejects (the data cannot be trusted);
   * a treatment is dropped if a guardrail's confidence sequence shows harm
     beyond its margin, if the primary metric is established to be worse,
     or if the primary metric's confidence sequence excludes the MDE
     (futility; non-binding, it does not affect the error rates);
   * ship a treatment once its primary metric shows an effect in the good
     direction (with the multiplicity correction) and every guardrail is
     shown non-inferior (its confidence sequence excludes harm beyond the
     margin); if several qualify, the one with the best estimate;
   * stop when no treatment is left, or when the maximum duration has
     passed and every window has closed;
   * otherwise continue.

   Each decision is written to the audit log with the streams it rests on.
"""
from __future__ import annotations

import hashlib
import heapq
import math
from typing import Any, Callable, Sequence

from sequentialist.core import Context, Engine, Handler, Record, Report, Stream, Task, parse_spec

from .analysis import ARM, Analysis, Estimate, Pair, denominator_input, exposure_input
from .assign import config
from .spec import KIND, ExperimentSpec, Metric


def exposure(unit: str, variant: str, time: float, *, source: str, seq: int,
             probability: float | None = None) -> Record:
    """A record of the unit's exposure to a variant (`probability`: of that assignment, as the platform knows it)."""
    data: dict[str, Any] = {"type": "exposure", "unit": unit, "variant": variant}
    if probability is not None:
        data["probability"] = probability
    return Record(source, seq, data, time)


def event(unit: str, name: str, time: float, *, source: str, seq: int, **values: Any) -> Record:
    """A record of an event of the unit, with optional numeric fields (``amount=12.5``)."""
    return Record(source, seq, {"type": "event", "unit": unit, "event": name, **values}, time)


def _key(unit: str) -> str:
    """The unit as stored: 16 hex digits of its SHA-256 (a collision among 10**6 units: about 3e-8)."""
    return hashlib.sha256(unit.encode("utf-8")).hexdigest()[:16]


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


COUNTS = ("exposures", "repeat_exposures", "conflicting_exposures", "unknown_variant", "qa_exposures",
          "before_start", "after_end", "after_decision", "events", "events_outside_window",
          "events_of_unknown_units", "bad_values", "unknown_records", "records_without_time",
          "assignment_mismatch", "probability_mismatch")


class ExperimentHandler(Handler):
    commands = ("stop",)

    def __init__(self, spec: ExperimentSpec):
        self.spec = spec
        self.analysis = Analysis(spec)
        d = spec.data
        self.window = float(d["attribution_window"])
        self.lateness = float(d["lateness"])
        self.metrics: list[Metric] = spec.metrics()
        self.variants = spec.variant_names
        self.index = {name: i for i, name in enumerate(self.variants)}
        self.overrides = set(d["overrides"])
        self._inputs = {name: tuple(s.procedure.inputs()) for name, s in self.analysis.streams.items()}
        # event name -> [(metric index, slot, field or None to count, conversion?)]
        self._events: dict[str, list[tuple[int, int, str | None, bool]]] = {}
        for i, m in enumerate(self.metrics):
            raw = m.raw
            if m.type == "conversion":
                self._on(raw["event"], (i, 0, None, True))
            elif m.type == "bounded":
                self._on(raw["event"], (i, 0, raw["value"], False))
            elif m.type == "ratio":
                self._on(raw["numerator"]["event"], (i, 0, raw["numerator"]["value"], False))
                self._on(raw["denominator"]["event"], (i, 1, raw["denominator"]["value"], False))
            else:
                self._on(raw["event"], (i, 0, None, False))
                self._on(raw["exposure"]["event"], (i, 1, raw["exposure"]["value"], False))
        self.status = "waiting"
        self.start: float | None = d["start"]
        self.decision: dict[str, Any] | None = None
        self.variant_status = {self.variants[t]: "running" for t in spec.treatments}
        self.significant: dict[str, int] = {}  # pair name -> sign of the effect when its test first rejected
        self.seen: dict[str, int] = {}
        self.open: dict[str, list[Any]] = {}  # key -> [variant, t0, [[a, b] per metric]]
        self.heap: list[list[Any]] = []  # [end of window, key]
        self.counts = {name: 0 for name in COUNTS}
        self.units = [0] * len(self.variants)  # closed units given to the analysis
        self.clipped = {m.name: 0 for m in self.metrics if m.type == "bounded"}
        self.no_exposure = {m.name: 0 for m in self.metrics if m.type == "rate"}
        self._config: tuple[float | None, Any] | None = None  # the assignment, for the current start

    def _on(self, name: str, target: tuple[int, int, str | None, bool]) -> None:
        self._events.setdefault(name, []).append(target)

    # ------------------------------------------------------------ handler protocol

    def streams(self) -> dict[str, Stream]:
        return dict(self.analysis.streams)

    def state(self) -> dict[str, Any]:
        return {"status": self.status, "start": self.start, "decision": self.decision,
                "variant_status": self.variant_status, "significant": self.significant, "seen": self.seen,
                "open": self.open, "heap": self.heap, "counts": self.counts, "units": self.units,
                "clipped": self.clipped, "no_exposure": self.no_exposure}

    def restore(self, state: dict[str, Any]) -> None:
        self.status, self.start, self.decision = state["status"], state["start"], state["decision"]
        self.variant_status, self.significant = state["variant_status"], state["significant"]
        self.seen, self.open, self.heap = state["seen"], state["open"], state["heap"]
        self.counts, self.units = state["counts"], state["units"]
        self.clipped, self.no_exposure = state["clipped"], state["no_exposure"]

    @property
    def end(self) -> float | None:
        return None if self.start is None else self.start + float(self.spec.data["max_duration"])

    def on_record(self, record: Record, ctx: Context) -> None:
        data = record.data
        if record.time is None:
            self.counts["records_without_time"] += 1
            return
        unit = data.get("unit")
        if not isinstance(unit, str) or not unit:
            self.counts["unknown_records"] += 1
            return
        if data.get("type") == "exposure":
            self._exposure(unit, data, float(record.time), ctx)
        elif data.get("type") == "event" and isinstance(data.get("event"), str):
            self._event(unit, data, float(record.time))
        else:
            self.counts["unknown_records"] += 1

    def on_time(self, time: float, ctx: Context) -> None:
        horizon = time - self.lateness
        closed = False
        while self.heap and self.heap[0][0] <= horizon:
            _, key = heapq.heappop(self.heap)
            self._close(key, ctx)
            closed = True
        if closed:
            self._evaluate(ctx)
        end = self.end
        if self.status == "running" and end is not None and horizon >= end + self.window:
            self._decide(ctx, "stop", None, "duration_limit", {}, self._primary_streams())

    def on_command(self, command: Any, ctx: Context) -> None:
        ctx.record("action", "stop", basis={}, actor=command.actor, reason=command.reason)
        if self.status != "decided":
            self._decide(ctx, "stop", None, "manual", {"actor": command.actor}, [])

    # ------------------------------------------------------------ intake

    def _exposure(self, unit: str, data: dict[str, Any], time: float, ctx: Context) -> None:
        variant = data.get("variant")
        if unit in self.overrides:
            self.counts["qa_exposures"] += 1
            return
        if self.status == "decided":
            self.counts["after_decision"] += 1
            return
        if not isinstance(variant, str) or variant not in self.index:
            self.counts["unknown_variant"] += 1
            return
        if self.start is not None and time < self.start:
            self.counts["before_start"] += 1
            return
        key = _key(unit)
        if key in self.seen:
            self.counts["repeat_exposures" if self.seen[key] == self.index[variant] else "conflicting_exposures"] += 1
            return
        if self.start is None:
            self.start = time
        end = self.end
        if end is not None and time >= end:
            self.counts["after_end"] += 1
            return
        if self.status == "waiting":
            self.status = "running"
        arm = self.index[variant]
        if self.spec.data["assignment"] == "engine":
            if self._config is None or self._config[0] != self.start:
                self._config = (self.start, config(self.spec, self.start))
            expected = self._config[1].assign(unit, time)
            if expected.variant != variant:
                self.counts["assignment_mismatch"] += 1
            given: Any = data.get("probability")
            if _number(given) and abs(float(given) - expected.probability) > 1e-9:
                self.counts["probability_mismatch"] += 1
        self.seen[key] = arm
        self.open[key] = [arm, time, [[0.0, 0.0] for _ in self.metrics]]
        heapq.heappush(self.heap, [time + self.window, key])
        self.counts["exposures"] += 1
        ctx.step("srm", {ARM: arm})
        srm = ctx.last_output("srm")
        if srm is not None and srm["decision"] == "reject_h0" and self.status == "running":
            self._decide(ctx, "stop", None, "sample_ratio_mismatch", {"counts": srm.get("counts")}, ["srm"])

    def _event(self, unit: str, data: dict[str, Any], time: float) -> None:
        self.counts["events"] += 1
        key = _key(unit)
        opened = self.open.get(key)
        if opened is None:
            # a unit never counted: exposed later, or its exposure was left out (QA, after the decision, ...)
            self.counts["events_outside_window" if key in self.seen else "events_of_unknown_units"] += 1
            return
        if not opened[1] <= time < opened[1] + self.window:
            self.counts["events_outside_window"] += 1
            return
        for metric, slot, field, conversion in self._events.get(data["event"], ()):
            if conversion:
                opened[2][metric][0] = 1.0
            elif field is None:
                opened[2][metric][slot] += 1.0
            elif _number(data.get(field)):
                opened[2][metric][slot] += float(data[field])
            else:
                self.counts["bad_values"] += 1

    def _close(self, key: str, ctx: Context) -> None:
        arm, _, accs = self.open.pop(key)
        x: dict[str, Any] = {ARM: arm}
        skip: set[str] = set()
        for m, (a, b) in zip(self.metrics, accs):
            if m.type == "conversion":
                x[m.name] = int(a > 0)
            elif m.type == "bounded":
                lo, hi = float(m.raw["bounds"][0]), float(m.raw["bounds"][1])
                clipped = min(max(a, lo), hi)
                if clipped != a:
                    self.clipped[m.name] += 1
                x[m.name] = clipped
            elif m.type == "ratio":
                x[m.name], x[denominator_input(m)] = a, b
            else:
                x[m.name], x[exposure_input(m)] = int(a), b
                if b <= 0:
                    self.no_exposure[m.name] += 1
                    skip.add(exposure_input(m))
        self.units[arm] += 1
        for name, inputs in self._inputs.items():
            if name == "srm" or any(k in skip for k in inputs):
                continue
            ctx.step(name, {k: x[k] for k in inputs})

    # ------------------------------------------------------------ decisions

    def _primary_streams(self) -> list[str]:
        out: list[str] = []
        for p in self.analysis.primary_pairs():
            for name in (p.decision_stream, p.interval_stream):
                if name is not None and name not in out:
                    out.append(name)
        return out

    def _decide(self, ctx: Context, decision: str, variant: str | None, reason: str, basis: dict[str, Any],
                evidence: Sequence[str]) -> None:
        self.status = "decided"
        self.decision = {"decision": decision, "variant": variant, "reason": reason, "at": ctx.now}
        ctx.record("decision", decision, basis={"variant": variant, "reason": reason, **basis}, evidence=evidence)

    def _drop(self, ctx: Context, pair: Pair, reason: str, est: Estimate, streams: Sequence[str]) -> None:
        self.variant_status[pair.variant_name] = reason
        ctx.record("decision", "drop_variant", evidence=streams, basis={
            "variant": pair.variant_name, "reason": reason, "metric": pair.metric.name,
            "estimate": est.estimate, "interval": [est.lower, est.upper], "level": pair.interval_level})

    def _evaluate(self, ctx: Context) -> None:
        if self.status != "running":
            return
        last: Callable[[str], Any] = ctx.last_output
        a = self.analysis
        for pair in a.pairs:  # first rejections, with the sign of the effect at that moment
            if pair.name in self.significant or pair.decision_stream is None:
                continue
            verdict = a.rejected(pair, last)
            if verdict is True:
                if pair.decision_kind == "planned":  # one-sided, in the good direction
                    self.significant[pair.name] = 1
                    continue
                est = a.estimate(pair, last(pair.interval_stream))
                if est.estimate:  # the sign of a two-sided rejection is read off the estimate
                    self.significant[pair.name] = (1 if est.estimate > 0 else -1) * pair.metric.sign
            elif verdict is False:
                self.significant[pair.name] = 0  # a planned test accepted: no effect shown
        candidates: list[tuple[float, Pair, Estimate]] = []
        for primary in a.primary_pairs():
            name = primary.variant_name
            if self.variant_status[name] != "running":
                continue
            est = a.estimate(primary, last(primary.interval_stream))
            dropped = False
            for guard in a.guardrail_pairs(primary.variant):
                g = a.estimate(guard, last(guard.interval_stream))
                if _beyond_margin(guard.metric, g):
                    self._drop(ctx, guard, "harm", g, [guard.interval_stream])
                    dropped = True
                    break
            if dropped:
                continue
            outcome = self.significant.get(primary.name)
            streams = [s for s in (primary.decision_stream, primary.interval_stream) if s]
            if outcome == -1:
                self._drop(ctx, primary, "worse", est, streams)
            elif outcome == 0:
                self._drop(ctx, primary, "no_effect", est, streams)
            elif outcome == 1:
                if all(_non_inferior(g.metric, a.estimate(g, last(g.interval_stream)))
                       for g in a.guardrail_pairs(primary.variant)):
                    candidates.append((primary.metric.sign * (est.estimate or 0.0), primary, est))
            elif _futile(primary.metric, est):
                self._drop(ctx, primary, "futility", est, streams)
        if candidates:
            _, best, est = max(candidates, key=lambda c: (c[0], -c[1].variant))
            guards = [g.interval_stream for g in a.guardrail_pairs(best.variant)]
            streams = [s for s in (best.decision_stream, best.interval_stream) if s] + guards
            self._decide(ctx, "ship", best.variant_name, "primary_success", {
                "metric": best.metric.name, "estimate": est.estimate, "interval": [est.lower, est.upper],
                "guarantee": best.decision_guarantee, "multiplicity": a.multiplicity}, streams)
        elif all(s != "running" for s in self.variant_status.values()):
            self._decide(ctx, "stop", None, "no_variant_left", {"variants": dict(self.variant_status)}, [])


def _beyond_margin(m: Metric, e: Estimate) -> bool:
    """Harm larger than the margin is established: the whole interval lies beyond it on the harmful side."""
    margin = float(m.margin or 0.0)
    if m.sign > 0:
        return e.upper is not None and e.upper < -margin
    return e.lower is not None and e.lower > margin


def _non_inferior(m: Metric, e: Estimate) -> bool:
    """Harm larger than the margin is excluded."""
    margin = float(m.margin or 0.0)
    if m.sign > 0:
        return e.lower is not None and e.lower > -margin
    return e.upper is not None and e.upper < margin


def _futile(m: Metric, e: Estimate) -> bool:
    """An effect as large as the MDE in the good direction is excluded."""
    mde = float(m.mde or 0.0)
    if m.sign > 0:
        return e.upper is not None and e.upper < mde
    return e.lower is not None and e.lower > -mde


def _factory(spec_json: dict[str, Any]) -> ExperimentHandler:
    return ExperimentHandler(parse_spec(spec_json, ExperimentSpec))


class Experiment:
    """An open experiment."""

    def __init__(self, task: Task):
        self.task = task
        handler = task.handler
        assert isinstance(handler, ExperimentHandler)
        self.handler = handler
        self.spec = handler.spec

    @classmethod
    def create(cls, engine: Engine, spec: ExperimentSpec) -> Experiment:
        return cls(engine.create(spec.experiment_id, KIND, spec.to_json(), ExperimentHandler(spec)))

    @classmethod
    def open(cls, engine: Engine, experiment_id: str) -> Experiment:
        return cls(engine.open(experiment_id, _factory))

    def ingest(self, records: Sequence[Record]) -> Any:
        return self.task.ingest(records)

    def advance(self, time: float) -> Any:
        return self.task.advance(time)

    def stop(self, *, actor: str, reason: str) -> Any:
        """Stop by hand; the actor and the reason go to the audit log."""
        return self.task.command("stop", actor=actor, reason=reason)

    @property
    def decision(self) -> dict[str, Any] | None:
        return self.handler.decision

    def status(self) -> dict[str, Any]:
        h = self.handler
        return {"id": self.spec.experiment_id, "status": h.status, "decision": h.decision, "start": h.start,
                "end": h.end, "watermark": self.task.watermark, "variants": dict(h.variant_status),
                "units": dict(zip(h.variants, h.units)), "open_units": len(h.open), "counts": dict(h.counts),
                "clipped": dict(h.clipped)}

    def results(self) -> list[dict[str, Any]]:
        """One row per metric and treatment: the effect, its interval and what is established, with guarantees."""
        h, a, last = self.handler, self.handler.analysis, self.task.last_output
        rows = []
        for pair in a.pairs:
            est = a.estimate(pair, last(pair.interval_stream))
            outcome = h.significant.get(pair.name)
            if pair.metric.role == "guardrail":
                verdict = ("harm beyond margin" if _beyond_margin(pair.metric, est) else
                           "non-inferior" if _non_inferior(pair.metric, est) else "inconclusive")
            else:
                verdict = {1: "better", -1: "worse", 0: "no effect shown"}.get(outcome or 0, "inconclusive") \
                    if outcome is not None else "inconclusive"
            run = self.task.run(pair.interval_stream)
            rows.append({
                "metric": pair.metric.name, "role": pair.metric.role, "type": pair.metric.type,
                "variant": pair.variant_name, "control": h.variants[self.spec.control],
                "scale": pair.metric.scale, "estimate": est.estimate, "lower": est.lower, "upper": est.upper,
                "level": pair.interval_level, "treatment_value": est.treatment, "control_value": est.control,
                "units": est.units, "log_e_value": a.e_value(pair, last), "p_value": a.p_value(pair, last),
                "verdict": verdict,
                "decision_procedure": None if pair.decision_stream is None else
                self.task.run(pair.decision_stream).procedure.name,
                "decision_guarantee": pair.decision_guarantee if pair.decision_stream else None,
                "interval_procedure": run.procedure.name, "interval_guarantee": pair.interval_guarantee,
                "multiplicity": a.multiplicity if pair.metric.role != "guardrail" else "Bonferroni",
                "history_digest": run.history_digest,
            })
        return rows

    def report(self) -> Report:
        from .report import build_report

        return build_report(self)

    def close(self) -> None:
        self.task.close()

    def __enter__(self) -> Experiment:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

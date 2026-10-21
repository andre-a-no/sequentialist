# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""A scheme of inspection as a task of the sequentialist engine: lots in, decisions out.

Records:

    {"type": "lot", "lot": "L-117", "size": 5000}      a lot is presented for inspection
    {"type": "item", "lot": "L-117", "value": 0}       one inspected item: 1/0 defective (attributes),
                                                       a count (nonconformities), a measurement (variables)

One lot is inspected at a time.  Its items go, in order, to the plan of the
current severity (normal, tightened or reduced), each plan a stream that
executes `PlanTest`; when the plan decides, the lot is accepted or rejected
and the decision is written to the audit log with the plan, its
operating characteristic and the stream's history digest.  Each lot's
inspection is one segment of its plan's stream.

Commands (people's decisions, with their name and reason in the audit log):

    dispose  {"lot": ..., "disposition": "scrap" | "screen" | "rework" | "return" | "concession"}
             what is done with a rejected lot; "rework" allows it to be presented again
             (inspected with the resubmission plan); "concession" accepts it by decision
             of an authority, against the plan
    resume   {}  restart acceptance after it was discontinued (on tightened inspection)

Switching (counted on first presentations only):

* normal -> tightened when `rejected` of the last `of` lots were rejected;
* tightened -> normal after `to_normal` consecutive accepted lots;
* normal -> reduced after `to_reduced` consecutive accepted lots, if set;
* reduced -> normal on the first rejected lot;
* discontinued when `discontinue` lots are rejected during one period of
  tightened inspection: lots presented then are refused until `resume`.

The rules are configurable; their defaults follow common practice in
acceptance sampling by attributes.  With `supplier_watch`, every item of a
first presentation also goes to a CUSUM from aql to lq of the normal plan,
which raises an alarm when the supplier's quality deteriorates lastingly,
across lots, and then starts again.
"""
from __future__ import annotations

from typing import Any, Sequence

from sequential_analysis.procedures import CUSUM

from sequentialist.core import Command, Context, Engine, Handler, Record, Stream, Task, parse_spec

from .plans import INPUT, compile_plan, family, parameter
from .spec import DISPOSITIONS, SCHEME_KIND, SEVERITIES, PlanSpec, SchemeSpec

COUNTS = ("lots", "items", "items_outside_inspection", "lot_while_inspecting", "lot_already_decided",
          "lot_refused_discontinued", "resubmission_not_allowed", "bad_records", "invalid_items")


def lot(lot_id: str, time: float | None, *, size: int | None = None, source: str, seq: int,
        **fields: Any) -> Record:
    """A record presenting a lot (``size``: its number of items, if known)."""
    data: dict[str, Any] = {"type": "lot", "lot": lot_id, **fields}
    if size is not None:
        data["size"] = size
    return Record(source, seq, data, time)


def item(lot_id: str, value: Any, time: float | None, *, source: str, seq: int, **fields: Any) -> Record:
    """A record of one inspected item of a lot."""
    return Record(source, seq, {"type": "item", "lot": lot_id, "value": value, **fields}, time)


class SchemeHandler(Handler):
    commands = ("dispose", "resume")

    def __init__(self, spec: SchemeSpec):
        self.spec = spec
        d = spec.data
        self.plans: dict[str, PlanSpec] = {}
        for severity in SEVERITIES:
            plan = spec.plan(severity)
            if plan is not None:
                self.plans[severity] = plan
        self.severity: str = d["start"]
        self.discontinued = False
        self.active: dict[str, Any] | None = None  # the lot under inspection
        self.lots: dict[str, dict[str, Any]] = {}  # lot id -> its history
        self.recent: list[str] = []  # outcomes of first presentations: "accepted" / "rejected", newest last
        self.tightened_rejections = 0
        self.counts = {name: 0 for name in COUNTS}
        self.watch = d["supplier_watch"] is not None

    # ------------------------------------------------------------ handler protocol

    def streams(self) -> dict[str, Stream]:
        out = {f"lot:{s}": Stream(compile_plan(p)) for s, p in self.plans.items()}
        watch = self.spec.data["supplier_watch"]
        if watch is not None:
            normal = self.plans["normal"]
            out["supplier"] = Stream(CUSUM(family(normal), parameter(normal, normal.data["aql"]),
                                           parameter(normal, normal.data["lq"]), float(watch["threshold"]),
                                           input=INPUT))
        return out

    def state(self) -> dict[str, Any]:
        return {"severity": self.severity, "discontinued": self.discontinued, "active": self.active,
                "lots": self.lots, "recent": self.recent, "tightened_rejections": self.tightened_rejections,
                "counts": self.counts}

    def restore(self, state: dict[str, Any]) -> None:
        self.severity, self.discontinued, self.active = state["severity"], state["discontinued"], state["active"]
        self.lots, self.recent = state["lots"], state["recent"]
        self.tightened_rejections, self.counts = state["tightened_rejections"], state["counts"]

    def on_record(self, record: Record, ctx: Context) -> None:
        data = record.data
        lot_id = data.get("lot")
        if not isinstance(lot_id, str) or not lot_id:
            self.counts["bad_records"] += 1
            return
        if data.get("type") == "lot":
            self._present(lot_id, data, record.time, ctx)
        elif data.get("type") == "item":
            self._item(lot_id, data.get("value"), ctx)
        else:
            self.counts["bad_records"] += 1

    def on_command(self, command: Command, ctx: Context) -> None:
        if command.name == "resume":
            ctx.record("action", "resume", basis={"severity": "tightened"}, actor=command.actor,
                       reason=command.reason)
            self.discontinued = False
            self.tightened_rejections = 0
            self.severity = "tightened" if "tightened" in self.plans else "normal"
            return
        lot_id, disposition = command.args.get("lot"), command.args.get("disposition")
        history = self.lots.get(lot_id) if isinstance(lot_id, str) else None
        if history is None or disposition not in DISPOSITIONS:
            ctx.record("action", "dispose_refused", basis={"lot": lot_id, "disposition": disposition,
                                                          "why": "unknown lot or disposition"},
                       actor=command.actor, reason=command.reason)
            return
        if history["status"] != "rejected":
            ctx.record("action", "dispose_refused", basis={"lot": lot_id, "disposition": disposition,
                                                          "why": f"the lot is {history['status']}"},
                       actor=command.actor, reason=command.reason)
            return
        history["disposition"] = disposition
        if disposition == "rework" and len(history["presentations"]) <= self.spec.data["resubmission"]["max"]:
            history["status"] = "awaiting_resubmission"
        elif disposition == "concession":
            history["status"] = "accepted_by_concession"
        else:
            history["status"] = "disposed"
        ctx.record("action", "dispose", basis={"lot": lot_id, "disposition": disposition,
                                               "status": history["status"]},
                   actor=command.actor, reason=command.reason)

    # ------------------------------------------------------------ lots

    def _present(self, lot_id: str, data: dict[str, Any], time: float | None, ctx: Context) -> None:
        if self.active is not None:
            self.counts["lot_while_inspecting"] += 1
            return
        history = self.lots.get(lot_id)
        if history is not None and history["status"] != "awaiting_resubmission":
            self.counts["resubmission_not_allowed" if history["status"] == "rejected" else
                        "lot_already_decided"] += 1
            return
        first = history is None
        if first and self.discontinued:
            self.counts["lot_refused_discontinued"] += 1
            ctx.record("decision", "refuse_lot", basis={"lot": lot_id, "why": "acceptance is discontinued"})
            return
        severity = self.severity if first else self.spec.data["resubmission"]["plan"]
        if history is None:
            size = data.get("size")
            history = {"lot": lot_id, "size": size if type(size) is int and size > 0 else None, "status": "inspecting",
                       "presentations": [], "disposition": None}
            self.lots[lot_id] = history
        else:
            history["status"] = "inspecting"
        history["presentations"].append({"severity": severity, "plan": self.plans[severity].name, "start": time,
                                         "n": 0, "sum": 0.0, "decision": None})
        self.active = {"lot": lot_id, "severity": severity, "first": first}
        self.counts["lots"] += first

    def _item(self, lot_id: str, value: Any, ctx: Context) -> None:
        active = self.active
        if active is None or active["lot"] != lot_id:
            self.counts["items_outside_inspection"] += 1
            return
        stream = f"lot:{active['severity']}"
        before = ctx.run(stream).counters["invalid_skipped"]
        event = ctx.step(stream, {INPUT: value})
        if ctx.run(stream).counters["invalid_skipped"] > before or event is None:
            self.counts["invalid_items"] += 1
            return
        self.counts["items"] += 1
        presentation = self.lots[lot_id]["presentations"][-1]
        presentation["n"], presentation["sum"] = event.output.n, event.output.statistic
        if active["first"] and self.watch:
            watch = ctx.step("supplier", {INPUT: value})
            if watch is not None and watch.terminal:
                ctx.record("decision", "supplier_alarm", evidence=["supplier"], basis={
                    "lot": lot_id, "statistic": watch.output.statistic,
                    "threshold": self.spec.data["supplier_watch"]["threshold"]})
                ctx.restart("supplier", "alarm")
        if event.output.decision is not None:
            self._decided(active, event.output.decision, stream, ctx)

    def _decided(self, active: dict[str, Any], decision: str, stream: str, ctx: Context) -> None:
        lot_id, severity = active["lot"], active["severity"]
        history = self.lots[lot_id]
        presentation = history["presentations"][-1]
        accepted = decision == "accept_h0"
        presentation["decision"] = "accepted" if accepted else "rejected"
        history["status"] = presentation["decision"]
        plan = self.plans[severity]
        oc = plan.data["design"]["oc"]
        ctx.record("decision", "accept_lot" if accepted else "reject_lot", evidence=[stream], basis={
            "lot": lot_id, "presentation": len(history["presentations"]), "severity": severity,
            "plan": plan.name, "characteristic": plan.characteristic, "items": presentation["n"],
            "sum": presentation["sum"], "aql": plan.data["aql"], "lq": plan.data["lq"],
            "accept_at_aql": oc["aql"]["accept"], "accept_at_lq": oc["lq"]["accept"],
            "oc_exact": plan.data["design"]["exact"]})
        self.active = None
        ctx.restart(stream, f"lot {lot_id} {presentation['decision']}")
        if active["first"]:
            self._switch(accepted, ctx)

    def _switch(self, accepted: bool, ctx: Context) -> None:
        rules = self.spec.data["switching"]
        self.recent = ([*self.recent, "accepted" if accepted else "rejected"])[-50:]
        before = self.severity
        streak = 0
        for outcome in reversed(self.recent):
            if outcome != "accepted":
                break
            streak += 1
        if self.severity == "normal":
            window = self.recent[-rules["to_tightened"]["of"]:]
            if "tightened" in self.plans and window.count("rejected") >= rules["to_tightened"]["rejected"]:
                self.severity, self.tightened_rejections = "tightened", 0
            elif "reduced" in self.plans and rules["to_reduced"] is not None and streak >= rules["to_reduced"]:
                self.severity = "reduced"
        elif self.severity == "tightened":
            if not accepted:
                self.tightened_rejections += 1
                if rules["discontinue"] is not None and self.tightened_rejections >= rules["discontinue"]:
                    self.discontinued = True
                    ctx.record("decision", "discontinue", basis={
                        "rejected_on_tightened": self.tightened_rejections,
                        "why": "too many lots rejected on tightened inspection"})
            elif streak >= rules["to_normal"]:
                self.severity = "normal"
        elif self.severity == "reduced" and not accepted:
            self.severity = "normal"
        if self.severity != before:
            self.recent = []  # each severity counts its own lots
            ctx.record("decision", "switch", basis={"from": before, "to": self.severity})


def _factory(spec_json: dict[str, Any]) -> SchemeHandler:
    return SchemeHandler(parse_spec(spec_json, SchemeSpec))


class Scheme:
    """An open scheme of inspection."""

    def __init__(self, task: Task):
        self.task = task
        handler = task.handler
        assert isinstance(handler, SchemeHandler)
        self.handler = handler
        self.spec = handler.spec

    @classmethod
    def create(cls, engine: Engine, spec: SchemeSpec) -> Scheme:
        return cls(engine.create(spec.scheme_id, SCHEME_KIND, spec.to_json(), SchemeHandler(spec)))

    @classmethod
    def open(cls, engine: Engine, scheme_id: str) -> Scheme:
        return cls(engine.open(scheme_id, _factory))

    def ingest(self, records: Sequence[Record]) -> Any:
        return self.task.ingest(records)

    def dispose(self, lot_id: str, disposition: str, *, actor: str, reason: str) -> Any:
        return self.task.command("dispose", {"lot": lot_id, "disposition": disposition}, actor=actor, reason=reason)

    def resume(self, *, actor: str, reason: str) -> Any:
        return self.task.command("resume", actor=actor, reason=reason)

    @property
    def severity(self) -> str:
        return self.handler.severity

    def lot(self, lot_id: str) -> dict[str, Any] | None:
        return self.handler.lots.get(lot_id)

    def status(self) -> dict[str, Any]:
        h = self.handler
        first = [p for x in h.lots.values() for i, p in enumerate(x["presentations"]) if i == 0 and p["decision"]]
        return {"id": self.spec.scheme_id, "severity": h.severity, "discontinued": h.discontinued,
                "inspecting": None if h.active is None else h.active["lot"], "lots": len(h.lots),
                "accepted": sum(p["decision"] == "accepted" for p in first),
                "rejected": sum(p["decision"] == "rejected" for p in first),
                "items_inspected": sum(p["n"] for x in h.lots.values() for p in x["presentations"]),
                "counts": dict(h.counts)}

    def report(self) -> Any:
        from .report import scheme_report

        return scheme_report(self)

    def protocol(self, lot_id: str) -> Any:
        from .report import lot_protocol

        return lot_protocol(self, lot_id)

    def close(self) -> None:
        self.task.close()

    def __enter__(self) -> Scheme:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

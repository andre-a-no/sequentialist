# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""The record of a lot's inspection, and the report of a scheme: decisions, their grounds, their guarantees."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sequentialist.core import Finding, Report, Section, Table

if TYPE_CHECKING:
    from .scheme import Scheme

_UNITS = {"attributes": "fraction defective", "nonconformities": "nonconformities per item",
          "variables": "fraction beyond the limit"}
_SUM = {"attributes": "defectives", "nonconformities": "nonconformities", "variables": "sum of measurements"}


def _guarantee(plan_data: dict[str, Any]) -> str:
    return "exact" if plan_data["design"]["exact"] else "approximate"


def _model_note(plan_data: dict[str, Any]) -> str:
    kind = plan_data["characteristic"]["type"]
    if kind == "attributes":
        return ("Model: items defective independently with the same probability (binomial: a process, or a lot "
                "much larger than the sample). For a small lot sampled without replacement the probabilities differ.")
    if kind == "nonconformities":
        return "Model: nonconformities per item follow a Poisson distribution with the same mean."
    return ("Model: measurements normal with the known standard deviation; the operating characteristic is "
            "computed on a lattice (error of order h²), hence approximate.")


def _plan_rows(plan_data: dict[str, Any]) -> list[list[Any]]:
    oc = plan_data["design"]["oc"]
    method = plan_data["method"]
    rows = [["plan", plan_data["name"]], ["characteristic", _UNITS[plan_data["characteristic"]["type"]]],
            ["procedure", plan_data["design"]["procedure"]],
            ["aql", plan_data["aql"]], ["lq", plan_data["lq"]],
            ["P(accept) at aql", oc["aql"]["accept"]], ["P(accept) at lq", oc["lq"]["accept"]],
            ["average items at aql", oc["aql"]["asn"]], ["average items at lq", oc["lq"]["asn"]],
            ["at most items", len(plan_data["design"]["continuation"])]]
    if method["type"] == "sequential":
        rows[5:5] = [["producer's risk (specified)", method["producer_risk"]],
                     ["consumer's risk (specified)", method["consumer_risk"]]]
    return rows


DISCLAIMER = ("Acceptance sampling controls the risks of the decisions on lots under the stated model; it does not "
              "certify any single item.")


def lot_protocol(scheme: Scheme, lot_id: str) -> Report:
    """The inspection record of one lot: each presentation, its plan, its result, and the actions on it."""
    h = scheme.handler
    history = h.lots.get(lot_id)
    if history is None:
        raise KeyError(f"no lot {lot_id!r}")
    task = scheme.task
    entries = [e for e in task.audit() if e.basis.get("lot") == lot_id]
    report = Report(f"Inspection of lot {lot_id}", task.task_id, task.spec_id,
                    task.watermark if task.watermark is not None else 0.0,
                    summary=f"Status: **{history['status'].replace('_', ' ')}**. {DISCLAIMER}")
    report.sections.append(Section("Lot", tables=[Table(["", ""], [
        ["lot", lot_id], ["size", history["size"]], ["product", scheme.spec.data["product"]],
        ["supplier", scheme.spec.data["supplier"]], ["disposition", history["disposition"]]])]))
    decisions = [e for e in entries if e.decision in ("accept_lot", "reject_lot")]
    for i, presentation in enumerate(history["presentations"]):
        plan = h.plans[presentation["severity"]].data
        section = Section(f"Presentation {i + 1}: {presentation['severity']} inspection",
                          text=_model_note(plan), tables=[Table(["plan", ""], _plan_rows(plan))])
        entry = decisions[i] if i < len(decisions) else None
        if entry is not None:
            kind = plan["characteristic"]["type"]
            section.findings.append(Finding(
                f"Lot {'accepted' if entry.decision == 'accept_lot' else 'rejected'} after "
                f"{entry.basis['items']} items ({_SUM[kind]}: {entry.basis['sum']:g}).", _guarantee(plan),
                {"accept_at_aql": entry.basis["accept_at_aql"], "accept_at_lq": entry.basis["accept_at_lq"]},
                stream=entry.evidence[0]["stream"], procedure=entry.evidence[0]["procedure"],
                history_digest=entry.evidence[0]["history_digest"],
                notes=[f"Audit entry {entry.n}, hash {entry.hash[:16]}…"]))
        else:
            section.text += f" Inspection in progress: {presentation['n']} items so far."
        report.sections.append(section)
    actions = [e for e in entries if e.kind == "action"]
    if actions:
        report.sections.append(Section("Actions", tables=[Table(
            ["n", "action", "disposition", "by", "reason"],
            [[e.n, e.decision, e.basis.get("disposition"), e.actor, e.reason] for e in actions])]))
    return report


def scheme_report(scheme: Scheme) -> Report:
    """Every lot, the switching between severities, the supplier watch, and data quality."""
    h, task = scheme.handler, scheme.task
    status = scheme.status()
    state = "discontinued" if h.discontinued else f"{h.severity} inspection"
    report = Report(f"Inspection scheme {scheme.spec.scheme_id}", task.task_id, task.spec_id,
                    task.watermark if task.watermark is not None else 0.0,
                    summary=f"Now: **{state}**. {status['accepted']} lots accepted and {status['rejected']} "
                            f"rejected at their first presentation. {DISCLAIMER}")
    for severity, plan in h.plans.items():
        report.sections.append(Section(f"Plan for {severity} inspection", text=_model_note(plan.data),
                                       tables=[Table(["", ""], _plan_rows(plan.data))]))
    rows = []
    for lot_id, history in h.lots.items():
        for i, p in enumerate(history["presentations"]):
            rows.append([lot_id, i + 1, p["severity"], p["n"], p["sum"], p["decision"] or "inspecting",
                         history["disposition"] if i == len(history["presentations"]) - 1 else None])
    report.sections.append(Section("Lots", tables=[Table(
        ["lot", "presentation", "severity", "items", "sum", "decision", "disposition"], rows)]))
    first = [x["presentations"][0] for x in h.lots.values() if x["presentations"][0]["decision"]]
    items = sum(p["n"] for p in first)
    if items and scheme.spec.plan("normal") is not None and h.plans["normal"].characteristic != "variables":
        report.sections.append(Section("Process average", findings=[Finding(
            f"Over first presentations: {sum(p['sum'] for p in first):g} in {items} items.", "none",
            {"average": sum(p["sum"] for p in first) / items},
            notes=["Descriptive only: items of a sequential plan are not a fixed-size sample, so this average is "
                   "slightly biased and comes without an interval."])]))
    events = [e for e in task.audit() if e.decision in ("switch", "discontinue", "supplier_alarm", "refuse_lot")
              or e.kind == "action"]
    report.sections.append(Section("Switching, alarms and actions", tables=[Table(
        ["n", "kind", "what", "details", "by", "reason"],
        [[e.n, e.kind, e.decision, {k: v for k, v in e.basis.items() if k in ("from", "to", "lot", "disposition",
                                                                             "why", "statistic")},
          e.actor, e.reason] for e in events])]))
    report.sections.append(Section("Data quality", tables=[Table(["count", "value"],
                                                                 [[k, v] for k, v in status["counts"].items()])]))
    return report

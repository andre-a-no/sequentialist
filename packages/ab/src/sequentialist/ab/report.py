# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""The report of an experiment: the decision, the sample ratio check, every metric with its guarantee, data quality."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sequentialist.core import Finding, Report, Section, Table
from sequentialist.core.report import fmt

if TYPE_CHECKING:
    from .experiment import Experiment

_REASONS = {
    "primary_success": "the primary metric improved and every guardrail is non-inferior",
    "sample_ratio_mismatch": "the traffic split differs from the intended one, so no result can be trusted",
    "no_variant_left": "every treatment was dropped",
    "duration_limit": "the maximum duration passed without a decision",
    "manual": "stopped by hand",
}

_MULTIPLICITY = {
    "e-BH": "Effects on the primary and secondary metrics are claimed with e-BH over all metrics and treatments "
            "(false discovery rate at most alpha at any stopping time). The intervals shown are marginal: "
            "each covers at level alpha on its own.",
    "Bonferroni": "Effects on the primary and secondary metrics are claimed with a Bonferroni split of alpha over "
                  "all metrics and treatments (family-wise error at most alpha at any stopping time); the "
                  "intervals use the same split. Conservative: e-BH needs an e-process for every metric type, "
                  "which sequential-analysis 0.4 reports for conversions only.",
    "none": "",
}


def _interval(row: dict[str, Any]) -> str:
    return f"[{fmt(row['lower'])}, {fmt(row['upper'])}]"


def build_report(experiment: Experiment) -> Report:
    spec, task, h = experiment.spec, experiment.task, experiment.handler
    status = experiment.status()
    rows = experiment.results()
    decision = h.decision
    if decision is None:
        summary = f"No decision yet: the experiment is {h.status}."
    elif decision["decision"] == "ship":
        reason = _REASONS[decision["reason"]] if any(m.role == "guardrail" for m in spec.metrics()) else \
            "the primary metric improved (no guardrails are defined)"
        summary = f"**Ship {decision['variant']}**: {reason}."
    else:
        summary = f"**Stop** without shipping: {_REASONS.get(decision['reason'], decision['reason'])}."
    summary += (" The numbers below are as of the latest data; the audit log keeps those the decision was "
                "taken on.")
    report = Report(f"Experiment {spec.experiment_id}", task.task_id, task.spec_id,
                    task.watermark if task.watermark is not None else 0.0, summary=summary)

    srm = task.last_output("srm")
    srm_section = Section("Sample ratio", text="Is the traffic split as intended? Checked on every exposure.")
    if srm is not None:
        srm_run = task.run("srm")
        verdict = "differs from" if srm["decision"] == "reject_h0" else "is consistent with"
        srm_section.findings.append(Finding(
            f"The split {verdict} the intended weights {spec.weights}.", srm_run.procedure.guarantee,
            {"units": srm["n"], "counts": list(srm["counts"]), "p_value": srm["p_value"],
             "alpha": spec.data["srm"]["alpha"]},
            stream="srm", procedure=srm_run.procedure.identity(), history_digest=srm_run.history_digest))
    report.sections.append(srm_section)

    effects = Section("Metrics", text=_MULTIPLICITY[h.analysis.multiplicity])
    for row in rows:
        if row["role"] == "guardrail" or row["verdict"] not in ("better", "worse"):
            continue
        scale = "difference" if row["scale"] == "absolute" else "relative change"
        notes = []
        if row["decision_procedure"] != row["interval_procedure"]:
            notes.append(f"Decided by {row['decision_procedure']} (anytime-valid p-value {fmt(row['p_value'])}); "
                         f"the interval is {row['interval_procedure']}'s, a separate and wider confidence "
                         f"sequence, and may still contain 0.")
        effects.findings.append(Finding(
            f"{row['metric']} ({row['role']}): {row['variant']} is {row['verdict']} than {row['control']}.",
            row["decision_guarantee"] or row["interval_guarantee"],
            {scale: row["estimate"], "interval": [row["lower"], row["upper"]], "level": row["level"]},
            stream=f"{row['metric']}:{row['variant']}", history_digest=row["history_digest"], notes=notes))
    effects.tables.append(Table(
        ["metric", "role", "variant", "treatment", "control", "effect", "interval", "level", "p-value", "units",
         "verdict", "procedure", "guarantee"],
        [[r["metric"], r["role"], r["variant"], r["treatment_value"], r["control_value"], r["estimate"],
          _interval(r), r["level"], r["p_value"], r["units"], r["verdict"],
          r["decision_procedure"] or r["interval_procedure"], r["decision_guarantee"] or r["interval_guarantee"]]
         for r in rows if r["role"] != "guardrail"],
        caption="Effects are differences for conversion and bounded metrics, relative changes for ratio and rate "
                "metrics; intervals are confidence sequences, valid whenever they are looked at."))
    report.sections.append(effects)

    guards = [r for r in rows if r["role"] == "guardrail"]
    if guards:
        section = Section("Guardrails", text="Non-inferior: harm beyond the margin is excluded. Harm: the whole "
                                             "interval lies beyond the margin. Bonferroni over guardrails and "
                                             "treatments at guardrail_alpha.")
        margins = {m.name: m.margin for m in spec.metrics()}
        section.tables.append(Table(
            ["metric", "variant", "effect", "interval", "margin", "level", "verdict", "procedure", "guarantee"],
            [[r["metric"], r["variant"], r["estimate"], _interval(r), margins[r["metric"]], r["level"], r["verdict"],
              r["interval_procedure"], r["interval_guarantee"]] for r in guards]))
        report.sections.append(section)

    quality = Section("Data quality")
    quality.tables.append(Table(["count", "value"], [[k, v] for k, v in status["counts"].items()] +
                                [[f"clipped:{k}", v] for k, v in status["clipped"].items()] +
                                [["units analysed", sum(h.units)], ["units still in their window", len(h.open)]]))
    report.sections.append(quality)

    log = Section("Decisions", text="From the audit log (hash-chained; `sequentialist audit` verifies it).")
    log.tables.append(Table(["n", "kind", "decision", "variant", "reason", "actor"],
                            [[e.n, e.kind, e.decision, e.basis.get("variant"), e.basis.get("reason") or e.reason,
                              e.actor] for e in task.audit() if e.kind in ("decision", "action")]))
    report.sections.append(log)
    return report

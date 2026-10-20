# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""The spec of an experiment (``kind: ab.experiment/1``).

Effects are measured on one scale per metric type, and `mde` and `margin`
are written on it:

=============  ===================================  ==========================
type           what a unit contributes               effect (treatment − control)
=============  ===================================  ==========================
conversion     1 if `event` happened in its window   difference of rates
bounded        sum of `value` of `event`, clipped    difference of means
               to `bounds`
ratio          numerator and denominator sums        relative lift of the ratio
               (counts if no `value`)                of sums, R_T / R_C − 1
rate           count of `event`, exposure sum        relative change of the
                                                     rate, rho − 1
=============  ===================================  ==========================

`direction` says which way is good: ``increase`` (conversions, revenue) or
``decrease`` (errors, latency).  A guardrail's `margin` is the harm the
experiment tolerates: with ``direction: decrease`` and ``margin: 0.1`` on a
rate, the error rate may grow by at most 10 %.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sequentialist.core import Spec
from sequentialist.core.schema import (
    Array,
    Boolean,
    Duration,
    Field,
    Map,
    Nullable,
    Number,
    Object,
    OneOf,
    String,
    Timestamp,
)
from sequentialist.core.storage.base import TASK_ID

KIND = "ab.experiment/1"
METRIC_NAME = r"[a-z][a-z0-9_]{0,63}"
VARIANT_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
EVENT_NAME = r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}"
FIELD_NAME = r"[A-Za-z_][A-Za-z0-9_]{0,63}"
TYPES = ("conversion", "bounded", "ratio", "rate")
ROLES = ("primary", "secondary", "guardrail")


def _source(doc: str) -> Object:
    return Object({
        "event": Field(String(pattern=EVENT_NAME), doc="Name of the event."),
        "value": Field(Nullable(String(pattern=FIELD_NAME)), default=None,
                       doc="Field of the event to sum; without it, events are counted."),
    }, doc=doc)


def _metric_fields(**extra: Field) -> dict[str, Field]:
    fields = {
        "name": Field(String(pattern=METRIC_NAME), doc="Name of the metric (lower case, digits, '_')."),
        "role": Field(String(enum=ROLES), default="secondary",
                      doc="primary: decides the experiment; secondary: reported; guardrail: must not get worse."),
        "direction": Field(String(enum=("increase", "decrease")), default="increase",
                           doc="Which way is good for this metric."),
        "mde": Field(Nullable(Number(exclusive_minimum=0)), default=None,
                     doc="Minimum detectable effect on the metric's effect scale (needed for the primary metric)."),
        "margin": Field(Nullable(Number(exclusive_minimum=0)), default=None,
                        doc="Guardrails: the harm tolerated, on the metric's effect scale."),
    }
    fields.update(extra)
    return fields


METRIC = OneOf("type", {
    "conversion": Object(_metric_fields(
        event=Field(String(pattern=EVENT_NAME), doc="The unit converts if this event happens in its window."))),
    "bounded": Object(_metric_fields(
        event=Field(String(pattern=EVENT_NAME), doc="Event whose values are summed per unit."),
        value=Field(String(pattern=FIELD_NAME), default="value", doc="Field of the event holding the value."),
        bounds=Field(Array(Number(), min_items=2, max_items=2),
                     doc="[lower, upper]: every unit's total is clipped to it; the number clipped is reported."))),
    "ratio": Object(_metric_fields(
        numerator=Field(_source("Summed per unit into the numerator.")),
        denominator=Field(_source("Summed per unit into the denominator.")))),
    "rate": Object(_metric_fields(
        event=Field(String(pattern=EVENT_NAME), doc="Events counted per unit."),
        exposure=Field(_source("The unit's exposure (sessions, days): summed, or counted.")))),
}, doc="A metric; the effect scale depends on its type (see the documentation).")


class ExperimentSpec(Spec):
    kind = KIND
    schema = Object({
        "id": Field(String(pattern=TASK_ID.pattern), doc="Identifier of the experiment (names files and tasks)."),
        "description": Field(String(), default=""),
        "salt": Field(String(min_length=1, max_length=256),
                      doc="Mixed into the assignment hash; a new salt reshuffles the units."),
        "unit": Field(String(min_length=1), default="user",
                      doc="What is randomised. Units arrive as pseudonyms, never as raw ids."),
        "variants": Field(Array(Object({
            "name": Field(String(pattern=VARIANT_NAME)),
            "weight": Field(Number(exclusive_minimum=0), default=1),
            "control": Field(Boolean(), default=False),
        }), min_items=2, max_items=26), doc="The arms; exactly one is the control."),
        "traffic": Field(Array(Object({
            "from": Field(Timestamp()),
            "share": Field(Number(exclusive_minimum=0, maximum=1)),
        })), default=list, doc="Ramp-up: the share of units in the experiment from each moment on (default 1)."),
        "assignment": Field(String(enum=("engine", "external")), default="engine",
                            doc="engine: variants come from this library's hash (ab-client), and exposures are "
                                "checked against it; external: the platform assigns, and only SRM checks the split."),
        "overrides": Field(Map(String(pattern=VARIANT_NAME)), default=dict,
                           doc="Manual assignments for QA: unit -> variant. These units are left out of the analysis."),
        "start": Field(Nullable(Timestamp()), default=None,
                       doc="Exposures before it are not counted; without it, the first exposure starts the clock."),
        "attribution_window": Field(Duration(), doc="Events count while they fall in [exposure, exposure + window)."),
        "lateness": Field(Duration(allow_zero=True), default=0,
                          doc="How late events may arrive (by event time) before a window is closed."),
        "max_duration": Field(Duration(), doc="Exposures are accepted for this long after the start."),
        "alpha": Field(Number(exclusive_minimum=0, exclusive_maximum=0.5), default=0.05,
                       doc="Error level for claims of an effect, over primary and secondary metrics and variants."),
        "guardrail_alpha": Field(Nullable(Number(exclusive_minimum=0, exclusive_maximum=0.5)), default=None,
                                 doc="Error level of non-inferiority claims, over guardrails and variants "
                                     "(default: alpha)."),
        "srm": Field(Object({"alpha": Field(Number(exclusive_minimum=0, exclusive_maximum=0.5), default=0.001)}),
                     default=lambda: {"alpha": 0.001}, doc="The sample ratio mismatch check."),
        "monitoring": Field(OneOf("mode", {
            "continuous": Object({}),
            "planned": Object({
                "looks": Field(Array(Number(exclusive_minimum=0, maximum=1), min_items=1),
                               doc="Fractions of max_units at which the primary metric is analysed; the last is 1."),
                "spending": Field(String(enum=("obrien-fleming", "pocock")), default="obrien-fleming"),
                "max_units": Field(Number(integer=True, minimum=4),
                                   doc="Units of a treatment and the control together at the last look."),
            }),
        }), default=lambda: {"mode": "continuous"},
            doc="continuous: decide at any moment (anytime-valid); planned: a group sequential design for the "
                "primary metric."),
        "metrics": Field(Array(METRIC, min_items=1)),
        "backend": Field(String(enum=("python", "numpy")), default="python",
                         doc="Arithmetic of the betting procedures: python (the same result everywhere) or numpy "
                             "(faster; needs sequential-analysis[fast] wherever the experiment is opened)."),
    }, doc="An online controlled experiment.")

    def problems(self) -> list[tuple[str, str]]:
        d = self.data
        out: list[tuple[str, str]] = []
        names = [v["name"] for v in d["variants"]]
        if len(set(names)) != len(names):
            out.append(("/variants", "variant names must differ"))
        if sum(v["control"] for v in d["variants"]) != 1:
            out.append(("/variants", "exactly one variant must be the control"))
        froms = [t["from"] for t in d["traffic"]]
        if any(b <= a for a, b in zip(froms, froms[1:])):
            out.append(("/traffic", "the moments must increase"))
        for unit, variant in d["overrides"].items():
            if variant not in names:
                out.append((f"/overrides/{unit}", f"unknown variant {variant!r}"))
        metrics = d["metrics"]
        if len({m["name"] for m in metrics}) != len(metrics):
            out.append(("/metrics", "metric names must differ"))
        primaries = [i for i, m in enumerate(metrics) if m["role"] == "primary"]
        if len(primaries) != 1:
            out.append(("/metrics", "exactly one metric must have role: primary"))
        for i, m in enumerate(metrics):
            at = f"/metrics/{i}"
            if m["role"] == "primary" and m["mde"] is None:
                out.append((f"{at}/mde", "the primary metric needs an mde (futility and planning use it)"))
            if m["role"] == "guardrail" and m["margin"] is None:
                out.append((f"{at}/margin", "a guardrail needs a margin: the harm it tolerates"))
            if m["role"] != "guardrail" and m["margin"] is not None:
                out.append((f"{at}/margin", "margin applies to guardrails only"))
            if m["type"] == "bounded" and not m["bounds"][0] < m["bounds"][1]:
                out.append((f"{at}/bounds", "need lower < upper"))
        monitoring = d["monitoring"]
        if monitoring["mode"] == "planned":
            looks = monitoring["looks"]
            if any(b <= a for a, b in zip(looks, looks[1:])) or looks[-1] != 1:
                out.append(("/monitoring/looks", "looks must increase and end at 1"))
            if primaries and metrics[primaries[0]]["type"] not in ("conversion", "bounded"):
                out.append(("/monitoring", "planned looks are available for a conversion or bounded primary metric"))
        return out

    # ------------------------------------------------------------ convenient views

    @property
    def experiment_id(self) -> str:
        return str(self.data["id"])

    @property
    def variant_names(self) -> list[str]:
        return [v["name"] for v in self.data["variants"]]

    @property
    def control(self) -> int:
        return next(i for i, v in enumerate(self.data["variants"]) if v["control"])

    @property
    def treatments(self) -> list[int]:
        return [i for i in range(len(self.data["variants"])) if i != self.control]

    @property
    def weights(self) -> list[float]:
        return [float(v["weight"]) for v in self.data["variants"]]

    def metrics(self) -> list[Metric]:
        return [Metric.of(m) for m in self.data["metrics"]]

    @property
    def primary(self) -> Metric:
        return next(m for m in self.metrics() if m.role == "primary")

    @property
    def guardrail_alpha(self) -> float:
        g = self.data["guardrail_alpha"]
        return float(self.data["alpha"] if g is None else g)


@dataclass(frozen=True)
class Metric:
    name: str
    type: str
    role: str
    direction: str
    mde: float | None
    margin: float | None
    raw: Any

    @classmethod
    def of(cls, data: dict[str, Any]) -> Metric:
        return cls(data["name"], data["type"], data["role"], data["direction"], data["mde"], data["margin"], data)

    @property
    def sign(self) -> int:
        """+1 if an increase is good, -1 if a decrease is."""
        return 1 if self.direction == "increase" else -1

    @property
    def scale(self) -> str:
        return "absolute" if self.type in ("conversion", "bounded") else "relative"

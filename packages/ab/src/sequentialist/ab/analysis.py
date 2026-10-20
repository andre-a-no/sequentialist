# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Which procedures of sequential-analysis analyse an experiment, at which levels, and how to read them.

Every comparison is one treatment against the control.  For each metric
and treatment (a *pair*) the analysis has

* an **interval**: a confidence sequence for the effect, valid at any time;
* for primary and secondary metrics, a **decision**: whether an effect is
  established, with the error controlled over all such pairs;
* for guardrails, a non-inferiority and a harm check read off the interval.

Default procedures (exact where an exact one answers the question):

=============  ==============================================  ==========
metric type    procedure                                       guarantee
=============  ==============================================  ==========
conversion     TwoProportionTest (decision; e-values),         exact
               BoundedABTest on [0, 1] (interval)              exact
bounded        BoundedABTest on the declared bounds            exact
ratio          RatioABTest, relative lift                      asymptotic
rate           RateRatioTest                                   exact
=============  ==============================================  ==========

Multiplicity over the primary and secondary metrics and the treatments:
e-BH (`MultipleTests`, false discovery rate at `alpha`) when every pair
reports an e-process, which in sequential-analysis 0.4 is the case for
conversions only; otherwise Bonferroni (`alpha` / number of pairs for each
pair: family-wise error at most `alpha`, which also bounds the false
discovery rate; exact, conservative).  Guardrails form their own family:
Bonferroni at `guardrail_alpha`.  The sample ratio mismatch test
(`SampleRatioMismatch`, exact) runs at `srm.alpha` on every exposure.

With planned looks the primary metric is analysed by a group sequential
test (`GroupSequentialProportions` or `GroupSequentialTwoMeans`, one-sided
in the good direction, `alpha` split over the treatments; asymptotic),
and the secondary metrics form a family of their own at `alpha`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sequential_analysis.group_sequential import group_sequential_design
from sequential_analysis.procedures import (
    BoundedABTest,
    GroupSequentialProportions,
    GroupSequentialTwoMeans,
    MultipleTests,
    RateRatioTest,
    RatioABTest,
    SampleRatioMismatch,
    TwoProportionTest,
)

from sequentialist.core import Stream

from .spec import ExperimentSpec, Metric

ARM = "arm"
REJECT = "reject_h0"
ACCEPT = "accept_h0"


def denominator_input(metric: Metric) -> str:
    return f"{metric.name}__d"


def exposure_input(metric: Metric) -> str:
    return f"{metric.name}__e"


@dataclass(frozen=True)
class Pair:
    metric: Metric
    variant: int  # the treatment's index among the variants
    variant_name: str
    interval_stream: str
    interval_level: float  # the confidence sequence covers with probability 1 - interval_level
    interval_guarantee: str
    decision_stream: str | None  # None for guardrails
    decision_kind: str  # "ebh", "test", "planned" or "none"
    decision_key: str  # the test's name inside MultipleTests
    decision_guarantee: str

    @property
    def name(self) -> str:
        return f"{self.metric.name}:{self.variant_name}"


@dataclass(frozen=True)
class Estimate:
    """An effect on its scale (see the spec), with its confidence sequence; None where not yet defined."""

    estimate: float | None
    lower: float | None  # None: unbounded below (or not yet available)
    upper: float | None
    treatment: float | None  # the treatment's value of the metric (rate, mean, ratio)
    control: float | None
    units: int


class Analysis:
    def __init__(self, spec: ExperimentSpec):
        self.spec = spec
        d = spec.data
        self.alpha = float(d["alpha"])
        self.control = spec.control
        self.names = spec.variant_names
        self.metrics = spec.metrics()
        self.streams: dict[str, Stream] = {
            "srm": Stream(SampleRatioMismatch(spec.weights, float(d["srm"]["alpha"]), input=ARM)),
        }
        self.pairs: list[Pair] = []
        planned = d["monitoring"]["mode"] == "planned"
        treatments = spec.treatments
        claims = [m for m in self.metrics if m.role != "guardrail" and not (planned and m.role == "primary")]
        guardrails = [m for m in self.metrics if m.role == "guardrail"]
        self.multiplicity = "none"
        if claims:
            self.multiplicity = "e-BH" if all(m.type == "conversion" for m in claims) else "Bonferroni"
        family = len(claims) * len(treatments)
        tests: dict[str, Any] = {}
        for m in claims:
            for t in treatments:
                key = f"{m.name}:{self.names[t]}"
                if self.multiplicity == "e-BH":
                    tests[key] = TwoProportionTest(arms=(t, self.control), input=m.name)
                    interval = self._interval(m, t, self.alpha, f"cs:{key}")
                    self.pairs.append(Pair(m, t, self.names[t], interval, self.alpha, "exact", "ebh", "ebh", key,
                                           "exact"))
                else:
                    level = self.alpha / family
                    interval = self._interval(m, t, level, f"cs:{key}")
                    if m.type == "conversion":
                        self.streams[f"test:{key}"] = Stream(TwoProportionTest(level, arms=(t, self.control),
                                                                               input=m.name))
                        decision = f"test:{key}"
                    else:
                        decision = interval  # the interval's procedure is the test
                    self.pairs.append(Pair(m, t, self.names[t], interval, level, self._guarantee(m), decision,
                                           "test", key, self._guarantee(m)))
        if tests:
            self.streams["ebh"] = Stream(MultipleTests(tests, self.alpha))
        if planned:
            m = spec.primary
            level = self.alpha / len(treatments)
            monitoring = d["monitoring"]
            design = group_sequential_design(level, monitoring["looks"], monitoring["spending"])
            for t in treatments:
                key = f"{m.name}:{self.names[t]}"
                alternative = "greater" if m.sign > 0 else "less"
                common: dict[str, Any] = {"arms": (t, self.control), "input": m.name, "alternative": alternative}
                if m.type == "conversion":
                    gs: Any = GroupSequentialProportions.from_design(design, monitoring["max_units"], **common)
                else:
                    gs = GroupSequentialTwoMeans.from_design(design, monitoring["max_units"], None, **common)
                self.streams[f"gs:{key}"] = Stream(gs)
                interval = self._interval(m, t, level, f"cs:{key}")
                self.pairs.append(Pair(m, t, self.names[t], interval, level, "exact", f"gs:{key}", "planned", key,
                                       "asymptotic"))
        if guardrails:
            level = spec.guardrail_alpha / (len(guardrails) * len(treatments))
            for m in guardrails:
                for t in treatments:
                    key = f"{m.name}:{self.names[t]}"
                    interval = self._interval(m, t, level, f"guard:{key}")
                    self.pairs.append(Pair(m, t, self.names[t], interval, level, self._guarantee(m), None, "none",
                                           key, self._guarantee(m)))

    @staticmethod
    def _guarantee(m: Metric) -> str:
        return "asymptotic" if m.type == "ratio" else "exact"

    def _interval(self, m: Metric, t: int, level: float, name: str) -> str:
        arms = (t, self.control)
        if m.type in ("conversion", "bounded"):
            lo, hi = (0.0, 1.0) if m.type == "conversion" else (float(m.raw["bounds"][0]), float(m.raw["bounds"][1]))
            w = self.spec.weights
            procedure: Any = BoundedABTest(lo, hi, w[t] / (w[t] + w[self.control]), level, arms=arms, input=m.name,
                                           backend=self.spec.data["backend"])
        elif m.type == "ratio":
            procedure = RatioABTest(level, scale="relative", arms=arms, input=m.name,
                                    denominator_input=denominator_input(m))
        else:
            procedure = RateRatioTest(level, arms=arms, input=m.name, exposure_input=exposure_input(m))
        self.streams[name] = Stream(procedure)
        return name

    # ------------------------------------------------------------ reading outputs

    def pairs_of(self, metric: str) -> list[Pair]:
        return [p for p in self.pairs if p.metric.name == metric]

    def primary_pairs(self) -> list[Pair]:
        return [p for p in self.pairs if p.metric.role == "primary"]

    def guardrail_pairs(self, variant: int) -> list[Pair]:
        return [p for p in self.pairs if p.metric.role == "guardrail" and p.variant == variant]

    @staticmethod
    def estimate(pair: Pair, output: dict[str, Any] | None) -> Estimate:
        """The effect on the metric's scale, read from the interval stream's last output."""
        if output is None:
            return Estimate(None, None, None, None, None, 0)
        kind = pair.metric.type
        if kind in ("conversion", "bounded"):
            t, c = output["means"]
            effect = None if t is None or c is None else t - c
            return Estimate(effect, output["lower"], output["upper"], t, c, output["n"])
        if kind == "ratio":
            t, c = output["ratios"]
            return Estimate(output["lift"], output["lift_lower"], output["lift_upper"], t, c, output["n"])
        t, c = output["rates"]
        rho = output["rate_ratio"]
        lower, upper = output["lower"], output["upper"]
        return Estimate(None if rho is None else rho - 1.0, None if lower is None else lower - 1.0,
                        None if upper is None else upper - 1.0, t, c, int(output["units"]))

    @staticmethod
    def rejected(pair: Pair, last: Callable[[str], Any]) -> bool | None:
        """True once the pair's test rejects "no effect"; False when a planned test accepts; None otherwise."""
        if pair.decision_stream is None:
            return None
        output = last(pair.decision_stream)
        if output is None:
            return None
        if pair.decision_kind == "ebh":
            return True if pair.decision_key in output["rejected"] else None
        decision = output.get("decision")
        if decision == REJECT:
            return True
        if decision == ACCEPT and pair.decision_kind == "planned":
            return False
        return None

    def p_value(self, pair: Pair, last: Callable[[str], Any]) -> float | None:
        """The anytime-valid p-value of the decision procedure for this pair (Bonferroni: before the split)."""
        if pair.decision_stream is None:
            return None
        output = last(pair.decision_stream)
        if output is None:
            return None
        if pair.decision_kind == "ebh":
            result = output["results"].get(pair.decision_key)
            return None if result is None else result.get("p_value")
        value = output.get("p_value")
        return None if value is None else float(value)

    def e_value(self, pair: Pair, last: Callable[[str], Any]) -> float | None:
        """The log e-value against "no effect", where the decision procedure reports one."""
        if pair.decision_stream is None:
            return None
        output = last(pair.decision_stream)
        if output is None:
            return None
        if pair.decision_kind == "ebh":
            value = output["log_e_values"].get(pair.decision_key)
            return None if value is None else float(value)
        value = output.get("log_e_value")
        return None if value is None else float(value)

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Plans: designed once with sequential-analysis, executed by its `PlanTest`, characterised by its `design` module.

A plan is a sequence of continuation intervals for the running sum S_n of
what the items give (defectives, nonconformities, measurements).  After
item n: accept the lot if S_n is at or below the lower end, reject it if at
or above the upper end, take another item otherwise.

* ``sequential`` plans are the optimal truncated plans of
  `design.kiefer_weiss_plan`: at most ``max_sample`` items, producer's and
  consumer's risks at most as specified, the smallest average sample number
  at the chosen quality (or the smallest maximum, ``least-favourable``).
* ``single`` plans take n items and accept with at most c defectives; with
  curtailment they reject as soon as c + 1 are found, which changes the
  number of items inspected but not the decision.

The operating characteristic (probability of acceptance, average sample
number) comes from `design.operating_characteristic`: exact for attributes
(binomial model) and nonconformities (Poisson model), on a fine lattice for
variables (error of order h^2, reported as not exact).

The binomial model is that of a process, or of a lot much larger than the
sample (an operating characteristic "of type B").  For a small lot sampled
without replacement the hypergeometric probabilities differ; this version
does not compute them.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import NormalDist
from typing import Any, Sequence

from sequential_analysis import __version__ as sequential_analysis_version
from sequential_analysis.design import kiefer_weiss_plan, operating_characteristic
from sequential_analysis.procedures import Bernoulli, Gaussian, PlanTest, Poisson

from sequentialist.core import parse_spec

from .spec import PLAN_KIND, PlanSpec

INPUT = "x"
_NORMAL = NormalDist()


def family(plan: PlanSpec) -> Any:
    c = plan.data["characteristic"]
    if c["type"] == "attributes":
        return Bernoulli()
    if c["type"] == "nonconformities":
        return Poisson()
    return Gaussian(float(c["sigma"]))


def parameter(plan: PlanSpec, quality: float) -> float:
    """The parameter of the family at a quality level (fraction, or nonconformities per item)."""
    c = plan.data["characteristic"]
    if c["type"] != "variables":
        return float(quality)
    if not 0 < quality < 1:
        raise ValueError("a fraction beyond the limit lies in (0, 1)")
    z = _NORMAL.inv_cdf(1.0 - quality)  # the limit is z sigmas beyond the mean
    limit, sigma = float(c["value"]), float(c["sigma"])
    return limit - z * sigma if c["limit"] == "upper" else limit + z * sigma


def _single(sample: int, acceptance: int, curtailed: bool) -> list[tuple[float | None, float | None]]:
    stages: list[tuple[float | None, float | None]] = [
        (None, float(acceptance + 1) if curtailed else None) for _ in range(sample - 1)]
    stages.append((acceptance + 0.5, acceptance + 0.5))
    return stages


def compile_plan(plan: PlanSpec) -> PlanTest:
    """The plan as a sequential-analysis procedure, from its stored boundaries."""
    design = plan.data["design"]
    if design is None:
        raise ValueError(f"plan {plan.name!r} is not designed yet")
    stages = [tuple(stage) for stage in design["continuation"]]
    return PlanTest(stages, contract=family(plan).contract(INPUT), reject_high=bool(design["reject_high"]),
                    label=plan.name, input=INPUT)


@dataclass(frozen=True)
class Point:
    quality: float
    accept: float  # probability of acceptance
    asn: float  # average sample number
    exact: bool

    def to_json(self) -> dict[str, Any]:
        return {"quality": self.quality, "accept": self.accept, "asn": self.asn, "exact": self.exact}


def characteristic(plan: PlanSpec, qualities: Sequence[float]) -> list[Point]:
    """Probability of acceptance and average sample number at each quality level."""
    test, fam = compile_plan(plan), family(plan)
    out = []
    for q in qualities:
        oc = operating_characteristic(test, fam, parameter(plan, q))
        out.append(Point(float(q), oc.accept, oc.expected_n, oc.exact))
    return out


def design(plan: PlanSpec) -> PlanSpec:
    """The plan with its boundaries and operating characteristic filled in (computed by sequential-analysis)."""
    d = plan.data
    method = d["method"]
    fam = family(plan)
    good, bad = parameter(plan, d["aql"]), parameter(plan, d["lq"])
    if method["type"] == "sequential":
        made = kiefer_weiss_plan(good, bad, float(method["producer_risk"]), float(method["consumer_risk"]),
                                 int(method["max_sample"]), family=fam,
                                 theta_star="least-favourable" if method["optimal_at"] == "least-favourable" else None,
                                 cache=False)
        stages = [list(stage) for stage in made.plan.plan]
        reject_high = made.plan.reject_high
        procedure = "kiefer_weiss_plan" + (" (least favourable)" if method["optimal_at"] == "least-favourable" else "")
    else:
        stages = [list(s) for s in _single(int(method["sample"]), int(method["acceptance"]), method["curtailed"])]
        reject_high = True
        procedure = "single sampling" + (", curtailed" if method["curtailed"] else "")
    draft = {**d, "design": {"continuation": stages, "reject_high": reject_high, "exact": True,
                             "procedure": procedure, "sequential_analysis": sequential_analysis_version,
                             "oc": {"aql": {"accept": 0.0, "asn": 0.0}, "lq": {"accept": 0.0, "asn": 0.0}}}}
    designed = PlanSpec(draft)
    at_aql, at_lq = characteristic(designed, [d["aql"], d["lq"]])
    draft["design"]["exact"] = at_aql.exact and at_lq.exact
    draft["design"]["oc"] = {"aql": {"accept": at_aql.accept, "asn": at_aql.asn},
                             "lq": {"accept": at_lq.accept, "asn": at_lq.asn}}
    return parse_spec({"kind": PLAN_KIND, **draft}, PlanSpec)


def verify(plan: PlanSpec, tolerance: float = 1e-9) -> list[str]:
    """Problems of a designed plan: recomputed characteristic differs from the stored one, or misses the risks."""
    d = plan.data
    if d["design"] is None:
        return ["the plan is not designed"]
    problems = []
    at_aql, at_lq = characteristic(plan, [d["aql"], d["lq"]])
    stored = d["design"]["oc"]
    for name, point in (("aql", at_aql), ("lq", at_lq)):
        for key in ("accept", "asn"):
            if abs(getattr(point, key) - stored[name][key]) > tolerance * max(1.0, abs(stored[name][key])):
                problems.append(f"at {name} the {key} recomputes to {getattr(point, key)!r}, stored "
                                f"{stored[name][key]!r}: the boundaries were changed")
    method = d["method"]
    if method["type"] == "sequential":
        slack = 0.0 if at_aql.exact else 1e-3
        if 1.0 - at_aql.accept > method["producer_risk"] + slack:
            problems.append(f"producer's risk {1.0 - at_aql.accept:.4g} exceeds {method['producer_risk']}")
        if at_lq.accept > method["consumer_risk"] + slack:
            problems.append(f"consumer's risk {at_lq.accept:.4g} exceeds {method['consumer_risk']}")
        if len(d["design"]["continuation"]) > method["max_sample"]:
            problems.append("the plan takes more items than max_sample")
    return problems

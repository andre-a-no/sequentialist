# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Specs of acceptance sampling: a plan (``quality.plan/1``) and a scheme of inspection (``quality.scheme/1``).

A **plan** says how one lot is inspected: what is checked on an item, the
quality levels that matter, and how many items to take before deciding.

=================  ====================================  ==========================================
characteristic     what an item gives                    quality level (aql, lq)
=================  ====================================  ==========================================
attributes         1 if defective, 0 if not              fraction defective
nonconformities    the number of nonconformities         nonconformities per item
variables          a measurement, normal with known      fraction beyond the specification limit
                   sigma, against one limit
=================  ====================================  ==========================================

``aql`` is the quality the producer must see accepted (with probability at
least 1 - producer_risk), ``lq`` the quality the consumer must see rejected
(with probability at least 1 - consumer_risk).  The method is ``sequential``
(an optimal truncated plan, computed by `design`) or ``single`` (take n
items, accept with at most c defectives).  `design` fills in ``design``:
the boundaries the plan executes and its operating characteristic.  A
designed plan is self-contained: inspecting a lot needs no computation.

A **scheme** is the inspection of one product (or supplier) over many lots:
the plans for normal, tightened and reduced inspection, the rules that
switch between them, and what happens to rejected lots.
"""
from __future__ import annotations

from typing import Any

from sequentialist.core import Spec
from sequentialist.core.schema import Array, Boolean, Field, Nullable, Number, Object, OneOf, String
from sequentialist.core.storage.base import TASK_ID

PLAN_KIND = "quality.plan/1"
SCHEME_KIND = "quality.scheme/1"
SEVERITIES = ("normal", "tightened", "reduced")
DISPOSITIONS = ("scrap", "screen", "rework", "return", "concession")


def _design_schema() -> Object:
    return Object({
        "continuation": Field(Array(Array(Nullable(Number()), min_items=2, max_items=2), min_items=1),
                              doc="For item n = 1, 2, ...: [lower, upper] of the running sum; accept at or below "
                                  "lower, reject at or above upper, otherwise take another item."),
        "reject_high": Field(Boolean(), doc="True when large sums mean poor quality."),
        "exact": Field(Boolean(), doc="True when the operating characteristic is exact (discrete data)."),
        "procedure": Field(String(), doc="How the plan was made."),
        "sequential_analysis": Field(String(), doc="Version of sequential-analysis that computed it."),
        "oc": Field(Object({
            "aql": Field(Object({"accept": Field(Number()), "asn": Field(Number())})),
            "lq": Field(Object({"accept": Field(Number()), "asn": Field(Number())})),
        }), doc="Probability of acceptance and average sample number at aql and lq."),
    }, doc="Filled in by `sequentialist quality design`.")


PLAN_FIELDS = {
    "name": Field(String(pattern=r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"), doc="Name of the plan."),
    "description": Field(String(), default=""),
    "characteristic": Field(OneOf("type", {
        "attributes": Object({}),
        "nonconformities": Object({}),
        "variables": Object({
            "limit": Field(String(enum=("upper", "lower")), doc="Which specification limit."),
            "value": Field(Number(), doc="The specification limit."),
            "sigma": Field(Number(exclusive_minimum=0), doc="The known standard deviation of a measurement."),
        }),
    }), doc="What an item gives."),
    "aql": Field(Number(exclusive_minimum=0), doc="Acceptable quality level: fraction defective, "
                                                  "nonconformities per item, or fraction beyond the limit."),
    "lq": Field(Number(exclusive_minimum=0), doc="Limiting quality, worse than aql."),
    "method": Field(OneOf("type", {
        "sequential": Object({
            "producer_risk": Field(Number(exclusive_minimum=0, exclusive_maximum=0.5), default=0.05,
                                   doc="Largest probability of rejecting a lot of quality aql."),
            "consumer_risk": Field(Number(exclusive_minimum=0, exclusive_maximum=0.5), default=0.10,
                                   doc="Largest probability of accepting a lot of quality lq."),
            "max_sample": Field(Number(integer=True, minimum=1),
                                doc="No lot needs more items than this (truncation)."),
            "optimal_at": Field(String(enum=("first-order", "least-favourable")), default="first-order",
                                doc="Where the average sample number is minimised: Lorden's first-order point, "
                                    "or the least favourable quality (the Kiefer-Weiss test: smallest maximum)."),
        }),
        "single": Object({
            "sample": Field(Number(integer=True, minimum=1), doc="Items to inspect (n)."),
            "acceptance": Field(Number(integer=True, minimum=0), doc="Accept with at most this many defectives (c)."),
            "curtailed": Field(Boolean(), default=True,
                               doc="Reject as soon as c + 1 defectives are found (the same decisions, fewer items)."),
        }),
    }), doc="How the lot is sampled."),
    "design": Field(Nullable(_design_schema()), default=None),
}


class PlanSpec(Spec):
    kind = PLAN_KIND
    schema = Object(PLAN_FIELDS, doc="A plan of acceptance sampling for one lot.")

    def problems(self) -> list[tuple[str, str]]:
        d = self.data
        out: list[tuple[str, str]] = []
        kind = d["characteristic"]["type"]
        if not d["aql"] < d["lq"]:
            out.append(("/lq", "the limiting quality must be worse (larger) than the acceptable quality level"))
        if kind in ("attributes", "variables") and not d["lq"] < 1:
            out.append(("/lq", "a fraction must be below 1"))
        method = d["method"]
        if method["type"] == "single":
            if method["acceptance"] >= method["sample"]:
                out.append(("/method/acceptance", "must be smaller than the sample"))
            if kind == "variables":
                out.append(("/method", "single sampling by variables is not available; use a sequential plan"))
        return out

    @property
    def name(self) -> str:
        return str(self.data["name"])

    @property
    def characteristic(self) -> str:
        return str(self.data["characteristic"]["type"])

    @property
    def designed(self) -> bool:
        return self.data["design"] is not None


def _plan_slot(doc: str) -> Field:
    full = dict(PLAN_FIELDS)
    full["kind"] = Field(String(enum=[PLAN_KIND]))
    return Field(Nullable(Object(full)), default=None, doc=doc)


class SchemeSpec(Spec):
    kind = SCHEME_KIND
    schema = Object({
        "id": Field(String(pattern=TASK_ID.pattern), doc="Identifier of the scheme (names files and tasks)."),
        "description": Field(String(), default=""),
        "product": Field(String(), default="", doc="What is inspected."),
        "supplier": Field(String(), default="", doc="Who supplies it, if the scheme is per supplier."),
        "plans": Field(Object({
            "normal": Field(Object({**PLAN_FIELDS, "kind": Field(String(enum=[PLAN_KIND]))}),
                            doc="The plan of normal inspection (designed)."),
            "tightened": _plan_slot("The plan of tightened inspection (designed); without it there is no switching."),
            "reduced": _plan_slot("The plan of reduced inspection (designed), if reduced inspection is used."),
        })),
        "start": Field(String(enum=SEVERITIES), default="normal"),
        "switching": Field(Object({
            "to_tightened": Field(Object({"rejected": Field(Number(integer=True, minimum=1), default=2),
                                          "of": Field(Number(integer=True, minimum=1), default=5)}),
                                  default=lambda: {"rejected": 2, "of": 5},
                                  doc="Normal to tightened when this many of the last lots were rejected."),
            "to_normal": Field(Number(integer=True, minimum=1), default=5,
                               doc="Tightened to normal after this many consecutive accepted lots."),
            "to_reduced": Field(Nullable(Number(integer=True, minimum=1)), default=None,
                                doc="Normal to reduced after this many consecutive accepted lots (null: never)."),
            "discontinue": Field(Nullable(Number(integer=True, minimum=1)), default=5,
                                 doc="Stop accepting lots when this many are rejected during one period of "
                                     "tightened inspection (null: never); `resume` restarts on tightened."),
        }), default=dict),
        "resubmission": Field(Object({
            "max": Field(Number(integer=True, minimum=0), default=1,
                         doc="How often a reworked lot may be presented again."),
            "plan": Field(String(enum=SEVERITIES), default="tightened",
                          doc="The plan a resubmitted lot is inspected with."),
        }), default=dict),
        "supplier_watch": Field(Nullable(Object({
            "threshold": Field(Number(exclusive_minimum=0),
                               doc="CUSUM threshold over all items of first presentations, from aql to lq of the "
                                   "normal plan: a lasting deterioration of the supplier, across lots."),
        })), default=None),
    }, doc="Inspection of one product or supplier, lot after lot.")

    def plan(self, severity: str) -> PlanSpec | None:
        raw = self.data["plans"][severity]
        if raw is None:
            return None
        plan = PlanSpec({k: v for k, v in raw.items() if k != "kind"})
        return plan

    def problems(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        plans = {s: self.plan(s) for s in SEVERITIES}
        kinds = {p.characteristic for p in plans.values() if p is not None}
        if len(kinds) > 1:
            out.append(("/plans", "every plan of a scheme inspects the same characteristic"))
        for severity, plan in plans.items():
            if plan is None:
                continue
            for path, message in plan.problems():
                out.append((f"/plans/{severity}{path}", message))
            if not plan.designed:
                out.append((f"/plans/{severity}/design", "the plan is not designed yet: run "
                                                          "`sequentialist quality design` on it"))
        if plans[self.data["start"]] is None:
            out.append(("/start", f"no {self.data['start']} plan"))
        if self.data["resubmission"]["max"] > 0 and plans[self.data["resubmission"]["plan"]] is None:
            out.append(("/resubmission/plan", f"no {self.data['resubmission']['plan']} plan"))
        if self.data["switching"]["to_reduced"] is not None and plans["reduced"] is None:
            out.append(("/switching/to_reduced", "no reduced plan"))
        to_tightened = self.data["switching"]["to_tightened"]
        if to_tightened["rejected"] > to_tightened["of"]:
            out.append(("/switching/to_tightened", "rejected cannot exceed of"))
        return out

    @property
    def scheme_id(self) -> str:
        return str(self.data["id"])

    def severities(self) -> list[str]:
        return [s for s in SEVERITIES if self.data["plans"][s] is not None]


def plan_json(plan: PlanSpec) -> dict[str, Any]:
    return plan.to_json()

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""`sequentialist quality`: design plans, show their characteristic, check schemes, print records.

    sequentialist quality design PLAN [-o DESIGNED]     compute the plan (boundaries and OC) with sequential-analysis
    sequentialist quality oc PLAN [--at q1 q2 ...]       probability of acceptance and average items at quality levels
    sequentialist quality check SPEC                     validate a plan or a scheme; recompute stored OCs
    sequentialist quality schema {plan,scheme}           JSON Schema of the spec formats
    sequentialist quality protocol STORAGE SCHEME LOT    the inspection record of a lot
    sequentialist quality report STORAGE SCHEME          the report of a scheme
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sequentialist.core import Engine, SpecError, json_schema, load_spec, open_storage
from sequentialist.core.report import fmt
from sequentialist.core.spec import read_document

from .plans import characteristic, design, verify
from .scheme import Scheme
from .spec import PLAN_KIND, PlanSpec, SchemeSpec


def _design(args: argparse.Namespace) -> int:
    plan = design(load_spec(args.plan, PlanSpec))
    text = json.dumps(plan.to_json(), indent=1)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    oc = plan.data["design"]["oc"]
    print(f"{plan.name}: at most {len(plan.data['design']['continuation'])} items; P(accept) "
          f"{oc['aql']['accept']:.4f} at aql, {oc['lq']['accept']:.4f} at lq; average items "
          f"{oc['aql']['asn']:.1f} and {oc['lq']['asn']:.1f}", file=sys.stderr)
    return 0


def _oc(args: argparse.Namespace) -> int:
    plan = load_spec(args.plan, PlanSpec)
    d = plan.data
    points = args.at or sorted({d["aql"] * f for f in (0.25, 0.5, 1.0)} | {d["lq"] * f for f in (0.5, 1.0, 1.5)})
    print("| quality | P(accept) | average items | exact |\n|---|---|---|---|")
    for p in characteristic(plan, points):
        print(f"| {fmt(p.quality)} | {p.accept:.4f} | {p.asn:.1f} | {'yes' if p.exact else 'no (lattice)'} |")
    return 0


def _check(args: argparse.Namespace) -> int:
    data = read_document(args.spec)
    if isinstance(data, dict) and data.get("kind") == PLAN_KIND:
        plans = {"plan": load_spec(args.spec, PlanSpec)}
    else:
        scheme = load_spec(args.spec, SchemeSpec)
        plans = {s: p for s in scheme.severities() if (p := scheme.plan(s)) is not None}
        print(f"{args.spec}: scheme {scheme.scheme_id}, spec id {scheme.id}")
    status = 0
    for name, plan in plans.items():
        problems = verify(plan)
        print(f"  {name}: {plan.name}: " + ("designed, characteristic verified" if not problems else
                                             "; ".join(problems)))
        status |= bool(problems)
    return status


def _schema(args: argparse.Namespace) -> int:
    print(json.dumps(json_schema(PlanSpec if args.which == "plan" else SchemeSpec), indent=2, ensure_ascii=False))
    return 0


def _open(args: argparse.Namespace, render: Any) -> int:
    with open_storage(args.storage) as storage:
        scheme = Scheme.open(Engine(storage), args.scheme)
        try:
            report = render(scheme)
        finally:
            scheme.close()
    print(json.dumps(report.to_json(), indent=2, ensure_ascii=False) if args.json else report.to_markdown())
    return 0


def register(commands: Any) -> None:
    parser = commands.add_parser("quality", help="acceptance sampling of lots")
    sub = parser.add_subparsers(dest="quality_command", required=True)
    p = sub.add_parser("design", help="compute a plan with sequential-analysis")
    p.add_argument("plan")
    p.add_argument("-o", "--output")
    p.set_defaults(handler=_design)
    p = sub.add_parser("oc", help="operating characteristic of a designed plan")
    p.add_argument("plan")
    p.add_argument("--at", type=float, nargs="+")
    p.set_defaults(handler=_oc)
    p = sub.add_parser("check", help="validate a plan or a scheme")
    p.add_argument("spec")
    p.set_defaults(handler=_check)
    p = sub.add_parser("schema", help="JSON Schema of a spec format")
    p.add_argument("which", choices=("plan", "scheme"))
    p.set_defaults(handler=_schema)
    p = sub.add_parser("protocol", help="the inspection record of a lot")
    p.add_argument("storage")
    p.add_argument("scheme")
    p.add_argument("lot")
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=lambda a: _open(a, lambda s: s.protocol(a.lot)))
    p = sub.add_parser("report", help="the report of a scheme")
    p.add_argument("storage")
    p.add_argument("scheme")
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=lambda a: _open(a, lambda s: s.report()))


__all__ = ["SpecError", "register"]

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""`sequentialist ab`: check specs, print schemas and client configurations, plan, report.

    sequentialist ab check SPEC                         validate; print the id and the analysis
    sequentialist ab schema                             JSON Schema of the spec format
    sequentialist ab client-config SPEC                 the configuration for @sequentialist/ab-client
    sequentialist ab plan SPEC --baseline 0.1 --units-per-day 5000
    sequentialist ab report STORAGE EXPERIMENT [--json]
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from sequentialist.core import Engine, json_schema, load_spec, open_storage

from .analysis import Analysis
from .assign import config
from .experiment import Experiment
from .plan import plan
from .spec import ExperimentSpec


def _check(args: argparse.Namespace) -> int:
    spec = load_spec(args.spec, ExperimentSpec)
    analysis = Analysis(spec)
    print(f"{args.spec}: experiment {spec.experiment_id}, spec id {spec.id}")
    print(f"  multiplicity over primary and secondary metrics: {analysis.multiplicity}")
    for pair in analysis.pairs:
        decision = f"decision {pair.decision_kind} ({pair.decision_guarantee})" if pair.decision_stream else \
            "guardrail"
        print(f"  {pair.name:<32} {pair.metric.type:<10} {pair.metric.role:<9} interval at level "
              f"{pair.interval_level:.4g} ({pair.interval_guarantee}); {decision}")
    return 0


def _schema(args: argparse.Namespace) -> int:
    print(json.dumps(json_schema(ExperimentSpec), indent=2, ensure_ascii=False))
    return 0


def _client_config(args: argparse.Namespace) -> int:
    print(json.dumps(config(load_spec(args.spec, ExperimentSpec)).to_json(), indent=2, ensure_ascii=False))
    return 0


def _plan(args: argparse.Namespace) -> int:
    spec = load_spec(args.spec, ExperimentSpec)
    baseline: Any = args.baseline
    if args.baseline_file:
        with open(args.baseline_file, encoding="utf-8") as f:
            baseline = [float(line) for line in f if line.strip()]
    print(json.dumps(plan(spec, baseline=baseline, units_per_day=args.units_per_day, effect=args.effect,
                          runs=args.runs, seed=args.seed), indent=2))
    return 0


def _report(args: argparse.Namespace) -> int:
    with open_storage(args.storage) as storage:
        experiment = Experiment.open(Engine(storage), args.experiment)
        try:
            report = experiment.report()
        finally:
            experiment.close()
    print(json.dumps(report.to_json(), indent=2, ensure_ascii=False) if args.json else report.to_markdown())
    return 0


def register(commands: Any) -> None:
    parser = commands.add_parser("ab", help="A/B experiments")
    sub = parser.add_subparsers(dest="ab_command", required=True)
    p = sub.add_parser("check", help="validate a spec and show its analysis")
    p.add_argument("spec")
    p.set_defaults(handler=_check)
    p = sub.add_parser("schema", help="print the JSON Schema of the spec format")
    p.set_defaults(handler=_schema)
    p = sub.add_parser("client-config", help="print the configuration for @sequentialist/ab-client")
    p.add_argument("spec")
    p.set_defaults(handler=_client_config)
    p = sub.add_parser("plan", help="power and time to decision of the primary metric")
    p.add_argument("spec")
    p.add_argument("--baseline", type=float, help="the control's conversion rate")
    p.add_argument("--baseline-file", help="past values of single units, one per line (bounded metrics)")
    p.add_argument("--units-per-day", type=float, required=True)
    p.add_argument("--effect", type=float, help="the effect to detect (default: the MDE)")
    p.add_argument("--runs", type=int, default=200)
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(handler=_plan)
    p = sub.add_parser("report", help="the report of a stored experiment")
    p.add_argument("storage")
    p.add_argument("experiment")
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=_report)

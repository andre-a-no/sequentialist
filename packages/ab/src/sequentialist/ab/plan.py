# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Before an experiment: how long it takes and how often it finds the MDE; whether the pipeline holds its error rate.

`plan` simulates the primary metric's decision procedure, at the level the
experiment will use, with `planning.simulate` of sequential-analysis: the
power to detect the MDE within the maximum duration, and the distribution
of the time to a decision.  With e-BH the first rejection needs the same
evidence as a Bonferroni test at alpha / (number of pairs), so the plan
uses that level: conservative when several metrics are affected.

`aa_check` runs the whole pipeline (assignment, attribution windows,
every stream, the decision rules) on real units with fresh salts, so that
no treatment has any effect, and counts how often it ships a variant,
claims an effect, or raises a sample ratio mismatch.  Each count comes
with its Monte Carlo interval.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Sequence

from sequential_analysis.planning import simulate
from sequential_analysis.procedures import (
    BoundedABTest,
    GroupSequentialProportions,
    GroupSequentialTwoMeans,
    TwoProportionTest,
)

from sequentialist.core import parse_spec, proportion, replay

from .analysis import Analysis
from .assign import config
from .experiment import KIND, ExperimentHandler, event, exposure
from .spec import ExperimentSpec


def _rejects(output: Any) -> bool:
    return getattr(output, "decision", None) == "reject_h0"


def plan(spec: ExperimentSpec, *, baseline: float | Sequence[float], units_per_day: float,
         effect: float | None = None, runs: int = 200, seed: int = 0) -> dict[str, Any]:
    """Power and time to decision of the primary metric, for the first treatment against the control.

    `baseline` is the control's conversion rate, or, for a bounded metric,
    past values of single units (resampled; the effect is added and the
    result clipped to the bounds).  `units_per_day` counts all units of the
    experiment; `effect` defaults to the MDE, in the good direction.
    """
    m = spec.primary
    if m.type not in ("conversion", "bounded"):
        raise ValueError("planning covers conversion and bounded primary metrics in this version")
    analysis = Analysis(spec)
    pair = analysis.primary_pairs()[0]
    t, c = pair.variant, spec.control
    w = spec.weights
    allocation = w[t] / (w[t] + w[c])
    pair_per_day = units_per_day * (w[t] + w[c]) / sum(w)
    days = float(spec.data["max_duration"]) / 86400.0
    max_n = max(1, int(pair_per_day * days))
    delta = m.sign * float(effect if effect is not None else (m.mde or 0.0))
    planned = spec.data["monitoring"]["mode"] == "planned"
    stream = analysis.streams[pair.decision_stream or pair.interval_stream]
    procedure = stream.procedure
    if m.type == "conversion":
        if not isinstance(baseline, (int, float)) or not 0 < baseline < 1:
            raise ValueError("baseline must be the control's conversion rate, in (0, 1)")
        p_c = float(baseline)
        p_t = p_c + delta
        if not 0 < p_t < 1:
            raise ValueError(f"baseline {p_c} with an effect of {delta} leaves (0, 1)")
        if not planned:
            procedure = TwoProportionTest(pair.interval_level if analysis.multiplicity == "Bonferroni" else
                                          analysis.alpha / max(1, sum(1 for p in analysis.pairs
                                                                      if p.decision_kind == "ebh")),
                                          arms=(1, 0), input="y")

        def draw(rnd: random.Random) -> dict[str, Any]:
            treated = rnd.random() < allocation
            return {"arm": 1 if treated else 0, "y": int(rnd.random() < (p_t if treated else p_c))}
    else:
        if isinstance(baseline, (int, float)):
            raise ValueError("for a bounded metric, baseline must be past values of single units")
        values = [float(v) for v in baseline]
        if not values:
            raise ValueError("baseline must hold past values of single units")
        lo, hi = (float(b) for b in m.raw["bounds"])
        if not planned:
            procedure = BoundedABTest(lo, hi, allocation, pair.interval_level, arms=(1, 0), input="y",
                                      backend=spec.data["backend"])

        def draw(rnd: random.Random) -> dict[str, Any]:
            treated = rnd.random() < allocation
            y = rnd.choice(values) + (delta if treated else 0.0)
            return {"arm": 1 if treated else 0, "y": min(max(y, lo), hi)}

    if planned:
        procedure = _relabelled(procedure)
    summary = simulate(lambda: procedure, draw, runs=runs, max_n=max_n, seed=seed, stop=_rejects)
    times = summary.stopping_times

    def quantile(q: float) -> float | None:
        return None if not times else times[min(len(times) - 1, math.ceil(q * len(times)) - 1)] / pair_per_day

    lo_power, hi_power = summary.power_interval
    return {
        "metric": m.name, "variant": pair.variant_name, "effect": delta, "procedure": procedure.name,
        "guarantee": procedure.guarantee, "level": getattr(procedure, "alpha", None),
        "max_units": max_n, "max_days": days, "runs": runs,
        "power": summary.power, "power_interval": [lo_power, hi_power],
        "days_to_decision": {"median": quantile(0.5), "p90": quantile(0.9)},
        "note": "Power and times of the primary metric alone, for one treatment; e-BH and several affected "
                "metrics can only make decisions earlier.",
    }


def _relabelled(gs: Any) -> Any:
    """The planned group sequential test with arms (1, 0) and input "y", as the simulation draws them."""
    config = gs.config()
    common = {"futility": config.get("futility"), "two_sided": config["two_sided"],
              "alternative": config["alternative"], "arms": (1, 0), "input": "y"}
    if isinstance(gs, GroupSequentialProportions):
        return GroupSequentialProportions(config["analyses"], config["bounds"], **common)
    assert isinstance(gs, GroupSequentialTwoMeans)
    return GroupSequentialTwoMeans(config["sigma"], config["analyses"], config["bounds"], **common)


@dataclass(frozen=True)
class Unit:
    """A unit's history without its variant: when it was exposed, and its events (seconds after, name, fields)."""

    time: float
    events: Sequence[tuple[float, str, dict[str, Any]]] = ()


def aa_check(spec: ExperimentSpec, units: Sequence[Unit], *, runs: int = 20, seed: int = 0,
             level: float = 0.05) -> dict[str, Any]:
    """The real pipeline on real units, re-randomised: how often does it ship, claim an effect, or alarm?"""
    if not units:
        raise ValueError("need units")
    ordered = sorted(units, key=lambda u: u.time)
    shipped = claimed = srm = 0
    decisions = []
    for i in range(runs):
        data = dict(spec.data)
        data.update(salt=f"{spec.data['salt']}/aa/{seed + i}", assignment="engine", overrides={}, traffic=[],
                    start=ordered[0].time, id=f"{spec.data['id']}-aa-{seed + i}"[:128])
        aa = parse_spec({"kind": KIND, **data}, ExperimentSpec)
        assignment = config(aa)
        n = 0
        timeline: list[tuple[float, int, Any]] = []
        for k, unit in enumerate(ordered):
            name = f"u{k}"
            chosen = assignment.assign(name, unit.time)
            if chosen.variant is None:
                continue
            timeline.append((unit.time, 0, exposure(name, chosen.variant, unit.time, source="aa", seq=n,
                                                    probability=chosen.probability)))
            n += 1
            for after, event_name, fields in unit.events:
                timeline.append((unit.time + after, 1, event(name, event_name, unit.time + after, source="aa", seq=n,
                                                             **fields)))
                n += 1
        timeline.sort(key=lambda item: (item[0], item[1], item[2].seq))
        inputs = [item[2] for item in timeline]
        inputs.append(ordered[0].time + float(aa.data["max_duration"]) + float(aa.data["attribution_window"])
                      + float(aa.data["lateness"]) + 1.0)
        task = replay(KIND, aa.to_json(), ExperimentHandler(aa), inputs, task_id="aa")
        handler = task.handler
        assert isinstance(handler, ExperimentHandler)
        decision = handler.decision or {}
        decisions.append(decision)
        shipped += decision.get("decision") == "ship"
        srm += decision.get("reason") == "sample_ratio_mismatch"
        claimed += any(v != 0 for v in handler.significant.values())
        task.close()
    return {
        "runs": runs,
        "shipped": proportion(shipped, runs, level).to_json(),
        "claimed_an_effect": proportion(claimed, runs, level).to_json(),
        "sample_ratio_mismatch": proportion(srm, runs, level).to_json(),
        "decisions": decisions,
        "alpha": spec.data["alpha"],
        "srm_alpha": spec.data["srm"]["alpha"],
    }


__all__ = ["Unit", "aa_check", "plan"]

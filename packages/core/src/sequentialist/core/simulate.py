# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Simulations of a whole pipeline: how often it decides what, with the Monte Carlo error stated.

`planning.simulate` of sequential-analysis gives the operating
characteristics of one procedure.  A library's pipeline adds more:
aggregation of raw events, several streams, decision rules.  Its error
rates are measured by running the real pipeline many times
(`run_simulations`), each run with its own seeded random generator, and
counting outcomes (`proportion`).  The interval of `proportion` is the
Wilson interval sequential-analysis uses for simulated power.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, TypeVar

from sequential_analysis.planning import SimulationSummary

T = TypeVar("T")


def run_simulations(trial: Callable[[random.Random, int], T], runs: int, *, seed: int = 0) -> list[T]:
    """`trial(random.Random(seed + i), i)` for i in 0..runs-1: reproducible, and each run independent."""
    if type(runs) is not int or runs < 1:
        raise ValueError("runs must be a positive int")
    return [trial(random.Random(seed + i), i) for i in range(runs)]


@dataclass(frozen=True)
class Proportion:
    """How often something happened in simulated runs."""

    hits: int
    runs: int
    level: float = 0.05  # the interval covers with probability 1 - level (Monte Carlo error)

    @property
    def rate(self) -> float:
        return self.hits / self.runs

    @property
    def interval(self) -> tuple[float, float]:
        summary = SimulationSummary(self.runs, 0, self.hits, (), self.level)
        return summary.power_interval

    def to_json(self) -> dict:
        lo, hi = self.interval
        return {"hits": self.hits, "runs": self.runs, "rate": self.rate, "interval": [lo, hi],
                "level": self.level}


def proportion(hits: int, runs: int, level: float = 0.05) -> Proportion:
    if not (type(runs) is int and runs >= 1 and type(hits) is int and 0 <= hits <= runs):
        raise ValueError("need 0 <= hits <= runs, runs >= 1")
    return Proportion(hits, runs, level)

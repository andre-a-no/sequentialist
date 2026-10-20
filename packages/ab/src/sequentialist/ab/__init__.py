# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""sequentialist.ab: the engine of an A/B testing platform.

spec        the experiment spec (``kind: ab.experiment/1``) and its checks
assign      deterministic assignment, identical in TypeScript (@sequentialist/ab-client)
experiment  exposures and events in, attribution windows, analysis streams, decisions, audit
analysis    which procedures of sequential-analysis, at which levels, with which guarantees
report      the report: decision, sample ratio, metrics, guardrails, data quality
plan        power and duration before the experiment; A/A on the real pipeline

Every statistical conclusion comes from sequential-analysis and states its
guarantee (exact, asymptotic, ...); this package does not compute tests.
"""
from .analysis import Analysis
from .assign import Assignment, AssignmentConfig, assign, config, uniform
from .experiment import Experiment, ExperimentHandler, event, exposure
from .plan import Unit, aa_check, plan
from .spec import KIND, ExperimentSpec, Metric
from .version import __version__

__all__ = [
    "KIND",
    "Analysis",
    "Assignment",
    "AssignmentConfig",
    "Experiment",
    "ExperimentHandler",
    "ExperimentSpec",
    "Metric",
    "Unit",
    "__version__",
    "aa_check",
    "assign",
    "config",
    "event",
    "exposure",
    "plan",
    "uniform",
]

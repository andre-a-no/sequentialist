# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC (Kazan, Tatarstan, Russian Federation)
"""Detectors: change-point procedures of sequential-analysis, built from configuration.

A detector is configured by the name of a procedure and its parameters, as
written in the procedure's own `config()`: the family of CUSUM is
``{family = "gaussian", sigma = 0.14}``, exactly what `Gaussian.spec()`
returns.  The configuration of a monitor and the identity in its
checkpoints are therefore the same object, and a changed configuration is
recognised when the checkpoint is restored.

Every detector here stops at its alarm (its state becomes terminal); the
monitor then starts a new segment.  The observations reach the procedure
as the input ``x``.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from sequential_analysis import Procedure
from sequential_analysis.procedures import (
    CUSUM,
    BayesianChangePoint,
    Bernoulli,
    BetaBernoulliModel,
    Exponential,
    Gaussian,
    MeanEDetector,
    NormalGammaModel,
    Poisson,
    PoissonGammaModel,
    QuantileEDetector,
    ShiryaevRoberts,
)

INPUT = "x"


class DetectorError(ValueError):
    """A detector cannot be built from its configuration."""


def _family(spec: Any):
    if not isinstance(spec, Mapping) or "family" not in spec:
        raise DetectorError('family must be a table such as {family = "gaussian", sigma = 1.0}')
    params = {k: v for k, v in spec.items() if k != "family"}
    kinds: dict[str, Callable[..., Any]] = {
        "gaussian": Gaussian, "bernoulli": Bernoulli, "poisson": Poisson, "exponential": Exponential,
    }
    if spec["family"] not in kinds:
        raise DetectorError(f"unknown family {spec['family']!r}; one of {sorted(kinds)}")
    return _call(kinds[spec["family"]], params, f"family {spec['family']!r}")


def _model(spec: Any):
    if not isinstance(spec, Mapping) or "model" not in spec:
        raise DetectorError('model must be a table such as {model = "normal_gamma", mu0 = 0.0}')
    params = {k: v for k, v in spec.items() if k != "model"}
    kinds: dict[str, Callable[..., Any]] = {
        "normal_gamma": NormalGammaModel, "poisson_gamma": PoissonGammaModel, "beta_bernoulli": BetaBernoulliModel,
    }
    if spec["model"] not in kinds:
        raise DetectorError(f"unknown model {spec['model']!r}; one of {sorted(kinds)}")
    return _call(kinds[spec["model"]], params, f"model {spec['model']!r}")


def _call(factory: Callable[..., Any], params: Mapping[str, Any], what: str):
    try:
        return factory(**params)
    except TypeError as error:  # an unknown or missing parameter
        raise DetectorError(f"{what}: {error}") from None
    except ValueError as error:  # a parameter out of range
        raise DetectorError(f"{what}: {error}") from None


def _likelihood_ratio(cls):
    def build(params: dict[str, Any]) -> Procedure:
        params = dict(params)
        params["family"] = _family(params.get("family"))
        return _call(cls, params, cls.name)
    return build


def _bayesian(params: dict[str, Any]) -> Procedure:
    params = dict(params)
    params["model"] = _model(params.get("model"))
    return _call(BayesianChangePoint, params, BayesianChangePoint.name)


def _plain(cls):
    def build(params: dict[str, Any]) -> Procedure:
        return _call(cls, params, cls.name)
    return build


#: Detector name (the procedure's `name`) -> builder from parameters.
DETECTORS: dict[str, Callable[[dict[str, Any]], Procedure]] = {
    CUSUM.name: _likelihood_ratio(CUSUM),
    ShiryaevRoberts.name: _likelihood_ratio(ShiryaevRoberts),
    MeanEDetector.name: _plain(MeanEDetector),
    QuantileEDetector.name: _plain(QuantileEDetector),
    BayesianChangePoint.name: _bayesian,
}


def build_detector(name: str, params: Mapping[str, Any]) -> Procedure:
    """The procedure named `name`, configured with `params`; DetectorError if that fails."""
    if name not in DETECTORS:
        raise DetectorError(f"unknown detector {name!r}; one of {sorted(DETECTORS)}")
    if "input" in params:
        raise DetectorError(f"the input of a detector is always {INPUT!r}; remove 'input'")
    procedure = DETECTORS[name](dict(params))
    if name == BayesianChangePoint.name and getattr(procedure, "alarm_probability", None) is None:
        raise DetectorError("bayesian_change_point needs alarm_probability: without it, it never alarms")
    return procedure


def alarm_fields(output: Any) -> dict[str, Any]:
    """What an alert reports from the output that raised it: the statistic and the change estimate."""
    keep = ("statistic", "log_statistic", "change_estimate", "recent_change_probability",
            "most_likely_run_length", "segment_mean")
    return {k: getattr(output, k) for k in keep if hasattr(output, k)}

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""sequentialist.quality: sequential acceptance sampling of lots, in the setting of Wald.

spec     plans (``quality.plan/1``) and schemes of inspection (``quality.scheme/1``)
plans    optimal truncated plans and single sampling, designed and characterised by sequential-analysis
scheme   lots in, accept/reject out; switching normal/tightened/reduced; rework and resubmission; supplier watch
report   the inspection record of a lot and the report of a scheme, each conclusion with its guarantee

Not a substitute for the quality management of the plant: the decisions are
those of the plans, under their stated models, and every action of people
(dispositions, concessions) is recorded with who and why.
"""
from .plans import Point, characteristic, compile_plan, design, verify
from .scheme import Scheme, SchemeHandler, item, lot
from .spec import PLAN_KIND, SCHEME_KIND, PlanSpec, SchemeSpec
from .version import __version__

__all__ = [
    "PLAN_KIND",
    "SCHEME_KIND",
    "PlanSpec",
    "Point",
    "Scheme",
    "SchemeHandler",
    "SchemeSpec",
    "__version__",
    "characteristic",
    "compile_plan",
    "design",
    "item",
    "lot",
    "verify",
]

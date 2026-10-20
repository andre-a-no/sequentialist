# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Synthetic experiments for the tests: units arrive, are assigned, convert, spend and see errors."""
import random

from sequentialist.ab import ExperimentSpec, config, event, exposure
from sequentialist.core import parse_spec

DAY = 86400.0


def make_spec(**changes):
    data = {
        "kind": "ab.experiment/1", "id": "exp", "salt": "salt-1", "start": 0,
        "variants": [{"name": "A", "control": True}, {"name": "B"}],
        "attribution_window": "1d", "max_duration": "28d",
        "metrics": [{"name": "purchase", "type": "conversion", "role": "primary", "event": "purchase", "mde": 0.02}],
    }
    data.update(changes)
    return parse_spec(data, ExperimentSpec)


def simulate(spec, units, *, rates=None, spend=None, errors=None, every=60.0, seed=0, assign=True, split=None):
    """Records of `units` units, one exposure every `every` seconds.

    rates:  variant -> conversion probability (event "purchase")
    spend:  variant -> mean amount of a purchase (field "amount", exponential)
    errors: variant -> errors per page view (Poisson-ish, 3 page views each)
    assign: True: the engine's hash; False: `split` (variant -> probability) draws the variant
    """
    rnd = random.Random(seed)
    assignment = config(spec)
    names = spec.variant_names
    records, seq = [], 0
    for i in range(units):
        t = i * every
        unit = f"user-{seed}-{i}"
        if assign:
            variant = assignment.assign(unit, t).variant
            if variant is None:
                continue
        else:
            draw, acc, variant = rnd.random(), 0.0, names[-1]
            for name in names:
                acc += split[name]
                if draw < acc:
                    variant = name
                    break
        records.append(exposure(unit, variant, t, source="log", seq=seq))
        seq += 1
        if errors is not None:
            for k in range(3):
                records.append(event(unit, "page_view", t + 10 + k, source="log", seq=seq))
                seq += 1
                if rnd.random() < errors[variant]:
                    records.append(event(unit, "error", t + 11 + k, source="log", seq=seq))
                    seq += 1
        if rates is not None and rnd.random() < rates[variant]:
            fields = {"amount": rnd.expovariate(1.0 / spend[variant])} if spend else {}
            records.append(event(unit, "purchase", t + 3600, source="log", seq=seq, **fields))
            seq += 1
    records.sort(key=lambda r: (r.time, r.seq))
    return records


def run(experiment, records, batch=500):
    for i in range(0, len(records), batch):
        experiment.ingest(records[i:i + batch])
        if experiment.decision is not None:
            break
    return experiment.decision

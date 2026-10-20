# sequentialist-ab

The engine of an A/B testing platform: specs of experiments, assignment of
units, attribution windows, anytime-valid analysis with the procedures of
[sequential-analysis](https://github.com/andre-a-no/sequential-analysis),
decisions (ship, stop, continue) with an audit trail, and reports in which
every conclusion names its guarantee. It is meant to be embedded in a
platform that owns the user interface, the event pipeline and the feature
flags.

```
pip install sequentialist-ab              # Python 3.8+
```

## The spec

```json
{
  "kind": "ab.experiment/1",
  "id": "checkout-button",
  "salt": "checkout-button-2026-11",
  "variants": [{"name": "A", "control": true}, {"name": "B"}],
  "traffic": [{"from": "2026-11-01T00:00:00Z", "share": 0.1}, {"from": "2026-11-03T00:00:00Z", "share": 1}],
  "attribution_window": "7d",
  "max_duration": "28d",
  "alpha": 0.05,
  "metrics": [
    {"name": "purchase", "type": "conversion", "role": "primary", "event": "purchase", "mde": 0.005},
    {"name": "revenue", "type": "bounded", "event": "purchase", "value": "amount", "bounds": [0, 500]},
    {"name": "clicks_per_view", "type": "ratio", "numerator": {"event": "click"}, "denominator": {"event": "view"}},
    {"name": "errors", "type": "rate", "role": "guardrail", "event": "error", "exposure": {"event": "view"},
     "direction": "decrease", "margin": 0.1}
  ]
}
```

`sequentialist ab schema` prints the JSON Schema (also in
`schemas/ab.experiment-1.schema.json`); `sequentialist ab check SPEC`
validates a spec and shows its analysis.

## Analysis and guarantees

| Metric type | Effect | Procedure (sequential-analysis) | Guarantee |
|---|---|---|---|
| conversion | difference of rates | `TwoProportionTest` decides; `BoundedABTest` on [0, 1] gives the interval | exact |
| bounded | difference of means, values clipped to the declared bounds (the number clipped is reported) | `BoundedABTest` | exact |
| ratio | relative lift of the ratio of sums | `RatioABTest` | **asymptotic** |
| rate (events per exposure) | relative change of the rate | `RateRatioTest` | exact |
| sample ratio mismatch | — | `SampleRatioMismatch`, on every exposure | exact |
| planned looks (primary metric) | — | `GroupSequentialProportions`, `GroupSequentialTwoMeans` | asymptotic |

Every comparison is a treatment against the control. Claims of an effect on
the primary and secondary metrics are corrected over metrics and
treatments: by e-BH (`MultipleTests`, false discovery rate) when every
metric reports an e-process, which in sequential-analysis 0.4 holds for
conversions only, and otherwise by Bonferroni (family-wise error; exact and
conservative). Guardrails form their own family at `guardrail_alpha`.

**Decisions**, after every closed attribution window, written to the audit
log with the streams they rest on: stop on a sample ratio mismatch; drop a
treatment whose guardrail shows harm beyond its margin, whose primary
metric is shown worse, or whose primary interval excludes the MDE
(futility, non-binding); ship a treatment whose primary metric improved
and whose guardrails are all shown non-inferior; stop at the maximum
duration or when no treatment is left; `stop` by hand with an actor and a
reason.

## Assignment

`assign(spec, unit, time)` and `@sequentialist/ab-client` (npm, Apache-2.0)
compute the same variant from the salt and the unit's pseudonym, bit for
bit, checked by the shared vectors. Raising the traffic share keeps every
unit already in; overrides serve QA and are left out of the analysis. With
`"assignment": "engine"` the engine checks every exposure against the hash
and counts mismatches; with `"external"` the platform assigns and only the
sample ratio test checks the split.

## Planning and A/A

`plan(spec, baseline=0.1, units_per_day=5000)` simulates the primary
metric's decision procedure at the level the experiment will use: power
to find the MDE within the maximum duration and days to a decision.
`aa_check(spec, units)` runs the whole pipeline on real units with fresh
salts, so that nothing differs, and counts how often it ships or claims an
effect, with Monte Carlo intervals.

## Example on open data

`examples/hillstrom.py` takes the MineThatData e-mail experiment (Hillstrom
2008: 64,000 customers, men's e-mail, women's e-mail, no e-mail), simulates
the arrival of the customers over 14 days, and runs the experiment to its
decision: ship the men's e-mail on visits (exact, Bonferroni over three
metrics and two treatments), after about 6,800 of the 64,000 customers.

## Known limits of 0.1

* `BoundedABTest`, which gives the intervals of conversion and bounded
  metrics, does not adapt to the variance of the data: its half-width was
  about 0.039 for 20,000 units whether the conversion rate was 0.6 % or
  50 %, 36 times the standard error at 0.6 %. Guardrails on rare
  conversions therefore cannot be shown non-inferior at realistic margins
  (on the Hillstrom data, a margin of 0.5 percentage points on purchases
  was not reached with 64,000 customers), and intervals on revenue are
  wide. This is a property of the procedure in sequential-analysis 0.4,
  reported upstream.
* `TwoProportionTest` reports no interval, so a conversion's decision and
  its interval come from two procedures; the report says so where they
  disagree.
* Throughput is bounded by `BoundedABTest` (about 0.6 ms per unit and
  comparison in pure Python, 0.2 ms with `"backend": "numpy"`).
* The handler keeps one entry per unit seen (16 hex digits), written at
  every checkpoint: experiments of up to about a million units per task.

License: GNU AGPL v3 or commercial; see COMMERCIAL.md.

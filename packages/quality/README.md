# sequentialist-quality

Sequential acceptance sampling of lots, in the setting in which Wald
introduced sequential analysis: inspect items one at a time and decide
about the lot as soon as the evidence suffices. Plans are designed and
characterised by
[sequential-analysis](https://github.com/andre-a-no/sequential-analysis);
this package runs them lot after lot, with switching rules, rework and
resubmission, decisions of people, an audit trail and inspection records.

```
pip install sequentialist-quality         # Python 3.8+
```

## Plans

A plan (`quality.plan/1`) says what an item gives and which risks are
accepted:

```json
{
  "kind": "quality.plan/1",
  "name": "fasteners-normal",
  "characteristic": {"type": "attributes"},
  "aql": 0.01,
  "lq": 0.05,
  "method": {"type": "sequential", "producer_risk": 0.05, "consumer_risk": 0.10, "max_sample": 300}
}
```

| characteristic | an item gives | quality level | operating characteristic |
|---|---|---|---|
| `attributes` | 1 if defective, else 0 | fraction defective | exact (binomial) |
| `nonconformities` | the number of nonconformities | nonconformities per item | exact (Poisson) |
| `variables` | a measurement, normal with known sigma, against one specification limit | fraction beyond the limit | on a lattice: approximate |

`sequentialist quality design plan.json -o plan.designed.json` computes the
plan once (a few seconds): for `sequential`, the optimal truncated plan of
`design.kiefer_weiss_plan` (at most `max_sample` items, both risks at most
as specified, the smallest average number of items at Lorden's point or, with
`optimal_at: least-favourable`, the smallest maximum); for `single`, n items
and an acceptance number c, with curtailment. The designed plan stores its
boundaries and its operating characteristic at `aql` and `lq`, so executing
it needs no computation, and the plan in the audit trail is the plan that
was executed. `sequentialist quality oc` prints the characteristic at any
quality, `sequentialist quality check` recomputes it and finds changed
boundaries.

For the example plans (`examples/`), at the same risks (5 % at 1 %
defective, 10 % at 5 % defective):

| fraction defective | sequential: P(accept) | sequential: average items | single n = 132, c = 3: P(accept) | items (curtailed) |
|---|---|---|---|---|
| 0.5 % | 0.993 | 74.5 | 0.996 | 131.9 |
| 1 % | 0.950 | 84.8 | 0.956 | 130.6 |
| 2.5 % | 0.566 | 92.7 | 0.580 | 115.0 |
| 5 % | 0.099 | 61.5 | 0.099 | 77.1 |

## Schemes

A scheme (`quality.scheme/1`) inspects one product or supplier lot after lot:
designed plans for normal, tightened and (optionally) reduced inspection;
switching between them (normal to tightened when 2 of the last 5 lots were
rejected, back after 5 accepted, discontinuation after 5 rejections on
tightened inspection: configurable); rework and resubmission of rejected
lots with the tightened plan; a CUSUM watch on the supplier across lots.

```python
from sequentialist.core import Engine, SQLiteStorage, load_spec
from sequentialist.quality import Scheme, SchemeSpec, item, lot

scheme = Scheme.create(Engine(SQLiteStorage("inspection.db")), load_spec("scheme.json", SchemeSpec))
scheme.ingest([lot("L-117", time, size=20000, source="station-1", seq=1)])
scheme.ingest([item("L-117", 0, time, source="station-1", seq=2)])     # until the plan decides
scheme.lot("L-117")["status"]                                          # "inspecting", "accepted", "rejected"
scheme.dispose("L-117", "rework", actor="lead", reason="sorted")       # or scrap, screen, return, concession
print(scheme.protocol("L-117").to_markdown())                          # the inspection record
```

Every decision (accept, reject, switch, discontinue, supplier alarm) is in
the hash-chained audit log with the plan, its operating characteristic and
the history digest of the run that executed it; every action of people
(dispositions, concessions, resuming) with their name and reason.
`examples/incoming_inspection.py` runs a supplier through a deterioration
and a recovery.

## Limits of 0.1

* The operating characteristic is that of a process or of a lot much larger
  than the sample (binomial, "type B"). For small lots sampled without
  replacement the hypergeometric probabilities ("type A") differ; they are
  not computed yet.
* Average outgoing quality (AOQ, AOQL) and average total inspection (ATI)
  under rectifying inspection are not computed yet: they need the
  distribution of the number of items by decision, which
  sequential-analysis does not report yet.
* Variables plans assume a normal measurement with a known standard
  deviation and one specification limit.
* Plans are designed with sequential-analysis rather than taken from the
  tables of ISO 2859 or ISO 39511; the tables of the standards are not
  reproduced here.

License: GNU AGPL v3 or commercial; see COMMERCIAL.md.

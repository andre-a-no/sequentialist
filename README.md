# sequentialist

Applications of sequential statistics, built on
[sequential-analysis](https://github.com/andre-a-no/sequential-analysis).
The statistics live in sequential-analysis; these libraries describe a task,
take its data, decide, keep an audit trail and report — and every
conclusion they report names its guarantee (`exact`, `asymptotic`, `model`,
`approximate` or `none`), as the procedure behind it states it.

| Package | Import | Status |
|---|---|---|
| [`sequentialist-core`](packages/core) | `sequentialist.core` | 0.1.0: specs, the checkpointed engine, storage, audit log, reports, simulations |
| [`sequentialist-ab`](packages/ab) | `sequentialist.ab` | 0.1.0: the engine of an A/B testing platform |
| [`@sequentialist/ab-client`](ts/ab-client) (npm) | | 0.1.0: assignment of variants in browsers and Node.js, identical to Python |
| [`sequentialist-quality`](packages/quality) | `sequentialist.quality` | 0.1.0: sequential acceptance sampling of lots |
| `sequentialist-monitor` | `sequentialist.monitor` | planned: equipment, processes and energy |
| `sequentialist-markets` | `sequentialist.markets` | planned: risk, backtests, strategy decay |
| `sequentialist-clinical` | `sequentialist.clinical` | planned: trials, drug safety, quality of care |
| `sequentialist-release` | `sequentialist.release` | planned: canary releases |
| `sequentialist-mlops` | `sequentialist.mlops` | planned: models in production |

The packages are installed separately and share the `sequentialist`
namespace. Each runs on Python 3.8 or later and needs nothing but
sequential-analysis (PyYAML optionally, for specs in YAML).

```
pip install sequentialist-ab          # brings sequentialist-core and sequential-analysis
```

## An experiment, from spec to decision

```python
from sequentialist.ab import Experiment, ExperimentSpec, event, exposure
from sequentialist.core import Engine, SQLiteStorage, load_spec

spec = load_spec("experiment.json", ExperimentSpec)      # variants, metrics, alpha, windows...
engine = Engine(SQLiteStorage("experiments.db"))
experiment = Experiment.create(engine, spec)              # later: Experiment.open(engine, "checkout")

experiment.ingest([
    exposure(unit, "B", time, source="exposures", seq=offset),        # unit: a pseudonym
    event(unit, "purchase", time, source="events", seq=offset, amount=23.5),
])
experiment.decision        # None, or {"decision": "ship", "variant": "B", "reason": "primary_success", ...}
print(experiment.report().to_markdown())
```

`packages/ab/examples/hillstrom.py` runs the whole path on open data (the
MineThatData e-mail experiment, 64,000 customers).

## What the engine guarantees

* **Idempotent intake**: a record is identified by its source and sequence
  number; sending it again changes nothing.
* **Nothing accepted is lost**: records are journaled before they are
  processed; after a crash the task is restored from its checkpoint and the
  journal is replayed, and the result is the uninterrupted run bit for bit
  (tested on every storage, with crashes at every point that matters).
* **An audit log of every decision**, chained by SHA-256, with the
  procedure, its guarantee, the step and the history digest of
  sequential-analysis that each decision rests on (`sequentialist audit`
  verifies it).
* **One writer per task**, enforced by locks and version checks.

## Licenses

The libraries are dual-licensed: GNU AGPL v3 or a commercial license
([COMMERCIAL.md](COMMERCIAL.md)). `@sequentialist/ab-client` and the test
vectors in `vectors/` are under the Apache License 2.0, so that the
assignment can run in any front end. Contributions need the
[Contributor License Agreement](CLA.md); see [CONTRIBUTING.md](CONTRIBUTING.md).

Contact: andrei@hobukob.ru, a.hobukob@gmail.com.

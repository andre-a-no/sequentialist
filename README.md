# sequentialist

A service that watches streams of metrics and raises an alert soon after
one of them changes, and rarely when nothing has. The detectors are the
sequential change-point procedures of
[sequential-analysis](https://github.com/andre-a-no/sequential-analysis);
sequentialist adds what a long-running service needs around them:
configuration, an HTTP API, a durable journal, checkpoints, alerts, and
re-arming after an alarm.

> Procedure infers; consumers act.

Status: pre-alpha. The formats on disk and the API may still change.

## What it does

* **One monitor per stream.** Each monitor runs one detector over the
  values pushed to it, in the order they arrive.
* **Detectors with stated guarantees.** Each one reports what it promises
  (`guarantee`):

  | Detector | You know | Guarantee |
  |---|---|---|
  | `mean_e_detector` | a bound on the values and their mean before the change | exact: average run length to a false alarm ≥ `arl` for *every* such distribution |
  | `quantile_e_detector` | a quantile (median, p95) before the change | exact, for any distribution |
  | `cusum`, `shiryaev_roberts` | both distributions (normal, Bernoulli, Poisson, exponential) | exact under the model |
  | `bayesian_change_point` | nothing; a conjugate prior | a posterior; no false-alarm guarantee |

  The [monitoring tutorial](https://andre-a-no.github.io/sequential-analysis/tutorials/monitoring/)
  of sequential-analysis compares them on the same data.
* **Re-arming.** A detector stops at its alarm. The monitor records the
  alert and starts a new *segment* with the next value: a new run whose
  provenance names the run that alarmed. With `rearm = false` it stops.
* **Nothing is lost in a crash.** Every accepted value is written to a
  journal before the detector sees it. The run is checkpointed every
  `checkpoint_every` values and at every alarm. On start, the last
  checkpoint is restored and the journal is replayed into it. The result
  is bit for bit the run that would have happened without the crash
  (`tests/test_monitor.py`).
* **A changed configuration is not silently applied** to a saved run. The
  detector's parameters are part of its checkpoint, and a mismatch stops
  the service with a message.

## Installation

```
pip install .            # Python 3.11 or later; depends only on sequential-analysis
```

## Use

```
sequentialist check examples/monitors.toml     # validate the configuration
sequentialist serve examples/monitors.toml     # run the service
```

```
curl -s localhost:8080/v1/monitors/api-latency/observations -d '{"values": [0.21, 0.18, 0.25]}'
{"accepted": 3, "invalid": 0, "alerts": []}
```

Tune a detector on history before deploying it: `replay` runs one monitor
over a file of past values (one per line, or the first column of a CSV)
exactly as the service would, and prints its alarms.

```
sequentialist replay examples/monitors.toml api-latency latency.csv
```

## Configuration

One TOML file; see [`examples/monitors.toml`](examples/monitors.toml).

```toml
[service]
state_dir = "state"          # relative to this file
host = "127.0.0.1"
port = 8080
checkpoint_every = 1000      # values between checkpoints
sync = "fsync"               # the journal: "fsync" survives a power failure, "flush" a crash

[[sink]]
type = "webhook"             # also "log" and "jsonl" (path = ...)
url = "https://alerts.example/hook"

[[monitor]]
name = "api-latency"
detector = "mean_e_detector"
params = { m = 0.2, lo = 0.0, hi = 1.0, arl = 10000, direction = "increase", statistic = "cusum" }
```

`params` are the parameters of the procedure in sequential-analysis,
written as its `config()` writes them: a family is
`{ family = "gaussian", sigma = 0.14 }`, a model
`{ model = "normal_gamma", mu0 = 0.5 }`.

## HTTP API

| Method and path | |
|---|---|
| `GET /healthz` | liveness |
| `GET /v1/monitors` | every monitor's status |
| `GET /v1/monitors/{name}` | status: detector, segment, run, values seen, alarms, last output |
| `GET /v1/monitors/{name}/alerts?limit=100` | the latest alerts |
| `POST /v1/monitors/{name}/observations` | `{"values": [...]}` or `{"observations": [{"value": v, "time": t}, ...]}` |

A value outside the detector's contract (above `hi`, a string, a boolean)
is skipped and counted in `invalid`. A stopped or failed monitor answers
409.

There is no authentication: keep the service on localhost, or behind a
proxy that authenticates.

## Alerts

Every alert is appended to the monitor's `alerts.jsonl` before anything
else happens; that file is the record. Sinks are notifications: `log`,
`jsonl`, and `webhook` (POSTed as JSON from a background thread, with
retries). A crash between an alarm's checkpoint and its notification
loses the notification, not the alert.

An alert:

```json
{"id": "api-latency/0@447", "monitor": "api-latency", "run_id": "api-latency/0", "segment": 0,
 "t": 447, "seq": 446, "time": null, "detector": "mean_e_detector",
 "details": {"statistic": 10412.7, "change_estimate": 402}, "raised_at": 1760990000.0}
```

## State on disk

```
state/<monitor>/state.json       segment, sequence counter, and the run's checkpoint
state/<monitor>/journal.jsonl    values accepted since that checkpoint
state/<monitor>/alerts.jsonl     every alert
state/<monitor>/segments/N.json  the final checkpoint of each segment that alarmed
```

Checkpoints are those of sequential-analysis (`sequential-analysis.checkpoint/2`):
a segment can be continued, audited or replayed with the library itself, in
Python or in its TypeScript executor.

## Development

```
python -m pip install -e .
python -m unittest discover -s tests -t .
ruff check . && mypy
```

## License

GNU AGPL v3 ([LICENSE](LICENSE)). Copyright (C) 2026 Novikov Laboratories LLC.

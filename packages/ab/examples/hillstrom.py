# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""From spec to decision on open data: the MineThatData e-mail experiment (Hillstrom, 2008).

64,000 customers were randomised to a men's e-mail, a women's e-mail or no
e-mail, and their visits, purchases and spend in the following two weeks
were recorded.  The data have no timestamps, so this example simulates the
arrival of the customers (evenly over 14 days, in a seeded random order)
and lets each customer's recorded outcome happen an hour after the e-mail.
The outcomes are the dataset's own.

    python examples/hillstrom.py                    # downloads the data (4 MB) once
    python examples/hillstrom.py --data hillstrom.csv --out results --aa 20

The data are read from the publisher's site and not redistributed with this
package; the file is checked against its SHA-256.

What it shows: the spec (examples/hillstrom.json), pseudonymised customer
ids, the engine with an SQLite store, the decision and the audit entry it
rests on, the report (Markdown and JSON), and, with --aa, the same pipeline
on customers without e-mail, re-randomised, to see how often it ships a
variant when nothing works.
"""
import argparse
import csv
import hashlib
import json
import random
import sys
import tempfile
import urllib.request
from pathlib import Path

from sequentialist.ab import Experiment, ExperimentSpec, Unit, aa_check, event, exposure
from sequentialist.core import Engine, SQLiteStorage, load_spec, pseudonymize, verify_audit

URL = "http://www.minethatdata.com/Kevin_Hillstrom_MineThatData_E-MailAnalytics_DataMiningChallenge_2008.03.20.csv"
SHA256 = "0e5893329d8b93cefecc571777672028290ab69865718020c78c7284f291aece"
HERE = Path(__file__).resolve().parent
VARIANTS = {"No E-Mail": "no-email", "Mens E-Mail": "mens", "Womens E-Mail": "womens"}
KEY = b"example key: in production, a secret that stays with the data owner"
DAY = 86400.0
T0 = 1205971200.0  # 2008-03-20T00:00:00Z, the date of the dataset: the simulated timeline starts there


def load(path):
    if path is None:
        path = Path(tempfile.gettempdir()) / "hillstrom-2008.csv"
        if not path.exists():
            print(f"downloading {URL}", file=sys.stderr)
            urllib.request.urlretrieve(URL, path)
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise SystemExit(f"{path}: not the expected file (SHA-256 differs)")
    return list(csv.DictReader(data.decode("utf-8").splitlines()))


def timeline(rows, seed=2008):
    """Customers in a random order, evenly over 14 days; each with (time, variant, visit, purchase, spend)."""
    order = list(range(len(rows)))
    random.Random(seed).shuffle(order)
    step = 14 * DAY / len(rows)
    for k, i in enumerate(order):
        r = rows[i]
        yield (pseudonymize(f"customer-{i}", KEY), T0 + k * step, VARIANTS[r["segment"]], int(r["visit"]),
               int(r["conversion"]), float(r["spend"]))


def records(customers):
    out, seq = [], 0
    for unit, t, variant, visit, purchase, spend in customers:
        out.append(exposure(unit, variant, t, source="example", seq=seq))
        seq += 1
        if visit:
            out.append(event(unit, "visit", t + 3600, source="example", seq=seq))
            seq += 1
        if purchase:
            out.append(event(unit, "purchase", t + 7200, source="example", seq=seq, amount=spend))
            seq += 1
    out.sort(key=lambda r: (r.time, r.seq))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", help="the CSV file (downloaded if not given)")
    parser.add_argument("--out", default=None, help="directory for the store and the reports (default: temporary)")
    parser.add_argument("--aa", type=int, default=0, help="A/A runs on the customers without e-mail")
    args = parser.parse_args()

    rows = load(args.data)
    spec = load_spec(HERE / "hillstrom.json", ExperimentSpec)
    customers = list(timeline(rows))
    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="hillstrom-"))
    out.mkdir(parents=True, exist_ok=True)
    store = out / "experiments.db"
    if store.exists():
        store.unlink()

    with SQLiteStorage(store) as storage:
        experiment = Experiment.create(Engine(storage, clock="event"), spec)
        data = records(customers)
        for i in range(0, len(data), 2000):
            experiment.ingest(data[i:i + 2000])
            if experiment.decision is not None:
                break
        decision = experiment.decision
        report = experiment.report()
        entries = [e for e in experiment.task.audit() if e.kind == "decision"]
        experiment.close()
        checked = verify_audit(storage.audit_read(spec.experiment_id), spec.experiment_id)

    (out / "report.md").write_text(report.to_markdown(), encoding="utf-8")
    (out / "report.json").write_text(json.dumps(report.to_json(), indent=2), encoding="utf-8")
    print(report.to_markdown())
    print(f"\ndecision: {json.dumps(decision)}")
    if entries:
        last = entries[-1]
        print(f"audit entry {last.n} ({checked} entries, chain verified): {last.decision} {json.dumps(last.basis)}")
        for e in last.evidence:
            print(f"  evidence: {e['stream']} {e['procedure']['name']} ({e['guarantee']}) t={e['t']} "
                  f"history {e['history_digest'][:16]}…")
    print(f"store and reports in {out}")

    if args.aa:
        control = [c for c in customers if c[2] == "no-email"][:3000]
        units = [Unit(t, ([(3600.0, "visit", {})] if visit else []) +
                      ([(7200.0, "purchase", {"amount": spend})] if purchase else []))
                 for _, t, _, visit, purchase, spend in control]
        result = aa_check(spec, units, runs=args.aa)
        print("\nA/A on 3,000 customers without e-mail, re-randomised into three variants:")
        for name in ("shipped", "claimed_an_effect", "sample_ratio_mismatch"):
            p = result[name]
            print(f"  {name}: {p['hits']} of {p['runs']} runs, 95% interval [{p['interval'][0]:.3f}, "
                  f"{p['interval'][1]:.3f}]")


if __name__ == "__main__":
    main()

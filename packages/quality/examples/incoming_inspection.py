# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Incoming inspection of a supplier whose quality deteriorates, then recovers.

The supplier's lots are 0.8 % defective at first, 4 % from the 16th lot (a
worn tool), and 0.5 % again from the 31st (after corrective action).  The
example runs the scheme of `scheme.json` (sequential plans for normal and
tightened inspection, switching rules, rework and resubmission, a CUSUM
watch on the supplier) and prints what happened, lot by lot, with the
inspection record of one lot.  The data are simulated; the plans and their
characteristics are the real ones.

    python examples/incoming_inspection.py
"""
import json
import random
from pathlib import Path

from sequentialist.core import Engine, MemoryStorage, load_spec
from sequentialist.quality import Scheme, SchemeSpec, item, lot

HERE = Path(__file__).resolve().parent


def main():
    spec = load_spec(HERE / "scheme.json", SchemeSpec)
    scheme = Scheme.create(Engine(MemoryStorage(), clock="event"), spec)
    rnd = random.Random(2026)
    seq, time = 0, 1_793_491_200.0  # 2026-11-01

    def send(record):
        nonlocal seq, time
        scheme.ingest([record])
        seq, time = seq + 1, time + 30.0

    def inspect(lot_id, p):
        send(lot(lot_id, time, size=20000, source="station-1", seq=seq))
        while (scheme.lot(lot_id) or {}).get("status") == "inspecting":
            send(item(lot_id, int(rnd.random() < p), time, source="station-1", seq=seq))
        return (scheme.lot(lot_id) or {}).get("status", "refused")

    total = 0
    print("lot   quality  severity   items  decision")
    for k in range(1, 46):
        p = 0.008 if k <= 15 else 0.04 if k <= 30 else 0.005
        severity = scheme.severity if not scheme.handler.discontinued else "-"
        lot_id = f"L{k:03d}"
        status = inspect(lot_id, p)
        history = scheme.lot(lot_id)
        n = history["presentations"][-1]["n"] if history else 0
        total += n
        print(f"{lot_id}  {p:6.1%}   {severity:<9} {n:>5}  {status}")
        if status == "rejected":
            scheme.dispose(lot_id, "rework", actor="incoming inspection", reason="returned to the supplier for sorting")
            again = inspect(lot_id, 0.002)
            print(f"{lot_id}  resubmitted after sorting: {again}")
        if scheme.handler.discontinued and k == 30:
            scheme.resume(actor="supplier quality engineer", reason="tool replaced, 8D report accepted")
            print("      acceptance resumed on tightened inspection")
    print(f"\n{total} items inspected for 45 lots; a single sampling plan with the same risks takes 132 per lot "
          f"({132 * 45} for 45).")
    events = [e for e in scheme.task.audit() if e.decision in ("switch", "discontinue", "supplier_alarm")]
    for e in events:
        shown = {k: v for k, v in e.basis.items() if k in ("from", "to", "lot")}
        print(f"audit {e.n}: {e.decision} {json.dumps(shown)}")
    rejected = next((x for x in scheme.handler.lots if len(scheme.handler.lots[x]["presentations"]) > 1), None)
    if rejected:
        print("\n" + scheme.protocol(rejected).to_markdown())
    scheme.close()


if __name__ == "__main__":
    main()

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Write vectors/assignment.json: the assignments every implementation must reproduce exactly.

    python tools/make_vectors.py           # write
    python tools/make_vectors.py --check   # fail if the file differs from what the code gives
"""
import argparse
import json
import sys
from pathlib import Path

from sequentialist.ab.assign import AssignmentConfig, uniform

OUT = Path(__file__).resolve().parent.parent / "vectors" / "assignment.json"

CONFIGS = [
    {"salt": "s1", "variants": [{"name": "A", "weight": 1}, {"name": "B", "weight": 1}]},
    {"salt": "exp-2026-11", "variants": [{"name": "control", "weight": 1}, {"name": "x", "weight": 2},
                                         {"name": "y", "weight": 3}]},
    {"salt": "ключ эксперимента", "variants": [{"name": "a", "weight": 0.1}, {"name": "b", "weight": 0.2},
                                                 {"name": "c", "weight": 0.7}]},
    {"salt": "🙂 salt\twith\nspaces", "variants": [{"name": f"v{i}", "weight": 1 + i % 3} for i in range(5)]},
    {"salt": "ramp", "variants": [{"name": "A", "weight": 1}, {"name": "B", "weight": 1}],
     "traffic": [{"from": 1000.0, "share": 0.1}, {"from": 2000.0, "share": 0.5}, {"from": 3000.0, "share": 1.0}]},
    {"salt": "window", "variants": [{"name": "A", "weight": 3}, {"name": "B", "weight": 1}],
     "overrides": {"qa-1": "B", "qa-2": "A"}, "start": 1793491200.0, "end": 1795910400.0},
]
UNITS = [f"u{i}" for i in range(40)] + ["пользователь-1", "🙂", "a" * 200, "0" * 64, "qa-1", "qa-2",
                                        "3f2a9c1e5b7d4a6f8e0c2b4d6f8a0c2e4b6d8f0a2c4e6b8d0f2a4c6e8b0d2f4a"]
TIMES = [None, 500.0, 1500.0, 2500.0, 3500.0, 1793491199.0, 1793491200.0, 1795910399.5, 1795910400.0]


def vectors():
    cases = []
    configs = []
    for index, c in enumerate(CONFIGS):
        config = AssignmentConfig.from_json(c)
        configs.append(config.to_json())
        needs_time = bool(c.get("traffic")) or c.get("start") is not None
        for unit in UNITS:
            for time in TIMES:
                if (time is None) == needs_time:  # without a ramp or a window, time is not needed
                    continue
                result = config.assign(unit, time)
                cases.append({"config": index, "unit": unit, "time": time, **result.to_json()})
    uniforms = [{"domain": d, "salt": c["salt"], "unit": u, "value": uniform(d, c["salt"], u)}
                for c in CONFIGS for u in UNITS[:10] + UNITS[40:43] for d in ("traffic", "variant")]
    return {
        "format": "sequentialist.vectors.assignment/1",
        "description": "Assignment of units to variants (sequentialist.ab.assign, @sequentialist/ab-client). "
                       "Every case must be reproduced exactly: variant, reason and probability (binary64).",
        "uniform": uniforms,
        "configs": configs,
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    data = vectors()
    lines = [json.dumps({k: v for k, v in data.items() if k not in ("uniform", "configs", "cases")},
                        ensure_ascii=False)[:-1] + ","]
    for key in ("uniform", "configs", "cases"):  # one item per line: readable diffs
        items = ",\n".join("  " + json.dumps(item, ensure_ascii=False) for item in data[key])
        lines.append(f' "{key}": [\n{items}\n ]' + ("," if key != "cases" else ""))
    text = "\n".join(lines) + "\n}\n"
    if args.check:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print(f"{OUT} differs from what the code gives; run tools/make_vectors.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

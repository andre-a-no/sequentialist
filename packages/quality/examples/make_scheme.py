# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Design the plans of the examples and write the scheme with the designed plans in it.

    python examples/make_scheme.py           # writes *.designed.json and scheme.json
    python examples/make_scheme.py --check   # fails if the stored files differ from a new design

Designing a sequential plan takes a few seconds; afterwards a scheme executes
the stored boundaries and needs no computation.
"""
import argparse
import json
import sys
from pathlib import Path

from sequentialist.core import load_spec
from sequentialist.quality import PlanSpec, SchemeSpec, design

HERE = Path(__file__).resolve().parent


def dump(value) -> str:
    return json.dumps(value, indent=1, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    outputs = {}
    designed = {}
    for path in sorted(HERE.glob("*.plan.json")):
        plan = design(load_spec(path, PlanSpec))
        designed[path.name] = plan.to_json()
        outputs[HERE / path.name.replace(".plan.json", ".designed.json")] = dump(plan.to_json())
    template = json.loads((HERE / "scheme.template.json").read_text(encoding="utf-8"))
    template["plans"] = {k: designed[v] for k, v in template["plans"].items()}
    load_spec(template, SchemeSpec)  # the result must be a valid scheme
    outputs[HERE / "scheme.json"] = dump(template)
    status = 0
    for path, text in outputs.items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                print(f"{path} differs from a new design; run examples/make_scheme.py", file=sys.stderr)
                status = 1
        else:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path}")
    return status


if __name__ == "__main__":
    sys.exit(main())

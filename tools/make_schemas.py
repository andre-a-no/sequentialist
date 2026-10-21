# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Write schemas/*.schema.json from the spec classes (--check: fail if they differ)."""
import argparse
import json
import sys
from pathlib import Path

from sequentialist.ab import ExperimentSpec
from sequentialist.core import json_schema
from sequentialist.quality import PlanSpec, SchemeSpec

OUT = Path(__file__).resolve().parent.parent / "schemas"
KINDS = [ExperimentSpec, PlanSpec, SchemeSpec]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    status = 0
    for kind in KINDS:
        path = OUT / f"{kind.kind.replace('/', '-')}.schema.json"
        text = json.dumps(json_schema(kind), ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                print(f"{path} differs from what the code gives; run tools/make_schemas.py", file=sys.stderr)
                status = 1
        else:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path}")
    return status


if __name__ == "__main__":
    sys.exit(main())

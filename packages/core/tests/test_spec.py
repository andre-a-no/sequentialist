# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Schemas and specs: normal forms, every error at once with its pointer, ids, JSON Schema, files."""
import json
import tempfile
import unittest
from pathlib import Path

from sequentialist.core import Spec, SpecError, json_schema, load_spec, parse_spec, spec_id
from sequentialist.core.schema import (
    Array,
    Boolean,
    Duration,
    Field,
    Map,
    Nullable,
    Number,
    Object,
    OneOf,
    String,
    Timestamp,
    format_timestamp,
    parse_timestamp,
)

try:
    import yaml
except ImportError:  # PyYAML is optional
    yaml = None


class Toy(Spec):
    kind = "test.toy/1"
    schema = Object({
        "name": Field(String(pattern=r"[a-z]+")),
        "window": Field(Duration()),
        "start": Field(Nullable(Timestamp()), default=None),
        "share": Field(Number(exclusive_minimum=0, maximum=1), default=1.0),
        "count": Field(Number(integer=True, minimum=1), default=3),
        "flag": Field(Boolean(), default=False),
        "tags": Field(Array(String(), max_items=3), default=list),
        "weights": Field(Map(Number(minimum=0), key_pattern=r"[a-z]+"), default=dict),
        "mode": Field(OneOf("type", {"a": Object({}), "b": Object({"k": Field(Number())})}),
                      default=lambda: {"type": "a"}),
    })

    def problems(self):
        if self.data["count"] > 10 and not self.data["flag"]:
            return [("/count", "more than 10 needs flag")]
        return []


class SchemaTest(unittest.TestCase):
    def test_normal_form_and_id(self):
        a = parse_spec({"kind": "test.toy/1", "name": "x", "window": "7d", "start": "2026-11-01T03:00:00+03:00"}, Toy)
        b = parse_spec({"kind": "test.toy/1", "name": "x", "window": 604800, "start": 1793491200,
                        "share": 1, "mode": {"type": "a"}}, Toy)
        self.assertEqual(a.data["window"], 604800)
        self.assertEqual(a.data["start"], 1793491200.0)
        self.assertEqual(a.data["tags"], [])
        self.assertEqual(spec_id(a), spec_id(b))
        self.assertEqual(parse_spec(a.to_json(), Toy).data, a.data)  # the normal form is a fixed point
        self.assertNotEqual(spec_id(a), spec_id(parse_spec({**a.to_json(), "count": 4}, Toy)))

    def test_every_error_at_once_with_pointers(self):
        with self.assertRaises(SpecError) as raised:
            parse_spec({"kind": "test.toy/1", "name": "X", "window": "soon", "share": 0, "count": 1.5,
                        "tags": ["a", "b", "c", 4], "weights": {"ok": -1, "Bad": 1}, "mode": {"type": "b"},
                        "extra": True}, Toy)
        errors = dict(raised.exception.errors)
        self.assertEqual(sorted(errors), sorted(["/name", "/window", "/share", "/count", "/tags", "/tags/3",
                                                 "/weights/ok", "/weights/Bad", "/mode/k", "/extra"]))
        self.assertIn("unknown member", errors["/extra"])
        self.assertIn("required", errors["/mode/k"])

    def test_kind_and_semantic_problems(self):
        with self.assertRaisesRegex(SpecError, "/kind: expected 'test.toy/1'"):
            parse_spec({"kind": "test.toy/2", "name": "x", "window": 1}, Toy)
        with self.assertRaisesRegex(SpecError, "/count: more than 10 needs flag"):
            parse_spec({"kind": "test.toy/1", "name": "x", "window": 1, "count": 11}, Toy)

    def test_timestamps(self):
        self.assertEqual(parse_timestamp("1970-01-01T00:00:01.5Z"), 1.5)
        self.assertEqual(format_timestamp(1793491200.0), "2026-11-01T00:00:00Z")
        for bad in ("2026-11-01", "2026-11-01T00:00:00", "yesterday"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_timestamp(bad)

    def test_json_schema(self):
        schema = json_schema(Toy)
        self.assertEqual(schema["properties"]["kind"]["enum"], ["test.toy/1"])
        self.assertEqual(schema["required"], ["kind", "name", "window"])
        self.assertFalse(schema["additionalProperties"])
        json.dumps(schema)

    def test_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "toy.json"
            path.write_text('{"kind": "test.toy/1", "name": "x", "window": "1h"}')
            self.assertEqual(load_spec(path, Toy).data["window"], 3600)
            path.write_text('{"kind": "test.toy/1", "name": "x", "window": NaN}')
            with self.assertRaisesRegex(SpecError, "toy.json.*not valid JSON"):
                load_spec(path, Toy)
            if yaml is not None:
                path = Path(tmp) / "toy.yaml"
                path.write_text("kind: test.toy/1\nname: x\nwindow: 2h\nstart: 2026-11-01T00:00:00Z\n")
                spec = load_spec(path, Toy)
                self.assertEqual((spec.data["window"], spec.data["start"]), (7200, 1793491200.0))


if __name__ == "__main__":
    unittest.main()

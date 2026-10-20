# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Specs: the declarative description of a task (an experiment, a monitor, a trial).

A kind of spec is a subclass of `Spec` with a `kind` (``"ab.experiment/1"``:
the name and the version of the format) and a `schema`.  Reading a spec
checks it against the schema, then against the subclass's own rules
(`problems`), and reports every error found at once, each with its JSON
pointer::

    spec = load_spec("experiment.yaml", ExperimentSpec)
    spec.data          # the normal form: plain JSON, defaults filled in
    spec_id(spec)      # SHA-256 of its canonical JSON

JSON needs nothing; YAML needs PyYAML (``pip install "sequentialist-core[yaml]"``).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar, Mapping, TypeVar

from .canonical import digest
from .errors import SpecError
from .schema import INVALID, Errors, Field, Object, String

S = TypeVar("S", bound="Spec")


class Spec:
    """Base class of every kind of spec.  Subclasses set `kind` and `schema` and may override `problems`."""

    kind: ClassVar[str] = ""
    schema: ClassVar[Object]

    def __init__(self, data: dict[str, Any]):
        """`data` is the normal form (as `parse_spec` returns it, without "kind")."""
        self.data = data

    def problems(self) -> list[tuple[str, str]]:
        """Errors the schema cannot express (relations between members), as (JSON pointer, message)."""
        return []

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, **self.data}

    @property
    def id(self) -> str:
        """The spec id: SHA-256 of the canonical JSON of the normal form."""
        return spec_id(self)


def _full_schema(cls: type[Spec]) -> Object:
    fields = {"kind": Field(String(enum=[cls.kind]), doc=f'The format of the spec: "{cls.kind}".')}
    fields.update(cls.schema.fields)
    return Object(fields, doc=cls.schema.doc)


def parse_spec(data: Any, cls: type[S], source: str = "") -> S:
    """A spec of kind `cls` from parsed JSON or YAML; SpecError lists every problem."""
    errors: Errors = []
    if isinstance(data, dict) and data.get("kind") != cls.kind:
        errors.append(("/kind", f"expected {cls.kind!r}, got {data.get('kind')!r}"))
        raise SpecError(errors, source)
    normal = _full_schema(cls).check(data, "", errors)
    if errors or normal is INVALID:
        raise SpecError(errors, source)
    del normal["kind"]
    spec = cls(normal)
    errors = list(spec.problems())
    if errors:
        raise SpecError(errors, source)
    return spec


def read_document(source: str | Path) -> Any:
    """Parse a JSON or YAML file (by its extension) into plain values."""
    path = Path(source)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            raise SpecError([("", "reading YAML needs PyYAML: pip install \"sequentialist-core[yaml]\"")],
                            str(path)) from None
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise SpecError([("", f"not valid YAML: {error}")], str(path)) from None
    try:
        return json.loads(text, parse_constant=_no_constant)
    except ValueError as error:
        raise SpecError([("", f"not valid JSON: {error}")], str(path)) from None


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON number")


def load_spec(source: str | Path | Mapping[str, Any], cls: type[S]) -> S:
    """A spec from a file (.json, .yaml, .yml) or from an already parsed mapping."""
    if isinstance(source, Mapping):
        return parse_spec(dict(source), cls)
    return parse_spec(read_document(source), cls, str(source))


def spec_id(spec: Spec) -> str:
    return digest(spec.to_json())


def json_schema(cls: type[Spec]) -> dict[str, Any]:
    """JSON Schema (draft 2020-12) of the spec format: for editors, other languages and validators."""
    out = _full_schema(cls).json_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://github.com/andre-a-no/sequentialist/schemas/{cls.kind.replace('/', '-')}.schema.json",
        "title": cls.kind,
        **out,
    }

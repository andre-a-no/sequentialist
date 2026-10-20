# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Declarative schemas for specs: validation with every error at once, normalisation, JSON Schema.

A schema checks a parsed JSON (or YAML) value and returns its *normal
form*: defaults filled in, durations in seconds, timestamps in seconds
since the epoch.  The normal form is plain JSON, checks again to itself,
and is what a spec id is computed from: two spellings of the same spec
(``"7d"`` and ``604800``) have the same id.

Errors are collected, not raised one by one: `check` appends
``(JSON pointer, message)`` to a list and returns `INVALID` for a value it
cannot use, so a user sees every problem of a spec in one go.
"""
from __future__ import annotations

import datetime as _dt
import math
import re
from typing import Any, List, Mapping, Sequence, Tuple

Errors = List[Tuple[str, str]]


class _Invalid:
    def __repr__(self) -> str:
        return "INVALID"


#: Returned by `Schema.check` for a value that could not be used.
INVALID: Any = _Invalid()
#: Default of a field that has none.
MISSING: Any = object()


def pointer(path: str, key: Any) -> str:
    """The JSON pointer of `key` (a member name or an index) below `path`."""
    return f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}"


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "a boolean"
    if type(value) in (int, float):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "an object"
    return type(value).__name__


class Schema:
    """Base class: `check` validates and normalises, `json_schema` describes."""

    def __init__(self, doc: str = ""):
        self.doc = doc

    def check(self, value: Any, path: str, errors: Errors) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def _describe(self) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    def json_schema(self) -> dict[str, Any]:
        out = self._describe()
        if self.doc:
            out["description"] = self.doc
        return out


class String(Schema):
    def __init__(self, *, pattern: str | None = None, enum: Sequence[str] | None = None, min_length: int = 0,
                 max_length: int | None = None, doc: str = ""):
        super().__init__(doc)
        self.pattern = pattern
        self._regex = re.compile(pattern) if pattern is not None else None
        self.enum = tuple(enum) if enum is not None else None
        self.min_length, self.max_length = min_length, max_length

    def check(self, value: Any, path: str, errors: Errors) -> Any:
        if not isinstance(value, str):
            errors.append((path, f"expected a string, got {_type_name(value)}"))
            return INVALID
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            errors.append((path, "not valid Unicode text"))
            return INVALID
        if self.enum is not None and value not in self.enum:
            errors.append((path, f"{value!r} is not one of {list(self.enum)}"))
            return INVALID
        if len(value) < self.min_length:
            errors.append((path, "must not be empty" if self.min_length == 1 else
                           f"must have at least {self.min_length} characters"))
            return INVALID
        if self.max_length is not None and len(value) > self.max_length:
            errors.append((path, f"must have at most {self.max_length} characters"))
            return INVALID
        if self._regex is not None and not self._regex.fullmatch(value):
            errors.append((path, f"{value!r} does not match {self.pattern}"))
            return INVALID
        return value

    def _describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "string"}
        if self.enum is not None:
            out["enum"] = list(self.enum)
        if self.pattern is not None:
            out["pattern"] = f"^(?:{self.pattern})$"
        if self.min_length:
            out["minLength"] = self.min_length
        if self.max_length is not None:
            out["maxLength"] = self.max_length
        return out


class Number(Schema):
    def __init__(self, *, minimum: float | None = None, maximum: float | None = None,
                 exclusive_minimum: float | None = None, exclusive_maximum: float | None = None,
                 integer: bool = False, doc: str = ""):
        super().__init__(doc)
        self.minimum, self.maximum = minimum, maximum
        self.exclusive_minimum, self.exclusive_maximum = exclusive_minimum, exclusive_maximum
        self.integer = integer

    def check(self, value: Any, path: str, errors: Errors) -> Any:
        if type(value) not in (int, float):
            errors.append((path, f"expected {'an integer' if self.integer else 'a number'}, got {_type_name(value)}"))
            return INVALID
        if not math.isfinite(value):
            errors.append((path, "must be finite"))
            return INVALID
        if self.integer:
            if value != int(value) or abs(value) > 2**53:
                errors.append((path, f"expected an integer within 2**53, got {value!r}"))
                return INVALID
            value = int(value)
        elif type(value) is int and abs(value) > 2**53:
            errors.append((path, "integers beyond 2**53 are not exact in JSON"))
            return INVALID
        for bound, ok, text in (
            (self.minimum, lambda b: value >= b, "at least"),
            (self.maximum, lambda b: value <= b, "at most"),
            (self.exclusive_minimum, lambda b: value > b, "greater than"),
            (self.exclusive_maximum, lambda b: value < b, "less than"),
        ):
            if bound is not None and not ok(bound):
                errors.append((path, f"must be {text} {bound}, got {value!r}"))
                return INVALID
        return value

    def _describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "integer" if self.integer else "number"}
        for key, bound in (("minimum", self.minimum), ("maximum", self.maximum),
                           ("exclusiveMinimum", self.exclusive_minimum),
                           ("exclusiveMaximum", self.exclusive_maximum)):
            if bound is not None:
                out[key] = bound
        return out


class Boolean(Schema):
    def check(self, value: Any, path: str, errors: Errors) -> Any:
        if type(value) is not bool:
            errors.append((path, f"expected true or false, got {_type_name(value)}"))
            return INVALID
        return value

    def _describe(self) -> dict[str, Any]:
        return {"type": "boolean"}


class Nullable(Schema):
    def __init__(self, inner: Schema, doc: str = ""):
        super().__init__(doc or inner.doc)
        self.inner = inner

    def check(self, value: Any, path: str, errors: Errors) -> Any:
        return None if value is None else self.inner.check(value, path, errors)

    def _describe(self) -> dict[str, Any]:
        inner = self.inner._describe()
        return {"anyOf": [inner, {"type": "null"}]}


class Array(Schema):
    def __init__(self, items: Schema, *, min_items: int = 0, max_items: int | None = None, doc: str = ""):
        super().__init__(doc)
        self.items, self.min_items, self.max_items = items, min_items, max_items

    def check(self, value: Any, path: str, errors: Errors) -> Any:
        if not isinstance(value, (list, tuple)):
            errors.append((path, f"expected a list, got {_type_name(value)}"))
            return INVALID
        if len(value) < self.min_items:
            errors.append((path, f"needs at least {self.min_items} item{'s' if self.min_items != 1 else ''}"))
        if self.max_items is not None and len(value) > self.max_items:
            errors.append((path, f"has at most {self.max_items} items"))
        out = [self.items.check(v, pointer(path, i), errors) for i, v in enumerate(value)]
        return INVALID if any(v is INVALID for v in out) else out

    def _describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "array", "items": self.items.json_schema()}
        if self.min_items:
            out["minItems"] = self.min_items
        if self.max_items is not None:
            out["maxItems"] = self.max_items
        return out


class Map(Schema):
    """An object with arbitrary keys (matching `key_pattern`) and values of one schema."""

    def __init__(self, values: Schema, *, key_pattern: str | None = None, doc: str = ""):
        super().__init__(doc)
        self.values, self.key_pattern = values, key_pattern
        self._regex = re.compile(key_pattern) if key_pattern is not None else None

    def check(self, value: Any, path: str, errors: Errors) -> Any:
        if not isinstance(value, dict):
            errors.append((path, f"expected an object, got {_type_name(value)}"))
            return INVALID
        out = {}
        for key, item in value.items():
            where = pointer(path, key)
            if not isinstance(key, str) or (self._regex is not None and not self._regex.fullmatch(key)):
                errors.append((where, f"key {key!r} does not match {self.key_pattern}"))
                out[str(key)] = INVALID
                continue
            out[key] = self.values.check(item, where, errors)
        return INVALID if any(v is INVALID for v in out.values()) else out

    def _describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "object", "additionalProperties": self.values.json_schema()}
        if self.key_pattern is not None:
            out["propertyNames"] = {"pattern": f"^(?:{self.key_pattern})$"}
        return out


class Field:
    """A member of an Object: its schema, whether it is required, its default."""

    def __init__(self, schema: Schema, *, default: Any = MISSING, doc: str = ""):
        self.schema, self.default = schema, default
        if doc:
            schema.doc = doc

    @property
    def required(self) -> bool:
        return self.default is MISSING


class Object(Schema):
    def __init__(self, fields: Mapping[str, Field], *, doc: str = ""):
        super().__init__(doc)
        self.fields = dict(fields)

    def check(self, value: Any, path: str, errors: Errors) -> Any:
        if not isinstance(value, dict):
            errors.append((path, f"expected an object, got {_type_name(value)}"))
            return INVALID
        out: dict[str, Any] = {}
        unknown = [key for key in value if key not in self.fields]
        for key in unknown:
            errors.append((pointer(path, key), f"unknown member; known: {', '.join(sorted(self.fields))}"))
        for name, member in self.fields.items():
            where = pointer(path, name)
            if name in value:
                out[name] = member.schema.check(value[name], where, errors)
            elif member.required:
                errors.append((where, "required"))
                out[name] = INVALID
            else:
                default = member.default() if callable(member.default) else member.default
                out[name] = member.schema.check(default, where, errors) if default is not None else None
        return INVALID if unknown or any(v is INVALID for v in out.values()) else out

    def _describe(self) -> dict[str, Any]:
        properties = {}
        for name, member in self.fields.items():
            described = member.schema.json_schema()
            if not member.required and not callable(member.default):
                described["default"] = member.default
            properties[name] = described
        out: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
        required = [name for name, member in self.fields.items() if member.required]
        if required:
            out["required"] = required
        return out


class OneOf(Schema):
    """One of several objects, told apart by the value of the member `tag`."""

    def __init__(self, tag: str, variants: Mapping[str, Object], *, doc: str = ""):
        super().__init__(doc)
        self.tag, self.variants = tag, dict(variants)

    def check(self, value: Any, path: str, errors: Errors) -> Any:
        if not isinstance(value, dict):
            errors.append((path, f"expected an object, got {_type_name(value)}"))
            return INVALID
        tag = value.get(self.tag)
        if tag not in self.variants:
            errors.append((pointer(path, self.tag), f"must be one of {sorted(self.variants)}"))
            return INVALID
        rest = {k: v for k, v in value.items() if k != self.tag}
        out = self.variants[tag].check(rest, path, errors)
        return INVALID if out is INVALID else {self.tag: tag, **out}

    def _describe(self) -> dict[str, Any]:
        options = []
        for tag, variant in self.variants.items():
            described = variant.json_schema()
            described["properties"] = {self.tag: {"const": tag}, **described["properties"]}
            described["required"] = [self.tag, *described.get("required", [])]
            options.append(described)
        return {"oneOf": options}


_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(s|m|h|d|w)")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class Duration(Schema):
    """A duration: ``"90s"``, ``"15m"``, ``"12h"``, ``"7d"``, ``"2w"``, or a number of seconds (the normal form)."""

    def __init__(self, *, allow_zero: bool = False, doc: str = ""):
        super().__init__(doc)
        self.allow_zero = allow_zero

    def check(self, value: Any, path: str, errors: Errors) -> Any:
        if type(value) in (int, float):
            seconds = value
        elif isinstance(value, str) and _DURATION.fullmatch(value.strip()):
            match = _DURATION.fullmatch(value.strip())
            assert match is not None
            seconds = float(match.group(1)) * _UNITS[match.group(2)]
            if seconds == int(seconds):
                seconds = int(seconds)
        else:
            errors.append((path, f"expected a duration such as \"7d\", \"12h\" or a number of seconds, "
                                 f"got {value!r}"))
            return INVALID
        if not math.isfinite(seconds) or seconds < 0 or (seconds == 0 and not self.allow_zero):
            errors.append((path, "must be positive" if not self.allow_zero else "must not be negative"))
            return INVALID
        return seconds

    def _describe(self) -> dict[str, Any]:
        return {"anyOf": [{"type": "string", "pattern": r"^\d+(\.\d+)?\s*(s|m|h|d|w)$"},
                          {"type": "number", "minimum": 0}]}


_TIMESTAMP = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(\.\d{1,9})?([Zz]|[+-]\d{2}:\d{2})")


def parse_timestamp(text: str) -> float:
    """Seconds since the epoch of an RFC 3339 timestamp with a time zone; ValueError if it is not one."""
    match = _TIMESTAMP.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"{text!r} is not an RFC 3339 timestamp such as 2026-11-01T00:00:00Z")
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    zone = match.group(8)
    offset = 0 if zone in ("Z", "z") else (1 if zone[0] == "+" else -1) * (int(zone[1:3]) * 60 + int(zone[4:6]))
    moment = _dt.datetime(year, month, day, hour, minute, second, tzinfo=_dt.timezone(_dt.timedelta(minutes=offset)))
    whole = int(moment.timestamp())
    fraction = match.group(7)
    return whole + float(fraction) if fraction else float(whole)


def format_timestamp(seconds: float) -> str:
    """RFC 3339, UTC, to the second (fractions are kept only if present)."""
    moment = _dt.datetime.fromtimestamp(seconds, _dt.timezone.utc)
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    fraction = seconds - math.floor(seconds)
    if fraction:
        text += f"{fraction:.6f}"[1:].rstrip("0")
    return text + "Z"


class Timestamp(Schema):
    """A moment: an RFC 3339 string with a time zone, or seconds since the epoch.  Normal form: seconds."""

    def check(self, value: Any, path: str, errors: Errors) -> Any:
        if type(value) in (int, float) and math.isfinite(value):
            return value
        if isinstance(value, _dt.datetime):  # YAML reads unquoted timestamps as datetimes
            if value.tzinfo is None:
                errors.append((path, "a timestamp needs a time zone (for example a trailing Z)"))
                return INVALID
            return value.timestamp()
        if isinstance(value, str):
            try:
                return parse_timestamp(value)
            except ValueError as error:
                errors.append((path, str(error)))
                return INVALID
        errors.append((path, f"expected an RFC 3339 timestamp or seconds since the epoch, got {_type_name(value)}"))
        return INVALID

    def _describe(self) -> dict[str, Any]:
        return {"anyOf": [{"type": "string", "format": "date-time"}, {"type": "number"}]}

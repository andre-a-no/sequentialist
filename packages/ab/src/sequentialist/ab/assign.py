# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Deterministic assignment of units to variants, the same in Python and in TypeScript, bit for bit.

For a unit u (its pseudonym) and the experiment's salt, two independent
uniform numbers in [0, 1) are drawn by hashing:

    h(domain) = first 8 bytes of SHA-256(UTF-8(domain + "\\x1f" + salt + "\\x1f" + u)),
                as a big-endian integer, shifted right by 11 bits (53 bits left)
    U(domain) = h(domain) / 2**53                       (exact in binary64)

* traffic:  the unit takes part if U("traffic") < share(t), the share of the
  ramp at time t.  Raising the share keeps everyone who was already in.
* variant:  with weights w_1..w_k (in the order of the spec), the variant is
  the first i with U("variant") * W < w_1 + ... + w_i, W their sum
  (computed left to right in binary64).

Manual overrides take precedence, and units outside [start, end) are not
assigned.  `ts/ab-client` implements the same function; the vectors in
`vectors/assignment.json` (made by `tools/make_vectors.py`) are checked by
both test suites.  The probability of the assignment is returned with it:
adaptive designs need it, and the engine checks it against the spec.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .spec import ExperimentSpec

SEPARATOR = "\x1f"
_SCALE = float(2**53)


def _text(value: str, what: str) -> bytes:
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a string")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{what} is not valid Unicode text (a lone surrogate)") from None


def uniform(domain: str, salt: str, unit: str) -> float:
    """U(domain): a number in [0, 1) that only (domain, salt, unit) determine."""
    message = _text(domain, "domain") + SEPARATOR.encode() + _text(salt, "salt") + SEPARATOR.encode() + \
        _text(unit, "unit")
    head = int.from_bytes(hashlib.sha256(message).digest()[:8], "big") >> 11
    return head / _SCALE


@dataclass(frozen=True)
class Assignment:
    variant: str | None  # None: the unit is not in the experiment now
    probability: float  # of this outcome, given the spec and the time
    reason: str  # "hash", "override", "traffic" (left out by the ramp), "inactive" (before start or after end)

    def to_json(self) -> dict[str, Any]:
        return {"variant": self.variant, "probability": self.probability, "reason": self.reason}


@dataclass(frozen=True)
class AssignmentConfig:
    """What the assignment needs from a spec, with times as seconds since the epoch (as the client gets it)."""

    salt: str
    variants: Sequence[tuple[str, float]]
    traffic: Sequence[tuple[float, float]] = ()
    overrides: Mapping[str, str] | None = None
    start: float | None = None
    end: float | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> AssignmentConfig:
        return cls(data["salt"], [(v["name"], float(v["weight"])) for v in data["variants"]],
                   [(float(t["from"]), float(t["share"])) for t in data.get("traffic", [])],
                   dict(data.get("overrides", {})), data.get("start"), data.get("end"))

    def to_json(self) -> dict[str, Any]:
        """The client configuration: give this to `@sequentialist/ab-client`."""
        return {
            "salt": self.salt,
            "variants": [{"name": n, "weight": w} for n, w in self.variants],
            "traffic": [{"from": f, "share": s} for f, s in self.traffic],
            "overrides": dict(self.overrides or {}),
            "start": self.start,
            "end": self.end,
        }

    def share(self, time: float | None) -> float:
        if not self.traffic:
            return 1.0
        if time is None:
            raise ValueError("a ramped experiment needs the time of the exposure")
        current = 0.0
        for moment, share in self.traffic:
            if time >= moment:
                current = share
        return current

    def assign(self, unit: str, time: float | None = None) -> Assignment:
        overrides = self.overrides or {}
        if unit in overrides:
            return Assignment(overrides[unit], 1.0, "override")
        if time is not None and ((self.start is not None and time < self.start)
                                 or (self.end is not None and time >= self.end)):
            return Assignment(None, 1.0, "inactive")
        if (self.start is not None or self.end is not None) and time is None:
            raise ValueError("an experiment with a start or an end needs the time of the exposure")
        share = self.share(time)
        if not uniform("traffic", self.salt, unit) < share:
            return Assignment(None, 1.0 - share, "traffic")
        total = 0.0
        for _, weight in self.variants:
            total += weight
        point = uniform("variant", self.salt, unit) * total
        cumulative = 0.0
        for name, weight in self.variants:
            cumulative += weight
            if point < cumulative:
                return Assignment(name, share * weight / total, "hash")
        name, weight = self.variants[-1]  # rounding put the point at the very end
        return Assignment(name, share * weight / total, "hash")


def config(spec: ExperimentSpec, start: float | None = None) -> AssignmentConfig:
    """The assignment configuration of a spec.  `start` overrides the spec's (for experiments started on use)."""
    d = spec.data
    begin = d["start"] if start is None else start
    end = None if begin is None else begin + d["max_duration"]
    return AssignmentConfig(d["salt"], [(v["name"], float(v["weight"])) for v in d["variants"]],
                            [(t["from"], t["share"]) for t in d["traffic"]], dict(d["overrides"]), begin, end)


def assign(spec: ExperimentSpec, unit: str, time: float | None = None) -> Assignment:
    return config(spec).assign(unit, time)

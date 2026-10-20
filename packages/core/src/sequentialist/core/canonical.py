# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""JSON as the stored state needs it: canonical bytes for hashes, exact numbers when read back.

These are the functions sequential-analysis uses for its checkpoints and
history digests, so that a spec id or an audit hash computed here is
computed the same way as the library's own digests.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any

from sequential_analysis.canonical import canonical_json, loads

__all__ = ["canonical_json", "digest", "is_plain", "loads"]


def digest(value: Any) -> str:
    """SHA-256 of the canonical JSON of `value`, as hex."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any) -> bool:
    if type(value) is not str:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def is_plain(value: Any) -> bool:
    """True for JSON that survives storage exactly: finite floats, integers within 2**53, UTF-8 text."""
    if value is None or type(value) is bool:
        return True
    if type(value) is int:
        return abs(value) <= 2**53
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is str:
        return _text(value)
    if type(value) is list:
        return all(is_plain(v) for v in value)
    if type(value) is dict:
        return all(_text(k) and is_plain(v) for k, v in value.items())
    return False

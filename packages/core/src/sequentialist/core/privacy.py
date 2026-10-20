# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Pseudonyms for identifiers of people, made before data reach a task.

The libraries store what they are given.  Give them pseudonyms, not user
or patient ids: `pseudonymize` is HMAC-SHA-256 under a key that stays with
the data owner, so the pseudonym cannot be traced back without the key,
and the same id always gets the same pseudonym (which assignment and
aggregation need).

Under the GDPR and Federal Law No. 152-FZ, pseudonymised data remain
personal data as long as someone holds the key; only aggregates without
identifiers are anonymous.  Keep the key out of the storage of the tasks,
and delete it when the pseudonyms must no longer be linkable.
"""
from __future__ import annotations

import hashlib
import hmac


def pseudonymize(identifier: str, key: bytes | str) -> str:
    """64 hexadecimal digits; the same identifier and key always give the same pseudonym."""
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("identifier must be a non-empty string")
    secret = key.encode("utf-8") if isinstance(key, str) else bytes(key)
    if len(secret) < 16:
        raise ValueError("the key must have at least 16 bytes")
    return hmac.new(secret, identifier.encode("utf-8"), hashlib.sha256).hexdigest()

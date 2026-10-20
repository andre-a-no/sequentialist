# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Exceptions of the sequentialist libraries."""
from __future__ import annotations


class SequentialistError(Exception):
    """Base class of every error raised on purpose by a sequentialist library."""


class SpecError(SequentialistError, ValueError):
    """A spec is invalid.  `errors` lists every problem found, each as (JSON pointer, message)."""

    def __init__(self, errors: list[tuple[str, str]], source: str = ""):
        self.errors = list(errors)
        self.source = source
        head = f"{source}: " if source else ""
        lines = [f"{path or '/'}: {message}" for path, message in self.errors]
        super().__init__(head + ("; ".join(lines) if len(lines) < 4 else "\n  " + "\n  ".join(lines)))


class StorageError(SequentialistError):
    """The storage cannot be used: a missing task, a malformed file, a wrong format."""


class TaskLocked(StorageError):
    """Another writer holds the task.  A task has one writer at a time."""


class ConflictError(StorageError):
    """The stored state changed since it was read: another writer saved it."""


class InvalidRecord(SequentialistError, ValueError):
    """A record cannot be accepted (no part of its batch was)."""


class TaskError(SequentialistError):
    """A task cannot do what was asked: wrong shard, unknown stream, a closed or failed task."""

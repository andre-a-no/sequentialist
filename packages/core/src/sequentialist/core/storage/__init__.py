# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Storages of tasks: in memory, in a directory of files, in SQLite.  Postgres follows in a later version."""
from __future__ import annotations

from .base import Storage, check_task_id
from .files import FileStorage
from .memory import MemoryStorage
from .sqlite import SQLiteStorage

__all__ = ["FileStorage", "MemoryStorage", "SQLiteStorage", "Storage", "check_task_id", "open_storage"]


def open_storage(url: str, *, sync: str = "fsync") -> Storage:
    """A storage from a short description: ``memory:``, ``files:<directory>`` or ``sqlite:<file>``."""
    kind, _, where = url.partition(":")
    if kind == "memory" and not where:
        return MemoryStorage()
    if kind == "files" and where:
        return FileStorage(where, sync=sync)
    if kind == "sqlite" and where:
        return SQLiteStorage(where, sync=sync)
    raise ValueError(f"unknown storage {url!r}: use memory:, files:<directory> or sqlite:<file>")

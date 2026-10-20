# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""What the engine needs from a storage, and the rules every storage follows.

Per task a storage keeps:

state      one JSON document with a version number.  `save` replaces it only
           if the stored version is the one the writer read (`expected`),
           so a second writer cannot silently overwrite the first;
journal    JSON entries appended since the last checkpoint: what arrived,
           written before it is processed;
audit      JSON entries appended forever: the decisions and actions;
documents  named JSON documents: archived segments, reports.

A task has one writer at a time: `lock` raises TaskLocked while another
holds it.  Values must be plain JSON (`sequentialist.core.canonical.is_plain`).
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Sequence

from ..errors import StorageError
from ._lock import Lock

TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
DOCUMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}(/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}){0,7}")


def check_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        raise StorageError(f"task id {task_id!r}: letters, digits, '_', '.' and '-' only, at most 128 characters, "
                           f"not starting with '.', '_' or '-' (it names files)")
    return task_id


def check_document_name(name: str) -> str:
    if not isinstance(name, str) or not DOCUMENT_NAME.fullmatch(name) or ".." in name:
        raise StorageError(f"document name {name!r}: path segments of letters, digits, '_', '.', ':' and '-'")
    return name


class Storage(ABC):
    @abstractmethod
    def lock(self, task_id: str) -> Lock:
        """Become the only writer of the task until the lock is released; TaskLocked otherwise."""

    @abstractmethod
    def tasks(self) -> list[str]:
        """The ids of the stored tasks, sorted."""

    @abstractmethod
    def load(self, task_id: str) -> tuple[int, dict[str, Any]] | None:
        """(version, state), or None if the task has never been saved."""

    @abstractmethod
    def save(self, task_id: str, state: dict[str, Any], *, expected: int, clear_journal: bool = False) -> int:
        """Replace the state if the stored version is `expected` (0: none yet); return the new version.

        With `clear_journal` the journal is emptied as well: atomically with
        the state where the storage can (SQLite), right after it otherwise.
        ConflictError if the version differs.
        """

    @abstractmethod
    def journal_append(self, task_id: str, entries: Sequence[dict[str, Any]]) -> None:
        """Append entries; they are on disk (per the storage's sync mode) when this returns."""

    @abstractmethod
    def journal_read(self, task_id: str) -> list[dict[str, Any]]:
        """The entries appended since the journal was last cleared, in order."""

    @abstractmethod
    def audit_append(self, task_id: str, entry: dict[str, Any]) -> None:
        """Append one audit entry; it is on disk when this returns."""

    @abstractmethod
    def audit_read(self, task_id: str) -> list[dict[str, Any]]:
        """Every audit entry of the task, in order."""

    @abstractmethod
    def put_document(self, task_id: str, name: str, document: dict[str, Any]) -> None:
        """Store (or replace) a named document."""

    @abstractmethod
    def get_document(self, task_id: str, name: str) -> dict[str, Any] | None:
        """The named document, or None."""

    @abstractmethod
    def documents(self, task_id: str) -> list[str]:
        """Names of the task's documents, sorted."""

    def close(self) -> None:  # noqa: B027 - optional
        """Release resources (connections, files)."""

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

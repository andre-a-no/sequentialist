# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""In-memory storage: for simulations, replays and tests.  Nothing survives the process."""
from __future__ import annotations

import copy
import threading
from typing import Any, Sequence

from ..errors import ConflictError
from ._lock import Lock, MemoryLock
from .base import Storage, check_document_name, check_task_id


class MemoryStorage(Storage):
    def __init__(self) -> None:
        self._states: dict[str, tuple[int, dict[str, Any]]] = {}
        self._journals: dict[str, list[dict[str, Any]]] = {}
        self._audits: dict[str, list[dict[str, Any]]] = {}
        self._documents: dict[str, dict[str, dict[str, Any]]] = {}
        self._held: set[str] = set()
        self._guard = threading.Lock()

    def lock(self, task_id: str) -> Lock:
        return MemoryLock(self._held, self._guard, check_task_id(task_id), f"task {task_id!r}")

    def tasks(self) -> list[str]:
        return sorted(self._states)

    def load(self, task_id: str) -> tuple[int, dict[str, Any]] | None:
        stored = self._states.get(check_task_id(task_id))
        return None if stored is None else (stored[0], copy.deepcopy(stored[1]))

    def save(self, task_id: str, state: dict[str, Any], *, expected: int, clear_journal: bool = False) -> int:
        current = self._states.get(check_task_id(task_id), (0, {}))[0]
        if current != expected:
            raise ConflictError(f"task {task_id!r}: stored version {current}, expected {expected}")
        self._states[task_id] = (current + 1, copy.deepcopy(state))
        if clear_journal:
            self._journals[task_id] = []
        return current + 1

    def journal_append(self, task_id: str, entries: Sequence[dict[str, Any]]) -> None:
        self._journals.setdefault(check_task_id(task_id), []).extend(copy.deepcopy(list(entries)))

    def journal_read(self, task_id: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self._journals.get(check_task_id(task_id), []))

    def audit_append(self, task_id: str, entry: dict[str, Any]) -> None:
        self._audits.setdefault(check_task_id(task_id), []).append(copy.deepcopy(entry))

    def audit_read(self, task_id: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self._audits.get(check_task_id(task_id), []))

    def put_document(self, task_id: str, name: str, document: dict[str, Any]) -> None:
        self._documents.setdefault(check_task_id(task_id), {})[check_document_name(name)] = copy.deepcopy(document)

    def get_document(self, task_id: str, name: str) -> dict[str, Any] | None:
        found = self._documents.get(check_task_id(task_id), {}).get(check_document_name(name))
        return None if found is None else copy.deepcopy(found)

    def documents(self, task_id: str) -> list[str]:
        return sorted(self._documents.get(check_task_id(task_id), {}))

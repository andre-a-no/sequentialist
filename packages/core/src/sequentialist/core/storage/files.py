# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Storage in a directory: one subdirectory per task, plain JSON files.

    <root>/<task>/state.json        {"format", "version", "state"}, replaced atomically
    <root>/<task>/journal.jsonl     one entry per line
    <root>/<task>/audit.jsonl       one entry per line
    <root>/<task>/documents/...     one JSON file per document
    <root>/<task>/.lock             held by the writer

A crash in the middle of an append leaves a torn last line; it is ignored
when reading and cut off by the next append.  `sync="fsync"` (the default)
forces every append and state to the disk, so they survive a power
failure; `sync="flush"` survives a crash of the process only.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Sequence

from ..canonical import loads
from ..errors import ConflictError, StorageError
from ._lock import FileLock, Lock
from .base import Storage, check_document_name, check_task_id

FORMAT = "sequentialist.files/1"


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json_atomically(path: Path, value: Any, sync: bool = True) -> None:
    """Write a file so that a crash leaves either the old content or the new one."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, sort_keys=True, ensure_ascii=False)
            f.flush()
            if sync:
                os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    if sync:
        _fsync_directory(path.parent)


def _read_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    with open(path, "rb") as f:
        for number, line in enumerate(f, 1):
            if not line.endswith(b"\n"):
                break  # a torn last line: the append it belonged to never completed
            try:
                out.append(loads(line.decode("utf-8")))
            except ValueError as error:
                raise StorageError(f"{path}:{number}: not valid JSON ({error})") from None
    return out


class FileStorage(Storage):
    def __init__(self, root: str | Path, *, sync: str = "fsync"):
        if sync not in ("fsync", "flush"):
            raise ValueError("sync must be 'fsync' or 'flush'")
        self.root = Path(root)
        self.sync = sync
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, task_id: str, create: bool = False) -> Path:
        directory = self.root / check_task_id(task_id)
        if create:
            (directory / "documents").mkdir(parents=True, exist_ok=True)
        return directory

    def lock(self, task_id: str) -> Lock:
        return FileLock(str(self._dir(task_id, create=True) / ".lock"), f"task {task_id!r} in {self.root}")

    def tasks(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if (p / "state.json").exists())

    def load(self, task_id: str) -> tuple[int, dict[str, Any]] | None:
        path = self._dir(task_id) / "state.json"
        if not path.exists():
            return None
        try:
            stored = loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise StorageError(f"{path}: not valid JSON ({error})") from None
        if not isinstance(stored, dict) or stored.get("format") != FORMAT or type(stored.get("version")) is not int:
            raise StorageError(f"{path}: not a state file of this storage")
        return stored["version"], stored["state"]

    def save(self, task_id: str, state: dict[str, Any], *, expected: int, clear_journal: bool = False) -> int:
        directory = self._dir(task_id, create=True)
        loaded = self.load(task_id)
        current = 0 if loaded is None else loaded[0]
        if current != expected:
            raise ConflictError(f"task {task_id!r}: stored version {current}, expected {expected}")
        write_json_atomically(directory / "state.json", {"format": FORMAT, "version": current + 1, "state": state},
                              self.sync == "fsync")
        if clear_journal:
            journal = directory / "journal.jsonl"
            if journal.exists():
                with open(journal, "r+b") as f:
                    f.truncate(0)
                    f.flush()
                    if self.sync == "fsync":
                        os.fsync(f.fileno())
        return current + 1

    def _append(self, path: Path, entries: Sequence[dict[str, Any]]) -> None:
        data = b"".join((json.dumps(e, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8") for e in entries)
        with open(path, "a+b") as f:
            size = f.seek(0, os.SEEK_END)
            if size:
                f.seek(size - 1)
                if f.read(1) != b"\n":  # a torn last line: cut it back to the last complete one
                    f.seek(0)
                    keep = f.read().rfind(b"\n") + 1
                    f.truncate(keep)
            f.write(data)
            f.flush()
            if self.sync == "fsync":
                os.fsync(f.fileno())

    def journal_append(self, task_id: str, entries: Sequence[dict[str, Any]]) -> None:
        if entries:
            self._append(self._dir(task_id, create=True) / "journal.jsonl", entries)

    def journal_read(self, task_id: str) -> list[dict[str, Any]]:
        return _read_lines(self._dir(task_id) / "journal.jsonl")

    def audit_append(self, task_id: str, entry: dict[str, Any]) -> None:
        self._append(self._dir(task_id, create=True) / "audit.jsonl", [entry])

    def audit_read(self, task_id: str) -> list[dict[str, Any]]:
        return _read_lines(self._dir(task_id) / "audit.jsonl")

    def _document_path(self, task_id: str, name: str) -> Path:
        return self._dir(task_id) / "documents" / (check_document_name(name) + ".json")

    def put_document(self, task_id: str, name: str, document: dict[str, Any]) -> None:
        self._dir(task_id, create=True)
        path = self._document_path(task_id, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomically(path, document, self.sync == "fsync")

    def get_document(self, task_id: str, name: str) -> dict[str, Any] | None:
        path = self._document_path(task_id, name)
        if not path.exists():
            return None
        found: dict[str, Any] = loads(path.read_text(encoding="utf-8"))
        return found

    def documents(self, task_id: str) -> list[str]:
        base = self._dir(task_id) / "documents"
        if not base.exists():
            return []
        return sorted(p.relative_to(base).as_posix()[:-5] for p in base.rglob("*.json"))

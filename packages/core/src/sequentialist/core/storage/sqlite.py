# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Storage in one SQLite database file (the sqlite3 module of the standard library).

The state and the clearing of the journal are one transaction.  The
database runs in WAL mode; `sync="fsync"` sets ``synchronous=FULL``
(survives a power failure), ``"flush"`` sets ``NORMAL`` (survives a crash of
the process).  Writers lock tasks with files next to the database
(``<database>.locks/<task>.lock``); ``":memory:"`` locks within the process.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Sequence

from ..canonical import loads
from ..errors import ConflictError, StorageError
from ._lock import FileLock, Lock, MemoryLock
from .base import Storage, check_document_name, check_task_id

FORMAT = "sequentialist.sqlite/1"

_SCHEMA = """
create table if not exists meta (key text primary key, value text not null);
create table if not exists tasks (task_id text primary key, version integer not null, state text not null);
create table if not exists journal (task_id text not null, pos integer not null, entry text not null,
                                    primary key (task_id, pos));
create table if not exists audit (task_id text not null, n integer not null, entry text not null,
                                  primary key (task_id, n));
create table if not exists documents (task_id text not null, name text not null, document text not null,
                                      primary key (task_id, name));
"""


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class SQLiteStorage(Storage):
    def __init__(self, path: str | Path, *, sync: str = "fsync"):
        if sync not in ("fsync", "flush"):
            raise ValueError("sync must be 'fsync' or 'flush'")
        self.path = str(path)
        self._memory = self.path == ":memory:"
        self._guard = threading.Lock()
        self._held: set[str] = set()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        with self._guard:
            if not self._memory:
                self._db.execute("pragma journal_mode=wal")
            self._db.execute(f"pragma synchronous={'full' if sync == 'fsync' else 'normal'}")
            self._db.executescript(_SCHEMA)
            row = self._db.execute("select value from meta where key = 'format'").fetchone()
            if row is None:
                self._db.execute("insert into meta values ('format', ?)", (FORMAT,))
            elif row[0] != FORMAT:
                raise StorageError(f"{self.path}: format {row[0]!r}, this version reads {FORMAT!r}")

    def lock(self, task_id: str) -> Lock:
        check_task_id(task_id)
        if self._memory:
            return MemoryLock(self._held, self._guard, task_id, f"task {task_id!r}")
        locks = Path(self.path + ".locks")
        locks.mkdir(exist_ok=True)
        return FileLock(str(locks / f"{task_id}.lock"), f"task {task_id!r} in {self.path}")

    def _query(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._guard:
            return self._db.execute(sql, args).fetchall()

    def tasks(self) -> list[str]:
        return [r[0] for r in self._query("select task_id from tasks order by task_id")]

    def load(self, task_id: str) -> tuple[int, dict[str, Any]] | None:
        rows = self._query("select version, state from tasks where task_id = ?", (check_task_id(task_id),))
        return None if not rows else (rows[0][0], loads(rows[0][1]))

    def save(self, task_id: str, state: dict[str, Any], *, expected: int, clear_journal: bool = False) -> int:
        check_task_id(task_id)
        with self._guard:
            db = self._db
            db.execute("begin immediate")
            try:
                row = db.execute("select version from tasks where task_id = ?", (task_id,)).fetchone()
                current = 0 if row is None else row[0]
                if current != expected:
                    raise ConflictError(f"task {task_id!r}: stored version {current}, expected {expected}")
                db.execute("insert or replace into tasks values (?, ?, ?)", (task_id, current + 1, _dump(state)))
                if clear_journal:
                    db.execute("delete from journal where task_id = ?", (task_id,))
                db.execute("commit")
            except BaseException:
                db.execute("rollback")
                raise
        return current + 1

    def _append(self, table: str, task_id: str, entries: Sequence[dict[str, Any]]) -> None:
        column = "pos" if table == "journal" else "n"
        with self._guard:
            db = self._db
            db.execute("begin immediate")
            try:
                row = db.execute(f"select max({column}) from {table} where task_id = ?", (task_id,)).fetchone()
                start = 0 if row[0] is None else row[0] + 1
                db.executemany(f"insert into {table} values (?, ?, ?)",
                               [(task_id, start + i, _dump(e)) for i, e in enumerate(entries)])
                db.execute("commit")
            except BaseException:
                db.execute("rollback")
                raise

    def journal_append(self, task_id: str, entries: Sequence[dict[str, Any]]) -> None:
        if entries:
            self._append("journal", check_task_id(task_id), entries)

    def journal_read(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._query("select entry from journal where task_id = ? order by pos", (check_task_id(task_id),))
        return [loads(r[0]) for r in rows]

    def audit_append(self, task_id: str, entry: dict[str, Any]) -> None:
        self._append("audit", check_task_id(task_id), [entry])

    def audit_read(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._query("select entry from audit where task_id = ? order by n", (check_task_id(task_id),))
        return [loads(r[0]) for r in rows]

    def put_document(self, task_id: str, name: str, document: dict[str, Any]) -> None:
        with self._guard:
            self._db.execute("insert or replace into documents values (?, ?, ?)",
                             (check_task_id(task_id), check_document_name(name), _dump(document)))

    def get_document(self, task_id: str, name: str) -> dict[str, Any] | None:
        rows = self._query("select document from documents where task_id = ? and name = ?",
                           (check_task_id(task_id), check_document_name(name)))
        if not rows:
            return None
        found: dict[str, Any] = loads(rows[0][0])
        return found

    def documents(self, task_id: str) -> list[str]:
        return [r[0] for r in self._query("select name from documents where task_id = ? order by name",
                                          (check_task_id(task_id),))]

    def close(self) -> None:
        with self._guard:
            self._db.close()

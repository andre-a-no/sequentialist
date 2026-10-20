# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC (Kazan, Tatarstan, Russian Federation)
"""Files of a monitor: its state, the journal of observations, and the alert log.

The run of a monitor is checkpointed every so many observations.  Values
pushed over HTTP cannot be asked for again, so every accepted observation
is first written to the journal; after a crash the run is restored from
the last checkpoint and the journal is replayed into it.  Delivery drops
what the checkpoint already holds (the same source and sequence number),
so the journal only needs to be cut back after a checkpoint, not exactly
at it.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sequential_analysis.canonical import loads


def write_json(path: Path, value: Any) -> None:
    """Write atomically: a crash leaves either the old file or the new one."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    _fsync_directory(path.parent)


def read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return loads(f.read())  # integers beyond 2**53 as binary64, as checkpoints require


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class JsonLines:
    """An append-only file of JSON lines, forced to the disk after every batch.

    A torn last line, left by a crash in the middle of a write, is ignored
    when reading and overwritten by the next append.
    """

    def __init__(self, path: Path, sync: str = "fsync"):
        if sync not in ("flush", "fsync"):
            raise ValueError("sync must be 'flush' or 'fsync'")
        self.path, self.sync = path, sync
        self._file = open(path, "ab")  # open for the lifetime of the monitor
        self._drop_torn_tail()

    def _drop_torn_tail(self) -> None:
        size = self._file.seek(0, os.SEEK_END)
        if size == 0:
            return
        with open(self.path, "rb") as f:
            data = f.read()
        end = data.rfind(b"\n") + 1
        if end != size:
            self._file.truncate(end)
            self._file.seek(end)

    def append(self, records: list[Any]) -> None:
        if not records:
            return
        self._file.write(b"".join((json.dumps(r, sort_keys=True) + "\n").encode("utf-8") for r in records))
        self._file.flush()
        if self.sync == "fsync":
            os.fsync(self._file.fileno())

    def read(self) -> Iterator[Any]:
        with open(self.path, "rb") as f:
            for line in f:
                if line.endswith(b"\n"):
                    yield loads(line.decode("utf-8"))

    def clear(self) -> None:
        self._file.truncate(0)
        self._file.seek(0)
        self._file.flush()
        if self.sync == "fsync":
            os.fsync(self._file.fileno())

    def close(self) -> None:
        self._file.close()

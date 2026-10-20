# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Exclusive locks on files, released by the operating system when the process ends.

A crashed writer leaves no stale lock behind.  Two opens of the same lock
file conflict even within one process, so a task cannot be opened twice by
mistake either.
"""
from __future__ import annotations

import os
import sys
import threading

from ..errors import TaskLocked

if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class Lock:
    """Held from creation until `release`."""

    def release(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def __enter__(self) -> Lock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class FileLock(Lock):
    def __init__(self, path: str, what: str):
        self.path = path
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        if not _try_lock(fd):
            os.close(fd)
            raise TaskLocked(f"{what} is open in another writer (lock {path})")
        self._fd: int = fd

    def release(self) -> None:
        if self._fd >= 0:
            try:
                _unlock(self._fd)
            finally:
                os.close(self._fd)
                self._fd = -1


class MemoryLock(Lock):
    """A lock among the users of one object in one process (for in-memory storage)."""

    def __init__(self, held: set[str], guard: threading.Lock, key: str, what: str):
        with guard:
            if key in held:
                raise TaskLocked(f"{what} is open in another writer")
            held.add(key)
        self._held, self._guard, self._key = held, guard, key
        self._released = False

    def release(self) -> None:
        with self._guard:
            if not self._released:
                self._held.discard(self._key)
                self._released = True

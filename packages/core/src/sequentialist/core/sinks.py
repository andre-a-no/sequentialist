# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Sinks: where audit entries (decisions, alarms, actions) are announced.

The audit log is the record; sinks are notifications.  The engine calls
every sink with every new entry after the checkpoint that made it
durable.  A failing sink is logged and never stops a task.  A webhook is
sent from a background thread, with retries, so a slow receiver does not
hold up the data.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Sequence

from .audit import AuditEntry

logger = logging.getLogger("sequentialist")


class LogSink:
    """Logs entries of the given kinds (all but "created" and "segment" by default) as warnings."""

    def __init__(self, kinds: Sequence[str] = ("decision", "action"), logger_name: str = "sequentialist"):
        self.kinds, self.log = tuple(kinds), logging.getLogger(logger_name)

    def __call__(self, entry: AuditEntry) -> None:
        if entry.kind in self.kinds:
            self.log.warning("%s %s: %s %s", entry.task_id, entry.kind, entry.decision or "", entry.basis)


class JsonlSink:
    """Appends every entry as a JSON line to a file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def __call__(self, entry: AuditEntry) -> None:
        line = json.dumps(entry.to_json(), sort_keys=True, ensure_ascii=False) + "\n"
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line)


Post = Callable[[str, bytes, Dict[str, str], float], None]


def _post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> None:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


class WebhookSink:
    """POSTs entries of the given kinds as JSON; retries with exponential backoff, gives up after `attempts`."""

    def __init__(self, url: str, *, kinds: Sequence[str] = ("decision", "action"), timeout: float = 5.0,
                 attempts: int = 5, headers: dict[str, str] | None = None, post: Post | None = None):
        self.url, self.kinds, self.timeout, self.attempts = url, tuple(kinds), float(timeout), int(attempts)
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self._post = post or _post
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._work, name="sequentialist-webhook", daemon=True)
        self._thread.start()

    def __call__(self, entry: AuditEntry) -> None:
        if entry.kind in self.kinds:
            self._queue.put(entry)

    def _work(self) -> None:
        while True:
            entry = self._queue.get()
            if entry is None:
                return
            body = json.dumps(entry.to_json()).encode("utf-8")
            for attempt in range(self.attempts):
                try:
                    self._post(self.url, body, self.headers, self.timeout)
                    break
                except Exception as error:
                    if attempt + 1 == self.attempts:
                        logger.error("webhook %s: entry %d of task %s not delivered: %s", self.url, entry.n,
                                     entry.task_id, error)
                    else:
                        time.sleep(min(60.0, 0.5 * 2**attempt))

    def close(self, timeout: float = 10.0) -> None:
        """Send what is queued, then stop."""
        self._queue.put(None)
        self._thread.join(timeout)

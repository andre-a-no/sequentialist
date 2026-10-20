# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC (Kazan, Tatarstan, Russian Federation)
"""Alerts and where they are sent.

Every alert is first recorded in the monitor's own alert log (see
`Monitor`), which is the durable record.  Sinks are notifications: a sink
that fails is logged and never stops the monitor.  A webhook is sent from a
background thread, with retries, so a slow receiver does not hold up the
observations.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import SinkConfig
from .storage import JsonLines

logger = logging.getLogger("sequentialist")


@dataclass(frozen=True)
class Alert:
    id: str  # "<run_id>@<t>": the same alarm, replayed after a crash, has the same id
    monitor: str
    run_id: str
    segment: int
    t: int  # observations of the segment up to and including the alarm
    seq: int  # sequence number of the observation that raised it
    time: float | None  # its event time, if the client gave one
    detector: str
    details: dict[str, Any]
    raised_at: float  # wall clock of the service

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


Sink = Callable[[Alert], None]


class LogSink:
    def __call__(self, alert: Alert) -> None:
        logger.warning("ALERT %s: %s raised an alarm at seq %d (segment %d, t=%d) %s",
                       alert.monitor, alert.detector, alert.seq, alert.segment, alert.t, alert.details)


class JsonlSink:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lines = JsonLines(Path(path), sync="flush")

    def __call__(self, alert: Alert) -> None:
        self._lines.append([alert.to_json()])

    def close(self) -> None:
        self._lines.close()


class WebhookSink:
    """POSTs each alert as JSON; retries with exponential backoff; gives up after `attempts`."""

    def __init__(self, url: str, *, timeout: float = 5.0, attempts: int = 5, headers: dict[str, str] | None = None,
                 post: Callable[[str, bytes, dict[str, str], float], None] | None = None):
        self.url, self.timeout, self.attempts = url, float(timeout), int(attempts)
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self._post = post or _post
        self._queue: queue.Queue[Alert | None] = queue.Queue()
        self._thread = threading.Thread(target=self._work, name="webhook", daemon=True)
        self._thread.start()

    def __call__(self, alert: Alert) -> None:
        self._queue.put(alert)

    def _work(self) -> None:
        while (alert := self._queue.get()) is not None:
            body = json.dumps(alert.to_json()).encode("utf-8")
            for attempt in range(self.attempts):
                try:
                    self._post(self.url, body, self.headers, self.timeout)
                    break
                except Exception as error:
                    if attempt + 1 == self.attempts:
                        logger.error("webhook %s: alert %s not delivered: %s", self.url, alert.id, error)
                    else:
                        time.sleep(min(60.0, 0.5 * 2**attempt))

    def close(self, timeout: float = 10.0) -> None:
        """Send what is queued, then stop."""
        self._queue.put(None)
        self._thread.join(timeout)


def _post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> None:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def make_sink(config: SinkConfig) -> Sink:
    options = dict(config.options)
    if config.type == "log":
        return LogSink()
    if config.type == "jsonl":
        return JsonlSink(options["path"])
    if config.type == "webhook":
        return WebhookSink(options.pop("url"), **options)
    raise ValueError(f"unknown sink type {config.type!r}")


class Dispatcher:
    """Hands an alert to every sink; a failing sink is logged, never raised."""

    def __init__(self, sinks: Sequence[Sink]):
        self.sinks = list(sinks)

    def __call__(self, alert: Alert) -> None:
        for sink in self.sinks:
            try:
                sink(alert)
            except Exception:
                logger.exception("sink %s failed on alert %s", type(sink).__name__, alert.id)

    def close(self) -> None:
        for sink in self.sinks:
            close = getattr(sink, "close", None)
            if close is not None:
                close()

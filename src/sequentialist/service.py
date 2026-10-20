# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC (Kazan, Tatarstan, Russian Federation)
"""Service: the monitors of one configuration, their alert sinks, and the order of startup and shutdown."""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from .alerts import Alert, Dispatcher, Sink, make_sink
from .config import ServiceConfig
from .monitor import Monitor

logger = logging.getLogger("sequentialist")


class UnknownMonitor(KeyError):
    pass


class Service:
    def __init__(self, config: ServiceConfig, *, sinks: Sequence[Sink] | None = None,
                 clock: Callable[[], float] | None = None):
        self.config = config
        self.dispatch = Dispatcher(sinks if sinks is not None else [make_sink(s) for s in config.sinks])
        self.monitors: dict[str, Monitor] = {}
        config.state_dir.mkdir(parents=True, exist_ok=True)
        extra = {"clock": clock} if clock is not None else {}
        try:
            for m in config.monitors:
                self.monitors[m.name] = Monitor(m, config.state_dir / m.name,
                                                checkpoint_every=config.checkpoint_every, sync=config.sync, **extra)
        except BaseException:
            self.close()
            raise
        for monitor in self.monitors.values():  # alarms raised by the journal of a crashed process
            for alert in monitor.replayed:
                self.dispatch(alert)

    def monitor(self, name: str) -> Monitor:
        try:
            return self.monitors[name]
        except KeyError:
            raise UnknownMonitor(name) from None

    def ingest(self, name: str, values: Iterable[Mapping[str, Any]]) -> tuple[list[Alert], dict[str, int]]:
        alerts, counts = self.monitor(name).ingest(values)
        for alert in alerts:
            self.dispatch(alert)
        return alerts, counts

    def close(self) -> None:
        """Checkpoint every monitor, then let the sinks send what is queued."""
        for monitor in self.monitors.values():
            try:
                monitor.close()
            except Exception:
                logger.exception("monitor %s: could not be closed cleanly", monitor.name)
        self.dispatch.close()

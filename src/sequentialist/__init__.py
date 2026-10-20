# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC (Kazan, Tatarstan, Russian Federation)
"""sequentialist: a service that watches streams of metrics with sequential change-point detectors.

Layer          Module
-------------  ------------------------------------------------------------
configuration  sequentialist.config, sequentialist.detectors
monitors       sequentialist.monitor (one detector, its segments, journal and checkpoints)
alerts         sequentialist.alerts (log, JSON lines, webhooks)
service        sequentialist.service, sequentialist.api (HTTP), sequentialist.cli

The statistics are those of sequential-analysis; this package adds what a
long-running service needs around them.
"""
from .alerts import Alert
from .config import ConfigError, MonitorConfig, ServiceConfig, load
from .detectors import DETECTORS, DetectorError, build_detector
from .monitor import Monitor, MonitorError
from .service import Service
from .version import __version__

__all__ = [
    "DETECTORS",
    "Alert",
    "ConfigError",
    "DetectorError",
    "Monitor",
    "MonitorConfig",
    "MonitorError",
    "Service",
    "ServiceConfig",
    "__version__",
    "build_detector",
    "load",
]

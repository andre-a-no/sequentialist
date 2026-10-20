# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC (Kazan, Tatarstan, Russian Federation)
"""Configuration: one TOML file with the service, its alert sinks and its monitors.

    [service]
    state_dir = "state"            # relative to this file
    host = "127.0.0.1"
    port = 8080
    checkpoint_every = 1000        # observations between checkpoints
    sync = "fsync"                 # the journal: "fsync" survives a power failure, "flush" a crash

    [[sink]]
    type = "webhook"
    url = "https://alerts.example/hook"

    [[monitor]]
    name = "api-latency"
    detector = "mean_e_detector"
    params = { m = 0.2, lo = 0.0, hi = 1.0, arl = 10000, direction = "increase", statistic = "cusum" }

Everything is checked here, detectors included, so that a service never
starts with a configuration it cannot run.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sequential_analysis import Procedure

from .detectors import DetectorError, build_detector

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
SINK_OPTIONS = {"log": set(), "jsonl": {"path"}, "webhook": {"url", "timeout", "attempts", "headers"}}
SINKS = tuple(SINK_OPTIONS)


class ConfigError(ValueError):
    """The configuration cannot be used; the message names the place."""


@dataclass(frozen=True)
class MonitorConfig:
    name: str
    detector: str
    params: dict[str, Any]
    description: str = ""
    rearm: bool = True  # after an alarm, start a new segment instead of stopping

    def procedure(self) -> Procedure:
        return build_detector(self.detector, self.params)


@dataclass(frozen=True)
class SinkConfig:
    type: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ServiceConfig:
    state_dir: Path
    host: str = "127.0.0.1"
    port: int = 8080
    checkpoint_every: int = 1000
    sync: str = "fsync"
    sinks: tuple[SinkConfig, ...] = ()
    monitors: tuple[MonitorConfig, ...] = ()

    def monitor(self, name: str) -> MonitorConfig:
        for m in self.monitors:
            if m.name == name:
                return m
        raise KeyError(name)


def _take(table: dict[str, Any], key: str, kind: type | tuple[type, ...], where: str, default: Any = ...) -> Any:
    if key not in table:
        if default is ...:
            raise ConfigError(f"{where}: missing {key!r}")
        return default
    value = table.pop(key)
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        names = kind.__name__ if isinstance(kind, type) else " or ".join(k.__name__ for k in kind)
        raise ConfigError(f"{where}: {key!r} must be {names}, got {value!r}")
    return value


def _no_more(table: dict[str, Any], where: str) -> None:
    if table:
        raise ConfigError(f"{where}: unknown keys {sorted(table)}")


def parse(data: dict[str, Any], base: Path) -> ServiceConfig:
    """A ServiceConfig from parsed TOML; relative paths are taken relative to `base`."""
    data = dict(data)
    service = dict(_take(data, "service", dict, "file", {}))
    sinks = _take(data, "sink", list, "file", [])
    monitors = _take(data, "monitor", list, "file", [])
    _no_more(data, "file")

    state_dir = Path(_take(service, "state_dir", str, "[service]", "state"))
    host = _take(service, "host", str, "[service]", "127.0.0.1")
    port = _take(service, "port", int, "[service]", 8080)
    checkpoint_every = _take(service, "checkpoint_every", int, "[service]", 1000)
    sync = _take(service, "sync", str, "[service]", "fsync")
    _no_more(service, "[service]")
    if not 0 <= port < 65536:
        raise ConfigError(f"[service]: port {port} out of range")
    if checkpoint_every < 1:
        raise ConfigError("[service]: checkpoint_every must be positive")
    if sync not in ("flush", "fsync"):
        raise ConfigError("[service]: sync must be 'flush' or 'fsync'")

    sink_configs = []
    for i, raw in enumerate(sinks):
        where = f"[[sink]] #{i + 1}"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: must be a table")
        raw = dict(raw)
        kind = _take(raw, "type", str, where)
        if kind not in SINKS:
            raise ConfigError(f"{where}: unknown type {kind!r}; one of {list(SINKS)}")
        if kind == "webhook" and not isinstance(raw.get("url"), str):
            raise ConfigError(f"{where}: a webhook needs a url")
        if kind == "jsonl" and not isinstance(raw.get("path"), str):
            raise ConfigError(f"{where}: a jsonl sink needs a path")
        unknown = set(raw) - SINK_OPTIONS[kind]
        if unknown:
            raise ConfigError(f"{where}: unknown keys {sorted(unknown)} for a {kind} sink")
        if kind == "jsonl":
            raw["path"] = str(base / raw["path"])
        sink_configs.append(SinkConfig(kind, raw))

    monitor_configs: list[MonitorConfig] = []
    for i, raw in enumerate(monitors):
        where = f"[[monitor]] #{i + 1}"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: must be a table")
        raw = dict(raw)
        name = _take(raw, "name", str, where)
        where = f"monitor {name!r}"
        if not NAME.fullmatch(name):
            raise ConfigError(f"{where}: a name has letters, digits, '_', '.' and '-' only (it names files and URLs)")
        if any(m.name == name for m in monitor_configs):
            raise ConfigError(f"{where}: defined twice")
        config = MonitorConfig(
            name=name,
            detector=_take(raw, "detector", str, where),
            params=_take(raw, "params", dict, where, {}),
            description=_take(raw, "description", str, where, ""),
            rearm=_take(raw, "rearm", bool, where, True),
        )
        _no_more(raw, where)
        try:
            config.procedure()
        except DetectorError as error:
            raise ConfigError(f"{where}: {error}") from None
        monitor_configs.append(config)

    return ServiceConfig(
        state_dir=state_dir if state_dir.is_absolute() else base / state_dir,
        host=host,
        port=port,
        checkpoint_every=checkpoint_every,
        sync=sync,
        sinks=tuple(sink_configs),
        monitors=tuple(monitor_configs),
    )


def load(path: str | Path) -> ServiceConfig:
    path = Path(path)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as error:
        raise ConfigError(f"{path}: {error.strerror}") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from None
    try:
        return parse(data, path.resolve().parent)
    except ConfigError as error:
        raise ConfigError(f"{path}: {error}") from None

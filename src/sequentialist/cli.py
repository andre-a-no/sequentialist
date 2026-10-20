# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC (Kazan, Tatarstan, Russian Federation)
"""Command line.

    sequentialist check CONFIG                     validate the configuration
    sequentialist serve CONFIG                     run the service
    sequentialist replay CONFIG MONITOR FILE       run one monitor over past values, print its alarms

`replay` reads one value per line (or the first column of a CSV, with a
header line skipped if it is not a number) and runs the monitor exactly as
the service would, alarms and new segments included, in a temporary state
directory: the way to choose a detector's parameters on history before
deploying it.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from types import FrameType

from .config import ConfigError, load
from .monitor import Monitor, MonitorError
from .version import __version__

logger = logging.getLogger("sequentialist")


def _check(args: argparse.Namespace) -> int:
    config = load(args.config)
    print(f"{args.config}: {len(config.monitors)} monitors, {len(config.sinks)} sinks, state in {config.state_dir}")
    for m in config.monitors:
        procedure = m.procedure()
        print(f"  {m.name}: {procedure.name} ({procedure.guarantee}) {json.dumps(procedure.config())}")
    return 0


def _serve(args: argparse.Namespace) -> int:
    from .api import make_server
    from .service import Service

    config = load(args.config)
    host = args.host or config.host
    port = args.port if args.port is not None else config.port
    service = Service(config)
    try:
        server = make_server(service, host, port)
    except OSError:
        service.close()
        raise
    stop = threading.Event()

    def shutdown(signum: int, frame: FrameType | None) -> None:
        if not stop.is_set():
            stop.set()
            threading.Thread(target=server.shutdown).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    logger.info("sequentialist %s: %d monitors, listening on http://%s:%d",
                __version__, len(service.monitors), host, server.server_address[1])
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()
        logger.info("stopped; every monitor checkpointed")
    return 0


def _values(path: Path) -> Iterator[float]:
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            cell = line.split(",")[0].strip()
            if not cell:
                continue
            try:
                yield float(cell)
            except ValueError:
                if i == 0:
                    continue  # a header
                raise ValueError(f"{path}:{i + 1}: {cell!r} is not a number") from None


def _replay(args: argparse.Namespace) -> int:
    config = load(args.config)
    try:
        monitor_config = config.monitor(args.monitor)
    except KeyError:
        raise ConfigError(f"{args.config}: no monitor {args.monitor!r}") from None
    with tempfile.TemporaryDirectory() as tmp:
        monitor = Monitor(monitor_config, Path(tmp), checkpoint_every=10**9, sync="flush")
        batch: list[dict] = []
        n = alarms = 0

        def flush() -> None:
            nonlocal alarms
            raised, _ = monitor.ingest(batch)
            for alert in raised:
                alarms += 1
                print(json.dumps({"seq": alert.seq, "segment": alert.segment, "t": alert.t, **alert.details}))
            batch.clear()

        for value in _values(args.file):
            batch.append({"value": value})
            n += 1
            if len(batch) == 1000:
                flush()
                if monitor.stopped:
                    break
        if batch and not monitor.stopped:
            flush()
        status = monitor.status()
        monitor.close()
    print(f"{n} values, {alarms} alarms, {status['invalid_skipped']} invalid in the last segment",
          file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sequentialist", description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--version", action="version", version=f"sequentialist {__version__}")
    parser.add_argument("--log-level", default="INFO")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="validate a configuration")
    check.add_argument("config", type=Path)
    check.set_defaults(handler=_check)
    serve = commands.add_parser("serve", help="run the service")
    serve.add_argument("config", type=Path)
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(handler=_serve)
    replay = commands.add_parser("replay", help="run one monitor over past values")
    replay.add_argument("config", type=Path)
    replay.add_argument("monitor")
    replay.add_argument("file", type=Path)
    replay.set_defaults(handler=_replay)
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return int(args.handler(args))
    except (ConfigError, MonitorError, ValueError, OSError) as error:
        print(f"sequentialist: {error}", file=sys.stderr)
        return 2

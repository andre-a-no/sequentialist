# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""The `sequentialist` command.  Every installed library adds its subcommands.

    sequentialist tasks STORAGE                 list the tasks in a storage
    sequentialist audit STORAGE TASK            verify a task's audit log and print it
    sequentialist <library> ...                 e.g. `sequentialist ab --help`

STORAGE is ``files:<directory>`` or ``sqlite:<file>``.

A library registers a subcommand with an entry point in the group
``sequentialist.commands``: a function that receives the subparsers object
and adds its parser, with ``set_defaults(handler=...)``.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Callable

from .audit import AuditError, verify_audit
from .errors import SequentialistError
from .storage import open_storage
from .version import __version__


def _entry_points() -> list[Any]:
    from importlib.metadata import entry_points

    found = entry_points()
    if hasattr(found, "select"):  # Python 3.10 and later
        return list(found.select(group="sequentialist.commands"))
    return list(found.get("sequentialist.commands", []))


def tasks_command(commands: Any) -> None:
    parser = commands.add_parser("tasks", help="list the tasks in a storage")
    parser.add_argument("storage")

    def handler(args: argparse.Namespace) -> int:
        with open_storage(args.storage) as storage:
            for task_id in storage.tasks():
                print(task_id)
        return 0

    parser.set_defaults(handler=handler)


def audit_command(commands: Any) -> None:
    parser = commands.add_parser("audit", help="verify a task's audit log and print it")
    parser.add_argument("storage")
    parser.add_argument("task")
    parser.add_argument("--json", action="store_true", help="print the entries as JSON lines")

    def handler(args: argparse.Namespace) -> int:
        with open_storage(args.storage) as storage:
            entries = storage.audit_read(args.task)
        try:
            count = verify_audit(entries, args.task)
        except AuditError as error:
            print(f"audit log of {args.task}: BROKEN: {error}", file=sys.stderr)
            return 1
        for e in entries:
            if args.json:
                print(json.dumps(e, sort_keys=True, ensure_ascii=False))
            else:
                who = f" by {e['actor']} ({e['reason']})" if e.get("actor") else ""
                print(f"{e['n']:>5}  {e['kind']:<9} {e['decision'] or '':<12}{who}  {json.dumps(e['basis'])}")
        print(f"audit log of {args.task}: {count} entries, chain verified", file=sys.stderr)
        return 0

    parser.set_defaults(handler=handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sequentialist", description="The sequentialist libraries.")
    parser.add_argument("--version", action="version", version=f"sequentialist-core {__version__}")
    parser.add_argument("--log-level", default="WARNING")
    commands = parser.add_subparsers(dest="command")
    for point in sorted(_entry_points(), key=lambda p: p.name):
        register: Callable[[Any], None] = point.load()
        register(commands)
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")
    if not getattr(args, "handler", None):
        parser.print_help()
        return 2
    try:
        return int(args.handler(args))
    except (SequentialistError, ValueError, OSError) as error:
        print(f"sequentialist: {error}", file=sys.stderr)
        return 2

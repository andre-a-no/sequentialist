# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""sequentialist.core: what the sequentialist libraries share.

Statistics live in sequential-analysis.  This package adds what turns its
procedures into applications:

spec       declarative specs with schemas, normal forms and ids (`Spec`, `load_spec`)
engine     tasks that take records idempotently, feed runs, decide and survive crashes (`Engine`, `Handler`)
storage    memory, a directory of files, SQLite (`open_storage`)
audit      the hash-chained log of decisions and actions (`verify_audit`)
report     JSON and Markdown reports in which every finding names its guarantee
simulate   error rates of a whole pipeline by simulation
replay     recorded data through the live code, on the data's clock
sinks      notifications: log, JSON lines, webhooks
privacy    pseudonyms for identifiers of people
"""
from .audit import GUARANTEES, AuditEntry, AuditError, Evidence, verify_audit
from .engine import Command, Context, Engine, Handler, IngestResult, Record, Stream, Task, shard_of
from .errors import (
    ConflictError,
    InvalidRecord,
    SequentialistError,
    SpecError,
    StorageError,
    TaskError,
    TaskLocked,
)
from .privacy import pseudonymize
from .replay import replay
from .report import Finding, Report, Section, Table
from .simulate import Proportion, proportion, run_simulations
from .sinks import JsonlSink, LogSink, WebhookSink
from .spec import Spec, json_schema, load_spec, parse_spec, spec_id
from .storage import FileStorage, MemoryStorage, SQLiteStorage, Storage, open_storage
from .version import __version__

__all__ = [
    "GUARANTEES",
    "AuditEntry",
    "AuditError",
    "Command",
    "ConflictError",
    "Context",
    "Engine",
    "Evidence",
    "FileStorage",
    "Finding",
    "Handler",
    "IngestResult",
    "InvalidRecord",
    "JsonlSink",
    "LogSink",
    "MemoryStorage",
    "Proportion",
    "Record",
    "Report",
    "SQLiteStorage",
    "Section",
    "SequentialistError",
    "Spec",
    "SpecError",
    "Storage",
    "StorageError",
    "Stream",
    "Table",
    "Task",
    "TaskError",
    "TaskLocked",
    "WebhookSink",
    "__version__",
    "json_schema",
    "load_spec",
    "open_storage",
    "parse_spec",
    "proportion",
    "pseudonymize",
    "replay",
    "run_simulations",
    "shard_of",
    "spec_id",
    "verify_audit",
]

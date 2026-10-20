# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""The audit log: every decision and action of a task, chained by hashes.

An entry records what was decided (``decision``), on what grounds
(``basis``: the rule and its numbers), from which evidence (``evidence``:
for every stream consulted, the procedure, its guarantee, the step and
the history digest of sequential-analysis), and, for
actions of people, who did it and why (``actor``, ``reason``).

Each entry holds the hash of the one before it::

    hash_n = SHA-256(hash_{n-1} || canonical JSON of entry n without its hash)

so an entry changed, removed or inserted afterwards breaks the chain at
that point, which `verify_audit` reports.  The chain shows tampering; it
does not prevent it, and it does not by itself make a system compliant
with any regulation.

The history digest in the evidence identifies the exact sequence of
inputs a procedure had seen; given the archived observations, anyone can
recompute it with sequential-analysis (in Python or TypeScript) and
check that the decision followed from the data.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Sequence

from .canonical import canonical_json
from .errors import SequentialistError

FORMAT = "sequentialist.audit/1"
GENESIS = hashlib.sha256(FORMAT.encode("utf-8")).hexdigest()
#: What a conclusion can promise, as sequential-analysis states it on every procedure.
GUARANTEES = ("exact", "asymptotic", "model", "approximate", "none")


class AuditError(SequentialistError):
    """The audit log is broken: the message names the first entry that does not verify."""


@dataclass(frozen=True)
class Evidence:
    """One stream's state behind a decision."""

    stream: str
    run_id: str
    t: int
    procedure: dict[str, Any]  # the procedure's identity(): name, version, config
    guarantee: str
    history_digest: str
    output: Any = None  # the stream's last output, encoded

    @classmethod
    def of(cls, stream: str, run: Any, output: Any = None) -> Evidence:
        """Evidence from a sequential_analysis Run (between transitions)."""
        return cls(stream, run.run_id, run.t, run.procedure.identity(), str(run.procedure.guarantee),
                   run.history_digest, output)

    def to_json(self) -> dict[str, Any]:
        return {"stream": self.stream, "run_id": self.run_id, "t": self.t, "procedure": self.procedure,
                "guarantee": self.guarantee, "history_digest": self.history_digest, "output": self.output}


@dataclass(frozen=True)
class AuditEntry:
    n: int  # position in the task's log, from 0
    task_id: str
    spec_id: str
    kind: str  # "created", "decision", "action", "segment", ...
    decision: str | None
    at: float  # seconds since the epoch, by the engine's clock
    basis: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[dict[str, Any], ...] = ()
    actor: str | None = None
    reason: str | None = None
    prev: str = GENESIS
    hash: str = ""

    def body(self) -> dict[str, Any]:
        return {"format": FORMAT, "n": self.n, "task_id": self.task_id, "spec_id": self.spec_id, "kind": self.kind,
                "decision": self.decision, "at": self.at, "basis": self.basis, "evidence": list(self.evidence),
                "actor": self.actor, "reason": self.reason, "prev": self.prev}

    def to_json(self) -> dict[str, Any]:
        return {**self.body(), "hash": self.hash}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AuditEntry:
        return cls(n=data["n"], task_id=data["task_id"], spec_id=data["spec_id"], kind=data["kind"],
                   decision=data["decision"], at=data["at"], basis=data["basis"], evidence=tuple(data["evidence"]),
                   actor=data["actor"], reason=data["reason"], prev=data["prev"], hash=data["hash"])

    def sealed(self) -> AuditEntry:
        """The entry with its hash computed."""
        body = self.body()
        digest = hashlib.sha256((self.prev + canonical_json(body)).encode("utf-8")).hexdigest()
        return AuditEntry(**{**self.__dict__, "hash": digest})

    def same_event(self, other: AuditEntry) -> bool:
        """The same decision or action, whenever it was written (`at` and the hashes aside)."""
        mine, theirs = self.body(), other.body()
        for name in ("at", "prev"):
            mine.pop(name)
            theirs.pop(name)
        return canonical_json(mine) == canonical_json(theirs)


def verify_audit(entries: Sequence[dict[str, Any]], task_id: str | None = None) -> int:
    """Check the chain of a task's audit log; return the number of entries, AuditError at the first bad one."""
    prev = GENESIS
    for i, data in enumerate(entries):
        try:
            entry = AuditEntry.from_json(data)
        except (KeyError, TypeError) as error:
            raise AuditError(f"entry {i}: malformed ({error!r})") from None
        if data.get("format") != FORMAT:
            raise AuditError(f"entry {i}: format {data.get('format')!r}, expected {FORMAT!r}")
        if entry.n != i:
            raise AuditError(f"entry {i}: numbered {entry.n}; an entry is missing or out of place")
        if task_id is not None and entry.task_id != task_id:
            raise AuditError(f"entry {i}: belongs to task {entry.task_id!r}")
        if entry.prev != prev:
            raise AuditError(f"entry {i}: does not follow the entry before it")
        if entry.sealed().hash != entry.hash:
            raise AuditError(f"entry {i}: its content does not match its hash; it was changed")
        for evidence in entry.evidence:
            if evidence.get("guarantee") not in GUARANTEES:
                raise AuditError(f"entry {i}: evidence without a known guarantee")
        prev = entry.hash
    return len(entries)

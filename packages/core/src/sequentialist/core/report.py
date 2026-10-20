# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Reports in JSON and Markdown in which every conclusion names its guarantee.

A `Finding` cannot be made without a guarantee, one of those
sequential-analysis states on its procedures (exact, asymptotic, model,
approximate, none); the Markdown rendering prints it next to the
conclusion and explains the terms at the end.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from sequential_analysis.core import GUARANTEES as _MEANINGS

from .audit import GUARANTEES
from .canonical import is_plain
from .schema import format_timestamp


@dataclass(frozen=True)
class Finding:
    statement: str
    guarantee: str
    values: dict[str, Any] = field(default_factory=dict)
    stream: str | None = None
    procedure: dict[str, Any] | None = None  # identity() of the procedure behind it
    history_digest: str | None = None
    notes: Sequence[str] = ()

    def __post_init__(self) -> None:
        if self.guarantee not in GUARANTEES:
            raise ValueError(f"a finding needs a guarantee, one of {GUARANTEES}; got {self.guarantee!r}")
        if not is_plain(self.values):
            raise ValueError("the values of a finding must be plain JSON")

    def to_json(self) -> dict[str, Any]:
        return {"statement": self.statement, "guarantee": self.guarantee, "values": self.values,
                "stream": self.stream, "procedure": self.procedure, "history_digest": self.history_digest,
                "notes": list(self.notes)}


@dataclass(frozen=True)
class Table:
    columns: Sequence[str]
    rows: Sequence[Sequence[Any]]
    caption: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"caption": self.caption, "columns": list(self.columns), "rows": [list(r) for r in self.rows]}


@dataclass
class Section:
    title: str
    text: str = ""
    findings: list[Finding] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"title": self.title, "text": self.text, "findings": [f.to_json() for f in self.findings],
                "tables": [t.to_json() for t in self.tables]}


def fmt(value: Any) -> str:
    """A value as a report prints it: numbers to four significant digits, None as a dash."""
    if value is None:
        return "—"
    if type(value) is bool:
        return "yes" if value else "no"
    if type(value) is float:
        if not math.isfinite(value):
            return str(value)
        if value == 0:
            return "0"
        if abs(value) >= 1e5 or abs(value) < 1e-3:
            return f"{value:.3e}"
        return f"{value:.4g}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(fmt(v) for v in value) + "]"
    return str(value)


@dataclass
class Report:
    title: str
    task_id: str
    spec_id: str
    generated_at: float
    summary: str = ""
    sections: list[Section] = field(default_factory=list)

    def findings(self) -> list[Finding]:
        return [f for s in self.sections for f in s.findings]

    def to_json(self) -> dict[str, Any]:
        return {"format": "sequentialist.report/1", "title": self.title, "task_id": self.task_id,
                "spec_id": self.spec_id, "generated_at": self.generated_at, "summary": self.summary,
                "sections": [s.to_json() for s in self.sections]}

    def to_markdown(self) -> str:
        out = [f"# {self.title}", "",
               f"Task `{self.task_id}`, spec `{self.spec_id[:16]}…`, generated {format_timestamp(self.generated_at)}.",
               ""]
        if self.summary:
            out += [self.summary, ""]
        used = set()
        for section in self.sections:
            out += [f"## {section.title}", ""]
            if section.text:
                out += [section.text, ""]
            for finding in section.findings:
                used.add(finding.guarantee)
                out.append(f"- {finding.statement} *Guarantee: {finding.guarantee}.*")
                if finding.values:
                    out.append("  " + "; ".join(f"{k} = {fmt(v)}" for k, v in finding.values.items()))
                for note in finding.notes:
                    out.append(f"  {note}")
            if section.findings:
                out.append("")
            for table in section.tables:
                if table.caption:
                    out += [table.caption, ""]
                out.append("| " + " | ".join(table.columns) + " |")
                out.append("|" + "---|" * len(table.columns))
                for row in table.rows:
                    out.append("| " + " | ".join(fmt(v) for v in row) + " |")
                out.append("")
                if "guarantee" in table.columns:
                    index = list(table.columns).index("guarantee")
                    used.update(str(r[index]) for r in table.rows if r[index] in GUARANTEES)
        if used:
            out += ["## Guarantees", ""]
            for name in GUARANTEES:
                if name in used:
                    out.append(f"- **{name}**: {_MEANINGS[name]}.")
            out.append("")
        return "\n".join(out)

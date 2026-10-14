"""Measure, from Claude Code transcripts, whether task state crossed each compaction.

A transcript keeps both the compaction boundary and every Mashu call under one Claude Code
session, so the measure needs no link between hook and MCP session ids. The transcript format
is Claude Code's own and undocumented; lines it cannot parse are counted, not guessed at.
"""

from __future__ import annotations

import json
import os
import pathlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: Tool calls that change files, so a checkpoint written before them no longer covers the work.
EDITS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})


def transcripts_root() -> pathlib.Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    home = pathlib.Path(base).expanduser() if base else pathlib.Path.home() / ".claude"
    return home / "projects"


def mashu_verb(name: str) -> str | None:
    """The Mashu tool a call names, under whatever server name the client registered."""
    # A plugin registers its server as mcp__plugin_<plugin>_<server>__<tool>.
    if name.startswith("mcp__") and "mashu" in name.split("__")[1]:
        return name.rsplit("__", 1)[-1]
    return None


@dataclass(frozen=True)
class Call:
    name: str
    task_id: str | None


@dataclass(frozen=True)
class Boundary:
    uuid: str
    trigger: str | None
    at: datetime | None
    cwd: str | None


@dataclass(frozen=True)
class Compaction:
    uuid: str
    trigger: str | None
    at: datetime | None
    project: str
    #: Tool calls in the context window the compaction summarized.
    calls_before: int
    #: The task the session last checkpointed or read before the compaction, in this window or
    #: an earlier one; None when it had touched no task.
    task_id: str | None
    #: Calls and edits after the window's last task_checkpoint; None when it wrote none.
    calls_since_checkpoint: int | None
    edits_since_checkpoint: int | None
    calls_after: int
    #: Calls made after the compaction before task_get read the same task; None when it never did.
    calls_before_retrieval: int | None
    edits_before_retrieval: int
    #: Whether task_get read some other task first, which may be a deliberate switch of work.
    read_other_task: bool


@dataclass(frozen=True)
class Report:
    transcripts: int
    unreadable_lines: int
    compactions: list[Compaction]


def _time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def read(path: pathlib.Path) -> tuple[list[Call | Boundary], int]:
    """The boundaries and tool calls of the main conversation, and the lines that did not parse."""
    out: list[Call | Boundary] = []
    unreadable = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                unreadable += 1
                continue
            # Subagent turns run in their own context, which this compaction did not summarize.
            if not isinstance(entry, dict) or entry.get("isSidechain"):
                continue
            if entry.get("subtype") == "compact_boundary":
                meta = entry.get("compactMetadata") or {}
                at, cwd = _time(entry.get("timestamp")), entry.get("cwd")
                out.append(Boundary(str(entry.get("uuid")), meta.get("trigger"), at, cwd))
                continue
            content = (entry.get("message") or {}).get("content")
            if entry.get("type") != "assistant" or not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    given = part.get("input")
                    task_id = given.get("task_id") if isinstance(given, dict) else None
                    out.append(Call(str(part.get("name")), task_id))
    return out, unreadable


def _current_task(calls: Iterable[Call], task_id: str | None) -> str | None:
    """The task the session was on after these calls, starting from `task_id`."""
    for call in calls:
        if mashu_verb(call.name) in ("task_checkpoint", "task_get") and call.task_id:
            task_id = call.task_id
    return task_id


def _measure(
    boundary: Boundary, before: list[Call], after: list[Call], task_id: str | None
) -> Compaction:
    last_checkpoint = None
    for index, call in enumerate(before):
        if mashu_verb(call.name) == "task_checkpoint":
            last_checkpoint = index
    since = before[last_checkpoint + 1 :] if last_checkpoint is not None else None

    retrieval, edits, other = None, 0, False
    for index, call in enumerate(after):
        if mashu_verb(call.name) == "task_get" and task_id:
            if call.task_id == task_id:
                retrieval = index
                break
            other = True
        edits += call.name in EDITS
    return Compaction(
        uuid=boundary.uuid,
        trigger=boundary.trigger,
        at=boundary.at,
        project=pathlib.PurePath(boundary.cwd).name if boundary.cwd else "",
        calls_before=len(before),
        task_id=task_id,
        calls_since_checkpoint=None if since is None else len(since),
        edits_since_checkpoint=None if since is None else sum(c.name in EDITS for c in since),
        calls_after=len(after),
        calls_before_retrieval=retrieval,
        edits_before_retrieval=edits,
        read_other_task=other,
    )


def compactions_in(events: list[Call | Boundary]) -> list[Compaction]:
    marks = [index for index, event in enumerate(events) if isinstance(event, Boundary)]
    out = []
    task_id = None
    for n, mark in enumerate(marks):
        start = marks[n - 1] + 1 if n else 0
        end = marks[n + 1] if n + 1 < len(marks) else len(events)
        boundary = events[mark]
        assert isinstance(boundary, Boundary)
        before = [e for e in events[start:mark] if isinstance(e, Call)]
        after = [e for e in events[mark + 1 : end] if isinstance(e, Call)]
        # Work carries over a compaction, so a window that never named its task still has one.
        task_id = _current_task(before, task_id)
        out.append(_measure(boundary, before, after, task_id))
    return out


def scan(paths: Iterable[pathlib.Path] | None = None, *, days: float | None = None) -> Report:
    """Every compaction in these transcripts, or in all local ones, within the last `days`."""
    files = list(paths) if paths is not None else sorted(transcripts_root().glob("*/*.jsonl"))
    unreadable = 0
    found: dict[str, Compaction] = {}
    for path in files:
        try:
            events, bad = read(path)
        except OSError:
            continue
        unreadable += bad
        for compaction in compactions_in(events):
            # A resumed or forked transcript copies the boundaries of the one it continues; the
            # copy that runs longest after a boundary is where the work went on.
            kept = found.get(compaction.uuid)
            if kept is None or compaction.calls_after > kept.calls_after:
                found[compaction.uuid] = compaction
    rows = list(found.values())
    if days is not None:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        rows = [row for row in rows if row.at is not None and row.at >= cutoff]
    rows.sort(key=lambda row: row.at or datetime.min.replace(tzinfo=UTC))
    return Report(transcripts=len(files), unreadable_lines=unreadable, compactions=rows)

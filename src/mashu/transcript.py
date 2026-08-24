"""Reading a CLI's session transcript (16.3).

The transcript format is not a stable interface — it belongs to the CLI, which
changes it whenever it likes — so the per-CLI knowledge is confined here and
pinned by fixtures. Everything upstream works on turns.

Three things a raw transcript is bad at, and all three are handled here.

It is mostly not conversation. A few megabytes of tool payloads surround a much
smaller exchange, and the extraction judges what was concluded, not the bytes
that flowed through, so reasoning blocks and result bodies are dropped or
clipped.

It repeats itself. Measured over two real sessions, 33% and 56% of what
survives the clipping is a turn whose text appeared earlier word for word —
notifications, boilerplate result lines, the same file edited the same way. An
agent-driven session is mostly made of these, and paying for the second copy
buys nothing the first did not already say. So a repeat is rendered as a
place-holder that keeps its position, its role and a few characters of what it
was, which leaves the order and the frequency intact and pays for the body
once.

Folding is decided against what has actually been sent to a model, not against
what is earlier in the file, and the difference is the whole correctness of it.
Sequential windows may fold across each other, because window one really did
carry the bodies window two refers back to, and that is where the saving lives:
folded inside one window it was 3%, because the repeats are scattered across an
evening rather than bunched, and folded across the windows of a session it is
47%. A reading that skips about the file may not, because the first occurrence
of a text may sit in a stretch no call ever saw — a place-holder standing for a
body nobody read is worse than the duplicate it saved.

And it grows. A session that is extracted twice would pay for its first half
again, every time, so every turn carries the ordinal of the record it came from
and a checkpoint slices the file at the point the last successful run stopped.
Records are only ever appended, which is what makes an ordinal stable enough to
be a checkpoint.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime

#: Tool arguments worth keeping, in the order they are tried.
TOOL_SUMMARY_KEYS = ("description", "command", "file_path", "pattern", "query", "prompt", "url")

TOOL_ARG_LIMIT = 160
RESULT_LIMIT = 200

#: A repeat shorter than this is printed in full. Below roughly this length the
#: place-holder costs as much as the text it stands in for, and an unreadable
#: log is a worse trade than a few duplicated lines.
FOLD_MIN_CHARS = 40

#: How much of a repeated turn its place-holder echoes. Not zero: folding is
#: decided over the session, so the body a place-holder stands for may be in a
#: window this call did not read, and without any handle at all the reader has
#: nothing. Not long either — measured over a real session, 24 characters cost
#: 13% of the whole rendered log to say what 12 says.
FOLD_ECHO = 12

CLIS = ("claude", "codex")


@dataclass
class Turn:
    """One thing said, where in the file it was said, and when."""

    ordinal: int
    role: str
    text: str
    at: datetime | None = None


@dataclass
class Session:
    path: pathlib.Path
    source_cli: str
    external_id: str | None = None
    cwd: str | None = None
    title: str | None = None
    records: int = 0
    turns: list[Turn] = field(default_factory=list)

    def sent(self, checkpoint: int | None) -> list[str]:
        """The texts of the turns an earlier call carried, for folding against.

        Only meaningful for a reading that took its windows from the front:
        those cover every turn up to the mark, so a repeat after it really was
        shown. A reading that selected turns here and there covers nothing in
        particular, and the caller passes nothing.
        """
        if not checkpoint:
            return []
        return [turn.text for turn in self.turns if turn.ordinal <= checkpoint]

    def since(self, checkpoint: int | None) -> list[Turn]:
        """The turns that arrived after the last successful extraction."""
        if not checkpoint:
            return list(self.turns)
        return [turn for turn in self.turns if turn.ordinal > checkpoint]

    def user_turns(self, turns: list[Turn] | None = None) -> int:
        return sum(1 for turn in (self.turns if turns is None else turns) if turn.role == "user")


def _at(record: dict) -> datetime | None:
    """When this record was written, if the CLI said.

    The only handle there is on where in a session something happened. A
    scratch item knows the moment it was put down and nothing about the file it
    was put down beside, so without this the two cannot be lined up and the
    extraction has no choice but to read everything.
    """
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.astimezone()


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:32]


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + " …"


# --------------------------------------------------------------------------
# claude code
# --------------------------------------------------------------------------
def _blocks(message: dict) -> list[dict]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def _summarise_tool(block: dict) -> str:
    name = block.get("name", "tool")
    args = block.get("input") or {}
    for key in TOOL_SUMMARY_KEYS:
        if args.get(key):
            return f"[{name}] {_clip(args[key], TOOL_ARG_LIMIT)}"
    return f"[{name}]"


def _claude_user(record: dict) -> str | None:
    if record.get("isMeta"):
        return None
    parts: list[str] = []
    for block in _blocks(record.get("message") or {}):
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "tool_result":
            body = block.get("content")
            if isinstance(body, list):
                body = " ".join(b.get("text", "") for b in body if isinstance(b, dict))
            marker = "error" if block.get("is_error") else "result"
            parts.append(f"    ({marker}: {_clip(body or '', RESULT_LIMIT)})")
    text = "\n".join(p for p in parts if p.strip())
    return text or None


def _claude_assistant(record: dict) -> str | None:
    parts: list[str] = []
    for block in _blocks(record.get("message") or {}):
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "tool_use":
            parts.append("    " + _summarise_tool(block))
        # thinking is dropped: it is the largest part of a transcript, and the
        # extraction judges what was concluded rather than how it was reached.
    text = "\n".join(p for p in parts if p.strip())
    return text or None


def _read_claude(path: pathlib.Path, session: Session) -> Session:
    for ordinal, record in _records(path):
        session.records = ordinal
        if record.get("cwd") and not session.cwd:
            session.cwd = record["cwd"]
        if record.get("sessionId") and not session.external_id:
            session.external_id = record["sessionId"]
        if not session.title:
            session.title = record.get("aiTitle") or record.get("customTitle")
        if record.get("isSidechain"):
            continue  # subagent transcripts have their own files
        kind = record.get("type")
        if kind == "user":
            text = _claude_user(record)
        elif kind == "assistant":
            text = _claude_assistant(record)
        else:
            continue
        if text:
            session.turns.append(
                Turn(ordinal, "user" if kind == "user" else "assistant", text.strip(), _at(record))
            )
    return session


# --------------------------------------------------------------------------
# codex
# --------------------------------------------------------------------------
def _read_codex(path: pathlib.Path, session: Session) -> Session:
    for ordinal, record in _records(path):
        session.records = ordinal
        payload = record.get("payload") or {}
        kind = record.get("type")

        if kind == "session_meta":
            session.external_id = (
                session.external_id or payload.get("session_id") or payload.get("id")
            )
            session.cwd = session.cwd or payload.get("cwd")
            continue

        # The user's own prompt and the agent's own message, before either is
        # wrapped in the developer preamble that the response_item carries.
        if kind == "event_msg" and payload.get("type") == "user_message":
            text = (payload.get("message") or "").strip()
            if text:
                session.turns.append(Turn(ordinal, "user", text, _at(record)))
        elif kind == "event_msg" and payload.get("type") == "agent_message":
            text = (payload.get("message") or "").strip()
            if text:
                session.turns.append(Turn(ordinal, "assistant", text, _at(record)))
        elif kind == "response_item" and payload.get("type") == "custom_tool_call":
            name = payload.get("name", "tool")
            session.turns.append(
                Turn(
                    ordinal,
                    "assistant",
                    f"    [{name}] {_clip(payload.get('input') or '', TOOL_ARG_LIMIT)}",
                    _at(record),
                )
            )
    return session


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------
def _records(path: pathlib.Path):
    with path.open(encoding="utf-8", errors="replace") as handle:
        for ordinal, line in enumerate(handle, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield ordinal, record


def sniff(path: pathlib.Path) -> str:
    """Which CLI wrote this file, from its first records rather than its path.

    A path can be moved or copied; the shape of the first record cannot.
    """
    for ordinal, record in _records(path):
        if record.get("type") == "session_meta" or "payload" in record:
            return "codex"
        if "sessionId" in record or "parentUuid" in record:
            return "claude"
        if ordinal > 20:
            break
    return "claude"


def peek(path: pathlib.Path, *, source_cli: str | None = None, records: int = 200) -> Session:
    """The session id and working directory, without reading the whole file.

    The session-end hook runs inside a CLI's exit path, which is measured in
    seconds, so it may not walk a few megabytes to learn two fields that both
    appear in the opening records.
    """
    path = pathlib.Path(path)
    cli = source_cli or sniff(path)
    session = Session(path=path, source_cli=cli)
    for ordinal, record in _records(path):
        payload = record.get("payload") or {}
        session.external_id = (
            session.external_id or record.get("sessionId") or payload.get("session_id")
        )
        session.cwd = session.cwd or record.get("cwd") or payload.get("cwd")
        if session.external_id and session.cwd:
            break
        if ordinal >= records:
            break
    return session


def read(path: pathlib.Path, *, source_cli: str | None = None) -> Session:
    """Turn one transcript into turns, keeping the ordinals a checkpoint needs."""
    path = pathlib.Path(path)
    cli = source_cli or sniff(path)
    session = Session(path=path, source_cli=cli)
    if cli == "codex":
        return _read_codex(path, session)
    return _read_claude(path, session)


def around(turns: list[Turn], moments: list[datetime], *, before: int, after: int) -> list[Turn]:
    """The turns either side of each moment, in order, each at most once.

    What a session flagged while it ran is a point in time, and the transcript
    is a sequence. This is the join between them, and it is the whole of what
    makes reading a fraction of a session defensible: the fraction is the one
    the session itself pointed at.

    The clock places the anchor and nothing else. The span around it is cut on
    the turns themselves, including the ones the CLI gave no timestamp: a
    transcript where every turn is undated falls back to reading the log, but
    one where a single turn happens to be dated would otherwise anchor there
    and drop every neighbour it has — the worst of the two, arrived at by the
    partial case rather than the missing one.

    Returns nothing when the transcript carries no clock at all, which is the
    case that has to fall back rather than quietly read a tenth of it.
    """
    placed = [(turn.at, index) for index, turn in enumerate(turns) if turn.at is not None]
    if not placed or not moments:
        return []

    clock = [moment for moment, _ in placed]
    keep: set[int] = set()
    for moment in moments:
        # The last turn at or before the moment: a note is written after the
        # thing it is about, so the span that matters is mostly behind it.
        found = bisect_right(clock, moment) - 1
        at = placed[found][1] if found >= 0 else 0
        keep.update(range(max(0, at - before + 1), min(len(turns), at + after + 1)))
    return [turns[index] for index in sorted(keep)]


def chunks(turns: list[Turn], *, sent: list[str] | None = None) -> list[str]:
    """One rendered block per turn, with repeats folded to a place-holder.

    sent is what an earlier call already carried; a text in it may be folded
    here because the body has been seen. Everything else folds only against
    what precedes it inside this selection, which is what stops a place-holder
    from standing for a body no call ever read.

    Reading forwards, so the first occurrence of a text is decided by what
    precedes it and nothing later can change an earlier block. A prefix of
    these turns therefore yields a prefix of these blocks.
    """
    seen: set[str] = set(sent or ())
    out: list[str] = []
    for turn in turns:
        if len(turn.text) >= FOLD_MIN_CHARS and turn.text in seen:
            out.append(f"\n## {turn.role} (再掲: {_clip(turn.text, FOLD_ECHO)})")
        else:
            seen.add(turn.text)
            out.append(f"\n## {turn.role}\n\n{turn.text}")
    return out


def render(
    session: Session, turns: list[Turn] | None = None, *, sent: list[str] | None = None
) -> str:
    """The condensed log the extraction reads."""
    turns = session.turns if turns is None else turns
    header = [
        f"# session {session.external_id or session.path.stem}",
        "",
        f"- cli: {session.source_cli}",
        f"- title: {session.title or '(none recorded)'}",
        f"- turns kept: {len(turns)}",
        "- このセッションで既に出た内容は「(再掲: …)」に畳んである。順序と回数はそのまま。",
        "- 畳まれた本文が前の窓にあることもある。その窓はこの抽出が既に読んでいる。",
    ]
    return "\n".join(header) + "\n" + "\n".join(chunks(turns, sent=sent)) + "\n"

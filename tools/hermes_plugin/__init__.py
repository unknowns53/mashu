"""Hermes plugin that delivers the Mashu bootstrap into the first turn of a conversation.

Hermes starts the Mashu MCP server outside every project folder and wraps MCP results as
untrusted data, so the bootstrap rides the pre_llm_call context instead. The project comes from
mashu-channels.json in the Hermes home, which maps a Discord thread or channel ID to a project.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sqlite3
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

MARKER = (
    "Mashu: この会話に配信される知識。session_bootstrap は済んでいるので、改めて呼ばなくてよい。"
)

BOUND = (
    "この会話は Mashu の project「{project}」に結びついている。"
    "Mashu の MCP サーバはどの project のフォルダでも起動していないので、"
    "`project` を取る tool（task_create、memory_admit、memory_nominate、pain_report）には "
    'project="{project}" を渡す。'
)

UNBOUND = (
    "この会話はどの project にも結びついていない（このチャンネルの ID は {channel}）。"
    "project の作業なら session_bootstrap に project を渡して読み、"
    "`project` を取る tool にも同じ名前を渡す。"
    "このチャンネルを project に結びつけたいときは、"
    "mashu-channels.json に ID と project 名を書くようユーザーに伝える。"
)

MISSING = "対応表の project「{project}」を Mashu から読めなかった（{reason}）。"

CHANNEL_MAP = "mashu-channels.json"

PLATFORMS = frozenset({"discord"})

# Shorter than the 30 s callback timeout Hermes puts on pre_llm_call
# (hermes_cli/plugins_dispatch.py _HOOK_CALLBACK_TIMEOUT_SECS); past it the callback is
# abandoned and suppressed for 60 s.
BOOTSTRAP_TIMEOUT = 20

_REPO = pathlib.Path(__file__).resolve().parent.parent.parent
if os.name == "nt":
    # python.exe rather than mashu.exe: a running mashu.exe blocks uv sync from replacing it.
    _MASHU = [str(_REPO / ".venv" / "Scripts" / "python.exe"), "-m", "mashu"]
else:
    _MASHU = [str(_REPO / ".venv" / "bin" / "python"), "-m", "mashu"]


def _hermes_home() -> pathlib.Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _texts(message: Any) -> list[str]:
    """Every string a message carries to the model, including the api_content sidecar."""
    if not isinstance(message, dict):
        return []
    found = []
    for key in ("content", "api_content"):
        value = message.get(key)
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, list):
            found.extend(
                part["text"]
                for part in value
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
    return found


def _delivered(history: Any) -> bool:
    # A replayed turn sends its api_content again (agent/turn_context.py build_api_messages), so
    # the marker stays visible until compaction replaces the turn with a summary.
    return any(MARKER in text for message in history or [] for text in _texts(message))


def _session_ids(session_id: str) -> tuple[list[tuple[str, str]], str]:
    """(kind, id) candidates in lookup order, and the ID to name in the unbound notice."""
    try:
        from gateway.session_context import get_session_env

        thread = get_session_env("HERMES_SESSION_THREAD_ID")
        parent = get_session_env("HERMES_SESSION_PARENT_CHAT_ID")
        chat = get_session_env("HERMES_SESSION_CHAT_ID")
    except Exception:
        thread = parent = chat = ""
    source = "session_env"
    if not (thread or chat) and session_id:
        thread, chat = _stored_ids(session_id)
        source = "state_db"
    ids = [
        (kind, value)
        for kind, value in (("thread", thread), ("parent", parent), ("chat", chat))
        if value
    ]
    # Hermes stores a thread's own ID as its chat_id, so a thread names itself in the notice.
    return [(f"{source}:{kind}", value) for kind, value in ids], thread or chat


def _stored_ids(session_id: str) -> tuple[str, str]:
    path = _hermes_home() / "state.db"
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2) as db:
            row = db.execute(
                "SELECT thread_id, chat_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
    except sqlite3.Error as exc:
        logger.warning("mashu: state.db lookup failed for session %s: %s", session_id, exc)
        return "", ""
    return (str(row[0] or ""), str(row[1] or "")) if row else ("", "")


def _channel_map() -> dict[str, str]:
    path = _hermes_home() / CHANNEL_MAP
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("mashu: %s is unreadable, treated as empty: %s", path, exc)
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _bootstrap(project: str | None) -> subprocess.CompletedProcess[str] | None:
    args = [*_MASHU, "bootstrap", *(["--project", project] if project else [])]
    # The CLI writes through the console code page unless told otherwise, and cp932 cannot
    # encode every character a memory may hold.
    env = {**os.environ, "PYTHONUTF8": "1"}
    try:
        # cwd is the Hermes home, which belongs to no project, so an unbound conversation gets
        # no project whatever folder the gateway was started from.
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=BOOTSTRAP_TIMEOUT,
            cwd=str(_hermes_home()),
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("mashu: bootstrap failed to run (%s): %s", " ".join(args[-3:]), exc)
        return None


def _on_pre_llm_call(**kwargs: Any) -> dict[str, str] | None:
    session_id = str(kwargs.get("session_id") or "")
    # Cron runs ("cron", cron/scheduler.py) and delegated children ("subagent",
    # tools/delegate_tool.py) open a fresh history on every run; only a person's chat gets it.
    if kwargs.get("platform") not in PLATFORMS:
        return None
    if _delivered(kwargs.get("conversation_history")):
        logger.info("mashu: skipped session=%s (already delivered)", session_id)
        return None

    ids, channel = _session_ids(session_id)
    table = _channel_map()
    hit = next(((kind, value) for kind, value in ids if value in table), None)
    project = table[hit[1]] if hit else None

    lines = [MARKER]
    done = _bootstrap(project)
    if project and done is not None and done.returncode == 1:
        logger.warning(
            "mashu: project %r from %s is not in Mashu: %s",
            project,
            CHANNEL_MAP,
            done.stderr.strip(),
        )
        reason = (done.stderr.strip().splitlines() or ["exit 1"])[-1]
        lines.append(MISSING.format(project=project, reason=reason))
        project = None
        done = _bootstrap(None)
    if done is None or done.returncode != 0 or not done.stdout.strip():
        logger.warning(
            "mashu: failed session=%s project=%s exit=%s stderr=%s",
            session_id,
            project,
            None if done is None else done.returncode,
            "" if done is None else done.stderr.strip()[:300],
        )
        return None

    lines.append(
        BOUND.format(project=project) if project else UNBOUND.format(channel=channel or "不明")
    )
    context = "\n".join(lines) + "\n\n" + done.stdout.rstrip()
    logger.info(
        "mashu: delivered session=%s project=%s via=%s ids=%s chars=%d",
        session_id,
        project,
        hit[0] if hit else "none",
        ",".join(kind for kind, _ in ids) or "none",
        len(context),
    )
    return {"context": context}


def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)

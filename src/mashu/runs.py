"""The ledger that stops an unattended capture from failing quietly (16.3).

Automatic capture that fails silently is worse than no capture at all: the
store looks maintained while it stops being maintained, and nobody is watching
for the difference. So every transcript ever seen has a row, including the ones
deliberately skipped, and the health of the whole is answerable in one query.

Where that answer goes matters as much as the answer. It is pushed through
session bootstrap rather than waiting in a dashboard, because during a week
nobody attends, the next session is the only reader that is guaranteed to
arrive.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu.errors import MashuError

#: How many times a transcript is retried before it is left for a person.
MAX_ATTEMPTS = 3

#: When the health line starts appearing at a session start.
FAILED_WARNING_THRESHOLD = 1
STALE_QUEUE_HOURS = 24

#: Input tokens capture may spend in one day (16.3). Over it, work is deferred
#: rather than dropped: a transcript nobody read is knowledge that never
#: arrives, and the ledger cannot tell that apart from a night with nothing to
#: say unless the deferral is written down.
DAILY_INPUT_BUDGET = 400_000


class RunError(MashuError):
    """A run cannot be moved the way the caller asked."""


def enqueue(
    cur: psycopg.Cursor,
    *,
    source_cli: str,
    external_session_id: str,
    transcript_digest: str,
    extractor_version: str,
    transcript_path: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Claim a transcript for extraction, once.

    The unique key is what makes an unattended retry safe: the hook, the
    sweeper that catches what the hook dropped, and a manual re-run all land on
    the same row instead of extracting the same session three times.
    """
    cur.execute(
        """
        INSERT INTO extraction_run
            (source_cli, external_session_id, transcript_digest, extractor_version,
             transcript_path, cwd)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_cli, external_session_id, transcript_digest, extractor_version)
        DO NOTHING
        RETURNING *
        """,
        (
            source_cli,
            external_session_id,
            transcript_digest,
            extractor_version,
            transcript_path,
            cwd,
        ),
    )
    row = cur.fetchone()
    if row is not None:
        return row

    cur.execute(
        """
        SELECT * FROM extraction_run
        WHERE source_cli = %s AND external_session_id = %s
          AND transcript_digest = %s AND extractor_version = %s
        """,
        (source_cli, external_session_id, transcript_digest, extractor_version),
    )
    return cur.fetchone()


def claim(cur: psycopg.Cursor, *, limit: int = 1) -> list[dict[str, Any]]:
    """Take the next runs to work on, skipping any another worker holds.

    Two orderings, and they are not the same one. The ORDER BY inside the
    subquery decides which rows are taken; RETURNING hands them back in
    whatever order the update happened to touch them, so the caller's order is
    restored afterwards. Left alone, a worker that stops after the first run of
    a batch stops on an arbitrary one.
    """
    cur.execute(
        """
        UPDATE extraction_run SET state = 'running', attempts = attempts + 1
        WHERE run_id IN (
            SELECT run_id FROM extraction_run
            WHERE (state = 'queued')
               OR (state = 'retrying' AND (next_retry_at IS NULL OR next_retry_at <= now()))
            ORDER BY seq
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        RETURNING *
        """,
        (limit,),
    )
    return sorted(cur.fetchall(), key=lambda row: row["seq"])


def succeeded(
    cur: psycopg.Cursor,
    *,
    run_id: UUID,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    note: str | None = None,
    checkpoint: int | None = None,
) -> dict[str, Any]:
    """Finish a run and mark how far into the transcript it read.

    The checkpoint is what stops a long session being paid for again every
    night: the next run over the same session starts from here.
    """
    row = _finish(cur, run_id, "succeeded", model, input_tokens, output_tokens, note, None)
    if checkpoint is not None:
        cur.execute(
            "UPDATE extraction_run SET checkpoint = %s WHERE run_id = %s RETURNING *",
            (checkpoint, run_id),
        )
        row = cur.fetchone()
    return row


def skipped(cur: psycopg.Cursor, *, run_id: UUID, note: str) -> dict[str, Any]:
    """Recorded, not dropped. A skip nobody can see is the same as a silent failure."""
    return _finish(cur, run_id, "skipped", None, None, None, note, None)


def held(cur: psycopg.Cursor, *, run_id: UUID, note: str) -> dict[str, Any]:
    """Stop before spending anything, and stay releasable (16.3).

    What this is for is an unmapped working directory. The alternative is
    guessing a scope, and a wrongly filed memory is not a worse version of a
    right one: it sits where the sessions that need it never look, and nothing
    about it reads as wrong.
    """
    return _finish(cur, run_id, "held", None, None, None, note, None)


def release(cur: psycopg.Cursor, *, cwd_prefix: str | None = None) -> int:
    """Put held runs back in the queue, once whatever held them is resolved."""
    cur.execute(
        """
        UPDATE extraction_run
        SET state = 'queued', note = NULL, completed_at = NULL
        WHERE state = 'held'
          AND (%(prefix)s::text IS NULL OR cwd LIKE %(prefix)s || '%%')
        """,
        {"prefix": cwd_prefix},
    )
    return cur.rowcount


def defer(cur: psycopg.Cursor, *, run_id: UUID, until_hours: int, note: str) -> dict[str, Any]:
    """Put a run back without spending one of its attempts.

    Deferral is not failure. The attempt counter exists to stop a broken
    transcript costing forever, and a run that never reached the model has not
    told us anything about whether it is broken.
    """
    cur.execute(
        """
        UPDATE extraction_run
        SET state = 'retrying', attempts = greatest(attempts - 1, 0), note = %s,
            next_retry_at = now() + make_interval(hours => %s)
        WHERE run_id = %s
        RETURNING *
        """,
        (note, until_hours, run_id),
    )
    row = cur.fetchone()
    if row is None:
        raise RunError(f"no run {run_id}")
    return row


def checkpoint_for(cur: psycopg.Cursor, *, source_cli: str, external_session_id: str) -> int | None:
    """How far a previous successful run read this session."""
    cur.execute(
        """
        SELECT max(checkpoint) AS mark FROM extraction_run
        WHERE source_cli = %s AND external_session_id = %s AND state = 'succeeded'
        """,
        (source_cli, external_session_id),
    )
    return (cur.fetchone() or {}).get("mark")


def spent_today(cur: psycopg.Cursor) -> int:
    """Input tokens capture has already spent since midnight (16.3)."""
    cur.execute(
        """
        SELECT coalesce(sum(input_tokens), 0) AS spent FROM extraction_run
        WHERE completed_at >= date_trunc('day', now())
        """
    )
    return int(cur.fetchone()["spent"])


def failed(cur: psycopg.Cursor, *, run_id: UUID, error: str) -> dict[str, Any]:
    """Retry until the attempts run out, then stop and leave it visible.

    Retrying forever would turn one broken transcript into a permanent cost,
    and a dead letter nobody sees would be the silence this ledger exists to
    prevent, so the count is bounded and the remains are reported.
    """
    cur.execute("SELECT attempts FROM extraction_run WHERE run_id = %s", (run_id,))
    row = cur.fetchone()
    if row is None:
        raise RunError(f"no run {run_id}")

    if row["attempts"] >= MAX_ATTEMPTS:
        return _finish(cur, run_id, "failed", None, None, None, None, error)

    backoff = 5 * (4 ** (row["attempts"] - 1))
    cur.execute(
        """
        UPDATE extraction_run
        SET state = 'retrying', last_error = %s,
            next_retry_at = now() + make_interval(mins => %s)
        WHERE run_id = %s
        RETURNING *
        """,
        (error, backoff, run_id),
    )
    return cur.fetchone()


def health(cur: psycopg.Cursor) -> dict[str, Any]:
    """One line for a session start: is capture still working?"""
    cur.execute(
        """
        SELECT
          count(*) FILTER (WHERE state = 'failed') AS failed,
          count(*) FILTER (WHERE state IN ('queued', 'retrying')) AS waiting,
          count(*) FILTER (WHERE state = 'succeeded') AS succeeded,
          count(*) FILTER (WHERE state = 'skipped') AS skipped,
          count(*) FILTER (WHERE state = 'held') AS held,
          EXTRACT(EPOCH FROM now() - min(created_at)
                  FILTER (WHERE state IN ('queued', 'retrying'))) / 3600 AS oldest_wait_hours
        FROM extraction_run
        """
    )
    row = dict(cur.fetchone())
    oldest = row["oldest_wait_hours"]
    row["oldest_wait_hours"] = round(float(oldest), 1) if oldest is not None else None
    row["ok"] = (
        row["failed"] < FAILED_WARNING_THRESHOLD
        and not row["held"]
        and (oldest is None or float(oldest) < STALE_QUEUE_HOURS)
    )
    row["warning"] = None if row["ok"] else _warning(row)
    return row


def _warning(row: dict[str, Any]) -> str:
    parts = []
    if row["failed"]:
        parts.append(f"{row['failed']} transcript(s) gave up after {MAX_ATTEMPTS} attempts")
    if row["oldest_wait_hours"] and row["oldest_wait_hours"] >= STALE_QUEUE_HOURS:
        parts.append(f"the oldest queued transcript has waited {row['oldest_wait_hours']}h")
    if row.get("held"):
        parts.append(
            f"{row['held']} transcript(s) are held because their working directory maps to no scope"
        )
    return (
        "capture is not keeping up: "
        + "; ".join(parts)
        + ". Nothing new is reaching the store from those sessions."
    )


def _finish(cur, run_id, state, model, inp, out, note, error) -> dict[str, Any]:
    cur.execute(
        """
        UPDATE extraction_run
        SET state = %s, model = %s, input_tokens = %s, output_tokens = %s,
            note = %s, last_error = %s, completed_at = now(), next_retry_at = NULL
        WHERE run_id = %s
        RETURNING *
        """,
        (state, model, inp, out, note, error, run_id),
    )
    row = cur.fetchone()
    if row is None:
        raise RunError(f"no run {run_id}")
    return row

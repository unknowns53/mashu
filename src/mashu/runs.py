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
from psycopg.types.json import Jsonb

from mashu.errors import MashuError

#: How many times a transcript is retried before it is left for a person.
MAX_ATTEMPTS = 3

#: When the health line starts appearing at a session start.
FAILED_WARNING_THRESHOLD = 1
STALE_QUEUE_HOURS = 24

#: After this, a run still marked running is taken to belong to a worker that
#: died. Nothing else would ever move it: the process that owned it is gone, so
#: without this the row sits in a state no query counts and the transcript is
#: never read again — the silent failure this ledger exists to prevent, arriving
#: through the ledger itself.
#:
#: Measured from claimed_at, never from created_at. created_at is when the
#: transcript was enqueued, so a backlog item — or any run the daily budget
#: deferred for a day — would be "stale" the instant it was claimed, and a
#: second worker would start the same extraction while the first was still
#: inside its model call.
STALE_RUNNING_HOURS = 2

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
    if row is None:
        cur.execute(
            """
            SELECT * FROM extraction_run
            WHERE source_cli = %s AND external_session_id = %s
              AND transcript_digest = %s AND extractor_version = %s
            """,
            (source_cli, external_session_id, transcript_digest, extractor_version),
        )
        return cur.fetchone()

    # A held row for the same session is about the same conversation, one
    # digest older. Left standing, adding the route releases both and two runs
    # read the session from the same mark.
    cur.execute(
        """
        UPDATE extraction_run
        SET state = 'skipped', completed_at = now(),
            note = 'superseded by a later digest of the same session'
        WHERE source_cli = %s AND external_session_id = %s AND state = 'held'
          AND run_id <> %s
        """,
        (source_cli, external_session_id, row["run_id"]),
    )
    return row


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
        UPDATE extraction_run
        SET state = 'running', attempts = attempts + 1, claimed_at = now()
        WHERE run_id IN (
            SELECT run_id FROM extraction_run
            WHERE (state = 'queued')
               OR (state = 'retrying' AND (next_retry_at IS NULL OR next_retry_at <= now()))
               OR (state = 'running'
                   AND claimed_at < now() - make_interval(hours => %s))
            ORDER BY seq
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        RETURNING *
        """,
        (STALE_RUNNING_HOURS, limit),
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


def defer(
    cur: psycopg.Cursor, *, run_id: UUID, note: str, until_hours: int | None = None
) -> dict[str, Any]:
    """Put a run back without spending one of its attempts.

    Deferral is not failure. The attempt counter exists to stop a broken
    transcript costing forever, and a run that never reached the model has not
    told us anything about whether it is broken.

    Leaving out until_hours means the next budget reset, and that is the right
    answer whenever the budget is what stopped the run. A fixed twenty-four
    hours looks equivalent and is not: the allowance frees at midnight while
    the run stays blocked until the same clock time tomorrow, so the one
    nightly pass in between finds it still shut and the run waits an extra
    whole day. Sixty transcripts deferred that way do not drain.
    """
    cur.execute(
        """
        UPDATE extraction_run
        SET state = 'retrying', attempts = greatest(attempts - 1, 0), note = %(note)s,
            next_retry_at = CASE
                WHEN %(hours)s::int IS NULL THEN date_trunc('day', now()) + interval '1 day'
                ELSE now() + make_interval(hours => %(hours)s::int)
            END
        WHERE run_id = %(run_id)s
        RETURNING *
        """,
        {"note": note, "hours": until_hours, "run_id": run_id},
    )
    row = cur.fetchone()
    if row is None:
        raise RunError(f"no run {run_id}")
    return row


def unclaim(cur: psycopg.Cursor, *, run_id: UUID) -> None:
    """Put a claimed run back untouched, without spending an attempt.

    A dry run reads and measures and decides nothing, so leaving the row marked
    running would strand it: no query counts that state and no later pass would
    pick it up.
    """
    cur.execute(
        "UPDATE extraction_run SET state = 'queued', attempts = greatest(attempts - 1, 0) "
        "WHERE run_id = %s AND state = 'running'",
        (run_id,),
    )


def spend(
    cur: psycopg.Cursor,
    *,
    run_id: UUID,
    input_tokens: int,
    output_tokens: int | None = None,
) -> None:
    """Add one model call's cost, to the run and to the moment it happened.

    Both, because they answer different questions. The run's total is what a
    session cost; the charge row is what a day cost, and summing the first to
    answer the second is how a windowed run came to pay yesterday's bill again
    every morning (0021).
    """
    cur.execute(
        """
        INSERT INTO extraction_charge (run_id, input_tokens, output_tokens)
        VALUES (%s, %s, %s)
        """,
        (run_id, input_tokens, output_tokens),
    )
    cur.execute(
        """
        UPDATE extraction_run
        SET input_tokens = coalesce(input_tokens, 0) + %s,
            output_tokens = coalesce(output_tokens, 0) + coalesce(%s, 0)
        WHERE run_id = %s
        """,
        (input_tokens, output_tokens, run_id),
    )


def record_dropped(cur: psycopg.Cursor, *, run_id: UUID, dropped: list[Any]) -> None:
    """Keep what the extraction read and chose not to propose (30 反証条件 1).

    The plan says a miss is an observable loss because it stays in the scratch.
    The scratch is cleared once extraction succeeds, so unless what was passed
    over is written down here, that claim is not true of the implementation and
    the falsification condition cannot be checked.

    Appended, for the same reason spend accumulates. A long session is read in
    windows and lands once per window, so an overwrite kept only the last
    window's passed-over items and erased every window before it. The condition
    this column exists for would then be checked against a record that is
    quietly short, which is worse than not keeping one: a falsifier reading it
    would see fewer misses than there were and conclude the extraction is
    keeping up.
    """
    if not dropped:
        return
    cur.execute(
        """
        UPDATE extraction_run
        SET dropped = coalesce(dropped, '[]'::jsonb) || %s::jsonb
        WHERE run_id = %s
        """,
        (Jsonb(dropped), run_id),
    )


def set_reading(cur: psycopg.Cursor, *, run_id: UUID, reading: str) -> None:
    """Which reading this run is using, kept between passes.

    Not a statistic. The next window is allowed to fold a repeat against what an
    earlier one carried only if the earlier one took its turns from the front,
    so the next pass has to be able to find out what the last one did.
    """
    cur.execute("UPDATE extraction_run SET reading = %s WHERE run_id = %s", (reading, run_id))


def advance(cur: psycopg.Cursor, *, run_id: UUID, checkpoint: int) -> None:
    """Move the mark without finishing the run (16.3).

    A session too long for one model call is read in windows at turn
    boundaries, and the mark has to move as each window is read. Otherwise the
    next window starts where the last one did and the run never reaches the
    end, which is the unbounded cost the windowing exists to avoid.
    """
    cur.execute("UPDATE extraction_run SET checkpoint = %s WHERE run_id = %s", (checkpoint, run_id))


def checkpoint_for(cur: psycopg.Cursor, *, source_cli: str, external_session_id: str) -> int | None:
    """How far this session has already been read.

    Every state counts, including failed. A long session is read in windows, so
    a run still in progress has a mark; and a run that gave up half way through
    still read the half it read. Excluding it would make the next digest of the
    same session pay for that half again.
    """
    cur.execute(
        """
        SELECT max(checkpoint) AS mark FROM extraction_run
        WHERE source_cli = %s AND external_session_id = %s
        """,
        (source_cli, external_session_id),
    )
    return (cur.fetchone() or {}).get("mark")


def spent_today(cur: psycopg.Cursor) -> int:
    """Input tokens capture has already spent since midnight (16.3).

    Summed over calls, each stamped when it was made. A run still working
    through its windows has therefore already contributed what it has already
    spent, without contributing it a second time tomorrow: the run's own total
    is cumulative, so a session windowed across midnight used to have every
    earlier day's spending counted again as today's, and one that had spent
    most of an allowance could never afford another window again (0021).
    """
    cur.execute(
        """
        SELECT coalesce(sum(input_tokens), 0) AS spent FROM extraction_charge
        WHERE charged_at >= date_trunc('day', now())
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

    # Integer minutes, and never a negative exponent: make_interval takes an
    # int, and a run that failed before its attempt was counted would otherwise
    # ask for a fractional wait and take down the recording of the failure.
    backoff = 5 * (4 ** max(row["attempts"] - 1, 0))
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
          count(*) FILTER (WHERE state = 'running'
                       AND claimed_at < now() - make_interval(hours => 2)) AS stranded,
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
        and not row["stranded"]
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
    if row.get("stranded"):
        parts.append(
            f"{row['stranded']} run(s) have been marked running for over "
            f"{STALE_RUNNING_HOURS}h, which means a worker died holding them"
        )
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
    # coalesce, not assignment. The counters are accumulated by spend() as each
    # model call happens, and a windowed run finishes with nothing more to add;
    # writing NULL over them at the end would erase the whole bill for exactly
    # the long sessions the daily budget exists to bound.
    cur.execute(
        """
        UPDATE extraction_run
        SET state = %s,
            model = coalesce(%s, model),
            input_tokens = coalesce(%s, input_tokens),
            output_tokens = coalesce(%s, output_tokens),
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

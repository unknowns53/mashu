"""What the running system is costing and how far behind it is (specification 27.3).

Every figure here is read back out of rows the system already writes. Nothing
is instrumented for the sake of being measured: the review queue, the event log
and the extraction ledger are what the answers come from, and a question they
cannot answer is reported as unanswered rather than approximated.

That last part is the point of the module. Section 27.3 names indicators whose
sources do not exist yet — a sitting is not timed, and content used after it
went stale is only visible once incidents are recorded (30 段 D). Printing the
rest without saying so would read as a complete picture of a system half of
whose failure modes are unwatched, which is the same mistake as a health line
that goes quiet when the thing it watches dies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import psycopg

from mashu import bootstrap
from mashu.models import Delivery

#: How far back the load figures look. Long enough that a quiet day does not
#: read as a stalled queue, short enough to still show a change of pace.
WINDOW_DAYS = 14

#: 27.3. Above this for two weeks, `unreviewed` has stopped telling agents
#: anything: what the tag needs is a contrast, not a balance.
TAG_SHARE_CEILING = 0.80


@dataclass
class Status:
    """The five-odd indicators of 27.3, and the ones with nowhere to read from."""

    capture: dict[str, Any] = field(default_factory=dict)
    queue: dict[str, Any] = field(default_factory=dict)
    latency: dict[str, Any] = field(default_factory=dict)
    unreviewed: list[dict[str, Any]] = field(default_factory=list)
    tag_share: dict[str, Any] = field(default_factory=dict)
    openings: list[dict[str, Any]] = field(default_factory=list)
    corrections: dict[str, Any] = field(default_factory=dict)
    self_dating: list[dict[str, Any]] = field(default_factory=list)
    unmeasured: list[str] = field(default_factory=list)


def collect(cur: psycopg.Cursor, *, window_days: int = WINDOW_DAYS) -> Status:
    """Read every indicator that has a source, and name the ones that do not."""
    from mashu import runs

    return Status(
        capture=runs.health(cur),
        queue=_queue(cur),
        latency=_latency(cur, window_days),
        unreviewed=_unreviewed_share(cur),
        tag_share=_tag_share(cur, window_days),
        openings=_openings(cur),
        corrections=_corrections(cur, window_days),
        self_dating=self_dating(cur),
        unmeasured=[
            "束あたりの処理時間 — the review sitting is not timed",
            "期限切れ・完了済みの内容が現在値として使われた件数 — needs incidents (30 段 D)",
            "例外的な Review に使った時間 — same, and not separable from the above",
        ],
    )


def _queue(cur: psycopg.Cursor) -> dict[str, Any]:
    """How much is waiting, in bundles, and how long the oldest has waited.

    Deferred proposals are counted apart from the rest. A sitting that put
    something off decided about it (30 段 C requires the note), so folding it
    back into "waiting" would report the backlog as untouched.
    """
    cur.execute(
        """
        SELECT count(*) AS proposals,
               count(DISTINCT coalesce(session_id::text, 'none')) AS bundles,
               count(*) FILTER (WHERE deferred_at IS NOT NULL) AS deferred,
               coalesce(max(EXTRACT(DAY FROM now() - created_at))::int, 0) AS oldest_days
        FROM proposal
        WHERE status = 'pending'
        """
    )
    return cur.fetchone()


def _latency(cur: psycopg.Cursor, window_days: int) -> dict[str, Any]:
    """How long a proposal sat before it was decided (27.3).

    Median and 90th percentile, never the mean: 27.3 asks for these because an
    average is pulled around by a few long stays and hides the ones that sank
    to the bottom of the queue, which are the ones the figure exists to find.
    """
    cur.execute(
        """
        SELECT count(*) AS decided,
               count(DISTINCT coalesce(session_id::text, 'none')) AS bundles,
               percentile_cont(0.5) WITHIN GROUP (
                   ORDER BY EXTRACT(EPOCH FROM decided_at - created_at)
               ) AS median_s,
               percentile_cont(0.9) WITHIN GROUP (
                   ORDER BY EXTRACT(EPOCH FROM decided_at - created_at)
               ) AS p90_s
        FROM proposal
        WHERE decided_at IS NOT NULL
          AND decided_at > now() - make_interval(days => %s)
        """,
        (window_days,),
    )
    row = dict(cur.fetchone())
    row["bundles_per_day"] = round(row["bundles"] / window_days, 2)
    return row


def _unreviewed_share(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    """Per scope, how much of what is held has never been decided on (27.3).

    Measured on the population, not on what retrieval returned. The question is
    whether review is keeping up with the store, and an answer taken from the
    returned set would be measuring the cap instead of the system — which is
    what 27.3 warns about, and what the tag share below deliberately does.
    """
    cur.execute(
        """
        SELECT s.name,
               count(*) AS held,
               count(e.active_version) AS adopted,
               count(*) - count(e.active_version) AS unreviewed
        FROM scope s
        JOIN memory_entity e ON e.scope_id = s.scope_id AND e.status <> 'merged'
        WHERE s.status = 'active'
        GROUP BY s.name
        ORDER BY s.name
        """
    )
    rows = []
    for row in cur.fetchall():
        row = dict(row)
        row["share"] = row["unreviewed"] / row["held"] if row["held"] else 0.0
        rows.append(row)
    return rows


def _tag_share(cur: psycopg.Cursor, window_days: int) -> dict[str, Any]:
    """Layer 2's share of what agents were actually handed (27.3, v0.11).

    Read straight out of context_assembled, which already records both layers
    by memory id, so nothing new had to be instrumented to answer it.

    Since v0.11 removed the relative cap this stopped being a reading of how
    healthy the candidate line is and became a reading of whether a scope has
    any settled contrast in it at all. A scope pinned at 100% is not broken; it
    is one nobody has looked at yet, and leaving it alone can be the right
    call if it is rarely used.
    """
    cur.execute(
        """
        SELECT jsonb_array_length(detail -> 'layer1') AS l1,
               jsonb_array_length(detail -> 'layer2') AS l2
        FROM event_log
        WHERE event_type = 'context_assembled'
          AND created_at > now() - make_interval(days => %s)
          AND detail ? 'layer1' AND detail ? 'layer2'
        """,
        (window_days,),
    )
    shares = []
    with_any = 0
    for row in cur.fetchall():
        total = (row["l1"] or 0) + (row["l2"] or 0)
        if not total:
            continue
        shares.append((row["l2"] or 0) / total)
        with_any += 1 if row["l2"] else 0

    shares.sort()
    median = shares[len(shares) // 2] if shares else None
    return {
        "assemblies": len(shares),
        "median": median,
        "with_any_unreviewed": with_any,
        "over_ceiling": median is not None and median > TAG_SHARE_CEILING,
    }


def _openings(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    """What a session opening costs, per scope, against the ceiling (21.2).

    Not one of the five, but the fixed cost every session pays whether or not
    it asks for anything, and the one figure that decides whether the push half
    of section 6.1 is delivering or quietly being trimmed.
    """
    cur.execute("SELECT scope_id, name FROM scope WHERE status = 'active' ORDER BY name")
    rows = []
    for scope in cur.fetchall():
        _, cost = bootstrap.would_fit(cur, scope_id=scope["scope_id"])
        pushed = bootstrap.session_bootstrap(
            cur, actor="status", scopes=[scope["scope_id"]], record_event=False
        )
        rows.append(
            {
                "name": scope["name"],
                "cost": cost,
                "budget": bootstrap.BOOTSTRAP_TOKEN_BUDGET,
                "trimmed": len(pushed.trimmed),
                "startup": len(pushed.startup),
                "scoped": len(pushed.scoped),
            }
        )
    return rows


def _corrections(cur: psycopg.Cursor, window_days: int) -> dict[str, Any]:
    """How often a person had to take something back (27.3, v0.12).

    27.3 asks for corrections per 100 retrievals and then declines to set a
    threshold, because no baseline exists yet. So this counts and does not
    judge: a rejection and a status change a person made by hand are both a
    person undoing what the store was going to say, and the retrieval count
    beside them is what makes the two comparable across a busier fortnight.
    """
    cur.execute(
        """
        SELECT
          (SELECT count(*) FROM event_log
            WHERE event_type = 'proposal_rejected'
              AND created_at > now() - make_interval(days => %(d)s)) AS rejected,
          (SELECT count(*) FROM event_log
            WHERE event_type = 'status_changed' AND actor = 'user'
              AND detail ->> 'to' IN ('disproven', 'dormant', 'completed')
              AND created_at > now() - make_interval(days => %(d)s)) AS retired_by_hand,
          (SELECT count(*) FROM event_log
            WHERE event_type = 'context_assembled'
              AND created_at > now() - make_interval(days => %(d)s)) AS retrievals
        """,
        {"d": window_days},
    )
    row = dict(cur.fetchone())
    undone = row["rejected"] + row["retired_by_hand"]
    row["per_100_retrievals"] = (
        round(100 * undone / row["retrievals"], 1) if row["retrievals"] else None
    )
    return row


#: A rule that names a moment in itself (25.2). Deliberately loose, and read
#: only against the title and the directive — the two places that carry the
#: standing rule. Measured against the real store, a body-wide search matched 44
#: of 94 memories and almost all of them were provenance ("2026-08-08、ユーザー
#: が…"), which is a date the rule rests on rather than one it expires with.
#: Against title and directive the same patterns matched six, which is a glance.
_SELF_DATING = re.compile(
    r"\d{4}\s*[-/年]\s*\d{1,2}\s*月?"
    r"|\d{4}\s*年"
    r"|現時点"
    r"|時点[のでにを]"
    r"|いまのところ"
    r"|当面"
    r"|暫定"
)


def self_dating(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    """Active memories whose own rule names a moment (25.2 移行).

    A screen, not a verdict. Some of these are rules *about* shelf life rather
    than rules *with* one — "a probe named after a milestone dies when the
    milestone closes" reads as dated and is not — and no pattern separates
    those from a rule that quietly stopped being true. What the screen owes the
    reader is the match, so dismissing a wrong one costs a glance.

    Precision is the cheap side here. A false positive is waved off; a stale
    rule nobody screens keeps being handed to every session that asks, which is
    the debt 25.2 names: a window written down in the years before there was
    anywhere to put one.
    """
    cur.execute(
        """
        SELECT e.memory_id, e.title, e.type, e.delivery, s.name AS scope_name,
               v.directive
        FROM memory_entity e
        JOIN memory_version v ON v.version_id = e.active_version
        JOIN scope s ON s.scope_id = e.scope_id
        WHERE e.status = 'active'
        ORDER BY s.name, e.title
        """
    )
    found = []
    for row in cur.fetchall():
        for field_name in ("title", "directive"):
            text = row[field_name]
            if not text:
                continue
            hit = _SELF_DATING.search(text)
            if hit:
                found.append({**row, "matched": hit.group(), "matched_in": field_name})
                break
    return found


def pushed_total(cur: psycopg.Cursor) -> int:
    """How many memories are pushed at all, of either kind."""
    cur.execute(
        "SELECT count(*) FROM memory_entity WHERE delivery <> %s AND active_version IS NOT NULL",
        (str(Delivery.PULL_ONLY),),
    )
    return cur.fetchone()["count"]

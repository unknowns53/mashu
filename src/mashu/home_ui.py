"""Human-facing dashboard shown when Mashu is run without a command."""

from __future__ import annotations

from dataclasses import dataclass

from mashu import (
    close_ui,
    db,
    memory_ui,
    review_ui,
    screen,
    settings_ui,
    work_ui,
)
from mashu import migrate as migration

_KEYS = (
    "  ↑↓/jk move   ⏎ open   a attention   m memories   w work   s settings\n"
    "  r review   d include deferred   t close tasks   ? help   q leave"
)

_ATTENTION_KEYS = "  ↑↓/jk move   ⏎ open   r ready   d include deferred   ←/q dashboard"

_HELP = """
  Mashu opens here when it is run without a command.

  ↑↓ or j/k  move between Attention, Memories, Work, and Settings & health
  ⏎            open the selected area
  a            open everything waiting for a person's attention
  m / w / s    open Memories, Work, or Settings & health directly
  r            open memory review directly
  d            review candidates including ones put off earlier
  t            open task closing directly
  ?            show this help
  q            leave Mashu

  Deferred candidates are counted separately. Press d to include them in the
  review queue; r and Enter keep the usual queue focused on ready candidates.
"""

_HELP_KEYS = "  any key goes back"


@dataclass(frozen=True)
class Dashboard:
    """Small, stable summary used to paint the home screen."""

    review_ready: int
    review_deferred: int
    tasks_active: int
    tasks_dormant: int
    task_proposals: int
    active_memories: int
    projects: int
    tasks_closed: int = 0
    retired_memories: int = 0
    temporary_contexts: int = 0
    scopes: int = 0
    routes: int = 0
    schema_pending: int = 0

    @property
    def pending_review(self) -> int:
        return self.review_ready + self.review_deferred

    @property
    def open_tasks(self) -> int:
        return self.tasks_active + self.tasks_dormant


def _dashboard(dsn: str | None) -> Dashboard:
    """Read only the aggregate values needed by the home screen."""
    with db.transaction(dsn) as cur:
        cur.execute(
            """
            SELECT count(*) FILTER (WHERE deferred_at IS NULL) AS ready,
                   count(*) FILTER (WHERE deferred_at IS NOT NULL) AS deferred
            FROM nomination WHERE status = 'pending'
            """
        )
        review = cur.fetchone()

        cur.execute(
            """
            SELECT count(*) FILTER (
                       WHERE t.status = 'open' AND now() <= t.active_until
                   ) AS active,
                   count(*) FILTER (
                       WHERE t.status = 'open' AND now() > t.active_until
                   ) AS dormant,
                   count(*) FILTER (WHERE t.status = 'closed') AS closed,
                   count(cp.task_id) FILTER (WHERE t.status = 'open') AS proposals
            FROM task t
            LEFT JOIN task_close_proposal cp ON cp.task_id = t.task_id
            """
        )
        task = cur.fetchone()

        cur.execute(
            "SELECT count(*) FILTER (WHERE status = 'active') AS active, "
            "count(*) FILTER (WHERE status = 'retired') AS retired FROM memory"
        )
        memory = cur.fetchone()
        cur.execute("SELECT count(*) AS n FROM temporary_context WHERE expires_at > now()")
        temporary_contexts = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM project WHERE archived_at IS NULL")
        project_count = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM scope")
        scope_count = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM route")
        route_count = cur.fetchone()["n"]
        schema_pending = len(migration.pending(cur))

    return Dashboard(
        review_ready=review["ready"],
        review_deferred=review["deferred"],
        tasks_active=task["active"],
        tasks_dormant=task["dormant"],
        task_proposals=task["proposals"],
        active_memories=memory["active"],
        projects=project_count,
        tasks_closed=task["closed"],
        retired_memories=memory["retired"],
        temporary_contexts=temporary_contexts,
        scopes=scope_count,
        routes=route_count,
        schema_pending=schema_pending,
    )


def _choice(label: str, detail: str, current: bool) -> str:
    width = screen.text_width()
    label_width = min(20, max(16, width // 4))
    line = screen.clip(f"    {screen.pad(label, label_width)} {detail}", width)
    return screen.selected(line) if current else line


def _screen_text(state: Dashboard, at: int) -> str:
    attention_detail = f"{state.review_ready} ready"
    if state.review_deferred:
        attention_detail += f"  ·  {state.review_deferred} deferred"
    if state.task_proposals:
        attention_detail += f"  ·  {state.task_proposals} close proposal(s)"
    if state.tasks_dormant:
        attention_detail += f"  ·  {state.tasks_dormant} dormant"

    memory_detail = (
        f"{state.active_memories} active  ·  {state.retired_memories} retired  ·  "
        f"{state.temporary_contexts} temporary"
    )
    work_detail = (
        f"{state.tasks_active} active  ·  {state.tasks_dormant} dormant  ·  "
        f"{state.tasks_closed} closed  ·  {state.projects} projects"
    )
    health = (
        screen.warning(f"{state.schema_pending} migration(s) pending")
        if state.schema_pending
        else screen.success("schema current")
    )
    settings_detail = f"{state.scopes} scopes  ·  {state.routes} routes  ·  {health}"

    rows = [
        _choice("Attention", attention_detail, at == 0),
        _choice("Memories", memory_detail, at == 1),
        _choice("Work", work_detail, at == 2),
        _choice("Settings & health", settings_detail, at == 3),
    ]
    return "\n".join(
        (
            screen.bold(screen.accent("Mashu")),
            screen.dim("Knowledge and work that need your attention"),
            "",
            *rows,
            "",
            _KEYS,
        )
    )


def _help() -> None:
    screen.paint(f"{screen.bold(screen.accent('Mashu help'))}\n\n{_HELP.strip()}\n\n{_HELP_KEYS}")
    screen.getkey()


def _attention_text(state: Dashboard, at: int) -> str:
    choices = [
        _choice(
            "Review ready",
            f"{state.review_ready} candidate(s)",
            at == 0,
        ),
        _choice(
            "Review all",
            f"{state.review_ready + state.review_deferred} including deferred",
            at == 1,
        ),
        _choice(
            "Close tasks",
            f"{state.task_proposals} proposal(s)  ·  "
            f"{state.tasks_active + state.tasks_dormant} open",
            at == 2,
        ),
        _choice("Dormant tasks", f"{state.tasks_dormant} task(s)", at == 3),
    ]
    return "\n".join(
        (
            screen.bold(screen.accent("Attention")),
            screen.dim("Decisions and stale work waiting for a person"),
            "",
            *choices,
            "",
            _ATTENTION_KEYS,
        )
    )


def _attention(dsn: str | None) -> None:
    at = 0
    while True:
        state = _dashboard(dsn)
        screen.paint(_attention_text(state, at))
        key = screen.getkey()
        if key in ("q", "left", "h"):
            return
        if key in ("up", "k"):
            at = max(0, at - 1)
            continue
        if key in ("down", "j"):
            at = min(3, at + 1)
            continue
        if key == "home":
            at = 0
            continue
        if key == "end":
            at = 3
            continue
        if key == "r" or (key == "enter" and at == 0):
            review_ui.run(dsn)
        elif key == "d" or (key == "enter" and at == 1):
            review_ui.run(dsn, show_deferred=True)
        elif key == "t" or (key == "enter" and at == 2):
            close_ui.run(dsn)
        elif key == "enter" and at == 3:
            work_ui.run(dsn, initial_view="dormant")


@screen.fullscreen
def run(dsn: str | None = None) -> int:
    """Keep the human dashboard open around the existing decision screens."""
    at = 0
    state = _dashboard(dsn)
    while True:
        screen.paint(_screen_text(state, at))
        key = screen.getkey()

        if key == "q":
            return 0
        if key == "?":
            _help()
            continue
        if key in ("up", "k"):
            at = max(0, at - 1)
            continue
        if key in ("down", "j"):
            at = min(3, at + 1)
            continue
        if key == "home":
            at = 0
            continue
        if key == "end":
            at = 3
            continue

        if key == "a" or (key == "enter" and at == 0):
            _attention(dsn)
            state = _dashboard(dsn)
        elif key == "m" or (key == "enter" and at == 1):
            memory_ui.run(dsn)
            state = _dashboard(dsn)
        elif key == "w" or (key == "enter" and at == 2):
            work_ui.run(dsn)
            state = _dashboard(dsn)
        elif key == "s" or (key == "enter" and at == 3):
            settings_ui.run(dsn)
            state = _dashboard(dsn)
        elif key == "r":
            review_ui.run(dsn)
            state = _dashboard(dsn)
        elif key == "d":
            review_ui.run(dsn, show_deferred=True)
            state = _dashboard(dsn)
        elif key == "t":
            close_ui.run(dsn)
            state = _dashboard(dsn)

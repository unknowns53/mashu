"""Human-facing dashboard shown when Mashu is run without a command."""

from __future__ import annotations

from dataclasses import dataclass

from mashu import close_ui, db, review_ui, screen

_KEYS = "  ↑↓/jk move   ⏎ open   r review   d include deferred   t close tasks\n  ? help   q leave"

_HELP = """
  Mashu opens here when it is run without a command.

  ↑↓ or j/k  move between the two decisions waiting for a person
  ⏎            open the selected decision screen
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
            SELECT count(*) FILTER (WHERE now() <= t.active_until) AS active,
                   count(*) FILTER (WHERE now() > t.active_until) AS dormant,
                   count(cp.task_id) AS proposals
            FROM task t
            LEFT JOIN task_close_proposal cp ON cp.task_id = t.task_id
            WHERE t.status = 'open'
            """
        )
        task = cur.fetchone()

        cur.execute("SELECT count(*) AS n FROM memory WHERE status = 'active'")
        active_memories = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM project WHERE archived_at IS NULL")
        project_count = cur.fetchone()["n"]

    return Dashboard(
        review_ready=review["ready"],
        review_deferred=review["deferred"],
        tasks_active=task["active"],
        tasks_dormant=task["dormant"],
        task_proposals=task["proposals"],
        active_memories=active_memories,
        projects=project_count,
    )


def _choice(label: str, detail: str, current: bool) -> str:
    width = screen.text_width()
    label_width = min(24, max(14, width // 3))
    line = screen.clip(f"    {screen.pad(label, label_width)} {detail}", width)
    return screen.selected(line) if current else line


def _screen_text(state: Dashboard, at: int) -> str:
    review_detail = f"{state.review_ready} ready"
    if state.review_deferred:
        review_detail += f"  ·  {state.review_deferred} deferred"

    task_detail = f"{state.tasks_active} active  ·  {state.tasks_dormant} dormant"
    if state.task_proposals:
        task_detail += f"  ·  {state.task_proposals} proposed closed"

    overview = f"  {state.active_memories} active memories  ·  {state.projects} open projects"
    rows = [
        _choice("Review candidates", review_detail, at == 0),
        _choice("Close tasks", task_detail, at == 1),
    ]
    return "\n".join(
        (
            screen.bold(screen.accent("Mashu")),
            screen.dim("Knowledge and work that need your attention"),
            "",
            overview,
            "",
            *rows,
            "",
            _KEYS,
        )
    )


def _help() -> None:
    screen.paint(f"{screen.bold(screen.accent('Mashu help'))}\n\n{_HELP.strip()}\n\n{_HELP_KEYS}")
    screen.getkey()


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
            at = min(1, at + 1)
            continue

        if key == "r" or (key == "enter" and at == 0):
            review_ui.run(dsn)
            state = _dashboard(dsn)
        elif key == "d":
            review_ui.run(dsn, show_deferred=True)
            state = _dashboard(dsn)
        elif key == "t" or (key == "enter" and at == 1):
            close_ui.run(dsn)
            state = _dashboard(dsn)

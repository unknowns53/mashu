"""Interactive screen for reviewing pending memory nominations."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from mashu import db, memory_ui, nominations, screen, tokens
from mashu.errors import MashuError

# the screens
_QUEUE_KEYS = (
    "  j/↓ k/↑ move   Home/End first/last   / search   ⏎ open   s put off   ? help   q leave"
)
_ITEM_KEYS = (
    "  y admit   e edit   r turn down   s put off   j/↓ k/↑ next   "
    "Home/End first/last   ← queue   ? help   q leave"
)

#: What the keys do, for the one keystroke that asks.
_HELP = """
  ⏎  open the candidate the cursor is on, and from inside one, admit it.
     ← goes back to the queue.

  y  admit it. The next question is where it is delivered from: enter takes
     the default, and 't NAME' files it under that topic ('t' alone lists
     the topics and can open a new one).

  e  edit the existing text in place; Enter saves it in the queue and Ctrl+C
     cancels. This does not admit it. Press y afterwards to try admission.

  r  turn it down, with a reason. The same pain will be reported again, and
     the reason is what tells the next reader this was considered.

  s  put it off, saying why. It stops leading the queue, and comes back with
     'mashu review --all'. Nothing is decided, and the count of what is
     pending does not change.

  /  search the queue by id, kind, scope, content, or the what/prevention in
     its evidence. Search ignores case; submit an empty search to show all.

  j/↓ and k/↑ move one candidate. Home and End go to the first and last;
     PageUp and PageDown move a screenful. These work in the queue and while
     reading a candidate. Escape or ← returns, and returns from the queue leave.

  space  read on where a candidate is longer than the screen; b goes back a
         page.

  There is no key that admits the rest. Each seat is given out on its own
  evidence, and every decision is written as it is made — leaving with q keeps
  what is behind you, and the next sitting opens on the rest.
"""

_HELP_KEYS = "  space read on   any other key goes back"

_NOTHING = "nothing waiting for review"


def _help() -> None:
    """Paint the explanation and wait, so the screens themselves can stay short."""
    offset = 0
    body = _HELP.strip("\n")  # the blank lines around the block, not the indent inside it
    while True:
        # three lines are pinned above the body, so one of them says what this is
        page, more = screen.paged(f"  what each key does\n\n\n{body}", offset, _HELP_KEYS)
        screen.paint(page + "\n" + _HELP_KEYS)
        if screen.getkey() != "space" or not more:
            return
        offset = more


def _short(value: Any) -> str:
    """Enough of an id to name it in a command without pasting all of it."""
    return str(value)[:8]


def _days(row: dict[str, Any]) -> int:
    """How long this has been waiting, which is half of why it is worth reading."""
    created = row.get("created_at")
    if not isinstance(created, datetime):
        return 0
    return max(0, (datetime.now(created.tzinfo) - created).days)


def _queue_preview(row: dict[str, Any]) -> str:
    """The decision-sized facts for the selected row, without opening it."""
    width = screen.terminal_width()
    evidence = len(row.get("evidence_rows", []))
    conflicts = len(row.get("conflict_rows", []))
    scope = row.get("scope_name") or "-"
    content = " ".join((row.get("content") or "").split()) or "(empty)"
    lines = [
        screen.bold(
            f"  preview  {_short(row['nomination_id'])}  v{row['version']}  "
            f"{row['kind']}  [{scope}]"
        ),
        screen.clip(f"  {content}", width - 1),
        screen.dim(f"  evidence: {evidence}  ·  conflicts: {conflicts}"),
    ]
    if row.get("deferred_at"):
        reason = " ".join((row.get("defer_reason") or "").split()) or "no reason given"
        lines.append(screen.warning(screen.clip(f"  put off: {reason}", width - 1)))
    return "\n".join(lines)


def _queue_screen(
    rows: list[dict[str, Any]],
    at: int,
    hidden: int,
    keys: str,
    *,
    query: str = "",
    total: int | None = None,
) -> str:
    """Everything waiting, with a preview that follows the cursor."""
    width = screen.terminal_width()
    lines = []
    for number, row in enumerate(rows):
        body = " ".join((row["content"] or "").split())
        mark = "·" if row.get("deferred_at") else " "
        line = screen.clip(
            f" {mark} {_short(row['nomination_id'])}  {screen.pad(row['kind'], 12)} "
            f"{screen.pad(row.get('scope_name') or '-', 12)} {_days(row):>3}d  {body}",
            width - 1,
        )
        lines.append(screen.selected(line) if number == at else line)

    total = len(rows) if total is None else total
    if query:
        shown_query = " ".join(query.split())
        head = f"{len(rows)} of {total} waiting for review  ·  search: {shown_query}"
    else:
        head = f"{len(rows)} waiting for review"
    if hidden:
        head += f"; {hidden} deferred; --all to see them"
    preview = _queue_preview(rows[at]) if rows else screen.warning("  no candidates match")
    return screen.list_screen(screen.bold(head), lines, at, screen.trailer(preview, keys))


def _item_text(row: dict[str, Any], place: int, total: int) -> str:
    """One candidate as a page of its own, with the pains it rests on under it."""
    label = f" {place} of {total} "
    across = screen.terminal_width()
    cost = tokens.pushed_cost([row["content"]])
    lines = [
        screen.dim("─" * 4 + label + "─" * max(4, across - 4 - screen.cells(label))),
        screen.bold(
            f"{row['kind']}  [{row.get('scope_name') or '-'}]  "
            f"{_short(row['nomination_id'])}  v{row['version']}  "
            f"waiting {_days(row)} day(s)  tokens ~{cost}"
        ),
        "",
    ]
    if row.get("deferred_at"):
        lines.extend(
            [
                screen.warning(screen.wrap(f"(put off earlier: {row.get('defer_reason') or ''})")),
                "",
            ]
        )
    lines.extend([screen.wrap(row["content"]), ""])

    # Above the evidence, not below it.
    for conflict in row.get("conflict_rows", []):
        retired = conflict.get("retired_at")
        when = retired.date().isoformat() if isinstance(retired, datetime) else str(retired or "")
        lines.append(
            screen.danger(
                f"  ! retired conflict  {_short(conflict['memory_id'])}  "
                f"{conflict.get('retirement_kind') or 'legacy'}  {when}"
            )
        )
        lines.append(screen.wrap(f"retired because: {conflict['retire_reason']}", indent="      "))
        if conflict.get("superseded_by"):
            lines.append(screen.wrap(f"successor: {conflict['superseded_by']}", indent="      "))
        if conflict.get("relocated_to_id"):
            lines.append(
                screen.wrap(
                    f"moved to {conflict['relocated_to_kind']}: {conflict['relocated_to_id']}",
                    indent="      ",
                )
            )
    if row.get("conflict_rows"):
        lines.append("")

    lines.append(screen.accent("  evidence"))
    for evidence in row.get("evidence_rows", []):
        created = evidence.get("created_at")
        when = created.date().isoformat() if isinstance(created, datetime) else str(created or "")
        lines.append(f"    {evidence['kind']}  {when}")
        lines.append(screen.wrap(f"what: {evidence['what']}", indent="      "))
        lines.append(screen.wrap(f"prevention: {evidence['prevention']}", indent="      "))
    lines.append(screen.dim("─" * across))
    return "\n".join(lines)


# carrying one decision into the store


def _delivery(
    row: dict[str, Any], dsn: str | None = None
) -> tuple[str, dict[str, Any] | None] | None:
    """Where the admitted memory is delivered from, asked once, at admission."""
    default = "scope" if row.get("scope_id") else "always"
    print(memory_ui.delivery_legend())
    print(
        f"delivery: enter for the default ({default}), or 't NAME' for a topic ('t' alone "
        "lists them)",
        flush=True,
    )
    submitted = screen.editline("> ", "")
    if isinstance(submitted, screen.Cancelled):
        return None
    answer = submitted.text
    if not answer:
        return default, None
    if answer == "t":
        topic = memory_ui.pick_topic(dsn)
        return None if topic is None else ("topic", topic)
    if answer.startswith("t ") and answer[2:].strip():
        return "topic", {"topic_id": None, "name": answer[2:].strip()}
    raise MashuError("delivery must be empty, 't NAME', or 't'")


def _admit(dsn: str | None, row: dict[str, Any]) -> str:
    """Admit one candidate in a transaction of its own."""
    try:
        choice = _delivery(row, dsn)
        if choice is None:
            return screen.warning("  ! admission cancelled")
        delivery, topic = choice
        with db.transaction(dsn) as cur:
            cur.execute(
                "SELECT status, version, content FROM nomination "
                "WHERE nomination_id = %s FOR UPDATE",
                (row["nomination_id"],),
            )
            current = cur.fetchone()
            if current is None or current["status"] != "pending":
                raise MashuError(nominations.NOT_PENDING)
            if current["version"] != row["version"]:
                fresh = next(
                    (
                        item
                        for item in nominations.pending_nominations(cur)
                        if item["nomination_id"] == row["nomination_id"]
                    ),
                    None,
                )
                if fresh is not None:
                    row.update(fresh)
                return screen.warning(
                    "  ! candidate changed; read the current wording and press y again"
                )
            current_conflicts = nominations.current_conflict_ids(cur, current["content"])
            refreshed = nominations.refresh_conflicts(
                cur, row["nomination_id"], current_conflicts, actor="user"
            )
            if refreshed is None:
                raise MashuError(nominations.NOT_PENDING)
            if refreshed["version"] != row["version"]:
                fresh = next(
                    (
                        item
                        for item in nominations.pending_nominations(cur)
                        if item["nomination_id"] == row["nomination_id"]
                    ),
                    None,
                )
                if fresh is not None:
                    row.update(fresh)
                return screen.warning(
                    "  ! retirement conflicts changed; read the updated candidate and press y again"
                )
            nominations.admit(
                cur,
                row["nomination_id"],
                actor="user",
                delivery=delivery,
                expected_version=row["version"],
                topic_id=memory_ui.topic_id_for(cur, topic),
                approval={
                    "kind": "user_direct",
                    "conflict_ids": [
                        item["memory_id"]
                        for item in row.get("conflict_rows", [])
                        if item.get("retirement_kind") in ("invalidated", "legacy")
                    ],
                },
                request_id=uuid4(),
            )
    except MashuError as refusal:
        # Keep the candidate selected when admission is refused.
        return screen.danger(f"  {refusal}")
    return ""


def _revise(dsn: str | None, row: dict[str, Any]) -> str:
    """Persist revised wording while leaving the candidate pending."""
    try:
        content = screen.edit_long_text("Edit pending candidate", row["content"])
        with db.transaction(dsn) as cur:
            nominations.revise(
                cur,
                row["nomination_id"],
                content=content,
                actor="user",
            )
    except MashuError as refusal:
        return screen.danger(f"  {refusal}")
    return ""


def _decide(dsn: str | None, row: dict[str, Any], verb: str, reason: str) -> str:
    """Turn one candidate down, or put it off, in a transaction of its own."""
    act = nominations.decline if verb == "r" else nominations.defer
    try:
        with db.transaction(dsn) as cur:
            result = act(cur, row["nomination_id"], actor="user", reason=reason)
    except MashuError as refusal:
        return screen.danger(f"  {refusal}")
    if result.get("unchecked"):
        return screen.warning("  ! banned-pattern list unavailable; reason was not checked")
    if result.get("malformed"):
        return screen.warning("  ! some banned-pattern lines could not be compiled")
    return ""


def _queue(dsn: str | None, show_deferred: bool) -> tuple[list[dict[str, Any]], int]:
    """The queue as it stands now, and how much of it is being held back."""
    with db.transaction(dsn) as cur:
        rows = nominations.pending_nominations(cur, include_deferred=show_deferred)
        hidden = 0 if show_deferred else nominations.deferred_count(cur)
    return rows, hidden


def _matches_query(row: dict[str, Any], query: str) -> bool:
    """Whether a queue search occurs anywhere useful for judging this candidate."""
    if not query:
        return True
    fields: list[Any] = [
        row.get("nomination_id"),
        row.get("kind"),
        row.get("scope_name"),
        row.get("content"),
    ]
    for evidence in row.get("evidence_rows", []):
        fields.extend((evidence.get("what"), evidence.get("prevention")))
    needle = query.casefold()
    return any(needle in str(value or "").casefold() for value in fields)


def _filtered(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """A view of the queue; the caller retains rows so clearing never needs a reload."""
    return [row for row in rows if _matches_query(row, query)]


def _search(current: str) -> str | None:
    """Ask for a filter, deliberately accepting empty input as 'show everything'."""
    prompt = f"  search [{current}]: " if current else "  search: "
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _move(at: int, total: int, key: str) -> int:
    """Move through candidates the same way from the queue and item views."""
    if total <= 0:
        return 0
    page = max(1, screen.room_under(_QUEUE_KEYS) - 6)
    if key in ("down", "j"):
        return min(total - 1, at + 1)
    if key in ("up", "k"):
        return max(0, at - 1)
    if key == "home":
        return 0
    if key == "end":
        return total - 1
    if key == "pagedown":
        return min(total - 1, at + page)
    if key == "pageup":
        return max(0, at - page)
    return at


def _feedback(verb: str, row: dict[str, Any]) -> str:
    """Name the durable decision on the next screen, including at the end."""
    words = {"y": "admitted", "e": "edited", "r": "declined", "s": "deferred"}
    return screen.success(f"  {words[verb]} {_short(row['nomination_id'])}")


# a sitting
@screen.fullscreen
def run(dsn: str | None = None, *, show_deferred: bool = False) -> int:
    """Work the queue: the list, one candidate at a time, decisions as they are made."""
    all_rows, hidden = _queue(dsn, show_deferred)
    query = ""
    rows = all_rows
    at, scroll, more = 0, 0, 0
    back: list[int] = []
    reading = False
    conflicts_seen = False
    note = ""

    while True:
        if not all_rows:
            if note:
                print(note)
            print(_NOTHING + (f"; {hidden} deferred, --all to see them" if hidden else ""))
            return 0
        at = min(at, max(0, len(rows) - 1))

        if reading:
            under = screen.trailer(_ITEM_KEYS, note)
            page, more = screen.paged(_item_text(rows[at], at + 1, len(rows)), scroll, under)
            screen.paint(page + "\n" + under)
            # A conflict's reason can sit on any page, so admitting waits for the last one.
            conflicts_seen = conflicts_seen or not more
        else:
            more = 0
            screen.paint(
                _queue_screen(
                    rows,
                    at,
                    hidden,
                    screen.trailer(_QUEUE_KEYS, note),
                    query=query,
                    total=len(all_rows),
                )
            )
        key = screen.getkey()
        note = ""  # it has been read now; the next screen starts clean

        if key == "?":
            _help()
            continue
        if key == "q":
            return 0

        if not reading and key == "/":
            searched = _search(query)
            if searched is None:
                continue
            selected = rows[at]["nomination_id"] if rows else None
            query = searched
            rows = _filtered(all_rows, query)
            at = next(
                (number for number, row in enumerate(rows) if row["nomination_id"] == selected),
                0,
            )
            back, scroll, conflicts_seen = [], 0, False
            continue

        if key in ("left", "h"):
            if reading:
                reading = False
                back, scroll, conflicts_seen = [], 0, False
                continue
            if query:
                selected = rows[at]["nomination_id"] if rows else None
                query = ""
                rows = all_rows
                at = next(
                    (number for number, row in enumerate(rows) if row["nomination_id"] == selected),
                    0,
                )
                continue
            return 0

        # A zero-result search is still a live queue: it can be searched again,
        # cleared with left/Escape, or left with q.
        if not rows:
            continue

        if reading and key == "space":
            # Read on where a candidate is longer than the screen.
            if more:
                back.append(scroll)
                scroll = more
                continue
        if key == "space":
            key = "down"
        if reading and key == "b":
            scroll = back.pop() if back else 0
            continue

        moved = _move(at, len(rows), key)
        if moved != at or key in (
            "down",
            "j",
            "up",
            "k",
            "home",
            "end",
            "pagedown",
            "pageup",
        ):
            at = moved
            back, scroll, conflicts_seen = [], 0, False
            continue

        if not reading:
            if key in ("enter", "right", "l"):
                reading = True
                back, scroll, conflicts_seen = [], 0, False
            elif key == "s":
                answer = screen.typed("  why put it off? ")
                if isinstance(answer, screen.Cancelled) or not answer.text:
                    continue
                decided = rows[at]
                note = _decide(dsn, decided, "s", answer.text)
                if not note:
                    note = _feedback("s", decided)
                    all_rows, hidden = _queue(dsn, show_deferred)
                    rows = _filtered(all_rows, query)
                    at = min(at, max(0, len(rows) - 1))
            continue

        decided = rows[at]
        if key in ("y", "enter"):
            if (
                any(
                    item.get("retirement_kind") in ("invalidated", "legacy")
                    for item in decided.get("conflict_rows", [])
                )
                and not conflicts_seen
            ):
                note = screen.warning("  ! read the retirement conflict reasons before admitting")
                continue
            version = decided["version"]
            note = _admit(dsn, decided)
            if decided["version"] != version:
                back, scroll, conflicts_seen = [], 0, False
            verb = "y"
        elif key == "e":
            note = _revise(dsn, decided)
            verb = "e"
        elif key in ("r", "s"):
            answer = screen.typed("  why turn it down? " if key == "r" else "  why put it off? ")
            if isinstance(answer, screen.Cancelled) or not answer.text:
                continue
            note = _decide(dsn, decided, key, answer.text)
            verb = key
        else:
            continue

        # Keep the current item selected when a decision is refused.
        if note:
            continue
        note = _feedback(verb, decided)
        all_rows, hidden = _queue(dsn, show_deferred)
        rows = _filtered(all_rows, query)
        if not rows:
            reading = False
        at = min(at, max(0, len(rows) - 1))
        back, scroll, conflicts_seen = [], 0, False

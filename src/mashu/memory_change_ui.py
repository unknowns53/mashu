"""Review proposed changes to existing memories."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from mashu import db, memories, memory_changes, nominations, screen
from mashu.errors import MashuError

_KEYS = "  j/↓ k/↑ move   ⏎ open   q leave"
_DETAIL_KEYS = (
    "  y apply   e edit reason   d decline   w withdraw   ← list   space read on   q leave"
)


def _short(value: Any) -> str:
    return str(value)[:8]


def _queue(dsn: str | None) -> list[dict[str, Any]]:
    with db.transaction(dsn) as cur:
        return memory_changes.pending(cur)


def _summary(row: dict[str, Any]) -> str:
    target = row["target"]
    successor = row.get("successor") or {}
    body = (
        (row.get("successor_snapshot") or {}).get("content")
        or successor.get("content")
        or target.get("content")
        or ""
    )
    return screen.clip(
        f" {_short(row['change_id'])}  {row['operation']:<7} "
        f"{row.get('retirement_kind') or '-':<12}  {_short(row['target_memory_id'])}  {body}",
        screen.terminal_width() - 1,
    )


def _detail(row: dict[str, Any], place: int, total: int) -> str:
    target = row["target"]
    successor = row.get("successor")
    label = f" {place} of {total} "
    across = screen.text_width()
    lines = [
        screen.dim("─" * 4 + label + "─" * max(4, across - 4 - screen.cells(label))),
        screen.bold(
            f"{row['operation']}  {_short(row['change_id'])}  v{row['version']}  "
            f"target {_short(row['target_memory_id'])}  "
            f"revision {_short(row['target_revision_id'])}"
        ),
        screen.dim(f"  proposed by {row['proposed_by']}  {row['proposed_at']}"),
        "",
        screen.accent("  target Memory"),
        screen.wrap(target["content"]),
        "",
    ]
    if target.get("status") == "retired":
        retirement = target.get("retirement") or {}
        lines.extend(
            [
                screen.warning(
                    f"  retired  {retirement.get('kind') or 'legacy'}  "
                    f"{retirement.get('retired_at') or ''}"
                ),
                screen.wrap(f"reason: {retirement.get('reason') or '-'}", indent="    "),
            ]
        )
        if retirement.get("superseded_by"):
            lines.append(screen.wrap(f"successor: {retirement['superseded_by']}", indent="    "))
        if retirement.get("relocated_to_id"):
            lines.append(
                screen.wrap(
                    f"moved to {retirement['relocated_to_kind']}: {retirement['relocated_to_id']}",
                    indent="    ",
                )
            )
        lines.append("")

    if row["operation"] in ("retire", "replace"):
        lines.extend(
            [
                screen.accent(f"  retirement kind: {row['retirement_kind']}"),
                screen.wrap(f"reason: {row['retire_reason']}", indent="    "),
                "",
            ]
        )
    if row["operation"] == "restore":
        lines.extend([screen.accent("  restore reason"), screen.wrap(row["restore_reason"]), ""])
    if successor:
        lines.extend(
            [
                screen.accent(
                    f"  successor nomination  {_short(successor['nomination_id'])}  "
                    f"v{row['successor_snapshot']['version']}"
                ),
                screen.wrap(row["successor_snapshot"]["content"]),
                "",
            ]
        )
        lines.append(f"    candidate scope: {row['successor_snapshot']['scope_id'] or '-'}")
        lines.append(f"    candidate kind: {row['successor_snapshot']['kind']}")
        candidate_evidence = row["successor_snapshot"]["evidence"]
        lines.append(
            "    candidate evidence: " + ", ".join(candidate_evidence)
            if candidate_evidence
            else "    candidate evidence: none"
        )
        delivery = row["successor_delivery"]
        label = f"guard: {row['successor_guard_action']}" if delivery == "guard" else delivery
        lines.append(screen.accent(f"  replacement delivery: {label}"))
        lines.append(f"    scope: {row['successor_scope_id'] or '-'}")
        if row.get("successor_changed"):
            lines.append(
                screen.warning(
                    "  ! successor nomination changed after this proposal was read; "
                    "update and reread before applying"
                )
            )
            lines.extend(
                [
                    screen.accent(f"  current candidate  v{successor['version']}"),
                    screen.wrap(successor["content"]),
                    f"    current scope: {successor['scope_id'] or '-'}",
                    "",
                ]
            )
        else:
            lines.append(f"    candidate version: {successor['version']}")
        lines.append("")

    lines.append(screen.accent("  evidence"))
    for evidence in row.get("evidence", []):
        reference = evidence.get("id") or evidence.get("ref") or "-"
        lines.append(f"    {evidence.get('kind', 'reference')}  {reference}")
        if evidence.get("observation"):
            lines.append(screen.wrap(evidence["observation"], indent="      "))
    if not row.get("evidence"):
        lines.append("    none")

    if row.get("conflicts"):
        lines.extend(["", screen.danger("  retirement conflicts")])
        for conflict in row["conflicts"]:
            kind = conflict.get("retirement_kind") or "legacy"
            lines.append(f"    {_short(conflict['memory_id'])}  {kind}")
            lines.append(
                screen.wrap(f"reason: {conflict.get('retire_reason') or '-'}", indent="      ")
            )
            if conflict.get("superseded_by"):
                lines.append(
                    screen.wrap(f"successor: {conflict['superseded_by']}", indent="      ")
                )
            if conflict.get("relocated_to_id"):
                lines.append(
                    screen.wrap(
                        f"moved to {conflict['relocated_to_kind']}: {conflict['relocated_to_id']}",
                        indent="      ",
                    )
                )
    lines.append(screen.dim("─" * across))
    return "\n".join(lines)


def _edit_reason(dsn: str | None, row: dict[str, Any]) -> str:
    field = "restore_reason" if row["operation"] == "restore" else "retire_reason"
    current = row[field] or ""
    edited = screen.editline(f"  {field} [enter keeps current]: ", current)
    if isinstance(edited, screen.Cancelled):
        return screen.warning("  ! proposal unchanged")
    reason = edited.text or current
    if not reason:
        return screen.warning("  ! proposal unchanged — reason is required")
    try:
        with db.transaction(dsn) as cur:
            updated = memory_changes.propose(
                cur,
                target_memory_id=row["target_memory_id"],
                target_revision_id=row["target"].get("current_revision_id"),
                target_updated_at=row["target"]["updated_at"],
                operation=row["operation"],
                evidence=row["evidence"],
                actor="user",
                retirement_kind=row["retirement_kind"],
                retire_reason=reason if field == "retire_reason" else row["retire_reason"],
                successor_nomination_id=row["successor_nomination_id"],
                successor_nomination_version=(
                    row["successor"]["version"] if row["operation"] == "replace" else None
                ),
                successor_settings=memory_changes.requested_settings(row),
                relocated_to_kind=row["relocated_to_kind"],
                relocated_to_id=row["relocated_to_id"],
                restore_reason=reason if field == "restore_reason" else row["restore_reason"],
                change_id=row["change_id"],
            )
    except MashuError as refusal:
        return screen.danger(f"  {refusal}")
    row.update(updated)
    return screen.success(f"  ✓ updated proposal v{updated['version']}")


def _apply(dsn: str | None, row: dict[str, Any]) -> str:
    conflicts = [
        item["memory_id"]
        for item in row.get("conflicts", [])
        if item.get("retirement_kind") in ("invalidated", "legacy")
    ]
    try:
        with db.transaction(dsn) as cur:
            result = memory_changes.apply(
                cur,
                row["change_id"],
                version=row["version"],
                request_id=uuid4(),
                approval={"kind": "user_direct", "conflict_ids": conflicts},
                actor="user",
            )
    except MashuError as refusal:
        if "changed" in str(refusal):
            try:
                refreshed = _refresh(dsn, row)
            except MashuError:
                return screen.danger(f"  {refusal}")
            row.update(refreshed)
            return screen.warning(
                "  ! target, successor, or retirement conflicts changed; read the updated proposal "
                "and press y again"
            )
        return screen.danger(f"  {refusal}")
    body = result.get("memory") or {}
    return screen.success(
        f"  ✓ {result['operation']} {_short(row['target_memory_id'])}"
        + (f" → {_short(body['memory_id'])}" if result["operation"] == "replace" else "")
    )


def _refresh(dsn: str | None, row: dict[str, Any]) -> dict[str, Any]:
    """Re-propose against the current target and displayed conflicts after a stale refusal."""
    with db.transaction(dsn) as cur:
        current = memory_changes.get(cur, row["change_id"])
        if current is None or current["status"] != "pending":
            raise MashuError("memory change is no longer pending")
        target = memories.memory_details(cur, current["target_memory_id"])
        if target is None:
            raise MashuError("target Memory no longer exists")
        if current["operation"] == "replace":
            cur.execute(
                "SELECT * FROM nomination WHERE nomination_id = %s FOR UPDATE",
                (current["successor_nomination_id"],),
            )
            successor = cur.fetchone()
            if successor is None or successor["status"] != "pending":
                raise MashuError("replacement nomination is no longer pending")
            conflict_ids = nominations.current_conflict_ids(cur, successor["content"])
            refreshed_successor = nominations.refresh_conflicts(
                cur, current["successor_nomination_id"], conflict_ids, actor="user"
            )
            if refreshed_successor is None:
                raise MashuError("replacement nomination is no longer pending")
            successor_version = refreshed_successor["version"]
        else:
            successor_version = None
        return memory_changes.propose(
            cur,
            target_memory_id=current["target_memory_id"],
            target_revision_id=target["current_revision_id"],
            target_updated_at=target["updated_at"],
            operation=current["operation"],
            evidence=current["evidence"],
            actor="user",
            retirement_kind=current["retirement_kind"],
            retire_reason=current["retire_reason"],
            successor_nomination_id=current["successor_nomination_id"],
            successor_nomination_version=successor_version,
            successor_settings=memory_changes.requested_settings(current),
            relocated_to_kind=current["relocated_to_kind"],
            relocated_to_id=current["relocated_to_id"],
            restore_reason=current["restore_reason"],
            change_id=current["change_id"],
        )


def _decide(dsn: str | None, row: dict[str, Any], status: str) -> str:
    answer = screen.typed(f"  why {status} this proposal? ")
    if isinstance(answer, screen.Cancelled) or not answer.text:
        return screen.warning("  ! proposal left pending")
    try:
        with db.transaction(dsn) as cur:
            memory_changes.decide(
                cur, row["change_id"], status=status, actor="user", reason=answer.text
            )
    except MashuError as refusal:
        return screen.danger(f"  {refusal}")
    return screen.success(f"  ✓ {status} {_short(row['change_id'])}")


def _move(at: int, total: int, key: str) -> int:
    if not total:
        return 0
    if key in ("down", "j"):
        return min(total - 1, at + 1)
    if key in ("up", "k"):
        return max(0, at - 1)
    if key == "home":
        return 0
    if key == "end":
        return total - 1
    return at


@screen.fullscreen
def run(dsn: str | None = None) -> int:
    rows = _queue(dsn)
    if not rows:
        print("nothing waiting for Memory change review")
        return 0
    at, offset, more = 0, 0, 0
    reading = False
    note = ""
    while rows:
        at = min(at, len(rows) - 1)
        if reading:
            under = screen.trailer(_DETAIL_KEYS, note)
            page, more = screen.paged(_detail(rows[at], at + 1, len(rows)), offset, under)
            screen.paint(page + "\n" + under)
        else:
            screen.paint(
                screen.list_screen(
                    screen.bold(f"{len(rows)} pending Memory change(s)"),
                    [_summary(row) for row in rows],
                    at,
                    screen.trailer(_KEYS, note),
                )
            )
        key = screen.getkey()
        note = ""
        if key == "q":
            return 0
        if key in ("up", "down", "j", "k", "home", "end"):
            moved = _move(at, len(rows), key)
            if moved != at:
                at, offset = moved, 0
            continue
        if not reading and key in ("enter", "right", "l"):
            reading, offset = True, 0
            continue
        if reading and key in ("left", "h"):
            reading, offset = False, 0
            continue
        if reading and key == "space" and more:
            offset = more
            continue
        if not reading:
            continue

        current = rows[at]
        if key == "e":
            note = _edit_reason(dsn, current)
            if "updated proposal" in note:
                rows = _queue(dsn)
                at = min(at, max(0, len(rows) - 1))
        elif key == "y":
            note = _apply(dsn, current)
            if note.startswith("  ✓"):
                rows = _queue(dsn)
                if not rows:
                    print(note)
                    return 0
                at = min(at, len(rows) - 1)
        elif key in ("d", "w"):
            note = _decide(dsn, current, "declined" if key == "d" else "withdrawn")
            if note.startswith("  ✓"):
                rows = _queue(dsn)
                if not rows:
                    print(note)
                    return 0
                at = min(at, len(rows) - 1)
    print(note or "nothing waiting for Memory change review")
    return 0

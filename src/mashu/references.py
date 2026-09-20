"""Resolve UUIDs and unambiguous UUID prefixes across human-facing screens."""

from __future__ import annotations

import re
from uuid import UUID

import psycopg

from mashu.errors import MashuError

_HEX_REF = re.compile(r"[0-9a-f][0-9a-f-]*")
MIN_PREFIX = 4


def is_uuid_ref(value: str | None) -> bool:
    return bool(_HEX_REF.fullmatch((value or "").strip().lower()))


def matching_ids(
    cur: psycopg.Cursor,
    ref: str,
    *,
    table: str,
    column: str,
    extra_where: str = "",
    limit: int = 10,
) -> list[UUID]:
    """Return rows named by one full UUID or a valid prefix."""
    text = (ref or "").strip().lower()
    try:
        exact = UUID(text)
    except (ValueError, AttributeError):
        exact = None
    if exact is None:
        if not is_uuid_ref(text):
            raise MashuError(
                f"'{ref}' is not an id, nor the front of one; use a full UUID or a prefix "
                f"with at least {MIN_PREFIX} hexadecimal characters"
            )
        if len(text) < MIN_PREFIX:
            raise MashuError(
                f"'{ref}' is too short to name a row; provide at least {MIN_PREFIX} ID characters"
            )
    clause = (
        f"{column} = %(exact)s" if exact is not None else f"{column}::text LIKE %(prefix)s || '%%'"
    )
    if extra_where:
        clause = f"{clause} AND ({extra_where})"
    cur.execute(
        f"SELECT {column} AS found FROM {table} WHERE {clause} ORDER BY {column} LIMIT %(limit)s",
        {"exact": exact, "prefix": text, "limit": limit},
    )
    return [row["found"] for row in cur.fetchall()]


def resolve_id(
    cur: psycopg.Cursor,
    ref: str,
    *,
    table: str,
    column: str,
    label: str,
    extra_where: str = "",
) -> UUID:
    """Return one exact row or unique prefix, with the same refusal in every UI."""
    found = matching_ids(
        cur,
        ref,
        table=table,
        column=column,
        extra_where=extra_where,
        limit=2,
    )
    if not found:
        try:
            UUID((ref or "").strip())
        except (ValueError, AttributeError):
            raise MashuError(
                f"no {label} begins with '{ref}'; check the ID or try another prefix"
            ) from None
        raise MashuError(f"no {label} {ref}")
    if len(found) > 1:
        named = "  ".join(str(value)[:8] for value in found)
        raise MashuError(
            f"'{ref}' names more than one {label}: {named}\n"
            "Use a longer ID prefix to select one row."
        )
    return found[0]

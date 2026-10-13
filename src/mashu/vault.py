"""Turn a vault-relative locator into a path under the configured vault, without reading it.

The vault's absolute path names the user, so it comes from MASHU_VAULT_ROOT at read time
and is never stored; only the relative locator is.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

from mashu import config
from mashu.errors import MashuError


@dataclass(frozen=True)
class Located:
    """Where a locator lands under the vault, and whether something is there."""

    path: pathlib.Path
    fragment: str | None
    exists: bool


def split(locator: str) -> tuple[str, str | None]:
    """The path and the `#heading` after it, if any."""
    path, mark, fragment = locator.strip().partition("#")
    return path.strip(), (fragment.strip() or None) if mark else None


def relative_parts(locator: str) -> tuple[str, ...]:
    """The path's components, or a refusal when it is not relative or climbs out."""
    path, _ = split(locator)
    if not path:
        raise MashuError(f"'{locator}' names no file before its #heading")
    if path.startswith(("/", "\\", "~")) or pathlib.PureWindowsPath(path).drive:
        raise MashuError(
            f"'{locator}' is not a relative path; write it from the vault or repository root"
        )
    parts = pathlib.PurePosixPath(path.replace("\\", "/")).parts
    if not parts:
        raise MashuError(f"'{locator}' names no file before its #heading")
    if ".." in parts:
        raise MashuError(f"'{locator}' climbs out of its root with '..'")
    return parts


def resolve(locator: str, root: pathlib.Path | None = None) -> Located | None:
    """The absolute path a vault-relative locator names; None when no vault is configured.

    Only a stat is made, so a note's contents are never read.
    """
    parts = relative_parts(locator)
    root = root or config.vault_root()
    if root is None:
        return None
    path = root.joinpath(*parts)
    # A symlink inside the vault may still lead out of it.
    if not path.resolve().is_relative_to(root.resolve()):
        raise MashuError(f"'{locator}' leads out of the vault")
    return Located(path=path, fragment=split(locator)[1], exists=path.exists())


def note(locator: str | None, root: pathlib.Path | None = None) -> Located | None:
    """Where a source lands when it reads as a vault note, else None.

    A source may also be relative to a repository, which this cannot see, so it counts as a
    vault note only when its first component is present at the vault's top level.
    """
    root = root or config.vault_root()
    if not locator or root is None:
        return None
    try:
        parts = relative_parts(locator)
        if not (root / parts[0]).exists():
            return None
        return resolve(locator, root)
    except MashuError:
        return None

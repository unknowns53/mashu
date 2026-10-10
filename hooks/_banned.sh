#!/bin/sh
# Shared helper: materialise the active banned-pattern list into $BANNED_PATTERNS_FILE.

banned_load() {
    # Linked worktrees have no copy of the gitignored list, so read the main checkout's.
    common_dir=$(git rev-parse --path-format=absolute --git-common-dir) || return 1
    src="$(dirname "$common_dir")/.git-banned-patterns"

    if [ ! -f "$src" ]; then
        printf '%s\n' "hook: .git-banned-patterns is missing." >&2
        printf '%s\n' "hook: run ./hooks/install.sh to create it, then retry." >&2
        return 1
    fi

    BANNED_PATTERNS_FILE=$(mktemp) || return 1
    if grep -vE '^[[:space:]]*(#|$)' "$src" > "$BANNED_PATTERNS_FILE"; then
        :
    else
        status=$?
        if [ "$status" -ne 1 ]; then
            printf '%s\n' "hook: cannot check banned patterns in $src." >&2
            rm -f "$BANNED_PATTERNS_FILE"
            return 1
        fi
    fi

    if [ ! -s "$BANNED_PATTERNS_FILE" ]; then
        printf '%s\n' "hook: .git-banned-patterns contains no patterns." >&2
        rm -f "$BANNED_PATTERNS_FILE"
        return 1
    fi

    if grep -E -f "$BANNED_PATTERNS_FILE" /dev/null >/dev/null; then
        :
    else
        status=$?
        if [ "$status" -ne 1 ]; then
            printf '%s\n' "hook: cannot check banned patterns in $src." >&2
            rm -f "$BANNED_PATTERNS_FILE"
            return 1
        fi
    fi

    export BANNED_PATTERNS_FILE
    return 0
}

banned_scan() {
    grep -E -f "$BANNED_PATTERNS_FILE" "$@"
}

#!/bin/sh
# Session-end hook for Claude Code and Codex. Both hand the hook a JSON payload
# on stdin; both give the exit path only seconds, so this claims the transcript
# and returns. No model runs here. The worker does the expensive part later,
# with nothing waiting on it.
#
# The working directory is passed along because it is what decides the scope,
# and by the time the worker looks, the process that knew it has exited.
#
# Install by pointing the CLI's SessionEnd hook at this file. It never exits
# non-zero: a hook that fails loudly interrupts something the user did not ask
# for, and a session that misses the queue is one the sweeper will find.
set -u

MASHU="${MASHU_BIN:-mashu}"
payload=$(cat 2>/dev/null || true)

field() {
    printf '%s' "$payload" | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" | head -1
}

transcript=$(field transcript_path)
session=$(field session_id)
cwd=$(field cwd)
cli="${MASHU_SOURCE_CLI:-claude}"

[ -n "$transcript" ] || exit 0

set -- enqueue "$transcript" --cli "$cli"
[ -n "$session" ] && set -- "$@" --session "$session"
[ -n "$cwd" ] && set -- "$@" --cwd "$cwd"

# MASHU_BIN must name one executable, not a command line.
if [ "${MASHU_HOOK_DEBUG:-}" = "1" ]; then
    "$MASHU" "$@" || true
else
    "$MASHU" "$@" >/dev/null 2>&1 || true
fi
exit 0

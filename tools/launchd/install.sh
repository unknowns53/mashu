#!/bin/sh
# Put the capture agent in place, with this checkout's paths filled in.
#
# It does not load the agent. Loading starts something that will spend a model
# allowance every night without anyone watching, and that is a decision for the
# person whose allowance it is — the command to run is printed at the end.
set -eu

here=$(cd "$(dirname "$0")/../.." && pwd)
target="$HOME/Library/LaunchAgents/com.mashu.capture.plist"

if [ ! -x "$here/.venv/bin/mashu" ]; then
    echo "no mashu executable at $here/.venv/bin/mashu; run 'uv sync' first" >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
sed "s|REPLACE_WITH_PROJECT|$here|g" "$here/tools/launchd/com.mashu.capture.plist" > "$target"

echo "wrote $target"
echo
echo "It is not loaded. To start it:"
echo "  launchctl load $target"
echo
echo "To check it without waiting for 4:30am:"
echo "  $here/.venv/bin/mashu sweep --days 2 && $here/.venv/bin/mashu work --limit 1 --dry-run"
echo
echo "To stop it later:"
echo "  launchctl unload $target"

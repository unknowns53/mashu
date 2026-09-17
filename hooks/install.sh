#!/bin/sh
# Set up Git hooks and create the local pattern file if needed.
set -eu

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

git config core.hooksPath hooks
printf '%s\n' "installed: core.hooksPath = hooks"

if [ -f .git-banned-patterns ]; then
    printf '%s\n' "kept: .git-banned-patterns already exists"
else
    cat > .git-banned-patterns <<'TEMPLATE'
# One extended regular expression per line. Lines starting with # are comments.
# Add the strings that must never reach a commit: real names, student IDs,
# affiliation details. This file is gitignored and stays local.
TEMPLATE
    printf '%s\n' "created: .git-banned-patterns (add your patterns before committing)"
fi

#!/usr/bin/env bash
# Release from the main checkout: bump the version, push main, wait for its CI, then push the tag.
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: tools/release.sh <version>

Writes <version> to src/mashu/__init__.py, commits it to main and pushes. Once main's CI has
passed, pushes the tag v<version> and waits for the tag's CI to publish the GitHub Release.
A version of release numbers alone, such as 3.0.1, is a release; one ending in a, b or rc and
a number, such as 3.1.0b1, is a prerelease.
USAGE
}

fail() {
  echo "error: $*" >&2
  exit 1
}

[[ $# -eq 1 ]] || { usage; exit 1; }
[[ "$1" != "-h" && "$1" != "--help" ]] || { usage; exit 0; }
version="$1"
# PEP 440 in its normalized spelling, so the package, the tag and the release all read the same.
[[ "$version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)((a|b|rc)(0|[1-9][0-9]*))?$ ]] ||
  fail "version $version is not of the form 3.0.1 or 3.1.0b1"
tag="v$version"

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

command -v gh >/dev/null || fail "gh is not installed; install the GitHub CLI first"
gh auth status >/dev/null 2>&1 || fail "gh is not logged in to GitHub; run gh auth login"

branch="$(git branch --show-current)"
[[ "$branch" == "main" ]] || fail "the branch is '${branch:-(detached)}'; switch to main first"
dirty="$(git status --porcelain --untracked-files=no)"
if [[ -n "$dirty" ]]; then
  echo "$dirty" >&2
  fail "the files above have uncommitted changes; commit or discard them first"
fi

echo "pulling the latest main"
git pull --ff-only -q
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null || [[ -n "$(git ls-remote --tags origin "refs/tags/$tag")" ]]; then
  fail "tag $tag already exists; choose another version"
fi

echo "writing version $version"
sed -i '' -E "s/^__version__ = \"[^\"]*\"$/__version__ = \"$version\"/" src/mashu/__init__.py
found="$(sed -n -E 's/^__version__ = "([^"]*)"$/\1/p' src/mashu/__init__.py)"
[[ "$found" == "$version" ]] || fail "src/mashu/__init__.py reads $found after the rewrite; restore it with git checkout -- src/mashu/__init__.py"

git add src/mashu/__init__.py
# After main's CI failed and was fixed, a rerun with the same version finds it already written
# and tags the current tip of main without a new commit.
if git diff --cached --quiet; then
  echo "the version is already $version; tagging the current main"
else
  git commit -q -m "Bump the version to $version"
fi
sha="$(git rev-parse HEAD)"
echo "pushing main (${sha:0:12})"
git push -q origin main

# A run takes a few seconds to appear in the list after the push.
run_of() {
  local event_ref="$1" id
  for _ in $(seq 1 30); do
    id="$(gh run list --workflow CI --commit "$sha" --branch "$event_ref" --json databaseId --jq '.[0].databaseId // empty' 2>/dev/null || true)"
    if [[ -n "$id" ]]; then
      echo "$id"
      return 0
    fi
    sleep 4
  done
  return 1
}

main_run="$(run_of main)" || fail "no CI run found for main; check https://github.com/$(gh repo view --json nameWithOwner --jq .nameWithOwner)/actions. No tag was pushed."
echo "waiting for main's CI ($main_run)"
gh run watch "$main_run" --exit-status --interval 30 >/dev/null ||
  fail "main's CI ($main_run) failed; read gh run view $main_run --log-failed, fix it, and run this again with the same version (the version commit stays on main). No tag was pushed."

echo "pushing tag $tag"
git tag -a "$tag" -m "mashu $tag" "$sha"
git push -q origin "$tag"

tag_run="$(run_of "$tag")" || fail "no CI run found for the tag; check whether the release exists with gh release view $tag"
echo "waiting for the tag's CI ($tag_run) to publish the release"
gh run watch "$tag_run" --exit-status --interval 15 >/dev/null ||
  fail "the tag's CI ($tag_run) failed; read gh run view $tag_run --log-failed"
gh release view "$tag" --json url,isPrerelease --jq '"published \(if .isPrerelease then "prerelease" else "release" end): \(.url)"'

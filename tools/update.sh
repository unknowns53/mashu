#!/usr/bin/env bash
# macOS. On Windows, use tools/update.ps1.
# The steps and their order are those of the app's own update (desktop/src-tauri/src/update.rs):
# the checkout moves before the app, so the app never runs against an older backend than itself.
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: tools/update.sh [--pre]

By default, moves this checkout's main to the latest stable release, runs uv sync and
mashu admin migrate, and replaces /Applications/Mashu.app with that release's app.

  --pre  Use the most recently published release whose tag starts with v, including prereleases.
USAGE
}

fail() {
  echo "error: $*" >&2
  exit 1
}

pre=0
for arg in "$@"; do
  case "$arg" in
    --pre) pre=1 ;;
    -h | --help) usage; exit 0 ;;
    *) usage; fail "unknown argument: $arg" ;;
  esac
done

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$root"

app_dest="/Applications/Mashu.app"
app_new="/Applications/.Mashu.app.new"
app_old="/Applications/.Mashu.app.old"
running_pattern="$app_dest/Contents/MacOS/"
archive="Mashu_aarch64.app.tar.gz"

# The release carries the app for Apple silicon only.
[[ "$(uname -m)" == "arm64" ]] ||
  fail "the release's app is for Apple silicon (arm64) Macs only; on this Mac, build the app as the README's 手元でビルドする describes"
for tool in git uv curl; do
  command -v "$tool" >/dev/null || fail "$tool was not found"
done

# The app updates the checkout named in its settings. Moving another one would leave the app
# newer than the backend it runs.
config="$HOME/Library/Application Support/dev.mashu.desktop/config.json"
configured="$(sed -n 's/.*"checkout"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$config" 2>/dev/null | head -n 1 || true)"
if [[ -n "$configured" && "$(cd "$configured" 2>/dev/null && pwd -P)" != "$root" ]]; then
  fail "the app runs the checkout at $configured, not this one ($root); run that checkout's tools/update.sh, or choose this checkout under the app's Settings > Version"
fi

branch="$(git branch --show-current)"
[[ "$branch" == "main" ]] || fail "the checkout at $root is on '${branch:-(no branch)}'; switch it to main first"
dirty="$(git status --porcelain --untracked-files=no)"
if [[ -n "$dirty" ]]; then
  echo "$dirty" >&2
  fail "the files above have uncommitted changes; commit or discard them first"
fi

# A fork updates from its own releases when origin is on GitHub.
repo_slug() {
  local url re
  url="$(git remote get-url origin 2>/dev/null || true)"
  re='^(https://([^@/]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$'
  url="${url%/}"
  url="${url%.git}"
  if [[ "$url" =~ $re ]]; then
    echo "${BASH_REMATCH[3]}"
  else
    echo "unknowns53/mashu"
  fi
}

fetch() {
  local url="$1" dest="$2" code
  code="$(curl -sSL --retry 3 --connect-timeout 20 -o "$dest" -w '%{http_code}' "$url")" ||
    fail "could not download $url; check the network connection"
  case "$code" in
    200) return 0 ;;
    404) return 1 ;;
    *) fail "could not download $url (HTTP $code)" ;;
  esac
}

# The list comes newest first; the first v release that is not a draft is the one wanted.
# A release's own fields are indented 4 spaces, which tells them from its assets'.
newest_v_release() {
  awk '
    /^    "tag_name": "/ { tag = $0; sub(/^    "tag_name": "/, "", tag); sub(/".*/, "", tag) }
    /^    "draft": false/ && tag ~ /^v/ { print tag; exit }
  ' "$1"
}

work="$(mktemp -d "${TMPDIR:-/tmp}/mashu-update.XXXXXX")"
trap 'rm -rf "$work"' EXIT
slug="$(repo_slug)"
if [[ $pre -eq 1 ]]; then
  api="https://api.github.com/repos/$slug/releases?per_page=50"
  fetch "$api" "$work/release.json" || fail "could not download $api (HTTP 404)"
  tag="$(newest_v_release "$work/release.json")"
  [[ -n "$tag" ]] || fail "https://github.com/$slug/releases has no release whose tag starts with v yet. Nothing was changed."
  kind="the latest release including prereleases"
else
  # This API leaves out drafts and prereleases.
  api="https://api.github.com/repos/$slug/releases/latest"
  fetch "$api" "$work/release.json" || fail "https://github.com/$slug/releases has no stable release yet. Nothing was changed."
  tag="$(sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$work/release.json" | head -n 1)"
  kind="the latest stable release"
fi
[[ -n "$tag" ]] || fail "could not read the release tag from $api"
echo "updating to $tag, $kind"

# Every refusal comes before the app is quit or anything is changed.
git fetch --quiet --tags origin
git rev-parse --verify --quiet "refs/tags/$tag^{commit}" >/dev/null || fail "origin has no tag $tag"
git merge-base --is-ancestor HEAD "$tag" ||
  fail "main at $root has commits that $tag does not; bring it up to date with git. Nothing was changed."

base="https://github.com/$slug/releases/download/$tag"
# For a few seconds after a release is published its files can still answer 404.
deadline=$((SECONDS + 60))
until fetch "$base/SHA256SUMS" "$work/SHA256SUMS" && fetch "$base/$archive" "$work/$archive"; do
  [[ $SECONDS -lt $deadline ]] ||
    fail "release $tag lacks SHA256SUMS or $archive; check https://github.com/$slug/releases/tag/$tag. Nothing was changed."
  echo "the release's files are not there yet; checking again in 10 seconds (up to 1 min)"
  sleep 10
done
expected="$(awk -v f="$archive" '$2 == f || $2 == "*" f { print $1 }' "$work/SHA256SUMS")"
actual="$(shasum -a 256 "$work/$archive" | awk '{ print $1 }')"
[[ -n "$expected" ]] || fail "SHA256SUMS has no line for $archive. Nothing was changed."
[[ "$expected" == "$actual" ]] || fail "the checksum of $archive does not match SHA256SUMS. Nothing was changed."
mkdir "$work/extract"
tar -xzf "$work/$archive" -C "$work/extract"
# curl leaves no quarantine flag, but one left on would stop the first launch at Gatekeeper.
xattr -dr com.apple.quarantine "$work/extract" 2>/dev/null || true
bundle="$work/extract/Mashu.app"
[[ -d "$bundle" ]] || fail "$archive holds no Mashu.app. Nothing was changed."

if pgrep -f "$running_pattern" >/dev/null; then
  echo "quitting the running Mashu"
  osascript -e 'tell application id "dev.mashu.desktop" to quit' >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do
    pgrep -f "$running_pattern" >/dev/null || break
    sleep 0.5
  done
  if pgrep -f "$running_pattern" >/dev/null; then
    fail "Mashu did not quit; choose Quit from its tray menu and run this again. Nothing was changed."
  fi
fi

# From here every step can run again, so after a failure the same command picks up where it stopped.
echo "moving main to $tag"
git merge --ff-only --quiet "$tag"
echo "running uv sync --extra mcp"
uv sync --extra mcp
echo "running mashu admin migrate"
"$root/.venv/bin/mashu" admin migrate

echo "replacing $app_dest"
rm -rf "$app_new" "$app_old"
ditto "$bundle" "$app_new"
if [[ -e "$app_dest" ]]; then
  mv "$app_dest" "$app_old"
fi
if ! mv "$app_new" "$app_dest"; then
  [[ -e "$app_old" ]] && mv "$app_old" "$app_dest"
  fail "could not move the new app to $app_dest; the old app is back in place. The checkout is already at $tag, so run this again."
fi
rm -rf "$app_old"

echo "starting Mashu"
open "$app_dest"
echo "updated to $tag"

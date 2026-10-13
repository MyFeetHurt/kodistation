#!/usr/bin/env bash
# CI release job guard (backlog 010): publish only an annotated tag vX.Y.Z that
#   - equals "v" + the addon.xml version of the checked-out commit,
#   - points at the checked-out commit,
#   - is on origin/main (the release commit was pushed to main, tools/release.sh + git push --atomic).
#   tools/ci_release_guard.sh TAG      (run in the CI checkout; needs fetch-depth 0)
set -euo pipefail
TAG=${1:-}
die() { echo "release guard: $*" >&2; exit 1; }
[[ $TAG =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || die "not a release tag: '$TAG'"

version=$(sed -n 's/.*<addon[^>]* version="\([^"]*\)".*/\1/p' plugin.video.epgstation/addon.xml | head -1)
[ "$TAG" = "v$version" ] || die "tag $TAG does not match the addon.xml version $version"

git fetch -q origin main
# Fetch the tag object itself: a checkout may hold only a lightweight copy of it.
git fetch -q --force origin "refs/tags/$TAG:refs/tags/$TAG"
[ "$(git cat-file -t "refs/tags/$TAG")" = tag ] || die "$TAG is not an annotated tag (use tools/release.sh)"
head=$(git rev-parse HEAD)
[ "$(git rev-parse "refs/tags/$TAG^{commit}")" = "$head" ] || die "$TAG does not point at the checked-out commit"
git merge-base --is-ancestor "$head" origin/main || die "$TAG ($head) is not on origin/main"
echo "release guard: $TAG = addon.xml $version, annotated, on origin/main"

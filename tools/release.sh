#!/usr/bin/env bash
# Cut a release of plugin.video.epgstation (backlog 010, docs/FACTORY.md "Releases").
#   tools/release.sh X.Y.Z          (make release VERSION=X.Y.Z)
# Needs: branch main, a clean tree, X.Y.Z higher than addon.xml's version, and changelog.txt starting with
# "vX.Y.Z (YYYY-MM-DD)", HEAD equal to origin/main (fetched) and no tag vX.Y.Z locally or on origin. Sets the addon.xml version, replaces <news> with that changelog entry, commits
# "Release vX.Y.Z" and creates the annotated tag vX.Y.Z. It never pushes: it prints the push command.
# RELEASE_TRAILER (optional) is appended to the commit message, e.g. the factory's Co-Authored-By line.
set -euo pipefail
VERSION=${1:-}
ADDON=plugin.video.epgstation
die() { echo "release: $*" >&2; exit 1; }

cd "$(git rev-parse --show-toplevel)"
XML=$ADDON/addon.xml
CHANGELOG=$ADDON/changelog.txt

[[ $VERSION =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || die "VERSION must be X.Y.Z, got '$VERSION'"
branch=$(git symbolic-ref --short -q HEAD || true)
[ "$branch" = main ] || die "releases are cut from main, not '${branch:-detached HEAD}'"
[ -z "$(git status --porcelain)" ] || die "the working tree is not clean"
! git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null || die "tag v$VERSION already exists"
git fetch -q origin main || die "cannot fetch origin main"
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] \
    || die "HEAD is not origin/main: release exactly what is on the server's main (pull or push first)"
remote_tags=$(git ls-remote --tags origin "refs/tags/v$VERSION") || die "cannot list tags on origin"
[ -z "$remote_tags" ] || die "tag v$VERSION already exists on origin"

current=$(sed -n 's/.*<addon[^>]* version="\([^"]*\)".*/\1/p' "$XML" | head -1)
[ -n "$current" ] || die "no version in $XML"
highest=$(printf '%s\n%s\n' "$current" "$VERSION" | sort -V | tail -1)
[ "$VERSION" != "$current" ] && [ "$highest" = "$VERSION" ] || die "$VERSION is not higher than $current"

[ -f "$CHANGELOG" ] || die "no $CHANGELOG"
head -1 "$CHANGELOG" | grep -Eq "^v${VERSION//./\\.} \([0-9]{4}-[0-9]{2}-[0-9]{2}\)\$" \
    || die "the first line of $CHANGELOG must be 'v$VERSION (YYYY-MM-DD)'; write the changelog entry first"

# From here on a failure (python, commit or tag) puts the tree back: addon.xml restored, no release commit.
# The tree was clean and at origin/main above, so resetting to that commit loses nothing.
start=$(git rev-parse HEAD)
restore() { echo "release: failed; restoring addon.xml and $start" >&2; git reset -q --hard "$start"; }
trap restore ERR

# Version + <news> = the top changelog entry (up to the first blank line), XML-escaped, at most 1500 chars.
python3 - "$XML" "$CHANGELOG" "$VERSION" <<'EOF'
import re, sys
from xml.sax.saxutils import escape
xml_path, changelog, version = sys.argv[1:]
entry = []
for line in open(changelog, encoding="utf-8").read().splitlines():
    if not line.strip():
        break
    entry.append(line.rstrip())
news = "\n".join(entry)
if len(news) > 1500:
    news = news[:1500]
    news = news[:news.rfind("\n")] if "\n" in news else news
text = open(xml_path, encoding="utf-8").read()
text, n1 = re.subn(r'(<addon\b[^>]*?\sversion=")[^"]*(")', lambda m: m.group(1) + version + m.group(2), text, count=1)
text, n2 = re.subn(r"<news>.*?</news>", lambda m: "<news>" + escape(news) + "</news>", text, count=1, flags=re.S)
if n1 != 1 or n2 != 1:
    sys.exit("release: addon.xml needs one <addon version> and one <news>")
open(xml_path, "w", encoding="utf-8").write(text)
EOF

msg="Release v$VERSION"
[ -z "${RELEASE_TRAILER:-}" ] || msg=$(printf '%s\n\n%s' "$msg" "$RELEASE_TRAILER")
git add "$XML"
git commit -q -m "$msg"
git tag -a "v$VERSION" -m "Release v$VERSION"
trap - ERR
echo "Released v$VERSION locally ($(git rev-parse --short HEAD)). Nothing was pushed. To publish:"
echo "  git push --atomic origin main v$VERSION"

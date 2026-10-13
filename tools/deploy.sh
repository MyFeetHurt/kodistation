#!/usr/bin/env bash
# Install the addon from dist/ onto the dev VM's Kodi.
#   tools/deploy.sh [host]        (default: $KODI_HOST from local.env, backlog 019; ssh user $KODI_SSH_USER)
# First install, or a change to addon.xml, settings.xml or strings.po, restarts Kodi:
# it only reads those at startup. Plain Python changes take effect on the next
# plugin call without a restart.
set -euo pipefail
# shellcheck source=tools/local_env.sh
. "$(dirname "$0")/local_env.sh"  # KODI_HOST / KODI_SSH_USER from local.env unless set in the environment
HOST=${1:-${KODI_HOST:-}}
[ -n "$HOST" ] || { echo "no Kodi host: pass one or set KODI_HOST (~/.config/epgstation-kodi/local.env)" >&2; exit 1; }
SSH_TARGET=${KODI_SSH_USER:+$KODI_SSH_USER@}$HOST
ID=plugin.video.epgstation
SRC=dist/$ID
DEST=.kodi/addons/$ID
EPGS_URL=${EPGS_URL:-http://localhost:8888}
[ -d "$SRC" ] || { echo "run 'make build' first" >&2; exit 1; }

# Run on the Kodi box: directly when deploying locally (CI on the VM), else over ssh.
if [ "$HOST" = localhost ] || [ "$HOST" = 127.0.0.1 ]; then
    on_host() { (cd ~ && bash -c "$1"); }
    TARGET=$HOME/$DEST
else
    on_host() { ssh "$SSH_TARGET" "$1"; }
    TARGET=$SSH_TARGET:$DEST
fi

# Files Kodi caches until restart; checksum them on both sides.
STATIC="addon.xml resources/settings.xml resources/language/*/strings.po"
sum_static() { cat $STATIC 2>/dev/null | sha256sum; }
remote_sum=$(on_host "cd $DEST 2>/dev/null && cat $STATIC 2>/dev/null | sha256sum" || true)
local_sum=$(cd "$SRC" && sum_static)
restart=0
[ "$remote_sum" = "$local_sum" ] || restart=1

rsync -a --delete "$SRC/" "$TARGET/"

# Seed settings once so the addon works out of the box on the dev VM.
on_host "f=.kodi/userdata/addon_data/$ID/settings.xml; [ -f \$f ] || { mkdir -p \$(dirname \$f); printf '<settings version=\"2\">\n    <setting id=\"server_url\">%s</setting>\n</settings>\n' '$EPGS_URL' > \$f; }"

rpc() { curl -sf -X POST -H 'Content-Type: application/json' -d "$1" "http://$HOST:8080/jsonrpc"; }

if [ $restart = 1 ]; then
    echo "addon.xml/settings.xml/strings.po changed -> restarting Kodi"
    on_host "sudo systemctl restart kodi"
    for _ in $(seq 1 30); do rpc '{"jsonrpc":"2.0","method":"JSONRPC.Ping","id":1}' >/dev/null 2>&1 && break; sleep 2; done
    sleep 3
fi
rpc "{\"jsonrpc\":\"2.0\",\"method\":\"Addons.SetAddonEnabled\",\"params\":{\"addonid\":\"$ID\",\"enabled\":true},\"id\":1}" >/dev/null
rpc "{\"jsonrpc\":\"2.0\",\"method\":\"Addons.GetAddonDetails\",\"params\":{\"addonid\":\"$ID\",\"properties\":[\"version\",\"enabled\"]},\"id\":1}"
echo

#!/usr/bin/env bash
# Install the Kodi repository web server on the dev VM (backlog 010). Idempotent.
#   tools/dev/install_kodi_repo_server.sh [host]     (default: $KODI_HOST from local.env; ssh user $KODI_SSH_USER)
# Creates /srv/kodi-repo (owned by the ssh user, who runs the CI release job that writes it) and installs, enables and starts
# kodi-repo.service (tools/dev/kodi_repo_server.py, copied to /usr/local/lib/kodi-repo/, on :8090). Refuses if something else already listens on :8090.
set -euo pipefail
# shellcheck source=tools/local_env.sh
. "$(dirname "$0")/../local_env.sh"  # KODI_HOST / KODI_SSH_USER from local.env unless set in the environment
HOST=${1:-${KODI_HOST:-}}
[ -n "$HOST" ] || { echo "no host: pass one or set KODI_HOST (~/.config/epgstation-kodi/local.env)" >&2; exit 1; }
SSH_TARGET=${KODI_SSH_USER:+$KODI_SSH_USER@}$HOST
UNIT=$(dirname "$0")/kodi-repo.service
SERVER=$(dirname "$0")/kodi_repo_server.py
PORT=8090

if [ "$HOST" = localhost ] || [ "$HOST" = 127.0.0.1 ]; then
    on_host() { bash -c "$1"; }
else
    on_host() { ssh "$SSH_TARGET" "$1"; }
fi

# The service runs as the account that owns /srv/kodi-repo: the ssh user on the box (kodi-repo.service @USER@).
SVC_USER=$(on_host 'id -un')
case $SVC_USER in ''|*[!a-z0-9_.-]*) echo "unexpected user name '$SVC_USER'" >&2; exit 1;; esac
on_host "set -e
    if sudo ss -Hltn 'sport = :$PORT' | grep -q . && ! systemctl is-active -q kodi-repo; then
        echo 'port $PORT is already in use by something else:' >&2; sudo ss -Hltnp 'sport = :$PORT' >&2; exit 1
    fi
    sudo install -d -o $SVC_USER -g $SVC_USER -m 755 /srv/kodi-repo"
on_host "sudo install -d -m 755 /usr/local/lib/kodi-repo && sudo tee /usr/local/lib/kodi-repo/kodi_repo_server.py >/dev/null && sudo chmod 644 /usr/local/lib/kodi-repo/kodi_repo_server.py" < "$SERVER"
sed "s/@USER@/$SVC_USER/g" "$UNIT" | on_host "sudo tee /etc/systemd/system/kodi-repo.service >/dev/null && sudo chmod 644 /etc/systemd/system/kodi-repo.service"
on_host "set -e
    sudo systemctl daemon-reload
    sudo systemctl enable kodi-repo.service
    sudo systemctl restart kodi-repo.service
    for _ in \$(seq 1 20); do curl -sf -o /dev/null http://127.0.0.1:$PORT/ && break; sleep 0.5; done
    echo \"kodi-repo: \$(systemctl is-enabled kodi-repo) \$(systemctl is-active kodi-repo)\"
    curl -sf -o /dev/null -w 'http://127.0.0.1:$PORT/ -> %{http_code}\n' http://127.0.0.1:$PORT/"

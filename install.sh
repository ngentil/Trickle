#!/bin/sh
# Trickle installer.
#
#   Scheduler (any x86-64 Linux box):
#     sudo ./install.sh server
#
#   Worker (OpenStick; get the token from `trickle worker add <name>` first):
#     sudo ./install.sh worker http://<server-tailscale-ip>:8700 tkw_xxxxx [slots]
set -e
cd "$(dirname "$0")"
ROLE=$1
mkdir -p /etc/trickle /opt/trickle

case "$ROLE" in
server)
    command -v python3 >/dev/null || { echo "need python3"; exit 1; }
    python3 -m venv /opt/trickle/venv 2>/dev/null || { apt-get install -y python3-venv && python3 -m venv /opt/trickle/venv; }
    /opt/trickle/venv/bin/pip install -q -r server/requirements.txt
    rm -rf /opt/trickle/server && cp -r server /opt/trickle/server
    install -m 755 cli/trickle.py /usr/local/bin/trickle
    if [ ! -f /etc/trickle/server.env ]; then
        ADMIN=$(head -c 24 /dev/urandom | base64 | tr -d '/+=')
        printf 'TRICKLE_ADMIN_TOKEN=%s\nTRICKLE_DB=/var/lib/trickle/trickle.db\n' "$ADMIN" > /etc/trickle/server.env
        chmod 600 /etc/trickle/server.env
        echo "Admin token: $ADMIN"
        echo "Add to your shell:  export TRICKLE_ADMIN_TOKEN=$ADMIN TRICKLE_SERVER=http://127.0.0.1:8700"
    fi
    cp systemd/trickle-server.service /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now trickle-server
    echo "Scheduler up on :8700 — dashboard http://<this-box>:8700/?token=<admin token>, API docs /docs"
    ;;
worker)
    SERVER=$2; TOKEN=$3; SLOTS=${4:-1}
    [ -n "$SERVER" ] && [ -n "$TOKEN" ] || { echo "usage: $0 worker <server-url> <worker-token> [slots]"; exit 1; }
    install -m 755 worker/worker.py /opt/trickle/worker.py
    printf 'TRICKLE_SERVER=%s\nTRICKLE_WORKER_TOKEN=%s\nTRICKLE_SLOTS=%s\nTRICKLE_WORKDIR=/var/tmp/trickle\n' \
        "$SERVER" "$TOKEN" "$SLOTS" > /etc/trickle/worker.env
    chmod 600 /etc/trickle/worker.env
    cp systemd/trickle-worker.service /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now trickle-worker
    echo "Worker up. Logs: journalctl -u trickle-worker -f"
    ;;
*)
    echo "usage: $0 server | worker <server-url> <worker-token> [slots]"; exit 1 ;;
esac

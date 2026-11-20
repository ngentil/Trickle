#!/bin/sh
# Ratrun installer.
#
#   Scheduler (any x86-64 Linux box):
#     sudo ./install.sh server
#
#   Worker (OpenStick; get the token from `ratrun worker add <name>` first):
#     sudo ./install.sh worker http://<server-tailscale-ip>:8700 rrw_xxxxx [slots]
set -e
cd "$(dirname "$0")"
ROLE=$1
mkdir -p /etc/ratrun /opt/ratrun

case "$ROLE" in
server)
    command -v python3 >/dev/null || { echo "need python3"; exit 1; }
    python3 -m venv /opt/ratrun/venv 2>/dev/null || { apt-get install -y python3-venv && python3 -m venv /opt/ratrun/venv; }
    /opt/ratrun/venv/bin/pip install -q -r server/requirements.txt
    rm -rf /opt/ratrun/server && cp -r server /opt/ratrun/server
    install -m 755 cli/ratrun.py /usr/local/bin/ratrun
    if [ ! -f /etc/ratrun/server.env ]; then
        ADMIN=$(head -c 24 /dev/urandom | base64 | tr -d '/+=')
        printf 'RATRUN_ADMIN_TOKEN=%s\nRATRUN_DB=/var/lib/ratrun/ratrun.db\n' "$ADMIN" > /etc/ratrun/server.env
        chmod 600 /etc/ratrun/server.env
        echo "Admin token: $ADMIN"
        echo "Add to your shell:  export RATRUN_ADMIN_TOKEN=$ADMIN RATRUN_SERVER=http://127.0.0.1:8700"
    fi
    cp systemd/ratrun-server.service /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now ratrun-server
    echo "Scheduler up on :8700 — dashboard http://<this-box>:8700/?token=<admin token>, API docs /docs"
    ;;
worker)
    SERVER=$2; TOKEN=$3; SLOTS=${4:-1}
    [ -n "$SERVER" ] && [ -n "$TOKEN" ] || { echo "usage: $0 worker <server-url> <worker-token> [slots]"; exit 1; }
    install -m 755 worker/worker.py /opt/ratrun/worker.py
    printf 'RATRUN_SERVER=%s\nRATRUN_WORKER_TOKEN=%s\nRATRUN_SLOTS=%s\nRATRUN_WORKDIR=/var/tmp/ratrun\n' \
        "$SERVER" "$TOKEN" "$SLOTS" > /etc/ratrun/worker.env
    chmod 600 /etc/ratrun/worker.env
    cp systemd/ratrun-worker.service /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now ratrun-worker
    echo "Worker up. Logs: journalctl -u ratrun-worker -f"
    ;;
*)
    echo "usage: $0 server | worker <server-url> <worker-token> [slots]"; exit 1 ;;
esac

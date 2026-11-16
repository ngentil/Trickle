#!/bin/sh
# Moon Pickle installer.
#
#   Scheduler (any x86-64 Linux box):
#     sudo ./install.sh server
#
#   Worker (OpenStick; get the token from `moonpickle worker add <name>` first):
#     sudo ./install.sh worker http://<server-tailscale-ip>:8700 mpw_xxxxx [slots]
set -e
cd "$(dirname "$0")"
ROLE=$1
mkdir -p /etc/moonpickle /opt/moonpickle

case "$ROLE" in
server)
    command -v python3 >/dev/null || { echo "need python3"; exit 1; }
    python3 -m venv /opt/moonpickle/venv 2>/dev/null || { apt-get install -y python3-venv && python3 -m venv /opt/moonpickle/venv; }
    /opt/moonpickle/venv/bin/pip install -q -r server/requirements.txt
    rm -rf /opt/moonpickle/server && cp -r server /opt/moonpickle/server
    install -m 755 cli/moonpickle.py /usr/local/bin/moonpickle
    if [ ! -f /etc/moonpickle/server.env ]; then
        ADMIN=$(head -c 24 /dev/urandom | base64 | tr -d '/+=')
        printf 'MOONPICKLE_ADMIN_TOKEN=%s\nMOONPICKLE_DB=/var/lib/moonpickle/moonpickle.db\n' "$ADMIN" > /etc/moonpickle/server.env
        chmod 600 /etc/moonpickle/server.env
        echo "Admin token: $ADMIN"
        echo "Add to your shell:  export MOONPICKLE_ADMIN_TOKEN=$ADMIN MOONPICKLE_SERVER=http://127.0.0.1:8700"
    fi
    cp systemd/moonpickle-server.service /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now moonpickle-server
    echo "Scheduler up on :8700 — dashboard http://<this-box>:8700/?token=<admin token>, API docs /docs"
    ;;
worker)
    SERVER=$2; TOKEN=$3; SLOTS=${4:-1}
    [ -n "$SERVER" ] && [ -n "$TOKEN" ] || { echo "usage: $0 worker <server-url> <worker-token> [slots]"; exit 1; }
    install -m 755 worker/worker.py /opt/moonpickle/worker.py
    printf 'MOONPICKLE_SERVER=%s\nMOONPICKLE_WORKER_TOKEN=%s\nMOONPICKLE_SLOTS=%s\nMOONPICKLE_WORKDIR=/var/tmp/moonpickle\n' \
        "$SERVER" "$TOKEN" "$SLOTS" > /etc/moonpickle/worker.env
    chmod 600 /etc/moonpickle/worker.env
    cp systemd/moonpickle-worker.service /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now moonpickle-worker
    echo "Worker up. Logs: journalctl -u moonpickle-worker -f"
    ;;
*)
    echo "usage: $0 server | worker <server-url> <worker-token> [slots]"; exit 1 ;;
esac

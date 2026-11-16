#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Nathan Gentil
r"""
moonpickle: admin command-line client (runs anywhere; stdlib only).

    moonpickle submit "python3 ocr.py --job \$MOONPICKLE_JOB_ID" --hours 168 --count 50 --tags plates
    moonpickle ls [--status queued] [--tag plates]
    moonpickle show <id>          moonpickle cancel <id>
    moonpickle status             moonpickle quote --hours 168 --cpu 3600
    moonpickle worker add <name>  moonpickle worker rm <name>

Uses MOONPICKLE_SERVER and MOONPICKLE_ADMIN_TOKEN.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

SERVER = os.environ.get("MOONPICKLE_SERVER", "http://127.0.0.1:8700").rstrip("/")
TOKEN = os.environ.get("MOONPICKLE_ADMIN_TOKEN", "")


def call(method, path, body=None):
    req = urllib.request.Request(SERVER + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        sys.exit(f"error {exc.code}: {exc.read().decode(errors='replace')}")
    except urllib.error.URLError as exc:
        sys.exit(f"can't reach {SERVER}: {exc.reason}")


def when(ts):
    d = ts - time.time()
    s = abs(int(d))
    t = f"{s // 86400}d" if s >= 86400 else f"{s // 3600}h" if s >= 3600 else f"{s // 60}m"
    return f"in {t}" if d > 0 else f"{t} ago"


def mb(b):
    return "–" if b is None else f"{b / 1e6:.1f}MB"


def main():
    ap = argparse.ArgumentParser(prog="moonpickle")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit", help="queue a job")
    s.add_argument("command")
    s.add_argument("--hours", type=float, default=24, help="deadline, hours from now")
    s.add_argument("--timeout", type=int, default=3600, help="kill after N seconds")
    s.add_argument("--cpu", type=int, help="estimated CPU seconds (pricing)")
    s.add_argument("--tags", default="")
    s.add_argument("--count", type=int, default=1)
    s.add_argument("--attempts", type=int, default=3)
    l = sub.add_parser("ls", help="list jobs")
    l.add_argument("--status")
    l.add_argument("--tag")
    l.add_argument("--limit", type=int, default=30)
    sub.add_parser("show").add_argument("id")
    sub.add_parser("cancel").add_argument("id")
    sub.add_parser("status", help="fleet + queue overview")
    qp = sub.add_parser("quote")
    qp.add_argument("--hours", type=float, default=24)
    qp.add_argument("--cpu", type=int, default=3600)
    w = sub.add_parser("worker", help="manage sticks")
    w.add_argument("action", choices=["add", "rm"])
    w.add_argument("name")
    a = ap.parse_args()

    if a.cmd == "submit":
        r = call("POST", "/api/jobs", {"cmd": a.command, "deadline_hours": a.hours, "timeout_s": a.timeout,
                                       "est_cpu_s": a.cpu, "tags": a.tags, "count": a.count,
                                       "max_attempts": a.attempts})
        print(f"queued {len(r['ids'])} job(s)  tier={r['tier']}  price each={r['price_each']}")
        for i in r["ids"][:20]:
            print(" ", i)
        if len(r["ids"]) > 20:
            print(f"  … and {len(r['ids']) - 20} more")
    elif a.cmd == "ls":
        q = f"?limit={a.limit}" + (f"&status={a.status}" if a.status else "") + (f"&tag={a.tag}" if a.tag else "")
        for j in call("GET", "/api/jobs" + q):
            print(f"{j['id']}  {j['status']:<9} {j['tier']:<8} due {when(j['deadline']):<9} "
                  f"{(j['worker'] or ''):<10} {mb(j['net_bytes']):>7}  {j['cmd'][:45]}")
    elif a.cmd == "show":
        print(json.dumps(call("GET", f"/api/jobs/{a.id}"), indent=2))
    elif a.cmd == "cancel":
        print(call("POST", f"/api/jobs/{a.id}/cancel"))
    elif a.cmd == "status":
        s = call("GET", "/api/status")
        print(f"moonpickle {s['version']}  jobs: {s['jobs']}  overdue: {s['overdue']}")
        for w in s["workers"]:
            lte, net = w.get("lte") or {}, w.get("net") or {}
            sig = f"{lte['signal']}% {lte.get('tech', '')}" if lte.get("signal") is not None else "–"
            print(f"  {'up  ' if w['online'] else 'DOWN'} {w['id']:<12} load {w.get('load1', '–')}/{w.get('cpus', '–')}"
                  f"  {w.get('mem_avail_mb', '–')}MB  {w.get('temp_c') or '–'}°C  LTE {sig}"
                  f"  data {mb(net.get('rx'))}↓ {mb(net.get('tx'))}↑  running={w.get('running', [])}")
    elif a.cmd == "quote":
        print(call("GET", f"/api/quote?deadline_hours={a.hours}&est_cpu_s={a.cpu}"))
    elif a.cmd == "worker":
        if a.action == "add":
            r = call("POST", "/api/workers", {"id": a.name})
            print(f"worker '{r['id']}' added. Token (shown once):\n\n  MOONPICKLE_WORKER_TOKEN={r['token']}\n")
        else:
            print(call("DELETE", f"/api/workers/{a.name}"))


if __name__ == "__main__":
    main()

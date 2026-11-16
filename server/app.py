# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Nathan Gentil
"""
Moon Pickle scheduler v0.2: deadline-priced background compute.

Runs on any x86-64 box (FastAPI + uvicorn + SQLite).
The fleet is OpenStick sticks on 4G LTE, behind carrier NAT, paying per byte.
Design choices that follow from that:

- Workers pull. The server never connects to a stick.
- Long-poll leasing: a stick asks for work once and the server holds the
  request open until a job turns up (or ~50 s pass). Idle sticks cost almost
  no data, and new jobs start within a second.
- Slow heartbeats (default 60 s) that also carry LTE signal + data counters,
  and the heartbeat reply tells a stick which jobs to kill (cancelled/requeued).
- Gzip both ways.
- Per-worker tokens: a stolen stick can only lease and report, not submit.

Run:
    MOONPICKLE_ADMIN_TOKEN=... uvicorn app:app --host 0.0.0.0 --port 8700
Docs (OpenAPI) at /docs.
"""
from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import html
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

VERSION = "0.2.1"
ADMIN_TOKEN = os.environ.get("MOONPICKLE_ADMIN_TOKEN", "")
DB_PATH = os.environ.get("MOONPICKLE_DB", "moonpickle.db")
LEASE_GRACE_S = int(os.environ.get("MOONPICKLE_LEASE_GRACE", "120"))     # past timeout before a lease expires
WORKER_STALE_S = int(os.environ.get("MOONPICKLE_WORKER_STALE", "300"))  # 4G drops out; be patient
LONGPOLL_MAX_S = 55
GHOST_AFTER_S = int(os.environ.get("MOONPICKLE_GHOST_AFTER", "90"))    # leased but stick says it never got it
REAP_EVERY_S = 15
MAX_OUTPUT = 64 * 1024
BASE_RATE = float(os.environ.get("MOONPICKLE_BASE_RATE", "1.0"))        # credits per est. CPU-hour, day tier

PRICE_TIERS = [  # (max hours of slack, multiplier, name)
    (1, 4.0, "rush"),
    (24, 1.0, "day"),
    (24 * 7, 0.5, "week"),
    (float("inf"), 0.25, "whenever"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, cmd TEXT NOT NULL, tags TEXT DEFAULT '',
    created REAL NOT NULL, deadline REAL NOT NULL,
    timeout_s INTEGER NOT NULL, est_cpu_s INTEGER NOT NULL,
    tier TEXT NOT NULL, price REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',          -- queued|leased|done|failed|cancelled
    worker TEXT, lease_until REAL,
    attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
    started REAL, finished REAL, runtime_s REAL, net_bytes INTEGER,
    exit_code INTEGER, stdout TEXT, stderr TEXT, error TEXT,
    late INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, deadline);
CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
    created REAL NOT NULL, last_seen REAL, info TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1
);
"""


# ------------------------------------------------------------------ storage

class DB:
    """SQLite in WAL mode behind one lock. Plenty for hundreds of sticks;
    swap for Postgres when it isn't."""

    def __init__(self, path: str):
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).rowcount

    def tx(self, fn):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                out = fn(self.conn)
                self.conn.execute("COMMIT")
                return out
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise


db = DB(DB_PATH)


def hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def quote(deadline_hours: float, est_cpu_s: int):
    for max_h, mult, name in PRICE_TIERS:
        if deadline_hours <= max_h:
            return name, round(BASE_RATE * mult * max(est_cpu_s, 1) / 3600.0, 6)
    raise AssertionError


# ------------------------------------------------------------------ wake-ups for long-polls

class Wakeup:
    """Broadcast 'there may be new work' to every waiting long-poll."""

    def __init__(self):
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.event: Optional[asyncio.Event] = None

    def bind(self):
        self.loop = asyncio.get_running_loop()
        self.event = asyncio.Event()

    def notify(self):
        if not self.loop:
            return
        def _fire():
            old, self.event = self.event, asyncio.Event()
            old.set()
        self.loop.call_soon_threadsafe(_fire)

    @staticmethod
    async def wait(ev: asyncio.Event, timeout: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(ev.wait(), timeout)


wake = Wakeup()


def reap(now: float) -> int:
    """Expire leases whose timeout passed, or whose worker has gone silent."""
    def _do(conn):
        stale_before = now - WORKER_STALE_S
        dead = [r[0] for r in conn.execute(
            "SELECT id FROM workers WHERE last_seen IS NOT NULL AND last_seen < ?", (stale_before,))]
        marks = ",".join("?" * len(dead)) or "NULL"
        cond = f"status='leased' AND (lease_until < ? OR worker IN ({marks}))"
        args = (now, *dead)
        conn.execute(f"UPDATE jobs SET status='failed', error='lease lost too many times', finished=?, worker=NULL "
                     f"WHERE {cond} AND attempts >= max_attempts", (now, *args))
        return conn.execute(f"UPDATE jobs SET status='queued', worker=NULL, lease_until=NULL WHERE {cond}",
                            args).rowcount
    n = db.tx(_do)
    if n:
        wake.notify()
    return n


async def reaper_task():
    while True:
        await asyncio.sleep(REAP_EVERY_S)
        await asyncio.to_thread(reap, time.time())


@contextlib.asynccontextmanager
async def lifespan(_app):
    wake.bind()
    if not ADMIN_TOKEN:
        print("WARNING: MOONPICKLE_ADMIN_TOKEN not set — admin API is open")
    task = asyncio.create_task(reaper_task())
    yield
    task.cancel()


app = FastAPI(title="Moon Pickle", version=VERSION, lifespan=lifespan,
              description="Deadline-priced background compute on a fleet of junk hardware.")
app.add_middleware(GZipMiddleware, minimum_size=500)


# ------------------------------------------------------------------ auth

def _bearer(authorization: Optional[str]) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def admin(authorization: Optional[str] = Header(None), token: Optional[str] = Query(None)):
    if not ADMIN_TOKEN:
        return
    tok = _bearer(authorization) or (token or "")
    if not secrets.compare_digest(tok, ADMIN_TOKEN):
        raise HTTPException(401, "admin token required")


def worker_auth(authorization: Optional[str] = Header(None)) -> str:
    tok = _bearer(authorization)
    if tok:
        rows = db.q("SELECT id FROM workers WHERE token_hash=? AND enabled=1", (hash_token(tok),))
        if rows:
            return rows[0]["id"]
    raise HTTPException(401, "worker token required")


async def json_body(request: Request) -> dict:
    """JSON body, gunzipped if the stick compressed it (saves 4G data)."""
    raw = await request.body()
    if request.headers.get("content-encoding") == "gzip":
        raw = gzip.decompress(raw)
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "body must be JSON")


# ------------------------------------------------------------------ models

class JobIn(BaseModel):
    cmd: str = Field(..., min_length=1, description="Shell command to run on a worker")
    deadline_hours: Optional[float] = Field(None, gt=0, description="Deadline as hours from now")
    deadline: Optional[float] = Field(None, description="Deadline as a unix timestamp (overrides deadline_hours)")
    timeout_s: int = Field(3600, gt=0, le=7 * 86400)
    est_cpu_s: Optional[int] = Field(None, gt=0, description="Estimated CPU seconds, for pricing")
    tags: str = ""
    max_attempts: int = Field(3, ge=1, le=20)
    count: int = Field(1, ge=1, le=10000, description="Submit N identical jobs")


class WorkerIn(BaseModel):
    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")


# ------------------------------------------------------------------ client / admin API

@app.post("/api/jobs", status_code=201, dependencies=[Depends(admin)], tags=["jobs"])
def submit(job: JobIn):
    now = time.time()
    deadline = job.deadline or now + (job.deadline_hours or 24) * 3600
    if deadline <= now:
        raise HTTPException(400, "deadline is in the past")
    est = job.est_cpu_s or job.timeout_s
    tier, price = quote((deadline - now) / 3600, est)
    ids = [uuid.uuid4().hex[:12] for _ in range(job.count)]

    def _ins(conn):
        conn.executemany(
            "INSERT INTO jobs (id, cmd, tags, created, deadline, timeout_s, est_cpu_s, tier, price, max_attempts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(i, job.cmd, job.tags, now, deadline, job.timeout_s, est, tier, price, job.max_attempts) for i in ids])
    db.tx(_ins)
    wake.notify()
    return {"ids": ids, "tier": tier, "price_each": price, "deadline": deadline}


@app.get("/api/jobs", dependencies=[Depends(admin)], tags=["jobs"])
def list_jobs(status: Optional[str] = None, tag: Optional[str] = None, limit: int = Query(50, le=1000)):
    sql = ("SELECT id, cmd, tags, tier, price, status, worker, created, deadline, finished, "
           "runtime_s, net_bytes, exit_code, late, attempts FROM jobs WHERE 1=1")
    args: list = []
    if status:
        sql += " AND status=?"; args.append(status)
    if tag:
        sql += " AND (',' || tags || ',') LIKE ?"; args.append(f"%,{tag},%")
    return db.q(sql + " ORDER BY created DESC LIMIT ?", (*args, limit))


@app.get("/api/jobs/{job_id}", dependencies=[Depends(admin)], tags=["jobs"])
def get_job(job_id: str):
    rows = db.q("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not rows:
        raise HTTPException(404, "no such job")
    return rows[0]


@app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(admin)], tags=["jobs"])
def cancel(job_id: str):
    n = db.x("UPDATE jobs SET status='cancelled', finished=?, lease_until=NULL "
             "WHERE id=? AND status IN ('queued','leased')", (time.time(), job_id))
    if not n:
        raise HTTPException(409, "job is not queued or running")
    return {"ok": True}   # the worker learns to kill it on its next heartbeat


@app.get("/api/quote", tags=["jobs"])
def get_quote(deadline_hours: float = 24, est_cpu_s: int = 3600):
    tier, price = quote(deadline_hours, est_cpu_s)
    return {"tier": tier, "price": price, "tiers": [
        {"name": n, "max_hours": (None if h == float("inf") else h), "multiplier": m} for h, m, n in PRICE_TIERS]}


@app.get("/api/status", dependencies=[Depends(admin)], tags=["fleet"])
def status():
    now = time.time()
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
    overdue = db.q("SELECT COUNT(*) n FROM jobs WHERE status IN ('queued','leased') AND deadline < ?",
                   (now,))[0]["n"]
    workers = []
    for w in db.q("SELECT id, last_seen, info, enabled FROM workers ORDER BY id"):
        info = json.loads(w["info"])
        info.update(id=w["id"], last_seen=w["last_seen"], enabled=bool(w["enabled"]),
                    online=bool(w["last_seen"]) and now - w["last_seen"] < WORKER_STALE_S)
        workers.append(info)
    return {"version": VERSION, "now": now, "jobs": counts, "overdue": overdue, "workers": workers}


@app.post("/api/workers", status_code=201, dependencies=[Depends(admin)], tags=["fleet"])
def add_worker(w: WorkerIn):
    tok = "mpw_" + secrets.token_urlsafe(24)
    try:
        db.x("INSERT INTO workers (id, token_hash, created) VALUES (?,?,?)", (w.id, hash_token(tok), time.time()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "worker already exists (remove it first to reissue a token)")
    return {"id": w.id, "token": tok, "note": "shown once — put it in the stick's /etc/moonpickle.env"}


@app.delete("/api/workers/{worker_id}", dependencies=[Depends(admin)], tags=["fleet"])
def remove_worker(worker_id: str):
    if not db.x("DELETE FROM workers WHERE id=?", (worker_id,)):
        raise HTTPException(404, "no such worker")
    reap(time.time())
    return {"ok": True}


# ------------------------------------------------------------------ worker API

def _take(worker_id: str):
    now = time.time()

    def _do(conn):
        row = conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY deadline, created LIMIT 1").fetchone()
        if not row:
            return None
        conn.execute("UPDATE jobs SET status='leased', worker=?, lease_until=?, attempts=attempts+1, started=? "
                     "WHERE id=?", (worker_id, now + row["timeout_s"] + LEASE_GRACE_S, now, row["id"]))
        return {k: row[k] for k in ("id", "cmd", "timeout_s", "deadline", "tags")}
    return db.tx(_do)


def _unlease(job_id: str, worker_id: str):
    db.x("UPDATE jobs SET status='queued', worker=NULL, lease_until=NULL, attempts=attempts-1 "
         "WHERE id=? AND worker=? AND status='leased'", (job_id, worker_id))
    wake.notify()


@app.post("/api/worker/lease", tags=["worker"])
async def lease(request: Request, response: Response,
                wait: int = Query(LONGPOLL_MAX_S, ge=0, le=LONGPOLL_MAX_S),
                worker_id: str = Depends(worker_auth)):
    """Long-poll: returns a job as soon as one exists, or 204 after `wait` seconds."""
    db.x("UPDATE workers SET last_seen=? WHERE id=?", (time.time(), worker_id))
    end = time.monotonic() + wait
    while True:
        if await request.is_disconnected():   # stick's 4G dropped; don't hand a job to nobody
            return Response(status_code=499)
        ev = wake.event            # grab before checking, so a submit in between still wakes us
        job = await asyncio.to_thread(_take, worker_id)
        if job:
            if await request.is_disconnected():
                await asyncio.to_thread(_unlease, job["id"], worker_id)
                return Response(status_code=499)
            return job
        left = end - time.monotonic()
        if left <= 0:
            response.status_code = 204
            return None
        await wake.wait(ev, left)


@app.post("/api/worker/heartbeat", tags=["worker"])
async def heartbeat(request: Request, worker_id: str = Depends(worker_auth)):
    body = await json_body(request)
    running = [str(j) for j in body.get("running", [])]
    db.x("UPDATE workers SET last_seen=?, info=? WHERE id=?", (time.time(), json.dumps(body), worker_id))
    now = time.time()
    leased = db.q("SELECT id, started FROM jobs WHERE status='leased' AND worker=?", (worker_id,))
    leased_ids = {r["id"] for r in leased}
    # Stick is running something we no longer want it to: tell it to kill.
    kill = [j for j in running if j not in leased_ids]
    # Ghost leases: we leased it to this stick a while ago, the stick is alive and says it
    # isn't running it. The lease response got lost on the way (4G drop). Requeue now
    # instead of waiting hours for the timeout.
    ghosts = [r["id"] for r in leased if r["id"] not in running and now - r["started"] > GHOST_AFTER_S]
    for jid in ghosts:
        await asyncio.to_thread(_unlease, jid, worker_id)
    return {"kill": kill, "requeued": ghosts, "server_time": now}


@app.post("/api/worker/jobs/{job_id}/result", tags=["worker"])
async def result(job_id: str, request: Request, worker_id: str = Depends(worker_auth)):
    body = await json_body(request)
    now = time.time()
    exit_code = int(body.get("exit_code", -1))
    n = db.x(
        "UPDATE jobs SET status=?, exit_code=?, stdout=?, stderr=?, finished=?, runtime_s=?, net_bytes=?, "
        "lease_until=NULL, late=CASE WHEN ? > deadline THEN 1 ELSE 0 END "
        "WHERE id=? AND worker=? AND status='leased'",
        ("done" if exit_code == 0 else "failed", exit_code,
         (body.get("stdout") or "")[-MAX_OUTPUT:], (body.get("stderr") or "")[-MAX_OUTPUT:],
         now, body.get("runtime_s"), body.get("net_bytes"), now, job_id, worker_id))
    db.x("UPDATE workers SET last_seen=? WHERE id=?", (now, worker_id))
    if not n:
        raise HTTPException(409, "job is not leased to you (expired, cancelled or reassigned)")
    return {"ok": True}


# ------------------------------------------------------------------ dashboard

def _ago(sec: float) -> str:
    s = int(abs(sec))
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


def _mb(b) -> str:
    return "–" if b is None else f"{b / 1e6:.1f}"


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(admin)], include_in_schema=False)
def dashboard():
    s = status()
    now = s["now"]
    e = html.escape
    jobs = db.q("SELECT id, cmd, tier, status, worker, deadline, late, runtime_s, net_bytes FROM jobs "
                "ORDER BY CASE status WHEN 'leased' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, "
                "CASE WHEN status IN ('leased','queued') THEN deadline ELSE -finished END LIMIT 60")
    wrows = "".join(
        f"<tr><td>{'🟢' if w['online'] else '🔴'} {e(w['id'])}</td>"
        f"<td>{w.get('load1', '–')}/{w.get('cpus', '–')}</td><td>{w.get('mem_avail_mb', '–')}</td>"
        f"<td>{w.get('temp_c') or '–'}</td>"
        f"<td>{e(str((w.get('lte') or {}).get('signal', '–')))}{'%' if (w.get('lte') or {}).get('signal') is not None else ''}"
        f" {e(str((w.get('lte') or {}).get('tech', '')))}</td>"
        f"<td>{_mb((w.get('net') or {}).get('rx'))}/{_mb((w.get('net') or {}).get('tx'))}</td>"
        f"<td>{_ago(now - w['last_seen']) + ' ago' if w['last_seen'] else 'never'}</td>"
        f"<td>{e(', '.join(w.get('running', [])) or 'idle')}</td></tr>" for w in s["workers"]) \
        or "<tr><td colspan=8>No workers — add one with <code>moonpickle worker add NAME</code></td></tr>"
    jrows = "".join(
        f"<tr class='{j['status']}'><td>{j['id']}</td><td>{e(j['status'])}{' ⚠late' if j['late'] else ''}</td>"
        f"<td>{e(j['tier'])}</td>"
        f"<td>{('in ' + _ago(j['deadline'] - now)) if j['deadline'] > now else _ago(now - j['deadline']) + ' ago'}</td>"
        f"<td>{e(j['worker'] or '')}</td><td>{'' if j['runtime_s'] is None else round(j['runtime_s'], 1)}</td>"
        f"<td>{'' if j['net_bytes'] is None else _mb(j['net_bytes'])}</td>"
        f"<td><code>{e(j['cmd'][:60])}</code></td></tr>" for j in jobs) \
        or "<tr><td colspan=8>No jobs yet</td></tr>"
    counts = " · ".join(f"{k}: {v}" for k, v in sorted(s["jobs"].items())) or "empty"
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta http-equiv=refresh content=10>
<title>Moon Pickle</title><style>
body{{font:14px system-ui,sans-serif;margin:12px;background:#111;color:#ddd}}
h1{{font-size:18px;margin:0 0 4px}} h2{{font-size:15px;margin:18px 0 6px}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;white-space:nowrap}}
td,th{{padding:4px 6px;border-bottom:1px solid #333;text-align:left}}
code{{font-size:12px;color:#9cf}} .leased td{{color:#fd6}} .failed td{{color:#f77}} .done td{{color:#8c8}}
.cancelled td{{color:#888}} .muted{{color:#888}}</style></head><body>
<h1>Moon Pickle <span class=muted>v{VERSION}</span></h1><div class=muted>{counts} · overdue: {s['overdue']}</div>
<h2>Fleet</h2><div class=wrap><table><tr><th>Worker</th><th>Load</th><th>RAM MB</th><th>°C</th>
<th>LTE</th><th>Data MB rx/tx</th><th>Seen</th><th>Running</th></tr>{wrows}</table></div>
<h2>Jobs</h2><div class=wrap><table><tr><th>ID</th><th>Status</th><th>Tier</th><th>Deadline</th><th>Worker</th>
<th>Secs</th><th>MB</th><th>Command</th></tr>{jrows}</table></div>
<p class=muted>API docs: <a href="/docs" style="color:#9cf">/docs</a></p></body></html>"""

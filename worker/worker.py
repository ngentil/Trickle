#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Nathan Gentil
"""
Ratrun worker v0.2. Runs on anything with Python 3.8+ (Linux, macOS, BSD).

Stdlib only, so there's nothing to install. The first target is OpenStick
4G LTE sticks, so it is built for the worst case, a metered, flaky mobile link:
- Long-poll leasing: one request sits open until the server has work, so an
  idle stick makes about one request a minute instead of polling every few seconds.
- Heartbeat every 60 s, gzipped, carrying LTE signal and data counters.
  The reply lists jobs to kill (cancelled or reassigned), so no wasted compute.
- Results are gzipped and retried with backoff when the link drops.
- Each job reports how many bytes the stick moved while it ran.

Run:
    RATRUN_SERVER=http://100.x.y.z:8700 RATRUN_WORKER_TOKEN=rrw_... python3 worker.py
"""
import argparse
import gzip
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

VERSION = "0.2.2"
MAX_OUTPUT = 64 * 1024
LONGPOLL_S = 50


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- HTTP

class Client:
    def __init__(self, server, token):
        self.server = server.rstrip("/")
        self.token = token

    def call(self, method, path, body=None, timeout=30):
        data = None
        req = urllib.request.Request(self.server + path, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Accept-Encoding", "gzip")
        if body is not None:
            data = gzip.compress(json.dumps(body, separators=(",", ":")).encode(), 6)
            req.add_header("Content-Type", "application/json")
            req.add_header("Content-Encoding", "gzip")
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return r.status, (json.loads(raw) if raw else None)


# ---------------------------------------------------------------- system + modem stats

def read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def mem_avail_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def temp_c():
    temps = []
    base = "/sys/class/thermal"
    if os.path.isdir(base):
        for z in os.listdir(base):
            t = read_int(os.path.join(base, z, "temp"))
            if t is not None:
                temps.append(t / 1000)
    return round(max(temps), 1) if temps else None


def net_bytes(ifaces=None):
    """rx/tx byte counters since boot. Default: every interface except loopback/tailscale
    (tailscale traffic is already counted on the LTE interface underneath it)."""
    rx = tx = 0
    try:
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                name, data = line.split(":", 1)
                name = name.strip()
                if ifaces:
                    if name not in ifaces:
                        continue
                elif name == "lo" or name.startswith(("tailscale", "docker", "veth")):
                    continue
                cols = data.split()
                rx += int(cols[0])
                tx += int(cols[8])
    except (OSError, ValueError, IndexError):
        return None
    return {"rx": rx, "tx": tx}


def lte_status():
    """Signal/operator from ModemManager, if this stick has it (OpenStick does)."""
    if not shutil.which("mmcli"):
        return {}
    try:
        out = subprocess.run(["mmcli", "-m", "any", "--output-keyvalue"], capture_output=True,
                             text=True, timeout=8).stdout
    except (subprocess.SubprocessError, OSError):
        return {}
    kv = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            kv[k.strip()] = v.strip()
    sig = kv.get("modem.generic.signal-quality.value")
    return {
        "signal": int(sig) if sig and sig.isdigit() else None,
        "tech": kv.get("modem.generic.access-technologies.value[1]", ""),
        "operator": kv.get("modem.3gpp.operator-name", ""),
        "state": kv.get("modem.generic.state", ""),
    }


# ---------------------------------------------------------------- worker

class Worker:
    def __init__(self, client, name, slots, workdir, max_temp, hb_every, ifaces):
        self.c = client
        self.name = name
        self.slots = slots
        self.workdir = workdir
        self.max_temp = max_temp
        self.hb_every = hb_every
        self.ifaces = ifaces
        self.procs = {}                 # job id -> Popen
        self.killed = set()
        self.reporting = set()          # finished, result not yet delivered (link down)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.started = time.time()

    # -- heartbeat

    def stats(self):
        with self.lock:
            running = list(self.procs) + list(self.reporting)
        return {
            "name": self.name, "version": VERSION, "arch": platform.machine(),
            "load1": round(os.getloadavg()[0], 2), "cpus": os.cpu_count(),
            "mem_avail_mb": mem_avail_mb(), "temp_c": temp_c(),
            "disk_free_mb": shutil.disk_usage(self.workdir).free // 2**20,
            "uptime_s": int(time.time() - self.started), "slots": self.slots,
            "running": running, "net": net_bytes(self.ifaces), "lte": lte_status(),
        }

    def heartbeat_loop(self):
        while not self.stop.is_set():
            try:
                _, r = self.c.call("POST", "/api/worker/heartbeat", self.stats(), timeout=30)
                for job_id in (r or {}).get("requeued", []):
                    log(f"server requeued {job_id} (lease reply never arrived)")
                for job_id in (r or {}).get("kill", []):
                    self.kill(job_id, "server says stop (cancelled or reassigned)")
            except urllib.error.HTTPError as exc:
                log(f"heartbeat rejected: {exc.code} {exc.read()[:200]!r}")
            except (urllib.error.URLError, OSError) as exc:
                log(f"heartbeat failed: {exc}")
            self.stop.wait(self.hb_every)

    def kill(self, job_id, why):
        with self.lock:
            p = self.procs.get(job_id)
            if not p:
                return
            self.killed.add(job_id)
        log(f"killing {job_id}: {why}")
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    # -- leasing

    def too_hot(self):
        t = temp_c()
        return bool(self.max_temp) and t is not None and t >= self.max_temp

    def slot_loop(self, slot):
        backoff = 2
        while not self.stop.is_set():
            if self.too_hot():
                log(f"slot {slot}: {temp_c()}°C, cooling off")
                self.stop.wait(60)
                continue
            try:
                code, job = self.c.call("POST", f"/api/worker/lease?wait={LONGPOLL_S}",
                                        timeout=LONGPOLL_S + 25)
                backoff = 2
            except urllib.error.HTTPError as exc:
                log(f"slot {slot}: lease rejected {exc.code}; check the worker token")
                self.stop.wait(300)
                continue
            except (urllib.error.URLError, OSError) as exc:
                log(f"slot {slot}: link down ({exc}), retry in {backoff}s")
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 300)       # don't hammer a dead 4G link
                continue
            if code == 200 and job:
                self.run(slot, job)

    # -- running

    def run(self, slot, job):
        jid = job["id"]
        log(f"slot {slot}: start {jid}  {job['cmd'][:70]}")
        jobdir = tempfile.mkdtemp(prefix=f"ratrun-{jid}-", dir=self.workdir)
        env = dict(os.environ, RATRUN_JOB_ID=jid, RATRUN_WORKER=self.name,
                   RATRUN_DEADLINE=str(job["deadline"]), RATRUN_TAGS=job.get("tags") or "")
        env.pop("RATRUN_WORKER_TOKEN", None)          # jobs don't get the stick's credentials
        net0 = net_bytes(self.ifaces)
        t0 = time.time()
        out = err = b""
        try:
            p = subprocess.Popen(job["cmd"], shell=True, cwd=jobdir, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=True)
            with self.lock:
                self.procs[jid] = p
            try:
                out, err = p.communicate(timeout=job["timeout_s"])
                exit_code = p.returncode
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                out, err = p.communicate()
                exit_code, err = 124, err + b"\n[ratrun] killed: timeout"
        except Exception as exc:  # noqa: BLE001 — report it, don't die
            exit_code, err = 125, f"[ratrun] worker error: {exc}".encode()
        finally:
            shutil.rmtree(jobdir, ignore_errors=True)
            with self.lock:
                self.procs.pop(jid, None)
                was_killed = jid in self.killed
                self.killed.discard(jid)
        runtime = round(time.time() - t0, 2)
        if was_killed:
            log(f"slot {slot}: {jid} killed after {runtime}s, not reporting")
            return
        net1 = net_bytes(self.ifaces)
        moved = (net1["rx"] + net1["tx"] - net0["rx"] - net0["tx"]) if net0 and net1 else None
        result = {"exit_code": exit_code, "runtime_s": runtime, "net_bytes": moved,
                  "stdout": out[-MAX_OUTPUT:].decode(errors="replace"),
                  "stderr": err[-MAX_OUTPUT:].decode(errors="replace")}
        log(f"slot {slot}: done  {jid}  exit={exit_code}  {runtime}s  net={moved}B")
        with self.lock:
            self.reporting.add(jid)
        try:
            self.report(jid, result)
        finally:
            with self.lock:
                self.reporting.discard(jid)

    def report(self, jid, result):
        delay = 5
        deadline = time.time() + 1800                  # keep trying through a 30 min outage
        while time.time() < deadline and not self.stop.is_set():
            try:
                self.c.call("POST", f"/api/worker/jobs/{jid}/result", result, timeout=60)
                return
            except urllib.error.HTTPError as exc:
                log(f"result for {jid} rejected ({exc.code}): lease expired or job cancelled")
                return
            except (urllib.error.URLError, OSError) as exc:
                log(f"result for {jid} not sent ({exc}), retry in {delay}s")
                self.stop.wait(delay)
                delay = min(delay * 2, 300)
        log(f"gave up reporting {jid}; the server will requeue it")

    def start(self):
        log(f"ratrun worker {VERSION} '{self.name}' → {self.c.server}  slots={self.slots}")
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        threads = [threading.Thread(target=self.slot_loop, args=(i,), daemon=True) for i in range(self.slots)]
        for t in threads:
            t.start()
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        try:
            while not self.stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop.set()
        log("stopping")


def main():
    env = os.environ.get
    ap = argparse.ArgumentParser(description="Ratrun worker")
    ap.add_argument("--server", default=env("RATRUN_SERVER", "http://127.0.0.1:8700"))
    ap.add_argument("--name", default=env("RATRUN_WORKER_NAME", socket.gethostname()), help="label for logs")
    ap.add_argument("--slots", type=int, default=int(env("RATRUN_SLOTS", "1")))
    ap.add_argument("--workdir", default=env("RATRUN_WORKDIR", tempfile.gettempdir()))
    ap.add_argument("--max-temp", type=float, default=float(env("RATRUN_MAX_TEMP", "75")))
    ap.add_argument("--heartbeat", type=int, default=int(env("RATRUN_HEARTBEAT", "60")),
                    help="seconds between heartbeats (data cost vs freshness)")
    ap.add_argument("--ifaces", default=env("RATRUN_NET_IFACES", ""),
                    help="comma list of interfaces to meter, e.g. wwan0 (default: all but lo/tailscale)")
    a = ap.parse_args()
    token = env("RATRUN_WORKER_TOKEN", "")
    if not token:
        raise SystemExit("RATRUN_WORKER_TOKEN is not set (get one with: ratrun worker add <name>)")
    os.makedirs(a.workdir, exist_ok=True)
    Worker(Client(a.server, token), a.name, a.slots, a.workdir, a.max_temp, a.heartbeat,
           [i for i in a.ifaces.split(",") if i]).start()


if __name__ == "__main__":
    main()

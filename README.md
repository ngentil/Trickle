# Moon Pickle

Deadline-priced background compute on whatever hardware you've got. Every job has a deadline, and the longer you can wait, the cheaper it is. The scheduler always runs the job due soonest.

- **Scheduler:** any x86-64 box. FastAPI + uvicorn + SQLite, with OpenAPI docs at `/docs`.
- **Fleet:** anything that runs Python 3.8+. The worker is one stdlib-only file, so there's nothing to install. The first target is OpenStick 4G LTE sticks, so the defaults assume the hardest case: tiny RAM and a metered, flaky link.

## Supported hardware

| Hardware | Status |
|---|---|
| OpenStick 4G LTE sticks (MSM8916, Debian) | Primary target. LTE signal and data metering via ModemManager |
| Linux SBCs: Raspberry Pi, Orange Pi, old TV boxes | Should work as-is |
| Old x86 laptops, mini PCs, servers | Should work as-is; raise `MOONPICKLE_SLOTS` |
| macOS / BSD | Should work; RAM and temperature stats show as blank |
| Android phones via Termux | Untested, likely works |
| Windows | Not yet (the worker uses POSIX process groups) |

Machines without a modem skip the LTE fields. Data cost only matters on metered links, so wired or Wi-Fi boxes can use a shorter `MOONPICKLE_HEARTBEAT` without worry.

```
  you / AI agent ──admin token──▶  scheduler (x86)  ◀──── long-poll lease ────  stick (4G, CGNAT)
                                   SQLite · dashboard     heartbeat 60 s, gzip   × N
```

## Built for 4G

| Problem on LTE | What Moon Pickle does |
|---|---|
| Sticks sit behind carrier NAT | Sticks always dial out; the server never connects in |
| Data costs money | Long-poll leasing: an idle stick makes ~1 request a minute, not one every few seconds. Heartbeats and results are gzipped |
| Links drop silently | A dropped long-poll can't take a job. If a lease reply gets lost, the heartbeat spots it and the job is requeued within ~90 s |
| Sticks vanish for minutes | A worker is only declared dead after 5 min of silence. Results are retried for up to 30 min |
| Cancelling a running job | The next heartbeat reply tells the stick to kill it |
| Knowing what each stick costs | Each heartbeat reports LTE signal, network type, operator and rx/tx counters. Each job records the bytes it moved |

A rough idle cost is ~100 MB/month per stick, mostly HTTP and TCP overhead. Setting `MOONPICKLE_HEARTBEAT=300` roughly halves it.

## Getting the sticks to the scheduler

The scheduler needs an address the sticks can reach. Easiest is **Tailscale on every stick and on the scheduler**: it works through CGNAT and encrypts the traffic. Then use `http://100.x.y.z:8700` as the server URL. The alternative is a Cloudflare Tunnel with an `https://` URL.

## Setup

**Scheduler:**
```sh
sudo ./install.sh server              # prints the admin token
export MOONPICKLE_SERVER=http://127.0.0.1:8700 MOONPICKLE_ADMIN_TOKEN=<token>
moonpickle worker add stick01            # prints that stick's token, shown once
```

**Each stick** (copy this folder over first):
```sh
sudo ./install.sh worker http://100.x.y.z:8700 mpw_xxxxx
```

To run the scheduler in Docker instead: `docker build -t moonpickle . && docker run -d -p 8700:8700 -v moonpickle:/data -e MOONPICKLE_ADMIN_TOKEN=... moonpickle`

## Using it

```sh
moonpickle submit 'python3 /opt/jobs/ocr.py --job $MOONPICKLE_JOB_ID' --count 200 --hours 168 --timeout 1800 --tags plates
moonpickle status                 # fleet: load, temp, LTE signal, data used
moonpickle ls --tag plates
moonpickle show <id>              # full output
moonpickle cancel <id>            # running jobs get killed on the stick
moonpickle quote --hours 168 --cpu 3600
moonpickle worker add|rm <name>
```

Dashboard (works on a phone): `http://<scheduler>:8700/?token=<admin token>`

A job is a shell command. It runs in a fresh temp dir, at the lowest CPU/IO priority, and gets these environment variables: `MOONPICKLE_JOB_ID`, `MOONPICKLE_WORKER`, `MOONPICKLE_DEADLINE`, `MOONPICKLE_TAGS`. The stick's own token is stripped from the job's environment. Jobs fetch their own input and push their own output (e.g. Supabase). Only the last 64 KB of stdout/stderr is kept.

## Tokens

- **Admin token:** submit, cancel, list, add and remove workers, dashboard.
- **Worker tokens:** one per stick, stored hashed. They can only lease, heartbeat and report. A stolen stick can't submit jobs to the rest of the fleet, and `moonpickle worker rm` cuts it off immediately.

## Settings (server env)

| Var | Default | |
|---|---|---|
| `MOONPICKLE_ADMIN_TOKEN` | – | required in production |
| `MOONPICKLE_DB` | `moonpickle.db` | SQLite path |
| `MOONPICKLE_WORKER_STALE` | 300 | seconds of silence before a stick's jobs are requeued |
| `MOONPICKLE_LEASE_GRACE` | 120 | seconds past a job's timeout before its lease expires |
| `MOONPICKLE_GHOST_AFTER` | 90 | requeue a lease the stick says it never got |
| `MOONPICKLE_BASE_RATE` | 1.0 | credits per estimated CPU-hour at the day tier |

**Worker env:** `MOONPICKLE_SERVER`, `MOONPICKLE_WORKER_TOKEN`, `MOONPICKLE_SLOTS` (1), `MOONPICKLE_HEARTBEAT` (60), `MOONPICKLE_MAX_TEMP` (75 °C), `MOONPICKLE_NET_IFACES` (e.g. `wwan0`; defaults to everything except lo and tailscale).

## Roadmap

1. ✅ v0.1: queue, deadlines, leases, dashboard
2. ✅ v0.2: x86 FastAPI scheduler, 4G-aware fleet, per-stick tokens, cancel-kill, ghost-lease recovery, data metering
3. Capability matching: workers advertise arch, RAM, cores, GPU and network type (LTE/wired), and jobs can require them, e.g. `--needs ram>=2048,net=wired`. This is the key step for mixed fleets
4. Job files: the scheduler stores inputs/outputs, so sticks stop needing their own Supabase credentials
5. Feasibility check on submit: "can the fleet actually hit this deadline?"
6. MCP server so AI agents can quote, submit and collect results
7. Sandboxed jobs + customer API keys + credit ledger, needed before anyone else's code runs
8. Data-cost-aware scheduling: send heavy-download jobs to sticks with strong signal or cheap data
9. Hosted control plane (the paid tier)

## License

Copyright (C) 2026 Nathan Gentil

Moon Pickle is free software under the [GNU Affero General Public License v3.0](LICENSE). You can use, modify and self-host it. If you run a modified version as a network service, you must publish your changes under the same licence.

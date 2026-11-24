# Primal Space CLI

Command-line client for the Primal Space experiment. Runs on Android/Termux,
discovers real device resources, maintains an authenticated heartbeat to the
Primal Space Monitor, and executes only an allow-listed deterministic benchmark
when the user explicitly enables compute.

## Requirements

- Android ARM64 (tested on Realme RMX3430 / Android 13)
- Termux with Python 3.9+
- Termux:API companion app and `termux-api` package for battery/Wi-Fi readings
  (optional; `/proc` and `/sys` fallbacks are used when unavailable)
- No third-party Python dependencies

## Install on Termux

```sh
pkg install python termux-api
python -m pip install --upgrade pip setuptools wheel
python -m pip install --no-build-isolation .

# or from GitHub
python -m pip install --no-build-isolation "git+https://github.com/mahithpaulin/primal-space-cli.git"

primal --version
```

If installing from a clone:

```sh
git clone https://github.com/mahithpaulin/primal-space-cli.git
cd primal-space-cli
bash install-termux.sh
```

## Quick start

```sh
primal init --server https://primalmonitor.vercel.app --enrollment-key "<key>"
primal discover
primal connect
primal status
```

`primal connect` registers the device (once), sends a heartbeat, and starts a
background heartbeat daemon. The device appears on the monitor at
`https://primalmonitor.vercel.app/`.

## Commands

| Command | Purpose |
| --- | --- |
| `primal init` | Create/update config, server URL, enrollment key, device name |
| `primal discover` | Print real device resources (`--json` for machine output) |
| `primal connect` | Register if needed, heartbeat, start heartbeat daemon |
| `primal status` | Local daemons + server-side device record |
| `primal monitor` | Live terminal view of the server record |
| `primal compute enable` | Explicitly authorize compute |
| `primal compute disable` | Revoke compute; aborts a running task |
| `primal worker start` | Start the background worker (requires compute enabled) |
| `primal worker stop` | Stop worker; cancels a running task safely |
| `primal worker pause` | Stop claiming new tasks (current task continues) |
| `primal worker resume` | Resume claiming tasks |
| `primal worker cancel` | Abort the current task without stopping the worker |
| `primal settings show` | Show compute mode, caps and lending status |
| `primal settings mode off\|ondemand\|lend` | Set the compute mode |
| `primal settings auto on\|off` | Start lending automatically when the daemon starts |
| `primal settings caps --day N --week N --month N` | Maximum compute minutes per period (0 = unlimited) |
| `primal profile set --name NAME --status TEXT --avatar EMOJI` | Set the profile other devices see |
| `primal profile show` | Show the current profile |
| `primal wallet [--history N]` | Compute-unit balance, earnings and cap usage |
| `primal credits` | Compute units banked by the lending ledger |
| `primal lend status\|enable\|disable` | Active-compute lending control |
| `primal config show` | Show contribution settings |
| `primal config set KEY VALUE` | Change a setting |
| `primal disconnect` | Stop daemons and mark the device offline |
| `primal logs heartbeat\|worker` | Show recent daemon logs |

## Default contribution settings

```text
compute_enabled = false
minimum_battery = 20
maximum_cpu_usage = 20
maximum_memory_usage_mb = 128
```

These are conservative targets, not exact Android enforcement guarantees. CPU
limiting uses process priority (`nice 19`) plus duty cycling. A 20% battery
minimum is enforced before work, and both Wi-Fi and mobile data are allowed.
Memory is monitored
against the configured cap and the task is aborted if exceeded. Workloads are
small, deterministic, and capped server-side.

No computation occurs until `primal compute enable` and `primal worker start` are
both run. Disabling compute aborts the current task within about one second.

## What is collected

Manufacturer, model, Android version/SDK, CPU architecture, CPU cores, CPU
utilization, RAM total/available/utilization, free storage (Termux app-private
view), battery percentage/charging, network connected/type, CLI version, worker
status, and last heartbeat timestamp.

Not collected: files, contacts, messages, precise location, SSID/IP/MAC, IMEI,
serial numbers, Android ID, or other personal identifiers. The install ID is a
random UUID generated locally and is not derived from hardware.

## Workloads

- `python` — sandboxed Python on CLI devices. Define `main(items)` and return
  JSON-serializable data. Imports are limited to safe stdlib modules (math, json,
  hashlib, hmac, base64, random, statistics, datetime, itertools, collections,
  functools, re, decimal, fractions, struct, uuid, zlib). No file, network, or
  process access.
- `shell` — operator-authorized `sh -c` command on CLI devices, CPU/time limited
  with a 4 MB file cap.
- `http` — HTTP(S) request from any device (CLI or Android). Useful for inference
  APIs, webhooks, and remote data checks.

Tasks can be pinned to a device or left unassigned for any eligible device. Jobs
split a JSON `items` array into parallel shards and reduce results with
`concat`, `sum`, `count`, or `first`.

Devices can message each other and the operator:

```sh
primal message send all "hello"
primal message send Realme\ Narzo\ 50A "status?"
primal message inbox
primal message threads
```

Compute modes control how a device participates:

| Mode | Behaviour |
| --- | --- |
| `off` | Never claims work; the worker refuses to start |
| `ondemand` | Claims work only when explicitly enabled |
| `lend` | Lends a CPU slice and earns compute units (CU) |

One hour of compute lent earns one CU. Earnings are weighted by measured CPU
time and capped at the promised CPU share. CU balances live on the device and
can be transferred peer-to-peer by the operator; the server keeps the matching
ledger, and both sides are notified over the message bus.

Day/week/month caps stop the worker from claiming once the configured compute
minutes for a period are used up.

## Security notes

- The device token is stored in `~/.config/primal/config.json` with mode 600.
- Only SHA-256 token hashes are stored server-side.
- HTTPS is required (HTTP allowed only for localhost development).
- `primal` never downloads or executes remote programs.

## License

MIT

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
primal init --server https://primal-space-monitor.vercel.app --enrollment-key "<key>"
primal discover
primal connect
primal status
```

`primal connect` registers the device (once), sends a heartbeat, and starts a
background heartbeat daemon. The device appears on the monitor at
`https://primal-space-monitor.vercel.app/`.

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
| `primal config show` | Show contribution settings |
| `primal config set KEY VALUE` | Change a setting |
| `primal disconnect` | Stop daemons and mark the device offline |
| `primal logs heartbeat\|worker` | Show recent daemon logs |

## Default contribution settings

```text
compute_enabled = false
wifi_only = true
charging_required = true
minimum_battery = 50
maximum_cpu_usage = 20
maximum_memory_usage_mb = 128
```

These are conservative targets, not exact Android enforcement guarantees. CPU
limiting uses process priority (`nice 19`) plus duty cycling. Memory is monitored
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

## Workload

`hash_sum_v1`: for each independent chunk, sum
`int(sha256("<seed>:<index>")[:8], "big") mod 1000000007`, then sum chunk values
mod 1000000007. The monitor recomputes the expected value and verifies the
device's result. No arbitrary shell or Python execution is supported.

## Security notes

- The device token is stored in `~/.config/primal/config.json` with mode 600.
- Only SHA-256 token hashes are stored server-side.
- HTTPS is required (HTTP allowed only for localhost development).
- `primal` never downloads or executes remote programs.

## License

MIT

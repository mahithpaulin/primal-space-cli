# Primal Space

Compute node and desktop app for the Primal Space network. Runs on Android/Termux,
Windows, macOS and Linux, discovers real device resources, maintains an
authenticated heartbeat to the Primal Space Monitor, and executes only
allow-listed workloads when the user explicitly enables compute.

## Requirements

- Android ARM64 (tested on Realme RMX3430 / Android 13) with Termux and Python 3.9+
- Windows 10/11, macOS 12+ or Linux with Python 3.9+ (or the packaged binaries)
- Termux:API companion app and `termux-api` package for battery/Wi-Fi readings
  (optional; `/proc`, `/sys` and platform fallbacks are used when unavailable)
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

## Desktop app (Windows, macOS, Linux)

Download the standalone binary for your platform from the latest `desktop-v*`
GitHub release:

| Platform | Asset |
| --- | --- |
| Windows x64 | `primal-space-windows-x64.exe` |
| macOS Intel | `primal-space-macos-x64` |
| macOS Apple Silicon | `primal-space-macos-arm64` |
| Linux x64 | `primal-space-linux-x64` |

Each release ships a `.sha256` file next to the binary. The same binary works as
the CLI (`primal-space status`) and as the windowed app (`primal-space` with no
arguments).

macOS may quarantine an unsigned download; allow it once with:

```sh
xattr -d com.apple.quarantine ./primal-space-macos-arm64
chmod +x ./primal-space-macos-arm64
```

From source instead:

```sh
python -m pip install --no-build-isolation .
primal desktop
```

The desktop window manages the same daemons as the CLI and mirrors the Android
app (v1.11.0): connect/disconnect, worker start/stop/pause/resume, a dedicated
**Compute** tab (mode, lending plan, caps, settled numbers), job history, group
chats with a group creator, the Primal agent, and log tails. Settings are
stored per platform:

- Windows: `%APPDATA%\primal\config.json`
- macOS: `~/Library/Application Support/primal/config.json`
- Linux/Termux: `~/.config/primal/config.json`

Binaries are built by `.github/workflows/build-desktop.yml` (PyInstaller on
`ubuntu-latest`, `windows-latest`, `macos-15-intel` and `macos-15`). Tag
`desktop-v*` to publish a release. Tests run on every push via
`.github/workflows/test.yml`.

The monitor console links the installers at
<https://primalmonitor.vercel.app> (Apps section).

## Primal agent (ChatGPT)

The node can host an agent that answers chats using the owner's ChatGPT plan.
Sign-in uses OpenAI's **Sign in with ChatGPT** flow for open-source clients
(dynamic client registration, PKCE, `ext_agent_host_id`, no client secret).

> **Requirement:** ChatGPT plan usage for agents requires a paid ChatGPT plan.
> On a Free account the consent screen reports *"A required permission is
> unavailable"* and the token exchange returns `invalid_grant`. Upgrade the
> account (or use another paid account) and run `primal agent login` again; the
> issued client id is kept so sign-in resumes with the same registration.

```sh
primal agent login           # opens the browser, then stores the session
primal agent name "Ada"      # name the agent anything you like
primal agent models          # models available to the signed-in account
primal agent groups          # group chats this device belongs to
primal agent group "Crew" "Realme Narzo 50A"
primal agent start           # background agent daemon
primal agent status
```

The agent replies to direct messages and to group messages that mention its
name (`primal agent reply-all on` makes it answer every group message). It can
report node status and notify peers with the `node_status` and `send_message`
tools. Tokens are stored only on the device (`~/.local/state/primal/chatgpt_credentials.json`,
mode `0600`) and never sent to the monitor.

Desktop: open the **Agent** tab and press *Continue with ChatGPT*.
Android: open **Primal agent** and press *Continue with ChatGPT*.

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
| `primal desktop` | Open the desktop window (Windows, macOS, Linux) |
| `primal agent login\|logout\|status\|name\|model\|models` | ChatGPT sign-in and agent identity |
| `primal agent groups\|group` | List or create group chats |
| `primal agent run\|start\|stop` | Answer pending messages now or in the background |

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

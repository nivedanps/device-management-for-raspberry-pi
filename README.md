# SARAS device monitoring (pilot)

Every SchoolAI box reports its health to a small monitoring server once a minute. The server
stores the readings, decides whether each box is fine, and shows the whole fleet on a web page
that updates by itself. This folder has everything needed to run it, and the pilot runs on this
AGX Thor box today.

The rules that the parts follow are in [CONTRACT.md](CONTRACT.md). You only need that file if
you change the code.

## How it fits together

There are three parts:

| Part | Where it runs | What it does |
|---|---|---|
| **Agent**: `agent/saras_agent.py` | On every box, as a background service | Reads the box's health every 60 seconds and sends it to the server. If the server can't be reached, it keeps the readings and sends them later. |
| **Server**: `server/fleet_server.py` | On one machine (for the pilot, this box) | Receives the readings, saves them in one database file, raises and clears alerts, and serves the dashboard. |
| **Dashboard**: `server/static/` | In a web browser | Shows the list of boxes and a page for each box with live charts. Nothing to install. |

How the data moves:

```
agent on the box ──(every 60 s)──▶ server ──▶ fleet.db ──▶ dashboard in the browser (refreshes every 5 s)
```

The box always starts the connection. Nothing on the box waits for calls from outside, so it
works behind a school's firewall. Each reading the agent sends is called a *heartbeat*.

## What is stored, and what never is

**Stored on the monitoring server:**

- **Box details:** ID, serial number, model, type (AGX Thor, Orin Nano, Orin, DGX Spark, or Raspberry Pi 5),
  host name, product and site code. You can also fill in the customer, location, install date,
  support contact, group and notes.
- **Current status:** online, warning, critical or offline; the reasons; when it was last seen; uptime.
- **Health readings, one set per heartbeat:** CPU, GPU (NVML/tegrastats on Jetson/DGX, VideoCore VII on Raspberry Pi 5),
  temperatures, throttling states (undervoltage and thermal throttling flags on Pi 5), power, fan speed, memory,
  disk, Wi‑Fi signal and network delay.
- **Service checks:** whether the app, PostgreSQL, system services, Ollama/vLLM inference services, and Docker containers
  are up, how fast they answer, restart counts, and log-stop/activity states (running, idle, stopped, unknown).
- **Software versions:** Ubuntu/Debian/Raspberry Pi OS, kernel, L4T/Raspberry Pi firmware, NVIDIA driver, CUDA, Docker and others.
- **Alerts and events**, such as "went offline", "confirmed reboot", "unexpected restart", "clean shutdown", "disk low",
  "reconnected", "connection interrupted", "throttled (undervoltage/thermal)", or "restarted".
- **Backup/sync status from saras-sync:** the last success, the last attempt, pending changes, failed batches, conflicts,
  central sync API health, database health, and copied row counts.
- **AI usage:** on DGX Spark running vLLM (requests, tokens, queue, KV cache) or Raspberry Pi 5 running Ollama (active models,
  parameter size, VRAM, context length).
- **Dynamic remote configuration:** versioned configuration inheritance (DEFAULT -> ORG -> SCHOOL -> DEVICE) with agent acknowledgement.
- **Structured device logs:** sanitized centralized log collection across APPLICATION, API, SYNC, SYSTEM, AI, SECURITY, and UPDATE categories with sensitive data redaction.
- **A plain-text copy of every heartbeat**, one file per box per day, which you can open in any text editor.

**Never collected or stored:** names or IDs of students, teachers or parents; marks, attendance,
lesson plans or question papers; AI prompts and answers; logins, passwords or keys; uploaded
documents; unredacted secrets; Wi‑Fi network names; MAC addresses; USB
sticks and other removable media (their folder, `/media/<login name>/<volume name>`, would name
a person and their files, so the agent skips them).

**School data stays on the box**, in the app's own database. The agent never reads that
database. It only checks that PostgreSQL answers, without logging in. School data is protected
by the backup job (saras-sync), not by monitoring. Monitoring only records whether the backup ran.

**How much space it takes** (measured on this box, one heartbeat a minute): about 6.5 MB per
box per day. About 2.5 MB of that goes into `fleet.db` and about 4 MB is the plain-text copies.
Readings are kept for 30 days, so each box uses about 200 MB and 100 boxes use about 20 GB.
Alerts, logs, configs, and sync history are small and are managed by retention policies. Turning off the plain-text copies
(`"archive_dir": null`) saves about 60%.

## The tables

All the data is in one SQLite file, `fleet.db`. When the pilot moves to PostgreSQL or
TimescaleDB, the core eight tables and extension tables move across cleanly.

| Table | What is in it |
|---|---|
| `devices` | One row per box: its identity plus the details you fill in (customer, location, install date and so on). |
| `device_status` | One row per box, overwritten by each heartbeat: state, reasons, last seen, uptime and the latest heartbeat. |
| `metrics` | One row per heartbeat with the health readings. Kept 30 days. |
| `service_checks` | One row per service per heartbeat: up or down, response time, restart count. Kept 30 days. |
| `software_versions` | One row for each version of each component, with when it was first and last seen. |
| `events` | Alerts, reboots (confirmed, unexpected, agent restart), and other events. Kept one year. |
| `sync_reports` | Backup/sync status and extended sync diagnostics, written only when it changes. Kept one year. |
| `ai_usage` | vLLM and Ollama numbers per heartbeat. Kept 30 days. |
| `device_configs` | Dynamic remote configuration overrides (DEFAULT, ORG, SCHOOL, DEVICE) with versions. |
| `device_logs` | Structured, sanitized logs across 7 categories with automatic deduplication. Kept 30 days. |
| `update_jobs` | Remote update jobs (app, OS, firmware, config, health check) with execution lifecycle states. |
| `scheduled_updates` | Scheduled maintenance jobs with timezone support and maintenance window windows. |

## Run the demo on this box

Open two terminals in this folder (`device-management`). Start the server in one and the agent
in the other:

```
python3 server/fleet_server.py --config server/server.dev.json
python3 agent/saras_agent.py --config agent/agent.dev.json
```

Then open **http://127.0.0.1:8090/** in a browser. (If port 8090 is already taken, change
`port` in `server/server.dev.json` and the port in `server_url` in `agent/agent.dev.json`.)

What you'll see:

- The box appears within a few seconds. In the demo it reports every 10 seconds instead of 60,
  so the charts fill up quickly.
- The box shows as **Critical**, and that's correct. This box has no network time sync (the
  "Clock not synchronised" alert), and saras-sync last succeeded on 25 September (the
  "Backup/sync stale" alert).
- The page for this box says "Not collected: docker". The demo agent runs as your normal user,
  which can't read Docker. The installed agent runs as root and shows every container.

Things to try:

- **Stop the server** for a minute, then start it again. The agent keeps its readings meanwhile
  and sends them once the server is back, so the charts have no gap. The charts shade that
  minute, the readings table marks it "late", and the box's history shows "Connection
  interrupted", so even a short drop leaves a trace.
- **Stop the agent.** After about 1.5 minutes the box turns **Offline**. Start the agent again
  and the box shows as reconnected.

Press Ctrl+C in each terminal to stop them. The data stays in the `data/` folder: `fleet.db`,
`heartbeats/` and `agent-buffer.db`. It's still there when you restart; to start with no data,
delete `data/fleet.db`. The `data/` folder never holds school data, and git ignores it.

To see one heartbeat without sending it anywhere:

```
python3 agent/saras_agent.py --config agent/agent.dev.json --once
```

## Install it for real

Both parts have an install script that you run with `sudo`. Each script is safe to run again,
for example to update the code: it keeps your settings and your data.

### 1. The server, once, on the machine that collects the data

```
sudo server/install-server.sh
```

It creates a system user called `saras-fleet` and copies the server and dashboard to
`/opt/saras-fleet`. The first time only, it writes the settings file
`/etc/saras-fleet/server.json` with new random passwords (tokens) and a random dashboard
password for the user `fleet`. It keeps the data in `/var/lib/saras-fleet`, starts the service
`fleet-server`, and checks that the server answers. On that machine the dashboard is at
http://127.0.0.1:8090/; to see its password, run `sudo grep dashboard_ /etc/saras-fleet/server.json`.

The server won't start with the example passwords (`change-me`, `change-me-too`) because they are
public: if you set it up by hand from `server.example.json`, put real secrets in first
(`openssl rand -hex 32`).

### 2. The agent, on every box

```
sudo agent/install-agent.sh
```

Then edit `/etc/saras-agent/agent.json`:

- `server_url`: `http://127.0.0.1:8090` if the server is on the same box, or your nginx
  address (see below), for example `https://fleet.example.org`. Plain `http://` to another
  machine is refused, because the token would travel unencrypted; on a network you trust you
  can allow it with `"allow_insecure_http": true`.
- `token`: the `fleet_token` from the server. To see it, run
  `sudo grep fleet_token /etc/saras-fleet/server.json` on the server.
- `claimed.site_code`, and `product` if it isn't SchoolAI in a Box.

Then run `sudo systemctl restart saras-agent`. To watch it, run `journalctl -u saras-agent -f`.
`systemctl status saras-agent` shows the last heartbeat and how many readings are waiting to be
sent.

**Why the agent runs as root:** so the Docker details light up. It can then see every
container's state, restart count and start time, and the version of the Docker engine that's
running. On a DGX Spark it can also read the serial number. The NVMe temperature already works
without root. NVMe wear and health would also need root, but the agent doesn't collect them yet.

The service is locked down. It runs as root but keeps only one privilege: raw network access,
for the ping to the router. Every folder except its own state folder is read-only to it, it may
use only the system calls ordinary services use, and it gets at most 10% of the CPU and 200 MB
of memory. It only reads and never changes anything on the box. These limits were checked as a
normal user; before rolling them out to every box, check one installed box once: its page should
list the Docker containers and show no new "Not collected" notes.

### When boxes at other sites report to one server

Keep the server listening on `127.0.0.1` and put **nginx with HTTPS (TLS)** in front of it.
Open only port 443 in the firewall and never port 8090. Here is a minimal nginx site:

```nginx
server {
    listen 443 ssl;
    server_name fleet.example.org;                    # your server's name
    ssl_certificate     /etc/ssl/fleet/fullchain.pem; # a real certificate: the agent checks it
    ssl_certificate_key /etc/ssl/fleet/privkey.pem;
    client_max_body_size 300k;                        # a heartbeat is at most 256 KB
    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $host;
    }
}
```

In `/etc/saras-fleet/server.json`, add the nginx name to `allowed_hosts`, for example
`"allowed_hosts": ["fleet.example.org"]`. The dashboard answers only IP addresses, `localhost` and
the names listed there, so a web page can't reach it by pointing a name of its own at the server
(DNS rebinding). Keep `dashboard_user` and `dashboard_password` set (the installer sets them), so
the dashboard asks for a password; the boxes don't need it. Then run
`sudo systemctl restart fleet-server`.

## Settings

### Agent: `/etc/saras-agent/agent.json` (demo: `agent/agent.dev.json`)

| Key | Default | What it means |
|---|---|---|
| `server_url` | `http://127.0.0.1:8090` | Where the server is: `https://`, or `http://` only to this box. |
| `allow_insecure_http` | `false` | `true` allows plain `http://` to another machine. The token then travels unencrypted, so use it only on a network you trust. |
| `token` | `change-me` | The server's `fleet_token`, or this box's own token. |
| `device_id` | `null` | Leave it empty: the serial number is used (or the host name if there's no serial). |
| `interval_s` | `60` | Seconds between heartbeats, from 5 to 3600. The demo uses 10. |
| `software_every_s` | `600` | How often (in seconds) to send the software versions again. |
| `request_timeout_s` | `10` | How long to wait for the server on each send. |
| `claimed` | product, site code, customer | What this box says it is. Only fills details that are still empty on the server. |
| `services.app_live_url`, `services.app_ready_url` | `http://127.0.0.1:8000/health/live`, `.../health/ready` | The app's health checks. The agent only reads them. |
| `services.postgres` | `127.0.0.1:5426` | PostgreSQL check. The agent connects and hangs up without logging in. |
| `services.vllm_url` | `null` | On a DGX Spark, vLLM's address (for example `http://127.0.0.1:8000/v1`). |
| `services.ollama_url` | `null` | On a Raspberry Pi 5 or edge box running Ollama, Ollama's address (for example `http://127.0.0.1:11434`). Reports loaded models, parameter sizes, and VRAM. |
| `systemd_units` | nginx, docker, saras-sync, jtop | The system services to report on. |
| `sync_state_dir` | `/opt/saras-sync/state` | Where saras-sync keeps its status files. |
| `ping_gateway` | `true` | Ping the router 3 times per heartbeat to measure the network delay. |
| `docker` | `true` | Report the Docker containers (needs root). |
| `buffer_path` | `/var/lib/saras-agent/buffer.db` | Where unsent readings wait while the server can't be reached. |
| `buffer_max_rows` | `20000` | Most readings kept while waiting, about two weeks at one a minute. The oldest are dropped first. |

### Server: `/etc/saras-fleet/server.json` (demo: `server/server.dev.json`)

| Key | Default | What it means |
|---|---|---|
| `bind` | `127.0.0.1` | Leave it. For access from outside, use nginx (see above). |
| `port` | `8090` | |
| `db_path` | `./fleet.db` | The database file. A relative path starts from the settings file's folder. The installer uses `/var/lib/saras-fleet/fleet.db`. |
| `fleet_token` | `change-me` | The shared password that every box sends. The server won't start with the example value. |
| `device_tokens` | `{}` | Optional per-box passwords, `{"<device id>": "<token>"}`. A box listed here must use its own. |
| `admin_token` | `change-me-too` | Needed to edit a box's details (see below). The server won't start with the example value. |
| `dashboard_user`, `dashboard_password` | `null` | Set both to put a password on the dashboard (the installer does). |
| `allowed_hosts` | `[]` | Extra names the dashboard answers to, such as the nginx name. IP addresses and `localhost` always work. |
| `retention_days` | `30` | Readings, logs, and plain-text copies older than this are deleted every hour. Alerts and sync history are kept for a year. |
| `offline_after_missed` | `3` | A box counts as offline after missing this many heartbeats... |
| `offline_min_s` | `90` | ...but never after less than this many seconds. |
| `archive_dir` | `null` | Folder for the plain-text copy of every heartbeat. `null` turns the copies off. The demo uses `data/heartbeats`. |

### Editing a box's details

In the pilot the dashboard only shows data; you can't edit it there. To fill in the customer,
location, install date, support contact, group or notes, send them to the server with the
admin token:

```
curl -X PATCH -H "Authorization: Bearer <admin_token>" -H "Content-Type: application/json" \
     -d '{"customer": "Example Public School", "installed_on": "2026-09-20", "support_contact": "support@example.org"}' \
     http://127.0.0.1:8090/api/v1/devices/<device id>
```

The other editable fields are `product`, `site_code`, `location`, `latitude`, `longitude`,
`device_group` and `notes`. The demo's admin token is `dev-admin-token`. This request needs only
the admin token, also when the dashboard has a password: nginx refuses a request that carries
two `Authorization` headers.

## Alerts

| Alert | Level | When it opens (it closes when the problem goes away) |
|---|---|---|
| Offline | critical | No heartbeat for 3 intervals, and at least 90 s |
| Service down | critical | The app, PostgreSQL, a system service or a container is down (it also closes when the box stops checking that service, for example a removed container) |
| GPU overheating | critical | GPU at 105 °C or more (Jetson Thor / DGX / Orin Nano) |
| CPU overheating | critical | CPU at 80 °C or more on Raspberry Pi 5 |
| Throttled (undervoltage) | critical | `THROTTLED_UNDERVOLTAGE` — Raspberry Pi 5 undervoltage detected (active bit 0x1) |
| Throttled (thermal) | warning | `THROTTLED_THERMAL` — Raspberry Pi 5 frequency capped / thermal throttling (active bit 0x4) |
| Disk almost full | critical | Any disk at 95% or more |
| Backup/sync stale | critical | No successful saras-sync run for more than 8 hours (it also closes if the box no longer has saras-sync) |
| Backup copy incomplete | critical | The last sync's row counts on the server differ from the box's in one or more tables |
| Central API unreachable | warning | `CENTRAL_API_UNREACHABLE` — saras-sync reported central API health is degraded/unreachable |
| Database unhealthy | critical | `DATABASE_UNHEALTHY` — local database check failed or marked degraded in sync status |
| Sync failed | critical | `SYNC_FAILED` — sync agent recorded batch failure or failed sync state |
| Sync conflicts | warning | `SYNC_CONFLICTS` — unresolved record conflicts during synchronization |
| GPU hot, board hot | warning | 95 °C or more (Jetson Thor / DGX / Orin Nano) |
| CPU hot | warning | CPU at 75 °C or more on Raspberry Pi 5 |
| Disk low | warning | Any disk at 85% or more |
| High RAM, high CPU | warning | 90% or more for 3 heartbeats in a row |
| High GPU use | warning | 90% or more for 3 heartbeats in a row; not on a box serving an AI model, and skipped on Raspberry Pi 5 |
| Clock not synchronised | warning | No network time sync, or the clock is more than 2 minutes off |
| Poor network | warning | 20% or more of the pings to the router are lost, or the Wi‑Fi signal is −75 dBm or weaker |
| AI server overloaded | warning | On DGX Spark: requests waiting in vLLM's queue, or its KV cache 90% full, for 3 heartbeats in a row |

These are recorded too, but don't count as problems: a service restarted, a box restarted (`CONFIRMED_REBOOT`, `RESTART_UNEXPECTED`, `AGENT_RESTARTED`), a
box reconnected, a new box registered, a software version changed, and a connection
interrupted (readings arrived late from the box's buffer, even for a drop too short to count as
offline).

A box's status is **offline** if it's offline. Otherwise it's **critical** if any critical
alert is open, **warning** if any warning is open, and **online** if nothing is open.

## Raspberry Pi 5 integration & setup

Raspberry Pi 5 is a first-class supported device type alongside NVIDIA Jetson Thor, DGX Spark, and Orin Nano.

### Required packages & system utilities
On Raspberry Pi OS (Debian Bookworm):
```bash
sudo apt update
sudo apt install -y python3 python3-psutil libraspberrypi-bin
```
- `libraspberrypi-bin`: Provides `vcgencmd` to read CPU temperature and throttling bitmasks. If absent, the agent automatically falls back to `/sys/class/thermal/thermal_zone0/temp` and marks throttling flags as unavailable without crashing.
- `python3-psutil`: Standard Linux system metrics (CPU per-core, RAM, disk mounts).

### Hardware detection & telemetry
- **Platform detection:** Reads `/proc/device-tree/model` and `/proc/device-tree/compatible`. Identifies `Raspberry Pi 5` or `bcm2712`.
- **GPU architecture:** Identifies `VideoCore VII` GPU. Suppresses calls to `nvml` or `tegrastats`. Unsupported GPU alerts are never raised.
- **Throttling bitmask:** Decodes `vcgencmd get_throttled` bitmask:
  - `0x1`: Undervoltage detected currently (`THROTTLED_UNDERVOLTAGE` critical alert).
  - `0x2`: Arm frequency capped currently.
  - `0x4`: Currently throttled (`THROTTLED_THERMAL` warning alert).
  - `0x8`: Soft temperature limit active.
  - `0x10000`, `0x20000`, `0x40000`, `0x80000`: Historical flags for past throttling events since boot.
- **Boot & reboot tracking:** Tracks kernel `/proc/sys/kernel/random/boot_id` and uptime resets to accurately classify reboots (`CONFIRMED_REBOOT`, `RESTART_UNEXPECTED`, `AGENT_RESTARTED`) without duplicate counts on buffered replay.

### Dynamic device configuration
Hierarchical configuration inheritance:
`DEFAULT` ➔ `ORGANIZATION` ➔ `SCHOOL` ➔ `DEVICE`

Configurations are validated against strict ranges (`interval_s`: 5–3600, `log_level`: DEBUG/INFO/WARNING/ERROR/CRITICAL) and atomically cached to `last_known_config.json`.
- Set configuration via API:
```bash
curl -X POST -H "Authorization: Bearer <admin_token>" -H "Content-Type: application/json" \
     -d '{"target_type": "device", "target_id": "rpi5-device-01", "config": {"interval_s": 30, "log_level": "INFO"}}' \
     http://127.0.0.1:8090/api/v1/config
```
- Retrieve effective config:
```bash
curl -H "Authorization: Bearer <fleet_token>" http://127.0.0.1:8090/api/v1/devices/rpi5-device-01/config
```

### Centralized structured logging
Structured log records are shipped via `POST /api/v1/logs` across 7 categories:
`APPLICATION`, `API`, `SYNC`, `SYSTEM`, `AI`, `SECURITY`, and `UPDATE`.
- Sensitive credentials, API tokens, passwords, and private keys are automatically redacted using `[REDACTED]`.
- Deduplication via `event_id` prevents duplicate log rows during replay.
- Retention cleanup purges logs older than `retention_days` (default 30 days).

### Inactivity & log-stop detection
Services are evaluated on both health status and log activity:
- `running`: Service is healthy and active.
- `idle`: Service is healthy, listening, but no recent logs written (normal idle behavior).
- `stopped`: Service process or systemd unit is inactive.
- `unknown`: Agent is offline or health check could not be completed.

### Ollama AI workload monitoring
When Ollama is running on Raspberry Pi 5 (`services.ollama_url: "http://127.0.0.1:11434"`):
- Polls `/api/version` and `/api/ps`.
- Reports active loaded models, parameter sizes, quantization levels, VRAM utilization, and model expiration.
- If Ollama is not installed or offline, metrics report gracefully as unavailable (`N/A`) without raising false alarms.

### Troubleshooting Raspberry Pi 5
1. **Undervoltage Alert (`THROTTLED_UNDERVOLTAGE`)**:
   - Cause: Insufficient power supply voltage or high-resistance cable.
   - Solution: Use the official Raspberry Pi 27W USB-C (5.1V / 5A) power supply. Avoid standard 5V phone chargers.
2. **Thermal Throttling Alert (`THROTTLED_THERMAL`) or CPU Overheating**:
   - Cause: Raspberry Pi 5 CPU temperature exceeded 75°C (warning) or 80°C (critical).
   - Solution: Check that the official Raspberry Pi 5 Active Cooler or heatsink is properly mounted and the fan connector is securely seated.
3. **Missing `vcgencmd`**:
   - Cause: Minimal OS installation without Raspberry Pi firmware userland tools.
   - Solution: Install `libraspberrypi-bin`. The agent will operate using sysfs fallback in the interim.
4. **Clock Not Synchronised (`CLOCK_UNSYNCED`)**:
   - Cause: Raspberry Pi has no hardware RTC battery installed by default or NTP is blocked.
   - Solution: Configure `systemd-timesyncd` or an edge-accessible local NTP server, or install an official RTC rechargeable coin-cell battery.

## Where each section of the PDF stands

This maps *Device Management Parameter.pdf* (14 sections) to the pilot:

- **Collected now:** works today, as the normal user in the demo.
- **Needs root:** appears once the agent is installed as root.
- **Needs an app change:** the app (or saras-sync) has to record or publish it first.
- **Later:** not built in the pilot.

| # | PDF section | Collected now | Needs root | Needs an app change | Later |
|---|---|---|---|---|---|
| 1 | Device identity & inventory | Device ID, serial number, device type, model, host name. Product and site come from the agent's settings. Customer, install date and support contact are typed in by an admin. | Serial number on a DGX Spark (without root the box's page says it wasn't collected) | — | Editing details in the dashboard. Orin Nano and DGX Spark are untested. |
| 2 | Heartbeat & online monitoring | Last seen, last reboot, uptime, network delay and loss, Wi‑Fi signal, automatic online/warning/critical/offline status, missed-heartbeat detection, alerts on the dashboard, recovery (reconnected, delayed readings delivered), short connection drops (late readings shaded on the charts and a "Connection interrupted" event) | — | — | Per-box keys kept in the TPM, with mutual TLS (today a shared `fleet_token`, or a per-box token through `device_tokens`, over HTTPS through nginx); changing intervals and limits remotely; email, Teams or SMS alerts |
| 3 | Hardware health | CPU (total, per core, temperature, frequency); GPU (use, temperature, clock, power); memory; power mode, total power and each power rail; fan; every temperature sensor | — | — | Throttling status (Thor's driver doesn't report it, so it has to be worked out from other readings). GPU memory is shared with the RAM on all three devices, so watch RAM % instead. |
| 4 | Storage | Size, used and free space and % full for every disk of the box (USB sticks are skipped); low-storage alerts at 85% and 95%; size of the system journal and of all logs in `/var/log` | — | — | A warning when a disk will fill up within a week; size of Docker images and volumes (needs root), the model folder (DGX Spark) and recordings (Proctor) |
| 5 | Application & service health | App checks (live and ready), PostgreSQL, the nginx, docker, saras-sync and jtop services, vLLM on a DGX Spark; response time, restart count, when each service last started, availability and failed checks over 24 h; saras-sync status | Docker containers: state and restart count | The app's ready check only tests its database, not Azure Blob storage or the cloud AI provider. Proctoring and Assessment in a Box services need health checks of their own. | Collecting logs and crash logs |
| 6 | Location tracking (PDF: no) | Location, latitude and longitude, typed in by an admin | — | — | Map view. GPS isn't possible: the boxes have no GPS receiver. |
| 7 | Software & version tracking | Ubuntu, kernel, L4T, JetPack, NVIDIA driver, CUDA driver, Docker, container toolkit, jetson-stats, bootloader, Python; the vLLM version and model on a DGX Spark; every change is recorded | Version of the running Docker engine (without root, the installed package's version is shown) | The app's version (the app doesn't publish one yet) | cuDNN and TensorRT, which live inside the AI containers, not on the box. |
| 8 | OTA updates | — | — | — | All of it. The server's reply already has an empty `commands` list ready for this. |
| 9 | Synchronization | Last success, last attempt, runs that found the server unreachable, the last run's result, hours since the last success; alert after 8 hours; rows and tables the last sync copied, with an alert when the server's copy doesn't match the box | — | saras-sync has to report failures (today a failed run is recorded as a success) | Pending queue, retries and sync errors (they are kept on the central sync server) |
| 10 | Alerts & notifications | On the dashboard: offline, service down, GPU overheating, disk full or low, sync stale, high RAM, high CPU, high GPU use, high GPU or board temperature, poor network, backup copy incomplete, AI server overloaded (DGX Spark), service restarted, box restarted, reconnected, connection interrupted, new box, version changed | — | — | Sending alerts by email, Teams or SMS; early signs of hardware failure; "update succeeded" and "settings changed" events |
| 11 | AI workload | On a DGX Spark with vLLM: requests served and aborted (inference errors), tokens, queue, how full the model's working memory (KV cache) is, average response time, time to the first word of an answer, model, availability, an alert when it is overloaded (tested only against a stand-in for vLLM). GPU use on every box. | — | On the Thor, the AI runs in the cloud through the app, so active users, requests, response times and errors have to come from the app | Model load time. For cloud AI, the queue and load time can't be seen at all. |
| 12 | Camera & proctoring | — | — | Proctoring services have to report stream status, frame rate, dropped frames and recording status | Camera count and stream checks on Proctor in a Box (this unit has no cameras) |
| 13 | Remote diagnostics | Live status of services, hardware and network, on the dashboard | — | — | Viewing logs, diagnostic bundles, remote health checks, restarting services or the box, and a record of who did what |
| 14 | Central dashboard | Fleet counts; a list you can filter by status, type and product and search by ID, host name, site, customer, location, group or alert; a page per box with charts from 1 hour to 7 days, services, sync, readings, versions, alerts and history | — | Filtering by app version (needs the app's version) | Map, groups view, filtering by software version, update history, editing details, camera status |

## Next steps

1. **Move to the team's own stack.** Port the server to .NET 8 and the storage to PostgreSQL
   with TimescaleDB; the eight tables move across one for one, and readings can then be kept
   as hourly averages for a year. The dashboard can move to React. The agent stays in Python.
   The heartbeat format in CONTRACT.md and the two test files are the checklist for the port.
2. **Secure it for many schools:** HTTPS with a separate key for each box, and a time source
   that school networks allow (NTP is blocked on this box's network).
3. **Send alerts** by email, Teams or SMS when a box goes critical or offline.
4. **Remote commands** through the `commands` list in the server's reply. Only actions on a
   fixed list: restart a service, collect logs, run a health check, back up now, reboot. Every
   action is recorded.
5. **Updates over the air (OTA):** pull app containers from a private registry, check them,
   start them, and roll back if the health check fails. No updates during exams.
6. **Backups:** make saras-sync fail loudly, encrypt and lock down its database dumps, store
   them in Azure Blob with a retention policy, take a dump before every update, and show the
   backup's age on the dashboard.
7. **App changes:** publish a version number, add a health check that covers PostgreSQL, Azure
   Blob and the AI provider, and record AI response times and errors.
8. **Check other hardware:** borrow an Orin Nano and a DGX Spark for a day to confirm their readings.

## Tests and files

```
python3 -m unittest -v tests/test_server.py        # core fleet server test suite (125 tests, ~12 s)
python3 -m unittest -v tests/test_rpi5.py          # Raspberry Pi 5 unit, integration, and regression suite (34 tests, ~2 s)
python3 -m unittest -v tests/test_features.py      # Extended 8-feature management test suite (25 tests, ~2 s)
python3 -m unittest -v tests/test_end_to_end.py    # hardware-dependent live Jetson end-to-end integration test
```

The server test covers all core endpoints, schema validation, retention, alerts, and API security (125 tests passing).
`test_rpi5.py` tests all 9 Raspberry Pi 5 requirements plus core regression fixes (34 tests passing).
`test_features.py` verifies all eight extended device management feature areas (25 tests passing):
1. **Application Version**: Discovery via URL, metadata files, and container inspection; validation and server-side persistence in `software_versions`.
2. **Firmware Updates**: Hardware platform capability detection, undervoltage protection preflight, and pilot simulation mode.
3. **Operating System Updates**: OS package manager capability detection, apt lock verification, and preflight dry-run simulation.
4. **Application & Docker Updates**: Disk space preflight requirements, container inspection, simulation, and post-update health verification.
5. **Configuration Updates**: Authorized REST API with 4-tier hierarchy (`DEFAULT -> ORG -> SCHOOL -> DEVICE`), deterministic SHA-256 revision hash computation, and dashboard JSON editor.
6. **Remote Update Deployment**: Job lifecycle states (`scheduled`, `dispatched`, `in_progress`, `completed`, `failed`, `cancelled`), delivery via heartbeat `commands: [...]`, execution tracking, and agent-side idempotency.
7. **Scheduled Updates**: Scheduled dispatch with timezone handling and UTC maintenance windows (`HH:MM-HH:MM`).
8. **Hardware Failure Detection**: NVMe SMART alerts (`STORAGE_CRITICAL`, `STORAGE_WEAR_HIGH`), cooling fan stall detection (`FAN_STALLED`), and memory ECC error detection (`MEMORY_ECC_ERROR`).

*Note on `test_end_to_end.py`:* This test is a hardware-dependent live integration test that probes physical Tegra/Jetson power rails and GPU sensors. On non-NVIDIA developer hosts (such as macOS or generic Linux), it expects real Tegra telemetry and will time out; it must be run on physical NVIDIA hardware.

| File | What it is |
|---|---|
| `agent/saras_agent.py` | The agent (supports Jetson Thor, DGX Spark, Orin Nano, and Raspberry Pi 5 with revision caching, log redaction, app version discovery, and update job execution) |
| `agent/agent.example.json`, `agent/agent.dev.json` | Agent settings: for real use, and for the demo (includes Ollama) |
| `agent/install-agent.sh`, `agent/saras-agent.service` | The agent's installer and its service definition |
| `server/fleet_server.py`, `server/schema.sql` | The server and database tables (with deterministic revision hashing, validation, log redaction, update jobs, and scheduling) |
| `server/server.example.json`, `server/server.dev.json` | Server settings: for real use, and for the demo |
| `server/install-server.sh`, `server/fleet-server.service` | The server's installer and its service definition |
| `server/static/` | The dashboard (with app version display, firmware/OS update statuses, update job dispatch, schedules, config editor, and hardware diagnostics) |
| `tests/test_server.py` | Core fleet server test suite (125 tests) |
| `tests/test_rpi5.py` | Raspberry Pi 5 test suite covering requirements 1–9, revision hashing, validation, and redaction (34 tests) |
| `tests/test_features.py` | Extended 8-feature test suite covering application versions, firmware, OS, container updates, config editor, jobs, schedules, and hardware failure alerts (25 tests) |
| `tests/test_end_to_end.py` | Jetson Tegra end-to-end integration test (requires physical NVIDIA hardware) |
| `data/` | Demo data (not school data; git ignores it) |
| `CONTRACT.md` | The detailed rules and API specifications |

## Physical Raspberry Pi 5 Validation Checklist

When deploying to a physical Raspberry Pi 5 unit, perform the following validation steps:
- [ ] **Power Supply**: Connect official Raspberry Pi 27W USB-C PD power supply to ensure sufficient current for CPU/GPU boost.
- [ ] **Hardware Detection**: Run `cat /proc/device-tree/model` and verify it contains `Raspberry Pi 5`.
- [ ] **Firmware Utilities**: Ensure `vcgencmd` is installed (`libraspberrypi-bin` or `raspberrypi-utils`) and accessible to the `saras-agent` service.
- [ ] **Thermal / Cooling**: Monitor `vcgencmd measure_temp` under stress (`stress-ng --cpu 4`). Verify active cooler activates and temperature stays below warning threshold (75°C).
- [ ] **Undervoltage Stress**: Test under intentional high load and check that `vcgencmd get_throttled` bit 0 raises `UNDERVOLTAGE` alert when voltage dips below 4.63V.
- [ ] **Hard Power Cycle / Reboot**: Perform sudden power disconnection and cold reboot; verify the fleet server logs a `CONFIRMED_REBOOT` without duplicate event counting.
- [ ] **Dynamic Configuration Sync**: Push an updated `interval_s` and `log_level` via `POST /api/v1/config`; verify agent logs reflect the new revision without restarting the process.
- [ ] **Offline Buffer Persistence**: Disconnect network cable for 5 minutes, reconnect, and verify buffered readings and logs are replayed with original timestamps preserved.

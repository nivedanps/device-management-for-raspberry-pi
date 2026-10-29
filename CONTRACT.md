# Device Management Pilot — Contract

This file is the single source of truth for the pilot. The agent, the server and the
dashboard are built separately against it. If code and this file disagree, the code is wrong.

Scope: live monitoring of everything the box can report **today** (the "Ready" items of the
R&D feasibility study), stored centrally, with alerts and a live dashboard. Remote commands,
OTA updates and backups are out of scope for the pilot (the response already carries an empty
`commands` list so they can be added later without changing the protocol).

## 1. Layout and ownership

```
device-management/
  CONTRACT.md                 this file
  README.md                   how to run, what is collected, what needs root
  agent/                      runs on every box
    saras_agent.py            collectors + sender + offline buffer (one file is fine; helpers allowed)
    agent.example.json        config with production defaults
    agent.dev.json            config for a local demo (server on 127.0.0.1:8090, interval 10 s)
    saras-agent.service       systemd unit (root, hardened)
    install-agent.sh          sudo installer (idempotent; not run by the build)
  server/                     runs centrally (for the pilot: on this box)
    fleet_server.py           HTTP API + storage + alert engine + retention + static files
    schema.sql                SQLite DDL (exactly section 5)
    server.example.json       config
    server.dev.json           config for a local demo (127.0.0.1:8090, ./fleet.db)
    fleet-server.service      systemd unit
    static/
      index.html              dashboard
      app.js
      style.css
  tests/
    test_server.py            unit/API tests for the server (stdlib unittest)
    test_end_to_end.py        server + real agent together
```

## 2. Runtime constraints

- Python 3.12, **standard library only**, plus `psutil` and `pynvml` (both already installed).
  Do not pip-install anything. `jtop` (jetson-stats) may be imported opportunistically on Jetson Orin.
- No sudo during build or test. The agent must run as a normal user and degrade gracefully:
  anything that needs root is reported in `collector_errors`, never crashes the agent.
- The production app runs on port 8000 and PostgreSQL on 5426. Only send read-only GETs / TCP probes
  to them. Never restart, stop or reconfigure anything outside `device-management/`.
- Demo/test ports: 8090–8099. Stop every process you start before you finish.
- Every subprocess call has a timeout (≤ 3 s) and every collector is wrapped so one failure
  cannot stop the heartbeat.
- All timestamps sent over the wire and stored are UTC ISO-8601 with milliseconds and a `Z`
  suffix, e.g. `2026-09-29T07:40:00.123Z`. The only exceptions are the raw saras-sync values,
  passed through as read from the files.

## 3. Heartbeat: agent → server

`POST /api/v1/heartbeat` with header `Authorization: Bearer <token>` and a JSON body.
Body size limit 256 KB. Any field inside an object may be `null` when it can't be read.

```json
{
  "schema": "saras.heartbeat/1",
  "agent_version": "0.1.0",
  "device_id": "1424225065010",
  "boot_id": "f7f33032-e7d7-4a4c-9ac6-d6f67394bc30",
  "seq": 42,
  "sent_at": "2026-09-29T07:40:00.123Z",
  "interval_s": 60,
  "buffered": false,
  "claimed": { "product": "SchoolAI in a Box", "site_code": "eps_koorgalli", "customer": null },
  "identity": {
    "serial": "1424225065010",
    "model": "NVIDIA Jetson AGX Thor Developer Kit",
    "device_type": "AGX Thor",
    "hostname": "sarasschoolai",
    "lan_ip": "10.10.7.125"
  },
  "system": {
    "booted_at": "2026-09-29T04:38:36.000Z",
    "uptime_s": 12345,
    "ntp_synced": false,
    "cpu_pct": 5.3,
    "cpu_per_core_pct": [12.9, 1.0, 11.0],
    "cpu_freq_mhz": 2601,
    "load_1m": 0.8, "load_5m": 0.7, "load_15m": 0.6,
    "ram_total_mb": 125749, "ram_used_mb": 9700, "ram_pct": 7.7, "swap_pct": 0.0,
    "disks": [ { "mount": "/", "total_gb": 936.0, "used_gb": 49.1, "free_gb": 839.0, "pct": 5.5 } ],
    "journal_mb": 395.1,
    "var_log_mb": 486.7
  },
  "gpu": {
    "source": "nvml",
    "name": "NVIDIA Thor",
    "util_pct": 0, "temp_c": 41, "clock_mhz": 315, "max_clock_mhz": 1575, "power_w": 2.77,
    "memory_note": "unified"
  },
  "thermal_c": { "tj": 40.9, "gpu": 40.9, "cpu": 40.4, "soc012": 40.8, "soc345": 40.3, "nvme": 39.9 },
  "power": {
    "mode": "MAXN",
    "total_w": 26.47,
    "rails_w": { "VDD_GPU": 2.77, "VDD_CPU_SOC_MSS": 9.1, "VIN_SYS_5V0": 6.8 },
    "fan_rpm": 1833
  },
  "network": {
    "iface": "wlP1p1s0", "link": "up",
    "wifi_signal_dbm": -66, "wifi_bitrate_mbps": 573.5,
    "gateway_rtt_ms": 5.5, "gateway_loss_pct": 0.0,
    "last_server_rtt_ms": 12.3
  },
  "services": [
    { "name": "app-live",  "kind": "http", "status": "up", "latency_ms": 2.1, "detail": "HTTP 200 Healthy", "restarts": null, "since": null },
    { "name": "app-ready", "kind": "http", "status": "up", "latency_ms": 1.5, "detail": "HTTP 200 Healthy", "restarts": null, "since": null },
    { "name": "postgres",  "kind": "tcp",  "status": "up", "latency_ms": 0.8, "detail": "ssl=off", "restarts": null, "since": null },
    { "name": "vllm",      "kind": "http", "status": "not_configured", "latency_ms": null, "detail": null, "restarts": null, "since": null },
    { "name": "systemd:nginx.service", "kind": "systemd", "status": "up", "latency_ms": null, "detail": "active/running", "restarts": 0, "since": "2026-09-29T04:39:10.000Z" },
    { "name": "container:rapiddev-app", "kind": "container", "status": "up", "latency_ms": null, "detail": "running", "restarts": 0, "since": "2026-09-29T06:33:00.000Z" }
  ],
  "sync": {
    "last_success": "2026-09-25T16:30:08+05:30",
    "last_attempt": "2026-09-29T10:27:09+05:30",
    "offline_count": 1,
    "unit_result": "success",
    "age_h": 90.2,
    "source_tables": 193, "source_rows": 1620892, "target_rows": 1620892, "mismatched_tables": 0
  },
  "software": {
    "os": "Ubuntu 24.04.4 LTS", "kernel": "6.8.12-1021-tegra", "l4t": "R39.2.1", "jetpack": null,
    "nvidia_driver": "595.78", "cuda_driver": "13.2", "docker": "29.1.3",
    "nvidia_container_toolkit": "1.19.1", "jetson_stats": "7.2.1", "bootloader": "39.2.1",
    "python": "3.12.3", "app_version": null, "vllm_version": null, "llm_model": null
  },
  "ai": {
    "vllm": {
      "status": "up", "model": "Qwen/Qwen3.5-35B", "running": 1, "waiting": 0, "kv_cache_pct": 23.5,
      "prompt_tokens_total": 12345, "generation_tokens_total": 6789, "requests_success_total": 321,
      "requests_aborted_total": 12, "avg_e2e_latency_s": 4.2, "avg_ttft_s": 0.35
    }
  },
  "collector_errors": { "docker": "permission denied: run the agent as root or add it to the docker group" }
}
```

Field rules:
- `device_id`: config value, else the serial number, else the hostname. When the box has a serial
  number the agent can't read (DMI is root-only, e.g. a DGX Spark agent not running as root),
  `collector_errors.serial` says so.
- `seq` restarts at 1 when the agent restarts. `boot_id` comes from `/proc/sys/kernel/random/boot_id`.
  `(boot_id, seq, sent_at)` identifies one heartbeat; the server uses it to store a heartbeat
  delivered twice only once (section 5).
- `sent_at` is the **device clock** and is not trusted; the server's receive time is authoritative.
- `buffered` is `true` when the heartbeat is replayed from the offline buffer.
- `identity.device_type` is one of `"AGX Thor"`, `"Orin Nano"`, `"Orin"`, `"DGX Spark"`, `"Raspberry Pi 5"`, `"Unknown"`.
- `system.cpu_pct` is measured between two heartbeats (psutil, non-blocking), not with a sleep.
- `system.disks`: the box's own filesystems, `/` first. Removable media and desktop automounts
  (under `/media` or `/run/media`, mounted through udisks2, or on a device whose sysfs `removable`
  is 1) are never reported: their mount point names the logged-in user and the volume label.
  `free_gb` is the space a normal user can still write (less than total minus used).
- `system.var_log_mb`: disk space taken by the files in `/var/log` (allocated blocks, the journal
  included), counting only what the agent may read; null with a `collector_errors.var_log`
  reason above 20,000 files or 1 s. Only sizes are read, never names or contents.
- `gpu.source` is `"nvml"` (Thor, DGX Spark), `"jtop"` or `"sysfs"` (Orin), `"bcm2712"` (Raspberry Pi 5 VideoCore VII), or `null`.
- `services[].status` is one of `up`, `down`, `degraded`, `unknown`, `not_configured`.
  Service names: `app-live`, `app-ready`, `postgres`, `vllm`, `ollama`, `systemd:<unit>`, `container:<name>`.
  `detail` is at most 200 characters.
- `sync` is `null` when `/opt/saras-sync/state` doesn't exist. `age_h` = device-clock now minus
  `last_success` (both on the device clock, so the offset cancels out). The four counts come from
  saras-sync's `source_counts.json` (the box) and `target_counts.json` (the server, after the
  restore) of its last run that reached the server: `source_tables`, `source_rows`, `target_rows`
  and `mismatched_tables` (tables whose row counts differ, as in saras-sync's `sync_audit`). Only
  totals are sent, never table names. `mismatched_tables` is null unless `target_counts.json` is at
  least as new as `source_counts.json` (a run that stopped halfway is not a mismatch). Extended sync metrics
  include `pending_changes`, `failed_batches`, `conflicts`, `sync_api_health`, and `db_health`.
- `software` is sent on the first heartbeat after start and then every `software_every_s`
  (default 600 s); otherwise `null`.
- `ai` is `null` unless `services.vllm_url` or `services.ollama_url` is configured.
  vLLM metrics: averages computed from Prometheus difference between two heartbeats.
  Ollama metrics: model and running state reported via lightweight device-side `/api/version` and `/api/ps` probe.
- `collector_errors` maps collector name → short reason. Empty object when all is well.

**Never collected** (privacy): anything about users or school data — no app database queries,
no process lists or command lines, no log contents, no usernames, no Wi‑Fi SSID, no MAC addresses,
no environment variables, no file contents other than the sync state files.

### Server response

`200` → `{"ok": true, "server_time": "…Z", "device_id": "…", "next_interval_s": 60, "commands": []}`
`400` → `{"ok": false, "error": "…"}` for invalid JSON or a missing required field
(`schema`, `device_id`, `boot_id`, `seq`, `sent_at`, `interval_s`, `identity`, `system`).
`401` → bad or missing token. `413` → body over 256 KB.

The agent sends live heartbeats first. After a successful live send it replays up to 50
buffered heartbeats (oldest first), one request each, with `"buffered": true`. A heartbeat the
server already has (its first 200 was lost) is answered `200` again and not stored twice.

## 4. Agent behaviour

- Loop every `interval_s` (the server may change it via `next_interval_s`, clamped to 5–3600; a
  value that is not a finite number is ignored, and an unreadable answer never stops the replay).
- `server_url` must be `https://`, or plain `http://` to this box (localhost, 127.0.0.0/8, ::1):
  every heartbeat carries the token. `allow_insecure_http: true` permits plain http to another
  machine (trusted networks only); the agent then logs a warning at start.
- On send failure, store the heartbeat JSON in the SQLite buffer (`buffer_path`), keep at most
  `buffer_max_rows` (drop oldest, log how many were dropped).
- `--once` prints one heartbeat to stdout and exits without sending. `--config <path>` selects
  the config file. SIGTERM/SIGINT stop cleanly.
- When run under systemd with `NOTIFY_SOCKET`, send `READY=1` at start and `WATCHDOG=1`
  every loop (stdlib socket, no dependency).
- Docker collector: talks to `/var/run/docker.sock` over HTTP (stdlib) only if accessible;
  reports each container as `container:<name>` with state, `RestartCount`, `StartedAt`.
- systemd units: `systemctl show <unit> --property=ActiveState,SubState,Result,NRestarts,ActiveEnterTimestamp`
  → status `up` when `ActiveState=active`; oneshot units (e.g. saras-sync) that are `inactive`
  with `Result=success` count as `up`; `failed` → `down`.

### agent config keys (JSON)

```json
{
  "server_url": "http://127.0.0.1:8090",
  "allow_insecure_http": false,
  "token": "change-me",
  "device_id": null,
  "interval_s": 60,
  "software_every_s": 600,
  "request_timeout_s": 10,
  "claimed": { "product": "SchoolAI in a Box", "site_code": "eps_koorgalli", "customer": null },
  "services": {
    "app_live_url": "http://127.0.0.1:8000/health/live",
    "app_ready_url": "http://127.0.0.1:8000/health/ready",
    "postgres": "127.0.0.1:5426",
    "vllm_url": null
  },
  "systemd_units": ["nginx.service", "docker.service", "saras-sync.service", "jtop.service"],
  "sync_state_dir": "/opt/saras-sync/state",
  "ping_gateway": true,
  "docker": true,
  "buffer_path": "/var/lib/saras-agent/buffer.db",
  "buffer_max_rows": 20000
}
```
`agent.dev.json`: same, but `interval_s` 10, `token` "dev-fleet-token",
`buffer_path` "../data/agent-buffer.db" (relative paths resolve against the config file's folder,
so this is `device-management/data/agent-buffer.db`; see section 10).

## 5. Storage (SQLite for the pilot; same tables map 1:1 to PostgreSQL/TimescaleDB later)

`server/schema.sql` must contain exactly these tables (indexes may be added):

```sql
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS devices (          -- box details (registry)
  device_id       TEXT PRIMARY KEY,
  serial          TEXT,
  model           TEXT,
  device_type     TEXT,
  hostname        TEXT,
  product         TEXT,
  customer        TEXT,
  site_code       TEXT,
  location        TEXT,
  latitude        REAL,
  longitude       REAL,
  installed_on    TEXT,
  support_contact TEXT,
  device_group    TEXT,
  notes           TEXT,
  first_seen      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_status (    -- current status, one row per box
  device_id      TEXT PRIMARY KEY REFERENCES devices(device_id),
  state          TEXT NOT NULL,               -- online | warning | critical | offline
  reasons        TEXT NOT NULL DEFAULT '[]',  -- JSON array of open alert codes
  last_seen      TEXT NOT NULL,               -- server receive time
  boot_id        TEXT,
  booted_at      TEXT,
  uptime_s       INTEGER,
  interval_s     INTEGER,
  clock_offset_s REAL,                        -- server receive time minus sent_at (live heartbeats only)
  agent_version  TEXT,
  lan_ip         TEXT,
  last_heartbeat TEXT                         -- latest live heartbeat JSON
);

CREATE TABLE IF NOT EXISTS metrics (          -- health readings, one row per heartbeat
  device_id        TEXT NOT NULL,
  ts               TEXT NOT NULL,             -- live: receive time; buffered: sent_at + clock_offset_s
  buffered         INTEGER NOT NULL DEFAULT 0,
  uptime_s         INTEGER,
  cpu_pct          REAL,
  cpu_temp_c       REAL,
  cpu_freq_mhz     REAL,
  load_1m          REAL,
  gpu_pct          REAL,
  gpu_temp_c       REAL,
  gpu_clock_mhz    REAL,
  gpu_power_w      REAL,
  ram_pct          REAL,
  ram_used_mb      REAL,
  disk_pct         REAL,                      -- root filesystem "/"
  disk_used_gb     REAL,
  power_total_w    REAL,
  fan_rpm          REAL,
  max_temp_c       REAL,                      -- hottest thermal zone
  wifi_signal_dbm  REAL,
  gateway_rtt_ms   REAL,
  gateway_loss_pct REAL,
  server_rtt_ms    REAL,
  extra            TEXT,                      -- JSON: cpu_per_core_pct, thermal_c, rails_w, disks, boot_id, seq, sent_at
  PRIMARY KEY (device_id, ts)
);

CREATE TABLE IF NOT EXISTS service_checks (   -- one row per service per heartbeat
  device_id  TEXT NOT NULL,
  ts         TEXT NOT NULL,
  service    TEXT NOT NULL,
  kind       TEXT,
  status     TEXT NOT NULL,
  latency_ms REAL,
  restarts   INTEGER,
  detail     TEXT,
  PRIMARY KEY (device_id, service, ts)
);

CREATE TABLE IF NOT EXISTS software_versions (  -- stored when a version first appears
  device_id  TEXT NOT NULL,
  component  TEXT NOT NULL,
  version    TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen  TEXT NOT NULL,
  PRIMARY KEY (device_id, component, version)
);

CREATE TABLE IF NOT EXISTS events (           -- alerts and informational events
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id   TEXT NOT NULL,
  ts          TEXT NOT NULL,
  kind        TEXT NOT NULL,                  -- alert | info
  severity    TEXT NOT NULL,                  -- critical | warning | info
  code        TEXT NOT NULL,
  message     TEXT NOT NULL,
  resolved_at TEXT,                           -- alerts only; NULL = still open
  data        TEXT                            -- JSON details
);

CREATE TABLE IF NOT EXISTS sync_reports (     -- written only when the sync state changes
  device_id     TEXT NOT NULL,
  ts            TEXT NOT NULL,
  last_success  TEXT,
  last_attempt  TEXT,
  offline_count INTEGER,
  unit_result   TEXT,
  age_h         REAL,
  PRIMARY KEY (device_id, ts)
);

CREATE TABLE IF NOT EXISTS ai_usage (         -- vLLM snapshot per heartbeat (DGX Spark)
  device_id               TEXT NOT NULL,
  ts                      TEXT NOT NULL,
  status                  TEXT,
  model                   TEXT,
  running                 REAL,
  waiting                 REAL,
  kv_cache_pct            REAL,
  prompt_tokens_total     REAL,
  generation_tokens_total REAL,
  requests_success_total  REAL,
  avg_e2e_latency_s       REAL,
  avg_ttft_s              REAL,
  PRIMARY KEY (device_id, ts)
);
```

Ingest rules:
- First heartbeat of an unknown `device_id` creates the `devices` row (from `identity`, and
  `claimed` fills `product`/`site_code`/`customer` **only if empty**) and emits `DEVICE_REGISTERED`.
- `metrics.cpu_temp_c` = `thermal_c.cpu`; `max_temp_c` = max of `thermal_c` values;
  `disk_*` from the `/` entry of `system.disks`; `server_rtt_ms` = `network.last_server_rtt_ms`.
  A reading whose magnitude is over 1e9 is stored as null (no real one comes close, and averages
  of values near the float limit overflow). `extra` also keeps the heartbeat's `boot_id`, `seq`
  and `sent_at`.
- A heartbeat whose `(boot_id, seq, sent_at)` is already stored for the device was delivered
  twice (the agent never saw the first 200, so it buffered and resent it): it is answered 200 and
  not stored, archived or evaluated again. A unique index on `metrics` enforces this. `seq` alone
  is not enough: it restarts with the agent within the same boot.
- Buffered heartbeats: write `metrics`, `service_checks`, `ai_usage` and the info event
  `CONNECTION_INTERRUPTED` (section 6); never overwrite `device_status` or change alert state;
  `ts` = `sent_at + device_status.clock_offset_s` (fallback: `sent_at`). Use `INSERT OR IGNORE`.
- `software_versions`: upsert every non-null component; a new version for an existing component
  emits `VERSION_CHANGED` (data: component, old, new).
- Retention (hourly job): delete `metrics`, `service_checks`, `ai_usage` older than
  `retention_days` (default 30); `events` and `sync_reports` older than 365 days; archive day files
  (section 10) older than `retention_days`, and device folders left empty.

## 6. Status and alert rules (server side)

Evaluated on every live heartbeat; offline is checked by a background thread every 15 s.
The silence of a box heard since the server started is timed on the server's monotonic clock,
so a step of its wall clock (e.g. NTP turned on) neither flags healthy boxes offline nor delays
the alert; otherwise it counts from the later of `last_seen` and the server start.
An alert is a row in `events` with `kind='alert'`; it stays open (`resolved_at IS NULL`) until
its clear condition holds. At most one open alert per (device, code). Per-service codes are
`SERVICE_DOWN:<service name>`. A reading that is null or missing never raises or clears an alert,
except where the box says it no longer monitors something: then that thing's alerts clear
(the table says where), so no alert can stay open for ever about a reading that stopped arriving.

| Code | Severity | Raise when | Clear when |
|---|---|---|---|
| DEVICE_OFFLINE | critical | no heartbeat for more than max(3 × interval_s, 90) s | next live heartbeat (also emit info `DEVICE_RECONNECTED`) |
| GPU_OVERHEAT | critical | `gpu.temp_c` or `thermal_c.gpu` ≥ 105 (NVIDIA devices) | < 100 |
| GPU_TEMP_HIGH | warning | ≥ 95 (NVIDIA devices) | < 90 |
| SOC_TEMP_HIGH | warning | max of `thermal_c` ≥ 95 (NVIDIA devices) | < 90 |
| CPU_OVERHEAT | critical | `thermal_c.cpu` or `system.cpu_temp_c` ≥ 80 (Raspberry Pi 5) | < 75 |
| CPU_TEMP_HIGH | warning | `thermal_c.cpu` or `system.cpu_temp_c` ≥ 75 (Raspberry Pi 5) | < 70 |
| THROTTLED_UNDERVOLTAGE | warning | `rpi_throttled.under_voltage_now` is true (Raspberry Pi 5) | under-voltage cleared |
| THROTTLED_THERMAL | warning | `rpi_throttled.throttled_now` or frequency capped (Raspberry Pi 5) | throttling cleared |
| DISK_CRITICAL | critical | any disk `pct` ≥ 95 | < 93 |
| DISK_LOW | warning | any disk `pct` ≥ 85 | < 83 |
| RAM_HIGH | warning | `ram_pct` ≥ 90 in 3 consecutive heartbeats | < 85 |
| CPU_HIGH | warning | `cpu_pct` ≥ 90 in 3 consecutive heartbeats | < 80 |
| GPU_HIGH | warning | `gpu.util_pct` ≥ 90 in 3 consecutive heartbeats, on a box without an `ai.vllm` block | < 80; or the heartbeat has an `ai.vllm` block (serving an LLM keeps the GPU busy by design) |
| SERVICE_DOWN:&lt;name&gt; | critical | service `status` = `down` | status `up` or `not_configured`; or the heartbeat's `services` list (not null) no longer contains the service (removed, renamed, dropped from the config, Docker no longer readable) |
| SYNC_STALE | critical | `sync.age_h` > 8 | ≤ 8; or `sync` is null (no saras-sync state on the box) |
| SYNC_MISMATCH | critical | `sync.mismatched_tables` > 0 (the last sync's copy on the server doesn't match the box) | = 0; or `sync` is null |
| SYNC_FAILED | warning | `sync.failed_batches` > 0 | = 0; or `sync` is null |
| SYNC_CONFLICTS | warning | `sync.conflicts` > 0 | = 0; or `sync` is null |
| CENTRAL_API_UNREACHABLE | warning | `sync.sync_api_health` = `"down"` or `"unreachable"` | status `"up"` or `"healthy"` |
| DATABASE_UNHEALTHY | warning | `sync.db_health` = `"down"` or `"unhealthy"` | status `"up"` or `"healthy"` |
| CLOCK_UNSYNCED | warning | `system.ntp_synced` is false or \|clock_offset_s\| > 120 | synced and ≤ 120 |
| NETWORK_POOR | warning | `gateway_loss_pct` ≥ 20 or `wifi_signal_dbm` ≤ −75 | loss < 10 and signal > −72 |
| LLM_SATURATED | warning | `ai.vllm.waiting` > 0 or `ai.vllm.kv_cache_pct` ≥ 90, in 3 consecutive heartbeats | waiting = 0 and KV cache < 85 (a null one counts as fine, but not both null); or `ai` is null |

One-shot warnings (recorded with `resolved_at = ts`, so they show in history but don't hold the
device in warning): `SERVICE_RESTARTED` when a service's `restarts` increases.

Info events (`kind='info'`, `severity='info'`):
- `DEVICE_REGISTERED`: first time a device ID reports to the fleet server.
- `CONFIRMED_REBOOT`: boot_id changed between observations following a clean shutdown or reconnect.
- `RESTART_UNEXPECTED`: uptime reset or reboot detected without clean shutdown reporting.
- `AGENT_RESTARTED`: agent process restarted while the host system remained running (same boot_id, seq reset).
- `SHUTDOWN_CLEAN`: graceful agent/device shutdown recorded prior to stop.
- `DEVICE_RECONNECTED`: heartbeat received again after being silent past the offline threshold.
- `VERSION_CHANGED`: software component version updated.
- `CONNECTION_INTERRUPTED`: buffered heartbeats arrived from a box that had been live before.

Device `state`: `offline` if DEVICE_OFFLINE is open; else `critical` if any open critical alert;
else `warning` if any open warning; else `online`. `reasons` = sorted list of open alert codes.

## 7. Read API (server → dashboard)

All responses are JSON. Times are UTC ISO strings.

`GET /api/v1/fleet`
```json
{
  "generated_at": "…Z",
  "counts": { "total": 1, "online": 0, "warning": 1, "critical": 0, "offline": 0 },
  "devices": [{
    "device_id": "1424225065010", "device_type": "AGX Thor", "model": "…", "hostname": "…",
    "product": "SchoolAI in a Box", "customer": null, "site_code": "eps_koorgalli",
    "location": null, "device_group": null,
    "state": "warning", "reasons": ["CLOCK_UNSYNCED"],
    "last_seen": "…Z", "seconds_since_seen": 4, "uptime_s": 12345, "agent_version": "0.1.0",
    "latest": { "cpu_pct": 5.3, "gpu_pct": 0, "gpu_temp_c": 41, "max_temp_c": 41.0,
                "ram_pct": 7.7, "disk_pct": 5.5, "power_total_w": 26.5 },
    "versions": { "os": "Ubuntu 24.04.4 LTS", "l4t": "R39.2.1", "app_version": null }
  }]
}
```
`latest` holds the readings of the last live heartbeat (the `metrics` row at `last_seen`), found
without trusting the newest `ts`: a backward step of the server clock leaves older rows with
later timestamps.

`GET /api/v1/devices/{device_id}` → `404` if unknown, else
```json
{
  "device": { "…all devices columns…": "" },
  "status": { "state": "", "reasons": [], "last_seen": "", "seconds_since_seen": 0, "boot_id": "",
              "booted_at": "", "uptime_s": 0, "interval_s": 0, "clock_offset_s": 0,
              "agent_version": "", "lan_ip": "" },
  "latest": { "…the last live heartbeat as received…": "" },
  "services": [ { "service": "", "kind": "", "status": "", "latency_ms": 0, "restarts": 0,
                  "detail": "", "ts": "", "availability_24h_pct": 100.0, "failed_checks_24h": 0 } ],
  "software": [ { "component": "", "version": "", "first_seen": "", "last_seen": "" } ],
  "sync": { "…latest sync_reports row…": "" },
  "ai": { "…latest ai_usage row…": "" },
  "open_alerts": [ { "…events row…": "" } ],
  "recent_events": [ { "…events row, newest first, max 50…": "" } ]
}
```
`sync` and `ai` are `null` when there is no row. `software` lists the current version per
component (latest `last_seen`). `services` lists every service checked in the 24 h before its
newest check: a service in the last live heartbeat shows that heartbeat's row (`ts` =
`status.last_seen`), any other its newest row (the dashboard marks it as no longer reported).
`availability_24h_pct` = up / (up + down + degraded) and `failed_checks_24h` = checks that were
`down`, both over the last 24 h (null when there were no checks).

`GET /api/v1/devices/{device_id}/metrics?minutes=60` (1–10080, default 60)
```json
{ "device_id": "…", "minutes": 60,
  "points": [ { "ts": "…Z", "cpu_pct": 5.3, "cpu_temp_c": 40.4, "gpu_pct": 0, "gpu_temp_c": 41,
                "ram_pct": 7.7, "disk_pct": 5.5, "power_total_w": 26.5, "gateway_rtt_ms": 5.5,
                "gateway_loss_pct": 0.0, "wifi_signal_dbm": -66, "fan_rpm": 1833, "max_temp_c": 41.0,
                "buffered": 0 } ] }
```
At most 720 points, oldest first; average into equal time buckets when there are more (an
average that is not a finite number is null). `buffered` is 1 for a reading that arrived late
from the agent's offline buffer (the connection was down); in an averaged bucket it is the share
of such readings (0–1).

`GET /api/v1/events?device_id=&open=1&limit=100` → `{"events": [ … newest first … ]}`
(`device_id` optional, `open=1` → open alerts only, `limit` 1–500).

`PATCH /api/v1/devices/{device_id}` with `Authorization: Bearer <admin_token>` and a JSON body
containing any of: `product, customer, site_code, location, latitude, longitude, installed_on,
support_contact, device_group, notes` → returns `{"device": {…}}`. Other keys → 400. PATCH takes
the admin token alone, also when a dashboard password is set: nginx answers 400 to a request with
two `Authorization` headers, and the admin token is the stronger secret.

`GET /api/v1/devices/{device_id}/availability?days=30&tz_offset_min=330` (days 1-365; the viewer's
UTC offset in minutes, so days are the viewer's local days) →
`{"days": [{"day", "readings", "first", "last"}], "gaps": [{"from", "to", "duration_s", "restarted",
"booted_at", "ongoing"}], "first_reading", "last_reading", "threshold_s", "interval_s", "raw_available"}`.
A gap is more than max(3 × interval_s, 180) s between readings. `restarted` is true when the box's uptime
went down across the gap (it was off or rebooting; `booted_at` says when it came back), false when the
box stayed on, null when unknown. `ongoing` marks a gap still running now (`to` is null).

`GET /api/v1/devices/{device_id}/raw?date=YYYY-MM-DD&tz_offset_min=330` → the stored readings of that local
day from the plain-text archive (section 10): `{"available", "readings": [{"time", "seq", "buffered"}]}`.
`GET /api/v1/devices/{device_id}/raw?at=<time>` → `{"time", "reading": {…every stored field…}}`; 404 when
there is none. `available` is false when `archive_dir` is null.

`GET /healthz` → `{"ok": true, "devices": 1}`.
`GET /` → `static/index.html`; `GET /static/<file>` → files inside `static/` only.

### server config keys (JSON)

```json
{
  "bind": "127.0.0.1",
  "port": 8090,
  "db_path": "./fleet.db",
  "fleet_token": "change-me",
  "device_tokens": {},
  "admin_token": "change-me-too",
  "dashboard_user": null,
  "dashboard_password": null,
  "retention_days": 30,
  "offline_after_missed": 3,
  "offline_min_s": 90,
  "archive_dir": null,
  "allowed_hosts": []
}
```
- `archive_dir`: folder for the plain-text heartbeat copies (section 10); `null` turns them off.
- `allowed_hosts`: host names (no port) the dashboard and read API answer for, besides IP
  addresses and `localhost`, e.g. the nginx `server_name`. Any other `Host` header gets 421
  (DNS-rebinding guard, section 9). The heartbeat route is not checked.
- The server refuses to start while `fleet_token`, `admin_token` or a `device_tokens` value is
  still an example placeholder (`change-me`, `change-me-too`): they are public. So the built-in
  defaults and an unedited copy of `server.example.json` do not start.
- Heartbeat auth: a token in `device_tokens[device_id]` if present, else `fleet_token`.
  Compare with `hmac.compare_digest`.
- If `dashboard_user`/`dashboard_password` are set, every non-heartbeat route except PATCH (see
  above) requires HTTP Basic auth.
- `server.dev.json`: `fleet_token` "dev-fleet-token", `admin_token` "dev-admin-token",
  `db_path` "../data/fleet.db", `archive_dir` "../data/heartbeats" (both inside `device-management/data/`).
- Relative paths resolve against the config file's folder.
- Server uses `ThreadingHTTPServer`; one SQLite connection per thread or a lock around writes.

## 8. Dashboard

Plain HTML/CSS/JS, no external libraries or CDNs (schools may be offline). Refresh every 5 s.
- Fleet view: counts (total / online / warning / critical / offline) and a device table:
  status, device ID, type, product, site, last seen ("4 s ago"), CPU %, GPU %, hottest temp,
  RAM %, disk %, power W. Filters: status, device type, product, text search.
- Device view (click a row): identity and versions; live charts for the last 60 minutes
  (CPU %, GPU %, temperatures, power W, RAM %, network latency) drawn with inline SVG, shaded
  where readings arrived late (`buffered`), which the readings table marks too; services table
  (status, latency, restarts, 24 h availability and failed checks, when each last started);
  sync status with the last sync's row counts; current readings (per-core CPU, free disk space,
  log size, aborted vLLM requests among them); open alerts; recent events; `collector_errors`
  shown as a quiet note.
- Every value that came from a device is inserted with `textContent`, never `innerHTML`.
- Light and dark themes via `prefers-color-scheme`. Works at 400 px width.

## 9. Security

- Default bind `127.0.0.1`. Exposing the server to other boxes means putting it behind nginx
  with TLS; document this in README. The agent sends its token only over https, or plain http to
  a server on the same box, unless `allow_insecure_http` is set (section 4).
- Every route except the heartbeat answers 421 unless the `Host` header names an IP address,
  `localhost` or an `allowed_hosts` entry: a web page whose name is re-pointed at the server (DNS
  rebinding) would otherwise read the fleet. The installer also sets a random dashboard password.
- The server refuses to start with the example placeholder tokens (section 7).
- Parameterised SQL only. Static file serving must reject path traversal.
- Response headers: `Content-Security-Policy: default-src 'self'`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`.
- Clamp every string from the device (≤ 200 chars for names/details, ≤ 64 KB for the stored heartbeat).
  Every device string that is stored in a column, used in an event or logged has control characters
  turned into spaces (they could forge log lines) and lone UTF-16 surrogates into `?` (SQLite can't
  store them); a `device_id` with either is refused with 400.
- A chunked request body may carry at most 32 trailer lines (400 after that).

## 10. Local data folder (pilot decision, 29 Sep 2026)

For the pilot all monitoring data stays on the machine running the server, in one folder.
Nothing is synced to any central or cloud database.

```
device-management/data/
  fleet.db                          SQLite: the tables in section 5 (one file, no database server)
  heartbeats/<device>/<YYYY-MM-DD>.jsonl
                                    plain-text copy of every accepted heartbeat (live and buffered),
                                    one JSON object per line, UTC date of the reading (`stored_ts`)
  agent-buffer.db                   the agent's undelivered heartbeats (only while the server is down)
```

- Each archive line is the stored heartbeat (as in `device_status.last_heartbeat`) with two keys
  in front: `received_at` (server receive time) and `stored_ts` (the `ts` used in the tables).
- The device folder name is the device ID with anything outside `[A-Za-z0-9._-]` replaced by `_`
  and, when that changed the ID, a short hash appended. A device ID is never used as a raw path.
- An archive write failure is logged and never rejects the heartbeat.
- Retention removes day files older than `retention_days` (section 5).
- `data/` holds no school data. It is git-ignored.

## 11. Configuration Hierarchy, Revisions, and Log Redaction (Raspberry Pi 5 & Multi-Platform Extensions)

### 11.1 Dynamic Configuration Revision Tracking
- **Hierarchy & Precedence**: Configurations are resolved through a 4-tier inheritance model:
  `Defaults -> Organization -> School -> Device`.
  More specific scopes cleanly override broader scopes, with nested dictionaries (`thresholds`, `health_thresholds`, `optional_collectors`) merged recursively.
- **Deterministic Revision Calculation**: Revisions are computed via SHA-256 hash of the canonical JSON representation (keys sorted, compact separators `","` and `":"`, UTF-8 encoded) of the final merged effective configuration.
  - Identical effective configurations always produce the exact same 64-character hex revision.
  - Any parameter change produces a strictly different revision hash.
  - Independent scope version increments do not cause spurious agent updates if the effective merged configuration remains identical.
- **API Response**: `GET /api/v1/devices/<device_id>/config` returns:
  ```json
  {
    "device_id": "rpi5-01",
    "revision": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    "scope_versions": {"organization": 1, "school": 2, "device": 1},
    "config": {
      "interval_s": 30,
      "sync_interval_s": 300,
      "log_level": "INFO",
      "thresholds": {"cpu_temp_warn_c": 75.0, "cpu_temp_crit_c": 80.0},
      "optional_collectors": {"collect_gpu": false, "collect_power": false},
      "monitored_services": ["saras-sync"]
    }
  }
  ```
- **Agent Caching and Atomic Persistence**:
  - The agent compares the server's `revision` against its active `_applied_revision`.
  - When the revision changes, the agent validates all fields before applying.
  - The new valid configuration is persisted atomically to `<buffer_dir>/last_known_config.json` via a write-to-tempfile and atomic rename (`os.replace`).
  - If the server is unreachable or returns non-200, the agent preserves and continues operating on its last-known-good configuration.
- **Rollback Behavior**:
  - Overrides can be removed by sending a `POST /api/v1/config` payload with `{"action": "delete", "target_type": "<type>", "target_id": "<id>"}`.
  - Removing a device override instantly restores inheritance from school or organization defaults and recalculates the corresponding previous deterministic revision.

### 11.2 Remote Configuration Validation and Application
The server strictly validates all submitted configurations before storing:
- `interval_s` / `heartbeat_interval_s`: Integer, 5 to 3600 seconds.
- `sync_interval_s` / `sync_check_interval_s`: Integer, 10 to 86400 seconds.
- `log_level`: String, one of `["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]`.
- `thresholds` / `health_thresholds`: Numeric values in valid operational ranges:
  - `cpu_temp_warn_c` (30.0 - 110.0 °C), `cpu_temp_crit_c` (30.0 - 120.0 °C).
  - Evaluated dynamically for Raspberry Pi 5 without altering fixed NVIDIA Jetson Orin Nano, Jetson Thor, or DGX Spark alert profiles.
- `optional_collectors`: Dictionary of boolean flags (`collect_gpu`, `collect_power`, `collect_thermal`, `collect_ai`).
- `monitored_services`: List of alphanumeric service names (with allowed `:` and `.`). Path traversal characters (`/`, `..`) or shell injection characters are strictly rejected with 400 Bad Request.
- Unsupported keys are rejected with 400 Bad Request to prevent unapplied or silent failure states.

### 11.3 Recursive Log Redaction
Both agent (`saras_agent.py`) and server (`fleet_server.py`) enforce non-mutating recursive redaction:
- **Sensitive Key Matcher**: Case-insensitive matching across variations containing `password`, `token`, `access_token`, `refresh_token`, `authorization`, `api_key`, `secret`, `connection_string`, `account_key`, `private_key`, `credential`.
- **Recursive Traversal**: Dicts and lists at any depth are traversed. Primitive values under sensitive keys are replaced with `"[REDACTED]"`. Container keys are recursed into to redact sensitive leaves while preserving nested non-sensitive diagnostics.
- **Free-Text Message Scanning**: Regex scanners scrub `Bearer <token>`, connection strings (`AccountKey=...`, `DefaultEndpointsProtocol=...`), private key blocks (`-----BEGIN ... PRIVATE KEY-----`), and query param secrets (`password=...`, `token=...`) before log transmission or storage.

### 11.4 Raspberry Pi 5 Telemetry Limitations
- **GPU Architecture**: Broadcom VideoCore VII has no proprietary driver daemon (no `tegrastats` or NVML). GPU memory is allocated from unified LPDDR4X/LPDDR5 RAM.
- **Sensors**: CPU core temperatures read from `/sys/class/thermal/thermal_zone0/temp` or `vcgencmd measure_temp`.
- **Throttling**: 24-bit bitmask from `vcgencmd get_throttled` captures active undervoltage (bit 0), frequency cap (bit 1), throttling (bit 2), and thermal limits (bit 3).

### 11.5 Hardware Validation Requirements
Automated CI validates all logic, schemas, downsampling, and network failure modes via mock interfaces. Full physical hardware validation requires:
1. Physical Raspberry Pi 5 connected to 27W USB-C PD power supply to test under-voltage alerts during active stress testing.
2. Active cooler fan curve testing under sustained 80°C thermal throttling loads.
3. Verification of sudden power disconnects to ensure switch-off and reboot detection without duplicate event generation.
4. NVIDIA Jetson Thor, DGX Spark, and Jetson Orin Nano physical board validation with tegrastats, NVML, and Ollama/vLLM endpoints.

## 12. Extended Device Management Features Specification (Features 1 - 8)

### 12.1 Feature 1 — Application Version
- **Discovery Mechanism**:
  1. `app_version_url` configured in agent config or services.
  2. Derived health endpoints `/api/version`, `/health/version`, or `/version` relative to `app_live_url`.
  3. Metadata file inspection on disk: `/opt/saras/version.json`, `/opt/saras-app/version.json`, `/etc/saras/version.json`, or custom `app_version_file`.
  4. Docker container inspection of image tag/labels matching `saras-app` or `schoolai`.
- **Validation**: Cleaned ASCII string matching `^[vV]?\d+(?:\.[a-zA-Z0-9_\-\.\+]+)*$`, maximum 64 characters. Null or placeholders ("unknown", "default", "0.0.0") are normalized to `None`.
- **Reporting & Persistence**: Reported under `software.app_version` in heartbeat payload; persisted into `software_versions` table and exposed in device software listings and fleet overview table.

### 12.2 Feature 2 — Firmware Updates
- **Platform Capability Detection**: Distinguishes Raspberry Pi 5 (`rpi-eeprom`), NVIDIA Jetson Thor/Orin (`jetson-qspi`), and generic UEFI platforms.
- **Safety Preflight**: Blocks firmware operations if active undervoltage (`under_voltage_now == True`) or power instability is detected.
- **Verification & Safe Pilot Mode**: Validates SHA-256 artifact checksum format. Live destructive flashing is prohibited during development and pilot mode; simulation reports preflight compatibility and reboot requirements safely.

### 12.3 Feature 3 — Operating System Updates
- **Capability Detection**: Identifies host OS, architecture, and available package manager (`apt`, `dnf`).
- **Preflight Checks**: Verifies package manager locks (`/var/lib/dpkg/lock-frontend`, `/var/lib/apt/lists/lock`), disk space, and pending reboot status (`/var/run/reboot-required`).
- **Safe Execution**: Controlled dry-run simulation via `apt-get -s upgrade` without arbitrary remote shell execution.

### 12.4 Feature 4 — Application and Docker Image Updates
- **Preflight Verification**: Checks available disk space (minimum 1.0 GB required on root mount).
- **Target Metadata**: Validates image reference and target version string.
- **Safe Orchestration**: Simulates or executes container replacement while preserving volumes and environment settings; verifies application health probe post-deployment.

### 12.5 Feature 5 — Configuration Updates
- **Authorized Management**: `GET /api/v1/config` and `POST /api/v1/config` protected by Bearer Admin Token.
- **Hierarchical Scopes**: Default -> Organization -> School -> Device inheritance with deterministic SHA-256 revision hash computation.
- **Dashboard Editor**: In-browser configuration editor with live JSON validation, SHA-256 revision hash preview, scope targeting, and confirmation modals.

### 12.6 Feature 6 — Remote Update Deployment
- **API Endpoints**:
  - `POST /api/v1/updates/jobs`: Creates an update job (status: `scheduled`).
  - `GET /api/v1/updates/jobs`: Lists update jobs with optional device and status filtering.
  - `GET /api/v1/updates/jobs/{id}`: Detailed job metadata and execution state.
  - `POST /api/v1/updates/jobs/{id}/status`: Agent reports progress, execution ID, message, and completion.
  - `POST /api/v1/updates/jobs/{id}/cancel`: Cancels pending or scheduled update jobs.
- **Controlled Delivery**: Jobs delivered to device agents inside live heartbeat response in `commands: [...]`.
- **Idempotency**: Agents record completed job IDs in `completed_jobs.json` to prevent duplicate re-execution across network retries or restarts.

### 12.7 Feature 7 — Scheduled Updates
- **API Endpoints**:
  - `POST /api/v1/updates/schedules`: Creates scheduled updates with ISO 8601 timestamps and optional maintenance windows.
  - `GET /api/v1/updates/schedules`: Lists active and past schedules.
  - `POST /api/v1/updates/schedules/{id}/cancel`: Cancels active schedules.
- **Maintenance Windows**: Supports `HH:MM-HH:MM` UTC windows (including overnight windows such as `22:00-04:00`). Due schedules trigger update job creation only when within the configured window.

### 12.8 Feature 8 — Hardware Failure Detection
- **NVMe Storage Health**:
  - `STORAGE_CRITICAL`: Triggers on NVMe SMART critical warning bit > 0, media errors > 0, percentage used >= 95%, or available spare capacity <= 5%.
  - `STORAGE_WEAR_HIGH`: Warning when drive wear >= 80% or available spare capacity <= 20%.
- **Cooling Fan Diagnostics**:
  - `FAN_STALLED`: Triggers when fan stall flag is raised or fan RPM <= 50 while CPU/SoC temperature >= 45°C.
- **Memory Hardware Health (EDAC)**:
  - `MEMORY_ECC_ERROR`: Triggers on uncorrectable memory errors (`ue_count > 0` or `ecc_ue_count > 0`) or sustained correctable errors.
- **Alert Recovery**: Hardware alerts resolve automatically when verified normal telemetry is received.


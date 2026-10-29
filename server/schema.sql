-- SARAS fleet server: SQLite schema (CONTRACT.md section 5).
-- The tables are exactly the contract's; only indexes are added (allowed by the contract).
-- Applied by fleet_server.py at every start; every statement is idempotent.

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

CREATE TABLE IF NOT EXISTS ai_usage (         -- vLLM / Ollama snapshot per heartbeat
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

-- Indexes -------------------------------------------------------------------------------

-- At most one open alert per (device, code). One-shot alerts are stored already resolved,
-- so they are not covered by this index.
CREATE UNIQUE INDEX IF NOT EXISTS events_one_open_alert
  ON events (device_id, code) WHERE kind = 'alert' AND resolved_at IS NULL;

-- Device history (newest first) and per-device retention.
CREATE INDEX IF NOT EXISTS events_device_ts ON events (device_id, ts);

-- Fleet-wide event list, newest first.
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);

-- Services seen in the last 24 h and per-device retention (the primary key starts with service).
CREATE INDEX IF NOT EXISTS service_checks_device_ts ON service_checks (device_id, ts);

-- One metrics row per heartbeat. boot_id, seq and sent_at (kept in extra) are fixed by the device
-- when it collects a heartbeat, so a heartbeat delivered twice (the agent never saw the first 200
-- and sent it again) is recognised and stored once. Rows without these keys never collide.
CREATE UNIQUE INDEX IF NOT EXISTS metrics_one_row_per_heartbeat
  ON metrics (device_id, json_extract(extra, '$.sent_at'), json_extract(extra, '$.seq'),
              json_extract(extra, '$.boot_id'));



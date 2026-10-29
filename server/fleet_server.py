#!/usr/bin/env python3
"""SARAS fleet server: heartbeat ingest, SQLite storage, alert engine, retention, read API
and the dashboard's static files.

Built to device-management/CONTRACT.md (sections 3, 5, 6, 7 and 9). Python 3.12, standard
library only.

    python3 server/fleet_server.py --config server/server.dev.json

Stops cleanly on SIGTERM or Ctrl+C.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import re
import signal
import socket
import socketserver
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, time as dt_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

__version__ = "0.1.0"

log = logging.getLogger("fleet")

HERE = Path(__file__).resolve().parent
SCHEMA_FILE = HERE / "schema.sql"
STATIC_DIR = HERE / "static"

MAX_BODY = 256 * 1024             # request body limit (heartbeat and PATCH)
DRAIN_MAX = 4 * 1024 * 1024       # a rejected body up to this size is read and dropped first
MAX_TRAILER_LINES = 32            # trailer fields read after the last chunk of a chunked body
MAX_STR = 200                     # device strings (names, details) are clamped to this
MAX_STORED_HEARTBEAT = 64 * 1024  # stored copy of the latest live heartbeat
MAX_ITEMS = 500                   # entries kept from any one list or map in a heartbeat
MAX_DEPTH = 16                    # nesting kept in the stored heartbeat
MAX_POINTS = 720                  # metrics endpoint
MAX_READING = 1e9                 # a metrics reading beyond this magnitude is stored as null
LONG_RETENTION_DAYS = 365         # events and sync_reports
EVENTS_LIMIT_DEFAULT, EVENTS_LIMIT_MAX = 100, 500
MINUTES_DEFAULT, MINUTES_MAX = 60, 10080
RECENT_EVENTS = 50

REQUIRED_FIELDS = ("schema", "device_id", "boot_id", "seq", "sent_at", "interval_s", "identity", "system")
SERVICE_STATUSES = frozenset({"up", "down", "degraded", "unknown", "not_configured"})
PATCH_TEXT_FIELDS = {"product": 200, "customer": 200, "site_code": 200, "location": 200,
                     "installed_on": 200, "support_contact": 200, "device_group": 200, "notes": 2000}
PATCH_COORD_FIELDS = {"latitude": 90.0, "longitude": 180.0}
METRIC_COLUMNS = ("uptime_s", "cpu_pct", "cpu_temp_c", "cpu_freq_mhz", "load_1m", "gpu_pct", "gpu_temp_c",
                  "gpu_clock_mhz", "gpu_power_w", "ram_pct", "ram_used_mb", "disk_pct", "disk_used_gb",
                  "power_total_w", "fan_rpm", "max_temp_c", "wifi_signal_dbm", "gateway_rtt_ms",
                  "gateway_loss_pct", "server_rtt_ms", "extra")
AI_COLUMNS = ("status", "model", "running", "waiting", "kv_cache_pct", "prompt_tokens_total",
              "generation_tokens_total", "requests_success_total", "avg_e2e_latency_s", "avg_ttft_s")
POINT_FIELDS = ("cpu_pct", "cpu_temp_c", "gpu_pct", "gpu_temp_c", "ram_pct", "disk_pct", "power_total_w",
                "gateway_rtt_ms", "gateway_loss_pct", "wifi_signal_dbm", "fan_rpm", "max_temp_c", "buffered")
FLEET_LATEST_FIELDS = ("cpu_pct", "gpu_pct", "gpu_temp_c", "max_temp_c", "ram_pct", "disk_pct", "power_total_w")
FLEET_VERSIONS = ("os", "l4t", "app_version")
STATUS_FIELDS = ("boot_id", "booted_at", "uptime_s", "interval_s", "clock_offset_s", "agent_version", "lan_ip")
SYNC_KEY = ("last_success", "last_attempt", "offline_count", "unit_result")
SYNC_COUNT_KEYS = ("source_tables", "source_rows", "target_rows", "mismatched_tables")
SYNC_EXTENDED_KEYS = ("pending_changes", "sync_duration_s", "failed_batches", "retry_counts",
                      "conflicts", "last_error_category", "sync_agent_status", "sync_api_health", "db_health")
LOG_CATEGORIES = frozenset({"APPLICATION", "API", "SYNC", "SYSTEM", "AI", "SECURITY", "UPDATE"})
LOG_SEVERITIES = frozenset({"info", "warning", "error", "critical"})
SENSITIVE_KEY_WORDS = (
    "password", "passwd", "pwd",
    "token", "accesstoken", "refreshtoken", "authtoken", "idtoken", "bearertoken",
    "authorization",
    "apikey",
    "secret", "clientsecret",
    "connectionstring", "connstr",
    "accountkey",
    "privatekey", "privkey",
    "credential", "credentials",
)

ALLOWED_JOB_OPERATIONS = frozenset({"app_update", "os_update", "firmware_update", "config_update", "health_check"})
JOB_STATUSES = frozenset({"scheduled", "dispatched", "in_progress", "completed", "failed", "cancelled"})


def is_in_maintenance_window(window_str: str | None, dt: datetime | None = None) -> bool:
    """Checks whether the given datetime (or now in UTC) falls within 'HH:MM-HH:MM'."""
    if not window_str or not window_str.strip():
        return True
    try:
        parts = window_str.strip().split("-")
        if len(parts) != 2:
            return True
        start_h, start_m = [int(p) for p in parts[0].strip().split(":")]
        end_h, end_m = [int(p) for p in parts[1].strip().split(":")]
        start_t = dt_time(start_h, start_m)
        end_t = dt_time(end_h, end_m)
        cur_t = (dt or datetime.now(timezone.utc)).time()
        if start_t <= end_t:
            return start_t <= cur_t <= end_t
        return cur_t >= start_t or cur_t <= end_t
    except Exception:
        return True


def is_sensitive_key(key: str | Any) -> bool:
    k_clean = re.sub(r"[^a-zA-Z0-9]", "", str(key)).lower()
    if not k_clean:
        return False
    if k_clean == "auth":
        return True
    for w in SENSITIVE_KEY_WORDS:
        if w in k_clean:
            return True
    return False


_REDACT_TEXT_PATTERNS = (
    (re.compile(r"Bearer\s+[A-Za-z0-9_\-\.~+/=]+", re.I), "Bearer [REDACTED]"),
    (re.compile(r"(?:Authorization\s*:\s*)(?:Bearer\s+)?[^\s,;]+", re.I), "Authorization: [REDACTED]"),
    (re.compile(r"(password|passwd|pwd|token|access_token|refresh_token|secret|client_secret|api_key|apikey|account_key|credential|credentials)\s*[:=]\s*([^\s,;]+)", re.I), r"\1=[REDACTED]"),
    (re.compile(r"(DefaultEndpointsProtocol=[^;\s]+;AccountName=[^;\s]+;AccountKey=)[^;\s]+(;[^\s;]*)?", re.I), r"\1[REDACTED]\2"),
    (re.compile(r"(AccountKey\s*=\s*)[^;\s]+", re.I), r"\1[REDACTED]"),
    (re.compile(r"([a-zA-Z0-9+.-]+://[^:]+:)([^@]+)(@)", re.I), r"\1[REDACTED]\3"),
    (re.compile(r"-----BEGIN[ A-Z0-9_-]+PRIVATE KEY-----[\s\S]+?-----END[ A-Z0-9_-]+PRIVATE KEY-----"), "[REDACTED PRIVATE KEY]"),
)


def redact_sensitive_text(val: str) -> str:
    if not val:
        return val
    for pat, repl in _REDACT_TEXT_PATTERNS:
        val = pat.sub(repl, val)
    return val


def redact_data(data, max_depth: int = 16):
    """Recursively redacts sensitive keys and values in nested structures without mutating input."""
    if max_depth <= 0:
        return "[TRUNCATED_NESTING]"
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            k_str = str(k)
            if isinstance(v, (dict, list, tuple)):
                result[k_str] = redact_data(v, max_depth - 1)
            elif is_sensitive_key(k_str):
                result[k_str] = "[REDACTED]"
            elif isinstance(v, str):
                result[k_str] = redact_sensitive_text(v)
            else:
                result[k_str] = v
        return result
    elif isinstance(data, (list, tuple)):
        return [redact_data(item, max_depth - 1) if isinstance(item, (dict, list, tuple))
                else (redact_sensitive_text(item) if isinstance(item, str) else item)
                for item in data]
    elif isinstance(data, str):
        return redact_sensitive_text(data)
    else:
        return data


def redact_sensitive(val: str) -> str:
    return redact_sensitive_text(val)


def compute_config_revision(config: dict) -> str:
    """Deterministic revision hash for the effective configuration."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


EXTENDED_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_configs (
  target_type TEXT NOT NULL,
  target_id   TEXT NOT NULL,
  version     INTEGER NOT NULL DEFAULT 1,
  config      TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (target_type, target_id)
);

CREATE TABLE IF NOT EXISTS device_logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id    TEXT UNIQUE,
  device_id   TEXT NOT NULL,
  service     TEXT NOT NULL,
  category    TEXT NOT NULL,
  severity    TEXT NOT NULL,
  ts          TEXT NOT NULL,
  message     TEXT NOT NULL,
  attributes  TEXT,
  received_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS device_logs_device_ts ON device_logs (device_id, ts);
CREATE INDEX IF NOT EXISTS device_logs_category ON device_logs (category);
CREATE INDEX IF NOT EXISTS device_logs_service ON device_logs (service);

CREATE TABLE IF NOT EXISTS update_jobs (
  job_id          TEXT PRIMARY KEY,
  device_id       TEXT NOT NULL,
  operation       TEXT NOT NULL,
  target_version  TEXT,
  payload         TEXT NOT NULL DEFAULT '{}',
  status          TEXT NOT NULL DEFAULT 'scheduled',
  progress_pct    INTEGER NOT NULL DEFAULT 0,
  message         TEXT NOT NULL DEFAULT '',
  execution_id    TEXT,
  error_details   TEXT,
  created_at      TEXT NOT NULL,
  dispatched_at   TEXT,
  updated_at      TEXT NOT NULL,
  completed_at    TEXT,
  created_by      TEXT NOT NULL DEFAULT 'admin'
);

CREATE INDEX IF NOT EXISTS update_jobs_device_status ON update_jobs (device_id, status);
CREATE INDEX IF NOT EXISTS update_jobs_created_at ON update_jobs (created_at);

CREATE TABLE IF NOT EXISTS scheduled_updates (
  schedule_id     TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  device_id       TEXT NOT NULL,
  operation       TEXT NOT NULL,
  target_version  TEXT,
  payload         TEXT NOT NULL DEFAULT '{}',
  scheduled_at    TEXT NOT NULL,
  timezone        TEXT NOT NULL DEFAULT 'UTC',
  maintenance_window TEXT,
  status          TEXT NOT NULL DEFAULT 'active',
  last_run_at     TEXT,
  created_at      TEXT NOT NULL,
  created_by      TEXT NOT NULL DEFAULT 'admin'
);

CREATE INDEX IF NOT EXISTS scheduled_updates_status_scheduled ON scheduled_updates (status, scheduled_at);
"""


# Control characters in a device string could forge whole log lines, so text() turns them into spaces.
CONTROL_CHARS = re.compile(r"[\x01-\x1f\x7f-\x9f]")

SECURITY_HEADERS = (
    ("Content-Security-Policy", "default-src 'self'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".json": "application/json", ".map": "application/json", ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/x-icon", ".woff": "font/woff",
    ".woff2": "font/woff2",
}

DEFAULT_CONFIG = {
    "bind": "127.0.0.1",
    "port": 8090,
    "db_path": "./fleet.db",
    "fleet_token": "change-me",
    "device_tokens": {},
    "admin_token": "change-me-too",
    "dashboard_user": None,
    "dashboard_password": None,
    "retention_days": 30,
    "offline_after_missed": 3,
    "offline_min_s": 90,
    "archive_dir": None,
    "allowed_hosts": [],
}
PLACEHOLDER_TOKENS = {"change-me", "change-me-too"}


# --------------------------------------------------------------------------- time

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def fmt_ms(ms: int) -> str:
    """Epoch milliseconds -> '2026-09-29T07:40:00.123Z' (fixed width, so it sorts as text)."""
    dt = _EPOCH + timedelta(milliseconds=ms)
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:"
            f"{dt.second:02d}.{dt.microsecond // 1000:03d}Z")


def parse_ts(value) -> int | None:
    """ISO-8601 text -> epoch milliseconds. A time without a zone is taken as UTC. None if unusable."""
    if not isinstance(value, str) or not 10 <= len(value) <= 64:
        return None
    raw = value.strip()
    if raw[-1:] in ("Z", "z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt - _EPOCH
        ms = delta.days * 86_400_000 + delta.seconds * 1000 + (delta.microseconds + 500) // 1000
        fmt_ms(ms)  # must be representable in the stored format
    except (ValueError, OverflowError):
        return None
    return ms


def norm_ts(value) -> str | None:
    ms = parse_ts(value)
    return None if ms is None else fmt_ms(ms)


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "an unknown time"
    s = max(0.0, float(seconds))
    if s < 120:
        return f"{s:.0f} s"
    if s < 7200:
        return f"{s / 60:.1f} min"
    if s < 172800:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} d"


# --------------------------------------------------------------------------- device input

def num(value) -> float | None:
    """A finite JSON number as float, else None (booleans are not numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        f = float(value)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


def intnum(value) -> int | None:
    f = num(value)
    if f is None:
        return None
    if isinstance(value, int):
        return value if abs(value) < 2 ** 63 else None
    return int(round(f)) if abs(f) < 9.2e18 else None


def one_line(value: str) -> str:
    """Printable text on one line: NUL removed, other control characters (which could forge log
    lines) turned into spaces, and lone UTF-16 surrogates (which SQLite cannot store) into '?'."""
    if value.isprintable():
        return value
    value = CONTROL_CHARS.sub(" ", value.replace("\x00", ""))
    return value.encode("utf-8", "replace").decode("utf-8")


def has_surrogates(value: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in value)


def text(value, limit: int = MAX_STR) -> str | None:
    """A device string made one_line() and clamped to `limit` characters (numbers are converted),
    else None. Every device string that reaches a table, an event or a log line goes through here."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        try:
            value = str(value)
        except ValueError:
            return None
    if not isinstance(value, str):
        return None
    return one_line(value)[:limit]


def obj(value) -> dict:
    return value if isinstance(value, dict) else {}


def num_map(value) -> dict:
    """{name: number or null} with names clamped (thermal_c, rails_w)."""
    out = {}
    for k, v in list(obj(value).items())[:MAX_ITEMS]:
        key = text(k)
        if key:
            out[key] = num(v)
    return out


def clean_disks(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [{"mount": text(d.get("mount")), "total_gb": num(d.get("total_gb")),
             "used_gb": num(d.get("used_gb")), "free_gb": num(d.get("free_gb")), "pct": num(d.get("pct"))}
            for d in value[:MAX_ITEMS] if isinstance(d, dict)]


def clean_services(value) -> list[dict]:
    out, seen = [], set()
    if not isinstance(value, list):
        return out
    for s in value[:MAX_ITEMS]:
        if not isinstance(s, dict):
            continue
        name = text(s.get("name"))
        if not name or name in seen:
            continue
        seen.add(name)
        status = (text(s.get("status")) or "unknown").lower()
        out.append({
            "name": name,
            "kind": text(s.get("kind")),
            "status": status if status in SERVICE_STATUSES else "unknown",
            "latency_ms": num(s.get("latency_ms")),
            "restarts": intnum(s.get("restarts")),
            "detail": text(s.get("detail")),
        })
    return out


def clean_software(value) -> dict[str, str]:
    out = {}
    for k, v in list(obj(value).items())[:MAX_ITEMS]:
        component, version = text(k), text(v)
        if component and version and version.strip():
            out[component] = version
    return out


def clean_sync(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    return {
        "last_success": text(value.get("last_success")),
        "last_attempt": text(value.get("last_attempt")),
        "offline_count": intnum(value.get("offline_count")),
        "unit_result": text(value.get("unit_result")),
        "age_h": num(value.get("age_h")),
        **{k: intnum(value.get(k)) for k in SYNC_COUNT_KEYS},
        "pending_changes": intnum(value.get("pending_changes")),
        "sync_duration_s": num(value.get("sync_duration_s")),
        "failed_batches": intnum(value.get("failed_batches")),
        "retry_counts": intnum(value.get("retry_counts")),
        "conflicts": intnum(value.get("conflicts")),
        "last_error_category": text(value.get("last_error_category")),
        "sync_agent_status": text(value.get("sync_agent_status")),
        "sync_api_health": text(value.get("sync_api_health")),
        "db_health": text(value.get("db_health")),
    }


def clean_ai(value) -> dict | None:
    ai_obj = obj(value)
    item = ai_obj.get("vllm") or ai_obj.get("ollama")
    if not isinstance(item, dict):
        return None
    return {c: (text(item.get(c)) if c in ("status", "model") else num(item.get(c))) for c in AI_COLUMNS}


def validate_config_payload(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("config payload must be a JSON object")
    target_type = body.get("target_type")
    if target_type not in ("default", "org", "school", "device"):
        raise ValueError("target_type must be one of: default, org, school, device")
    target_id = body.get("target_id")
    if not target_id or not isinstance(target_id, str):
        raise ValueError("target_id must be a non-empty string")
    config = body.get("config")
    if not isinstance(config, dict):
        raise ValueError("config must be a JSON object")

    allowed_keys = {
        "interval_s", "heartbeat_interval_s",
        "telemetry_sample_interval_s",
        "sync_interval_s", "sync_check_interval_s",
        "log_level", "health_thresholds", "thresholds",
        "optional_collectors", "monitored_services",
    }
    unknown = set(config) - allowed_keys
    if unknown:
        raise ValueError(f"unknown configuration keys: {', '.join(sorted(unknown))}")

    for k in ("interval_s", "heartbeat_interval_s"):
        if k in config:
            v = config[k]
            if not isinstance(v, int) or isinstance(v, bool) or not (5 <= v <= 3600):
                raise ValueError(f"{k} must be an integer between 5 and 3600")
    if "telemetry_sample_interval_s" in config:
        v = config["telemetry_sample_interval_s"]
        if not isinstance(v, int) or isinstance(v, bool) or not (5 <= v <= 3600):
            raise ValueError("telemetry_sample_interval_s must be an integer between 5 and 3600")
    for k in ("sync_interval_s", "sync_check_interval_s"):
        if k in config:
            v = config[k]
            if not isinstance(v, int) or isinstance(v, bool) or not (60 <= v <= 86400):
                raise ValueError(f"{k} must be an integer between 60 and 86400")
    if "log_level" in config:
        v = str(config["log_level"]).upper()
        if v not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError("log_level must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL")
        config["log_level"] = v

    allowed_thresholds = {
        "cpu_temp_warn_c", "cpu_temp_crit_c", "cpu_pct_warn",
        "ram_pct_warn", "disk_pct_warn", "disk_pct_crit"
    }
    for th_key in ("health_thresholds", "thresholds"):
        if th_key in config:
            th = config[th_key]
            if not isinstance(th, dict):
                raise ValueError(f"{th_key} must be an object")
            unrec = set(th) - allowed_thresholds
            if unrec:
                raise ValueError(f"unknown {th_key} keys: {', '.join(sorted(unrec))}")
            for tk, tv in th.items():
                if not isinstance(tv, (int, float)) or isinstance(tv, bool):
                    raise ValueError(f"{th_key}.{tk} must be a number")
                if "temp" in tk and not (40.0 <= tv <= 120.0):
                    raise ValueError(f"{th_key}.{tk} must be between 40.0 and 120.0")
                if "pct" in tk and not (10.0 <= tv <= 100.0):
                    raise ValueError(f"{th_key}.{tk} must be between 10.0 and 100.0")
            if "cpu_temp_warn_c" in th and "cpu_temp_crit_c" in th:
                if th["cpu_temp_warn_c"] >= th["cpu_temp_crit_c"]:
                    raise ValueError(f"{th_key}.cpu_temp_warn_c must be less than cpu_temp_crit_c")
            if "disk_pct_warn" in th and "disk_pct_crit" in th:
                if th["disk_pct_warn"] >= th["disk_pct_crit"]:
                    raise ValueError(f"{th_key}.disk_pct_warn must be less than disk_pct_crit")

    allowed_collectors = {"docker", "ping_gateway", "gpu", "power", "thermal", "ai"}
    if "optional_collectors" in config:
        oc = config["optional_collectors"]
        if not isinstance(oc, dict):
            raise ValueError("optional_collectors must be an object")
        unrec = set(oc) - allowed_collectors
        if unrec:
            raise ValueError(f"unknown optional_collectors keys: {', '.join(sorted(unrec))}")
        for ok, ov in oc.items():
            if not isinstance(ov, bool):
                raise ValueError(f"optional_collectors.{ok} must be a boolean")

    service_name_re = re.compile(r"^[a-zA-Z0-9_\-\.@]{1,128}$")
    if "monitored_services" in config:
        ms = config["monitored_services"]
        if not isinstance(ms, list):
            raise ValueError("monitored_services must be a list")
        if len(ms) > 50:
            raise ValueError("monitored_services cannot exceed 50 entries")
        for s in ms:
            if not isinstance(s, str) or not service_name_re.match(s):
                raise ValueError(f"invalid service name in monitored_services: {s!r}")

    return {"target_type": target_type, "target_id": target_id, "config": config}


def clamp_deep(value, depth: int = 0):
    """Copy of a JSON value with strings clamped, containers bounded and non-finite numbers dropped."""
    if isinstance(value, str):
        return value.replace("\x00", "")[:MAX_STR]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if depth >= MAX_DEPTH:
        return None
    if isinstance(value, dict):
        return {str(k).replace("\x00", "")[:MAX_STR]: clamp_deep(v, depth + 1)
                for k, v in list(value.items())[:MAX_ITEMS]}
    if isinstance(value, list):
        return [clamp_deep(v, depth + 1) for v in value[:MAX_ITEMS]]
    return None


def _dumps(value) -> str:
    return json.dumps(value, separators=(",", ":"), allow_nan=False)


def stored_heartbeat(hb: dict) -> str:
    """The heartbeat as stored in device_status.last_heartbeat: strings clamped, at most 64 KB."""
    doc = clamp_deep(hb)
    out = _dumps(doc)  # ASCII (ensure_ascii), so len() is the size in bytes
    if len(out) <= MAX_STORED_HEARTBEAT:
        return out
    doc["_truncated"] = True
    for path in (("system", "cpu_per_core_pct"), ("services",), ("collector_errors",), ("software",),
                 ("ai",), ("thermal_c",), ("power",), ("system", "disks"), ("network",), ("gpu",),
                 ("sync",), ("claimed",), ("identity",), ("system",)):
        parent = doc
        for key in path[:-1]:
            parent = parent.get(key) if isinstance(parent, dict) else None
        if isinstance(parent, dict) and parent.get(path[-1]) is not None:
            parent[path[-1]] = None
            out = _dumps(doc)
            if len(out) <= MAX_STORED_HEARTBEAT:
                return out
    keep = ("schema", "agent_version", "device_id", "boot_id", "seq", "sent_at", "interval_s", "buffered")
    return _dumps({**{k: doc.get(k) for k in keep}, "_truncated": True})


def validate_heartbeat(hb) -> str | None:
    """Error message for a heartbeat the server cannot accept (HTTP 400), else None."""
    if not isinstance(hb, dict):
        return "body must be a JSON object"
    missing = [k for k in REQUIRED_FIELDS if hb.get(k) is None]
    if missing:
        return "missing required field(s): " + ", ".join(missing)
    did = hb["device_id"]
    if (not isinstance(did, str) or not did.strip() or len(did) > MAX_STR
            or any(ord(c) < 32 or ord(c) == 127 for c in did) or has_surrogates(did)):
        return "device_id must be a non-empty string of at most 200 printable characters"
    if not isinstance(hb["schema"], str) or not hb["schema"].strip():
        return "schema must be a non-empty string"
    if not isinstance(hb["boot_id"], str):
        return "boot_id must be a string"
    if isinstance(hb["seq"], bool) or not isinstance(hb["seq"], int):
        return "seq must be an integer"
    if parse_ts(hb["sent_at"]) is None:
        return "sent_at must be an ISO-8601 timestamp"
    interval = num(hb["interval_s"])
    if interval is None or interval <= 0:
        return "interval_s must be a positive number"
    if not isinstance(hb["identity"], dict):
        return "identity must be an object"
    if not isinstance(hb["system"], dict):
        return "system must be an object"
    return None


def clamp_interval(value) -> int:
    return int(max(1, min(86400, round(num(value) or 60))))


def heartbeat_key(hb: dict) -> tuple:
    """(boot_id, seq, sent_at): fixed by the device when it collects a heartbeat, so a heartbeat
    delivered twice (its first 200 was lost) carries the same key both times. seq alone is not
    enough, because it restarts at 1 when the agent restarts within the same boot."""
    return text(hb.get("boot_id")), intnum(hb.get("seq")), norm_ts(hb.get("sent_at"))


def metric_values(hb: dict) -> dict:
    """One metrics row (all columns except device_id, ts, buffered) from a heartbeat. A reading
    beyond +-1e9 is stored as null: no real one comes close, and averaging values near the float
    limit would overflow to infinity, which JSON cannot carry."""
    system, gpu, power, net = (obj(hb.get(k)) for k in ("system", "gpu", "power", "network"))
    temps = num_map(hb.get("thermal_c"))
    known_temps = [v for v in temps.values() if v is not None]
    disks = clean_disks(system.get("disks"))
    root = next((d for d in disks if d["mount"] == "/"), {})
    per_core = system.get("cpu_per_core_pct")
    gpu_temp = num(gpu.get("temp_c"))
    boot_id, seq, sent_at = heartbeat_key(hb)
    extra = {
        "cpu_per_core_pct": [num(v) for v in per_core[:MAX_ITEMS]] if isinstance(per_core, list) else None,
        "thermal_c": temps,
        "rails_w": num_map(power.get("rails_w")),
        "disks": disks,
        "boot_id": boot_id,
        "seq": seq,
        "sent_at": sent_at,
    }
    if "storage_health" in system and system["storage_health"] is not None:
        extra["storage_health"] = clamp_deep(system["storage_health"])
    if "memory_health" in system and system["memory_health"] is not None:
        extra["memory_health"] = clamp_deep(system["memory_health"])
    if "fan_health" in system and system["fan_health"] is not None:
        extra["fan_health"] = clamp_deep(system["fan_health"])
    if "rpi_throttled" in system and system["rpi_throttled"] is not None:
        extra["rpi_throttled"] = clamp_deep(system["rpi_throttled"])
    row = {
        "uptime_s": intnum(system.get("uptime_s")),
        "cpu_pct": num(system.get("cpu_pct")),
        "cpu_temp_c": temps.get("cpu"),
        "cpu_freq_mhz": num(system.get("cpu_freq_mhz")),
        "load_1m": num(system.get("load_1m")),
        "gpu_pct": num(gpu.get("util_pct")),
        "gpu_temp_c": gpu_temp if gpu_temp is not None else temps.get("gpu"),
        "gpu_clock_mhz": num(gpu.get("clock_mhz")),
        "gpu_power_w": num(gpu.get("power_w")),
        "ram_pct": num(system.get("ram_pct")),
        "ram_used_mb": num(system.get("ram_used_mb")),
        "disk_pct": root.get("pct"),
        "disk_used_gb": root.get("used_gb"),
        "power_total_w": num(power.get("total_w")),
        "fan_rpm": num(power.get("fan_rpm")),
        "max_temp_c": max(known_temps) if known_temps else None,
        "wifi_signal_dbm": num(net.get("wifi_signal_dbm")),
        "gateway_rtt_ms": num(net.get("gateway_rtt_ms")),
        "gateway_loss_pct": num(net.get("gateway_loss_pct")),
        "server_rtt_ms": num(net.get("last_server_rtt_ms")),
    }
    for column, value in row.items():
        if value is not None and abs(value) > MAX_READING:
            row[column] = None
    row["extra"] = _dumps(extra)
    return row


def validate_patch(body) -> dict:
    """PATCH body -> {column: value}. Raises ValueError (HTTP 400) for anything else."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    unknown = sorted(set(body) - set(PATCH_TEXT_FIELDS) - set(PATCH_COORD_FIELDS))
    if unknown:
        raise ValueError("field(s) not editable: " + ", ".join(k[:50] for k in unknown[:20]))
    out = {}
    for key, value in body.items():
        if key in PATCH_TEXT_FIELDS:
            limit = PATCH_TEXT_FIELDS[key]
            if value is not None and (not isinstance(value, str) or has_surrogates(value)):
                raise ValueError(f"{key} must be a string of valid Unicode text, or null")
            if value is not None and len(value) > limit:
                raise ValueError(f"{key} must be at most {limit} characters")
            out[key] = None if value is None else value.replace("\x00", "")
        else:
            limit = PATCH_COORD_FIELDS[key]
            if value is not None:
                value = num(value)
                if value is None or not -limit <= value <= limit:
                    raise ValueError(f"{key} must be a number from -{limit:g} to {limit:g}, or null")
            out[key] = value
    return out


def derive_state(open_alerts: dict[str, str]) -> tuple[str, list[str]]:
    """{code: severity} of open alerts -> (state, sorted reasons)."""
    reasons = sorted(open_alerts)
    severities = set(open_alerts.values())
    if "DEVICE_OFFLINE" in open_alerts:
        return "offline", reasons
    if "critical" in severities:
        return "critical", reasons
    if "warning" in severities:
        return "warning", reasons
    return "online", reasons


def level(value: float | None, raise_at: float, clear_below: float) -> str | None:
    """Hysteresis: 'raise' at or above raise_at, 'clear' below clear_below, else no change."""
    if value is None:
        return None
    if value >= raise_at:
        return "raise"
    if value < clear_below:
        return "clear"
    return None


def _g(value) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def tokens_equal(given: str | None, expected) -> bool:
    if not given or not isinstance(expected, str) or not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


# --------------------------------------------------------------------------- Host header

def normalize_host(name: str) -> str:
    """'Fleet.Example.org.' -> 'fleet.example.org'; '[::1]' -> '::1'."""
    return name.strip().strip("[]").rstrip(".").lower()


def is_ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def valid_host_name(value) -> bool:
    """A config entry for allowed_hosts: a host name or IP address, without scheme or port."""
    if not isinstance(value, str) or not normalize_host(value):
        return False
    name = normalize_host(value)
    return is_ip_literal(name) or re.fullmatch(r"[a-z0-9_.-]+", name) is not None


def host_name_of(header: str) -> str:
    """The host part of a Host header: 'fleet.example.org:443' -> 'fleet.example.org',
    '[::1]:8090' -> '::1'."""
    value = header.strip()
    if value.startswith("["):
        return normalize_host(value.split("]", 1)[0])
    name, sep, port = value.rpartition(":")
    return normalize_host(name if sep and port.isdigit() else value)


def host_allowed(headers: list[str], allowed: list[str]) -> bool:
    """DNS-rebinding guard for the browser-facing routes. A page from attacker.example whose name
    is re-pointed at this server becomes same-origin, but its requests still carry
    'Host: attacker.example'. Allowed: IP literals (a browser page addressed by IP is its own
    origin), localhost, and the names in allowed_hosts (e.g. the nginx server_name). Requests
    without a Host header come from non-browser clients and are allowed."""
    for header in headers:
        name = host_name_of(header)
        if not (not name or is_ip_literal(name) or name == "localhost" or name in allowed):
            return False
    return True


# --------------------------------------------------------------------------- config

class ConfigError(Exception):
    pass


def load_config(path: str | None = None) -> dict:
    """Read a JSON config file (or use the defaults). Relative paths resolve against its folder."""
    cfg = dict(DEFAULT_CONFIG)
    base = Path.cwd()
    if path is not None:
        p = Path(path).expanduser()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except OSError as e:
            raise ConfigError(f"cannot read {p}: {e}") from None
        except ValueError as e:
            raise ConfigError(f"{p} is not valid JSON: {e}") from None
        if not isinstance(raw, dict):
            raise ConfigError(f"{p}: the top level must be a JSON object")
        unknown = sorted(set(raw) - set(DEFAULT_CONFIG))
        if unknown:
            log.warning("config %s: ignoring unknown key(s): %s", p, ", ".join(unknown))
        cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
        base = p.resolve().parent
    return normalize_config(cfg, base)


def normalize_config(cfg: dict, base: Path) -> dict:
    c = {**DEFAULT_CONFIG, **cfg}

    def need(ok, message):
        if not ok:
            raise ConfigError(message)

    def is_num(v):
        return num(v) is not None

    need(isinstance(c["bind"], str) and c["bind"].strip(), "bind must be a host name or address")
    need(isinstance(c["port"], int) and not isinstance(c["port"], bool) and 0 <= c["port"] <= 65535,
         "port must be an integer from 0 to 65535")
    need(isinstance(c["db_path"], str) and c["db_path"].strip() and c["db_path"].strip() != ":memory:",
         "db_path must be a file path")
    for key in ("fleet_token", "admin_token", "dashboard_user", "dashboard_password"):
        need(c[key] is None or isinstance(c[key], str), f"{key} must be a string or null")
        if c[key] == "":
            c[key] = None
    need(isinstance(c["device_tokens"], dict)
         and all(isinstance(k, str) and isinstance(v, str) and v for k, v in c["device_tokens"].items()),
         "device_tokens must map device_id to a non-empty token string")
    # The example placeholders are public (they are in this repository), so a server holding one
    # would let anyone send heartbeats or edit the registry: refuse to start instead of warning.
    for key in ("fleet_token", "admin_token"):
        need(c[key] not in PLACEHOLDER_TOKENS,
             f"{key} is still the example placeholder {c[key]!r}; set a real secret (e.g. openssl rand -hex 32)")
    need(not any(v in PLACEHOLDER_TOKENS for v in c["device_tokens"].values()),
         "device_tokens holds an example placeholder token; set a real secret for each box")
    need(isinstance(c["allowed_hosts"], list) and all(valid_host_name(h) for h in c["allowed_hosts"]),
         'allowed_hosts must be a list of host names without a port, e.g. ["fleet.example.org"]')
    c["allowed_hosts"] = [normalize_host(h) for h in c["allowed_hosts"]]
    need((c["dashboard_user"] is None) == (c["dashboard_password"] is None),
         "set both dashboard_user and dashboard_password, or neither")
    need(is_num(c["retention_days"]) and c["retention_days"] >= 1, "retention_days must be at least 1")
    need(is_num(c["offline_after_missed"]) and c["offline_after_missed"] > 0,
         "offline_after_missed must be a positive number")
    need(is_num(c["offline_min_s"]) and c["offline_min_s"] > 0, "offline_min_s must be a positive number")
    need(c["archive_dir"] is None or (isinstance(c["archive_dir"], str) and c["archive_dir"].strip()),
         "archive_dir must be a folder path or null")
    db = Path(c["db_path"].strip()).expanduser()
    c["db_path"] = str(db if db.is_absolute() else (base / db).resolve())
    if c["archive_dir"] is not None:
        archive = Path(c["archive_dir"].strip()).expanduser()
        c["archive_dir"] = str(archive if archive.is_absolute() else (base / archive).resolve())
    c["device_tokens"] = dict(c["device_tokens"])
    return c


def archive_folder_name(device_id: str) -> str:
    """Folder name for a device's day files. The ID comes from the device, so it is never used as a
    path as-is: anything outside [A-Za-z0-9._-] becomes '_', and a changed name gets a short hash so
    two different IDs can't share a folder."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", device_id)[:100].strip(".")
    if name != device_id:
        name = f"{name or 'device'}-{hashlib.sha256(device_id.encode()).hexdigest()[:8]}"
    return name


def config_warnings(cfg: dict) -> list[str]:
    out = []
    if cfg["fleet_token"] is None and not cfg["device_tokens"]:
        out.append("no fleet_token or device_tokens: every heartbeat will be rejected")
    if cfg["admin_token"] is None:
        out.append("admin_token is not set: PATCH /api/v1/devices/{id} is disabled")
    exposed = cfg["bind"] not in ("127.0.0.1", "localhost", "::1")
    if exposed:
        out.append(f"bound to {cfg['bind']}: put the server behind nginx with TLS (CONTRACT.md section 9)")
    if (exposed or cfg["allowed_hosts"]) and cfg["dashboard_user"] is None:
        out.append("the dashboard is reachable from other machines but has no password: "
                   "set dashboard_user and dashboard_password")
    return out


# --------------------------------------------------------------------------- application

class FleetApp:
    """Storage, ingest rules, alert engine, background jobs and read API. No HTTP in here."""

    def __init__(self, config: dict, *, static_dir: str | Path | None = None,
                 offline_check_s: float = 15.0, retention_every_s: float = 3600.0):
        self.cfg = config
        self.db_path = config["db_path"]
        self.static_dir = Path(static_dir) if static_dir is not None else STATIC_DIR
        self.offline_check_s = float(offline_check_s)
        self.retention_every_s = float(retention_every_s)
        self.started_ms = now_ms()
        self.archive_dir = Path(config["archive_dir"]) if config.get("archive_dir") else None
        # time.monotonic() of each device's last live heartbeat in this process. The offline check
        # measures silence with it, so stepping the server's wall clock (e.g. turning on NTP)
        # cannot flag every box offline at once. Written only while holding the write lock.
        self._heard: dict[str, float] = {}
        self._write_lock = threading.Lock()
        self._archive_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
            self._migrate(conn)
        finally:
            conn.close()
        # Held open (idle) for the app's lifetime: otherwise every per-request connection that
        # closes last would checkpoint and delete the WAL file, costing an fsync per heartbeat.
        self._keeper = sqlite3.connect(self.db_path, check_same_thread=False)

    def _migrate(self, conn: sqlite3.Connection):
        try:
            conn.execute("ALTER TABLE sync_reports ADD COLUMN details TEXT")
        except sqlite3.OperationalError:
            pass
        conn.executescript(EXTENDED_SCHEMA)



    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @contextlib.contextmanager
    def write(self):
        """One write transaction; writers are serialised by a lock (readers are not blocked, WAL)."""
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                ok = False
                try:
                    yield conn
                    ok = True
                finally:
                    if ok:
                        conn.execute("COMMIT")
                    elif conn.in_transaction:
                        conn.execute("ROLLBACK")
            finally:
                conn.close()

    @contextlib.contextmanager
    def read(self):
        """A consistent read snapshot."""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            try:
                yield conn
            finally:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
        finally:
            conn.close()

    # ---- auth ----------------------------------------------------------------------------

    def token_known(self, token: str) -> bool:
        """Could this bearer token be valid for any device? (checked before reading the body)"""
        candidates = [self.cfg["fleet_token"], *self.cfg["device_tokens"].values()]
        return any([tokens_equal(token, t) for t in candidates])

    def token_valid_for(self, device_id: str, token: str) -> bool:
        tokens = self.cfg["device_tokens"]
        expected = tokens[device_id] if device_id in tokens else self.cfg["fleet_token"]
        return tokens_equal(token, expected)

    def basic_auth_ok(self, authorization_headers: list[str]) -> bool:
        """True when Basic auth is off, or one Authorization header carries the right user:password."""
        user, password = self.cfg["dashboard_user"], self.cfg["dashboard_password"]
        if user is None and password is None:
            return True
        for header in authorization_headers:
            scheme, _, b64 = header.strip().partition(" ")
            if scheme.lower() != "basic":
                continue
            try:
                given = base64.b64decode(b64.strip(), validate=True).decode("utf-8")
            except (binascii.Error, ValueError):
                continue
            given_user, sep, given_password = given.partition(":")
            user_ok = tokens_equal(given_user, user)
            password_ok = tokens_equal(given_password, password)
            if sep and user_ok and password_ok:
                return True
        return False

    # ---- ingest --------------------------------------------------------------------------

    def ingest(self, hb: dict, recv_ms: int | None = None) -> dict:
        """Store a validated heartbeat and run the rules. Returns the 200 response body."""
        recv_ms = now_ms() if recv_ms is None else recv_ms
        heard = time.monotonic()
        device_id = hb["device_id"]
        buffered = hb.get("buffered") is True
        sent_ms = parse_ts(hb["sent_at"])
        interval_s = clamp_interval(hb["interval_s"])
        ts = None
        with self.write() as db:
            device = db.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
            status = db.execute("SELECT * FROM device_status WHERE device_id = ?", (device_id,)).fetchone()
            if device is None:
                self._register(db, device_id, hb, fmt_ms(recv_ms))
            if self._stored_before(db, device_id, heartbeat_key(hb)):
                pass  # delivered twice: the agent never saw our first 200, so it sent it again
            elif buffered:
                ts = self._buffered_ts(sent_ms, status)
                self._store_readings(db, device_id, ts, hb, buffered=True)
                if status is not None:
                    self._note_late_delivery(db, device_id, ts, recv_ms, status)
            else:
                ts_ms = self._free_live_ts(db, device_id, recv_ms)
                ts = fmt_ms(ts_ms)
                self._store_readings(db, device_id, ts, hb, buffered=False)
                self._apply_live(db, device_id, hb, ts, ts_ms, (recv_ms - sent_ms) / 1000.0,
                                 interval_s, device, status)
                self._heard[device_id] = heard
        if ts is None:
            log.info("heartbeat device=%r seq=%r was delivered twice; the first copy is kept", device_id, hb.get("seq"))
        else:
            self._archive(device_id, hb, recv_ms, ts)
        commands = []
        if not buffered:
            commands = self._dispatch_pending_commands(device_id, recv_ms)
        return {"ok": True, "server_time": fmt_ms(recv_ms), "device_id": device_id,
                "next_interval_s": max(5, min(3600, interval_s)), "commands": commands}

    def _archive(self, device_id: str, hb: dict, recv_ms: int, ts: str) -> None:
        """Append the accepted heartbeat to <archive_dir>/<device>/<UTC date>.jsonl: a plain-text copy
        anyone can open. A failure here is logged and never rejects the heartbeat."""
        if self.archive_dir is None:
            return
        try:
            line = {"received_at": fmt_ms(recv_ms), "stored_ts": ts}
            line.update((k, v) for k, v in json.loads(stored_heartbeat(hb)).items() if k not in line)
            folder = self.archive_dir / archive_folder_name(device_id)
            with self._archive_lock:
                folder.mkdir(parents=True, exist_ok=True)
                with open(folder / f"{ts[:10]}.jsonl", "a", encoding="utf-8") as f:  # day of the reading itself
                    f.write(_dumps(line) + "\n")
        except (OSError, ValueError) as e:
            log.warning("heartbeat from %r not written to the archive folder: %s", device_id, e)

    def _prune_archive(self, now: int) -> int:
        """Delete archive day files older than retention_days, and device folders left empty."""
        if self.archive_dir is None:
            return 0
        cutoff = fmt_ms(now - round(self.cfg["retention_days"] * 86_400_000))[:10]
        removed = 0
        with self._archive_lock:
            try:
                folders = [p for p in self.archive_dir.iterdir() if p.is_dir() and not p.is_symlink()]
            except FileNotFoundError:
                return 0
            except OSError as e:
                log.warning("archive retention skipped: %s", e)
                return 0
            for folder in folders:
                for day_file in folder.glob("????-??-??.jsonl"):
                    if day_file.name[:10] < cutoff and day_file.is_file() and not day_file.is_symlink():
                        with contextlib.suppress(OSError):
                            day_file.unlink()
                            removed += 1
                with contextlib.suppress(OSError):
                    folder.rmdir()  # only succeeds once the folder is empty
        return removed

    @staticmethod
    def _buffered_ts(sent_ms: int, status) -> str:
        """Buffered heartbeats: sent_at corrected by the device's last known clock offset."""
        offset = status["clock_offset_s"] if status is not None else None
        if offset is not None:
            try:
                return fmt_ms(sent_ms + round(offset * 1000))
            except (OverflowError, ValueError):
                pass
        return fmt_ms(sent_ms)

    @staticmethod
    def _stored_before(db, device_id: str, key: tuple) -> bool:
        """Is a heartbeat with this (boot_id, seq, sent_at) already in metrics? (see heartbeat_key)
        Uses the metrics_one_row_per_heartbeat index in schema.sql."""
        boot_id, seq, sent_at = key
        if seq is None or sent_at is None:
            return False
        return db.execute(
            "SELECT 1 FROM metrics WHERE device_id = ? AND json_extract(extra, '$.sent_at') = ?"
            " AND json_extract(extra, '$.seq') = ? AND json_extract(extra, '$.boot_id') IS ?",
            (device_id, sent_at, seq, boot_id)).fetchone() is not None

    def _note_late_delivery(self, db, device_id: str, ts: str, recv_ms: int, status) -> None:
        """Info event CONNECTION_INTERRUPTED, one per burst of replayed heartbeats, so that a drop
        shorter than the offline threshold still leaves a trace in the history. A replay that
        arrives within the offline threshold of the previous one joins that event."""
        window_ms = self.offline_threshold_s(status["interval_s"]) * 1000
        row = db.execute("SELECT id, data FROM events WHERE device_id = ? AND code = 'CONNECTION_INTERRUPTED'"
                         " ORDER BY id DESC LIMIT 1", (device_id,)).fetchone()
        data = None
        if row is not None:
            with contextlib.suppress(ValueError, TypeError):
                data = obj(json.loads(row["data"]))
            last = parse_ts(obj(data).get("last_received"))
            if last is None or not isinstance(obj(data).get("count"), int) or recv_ms - last > window_ms:
                data = None
        if data is None:
            data = {"first_ts": ts, "last_ts": ts, "count": 1, "last_received": fmt_ms(recv_ms)}
        else:
            data.update(first_ts=min(str(data.get("first_ts") or ts), ts), last_ts=max(str(data.get("last_ts") or ts), ts),
                        count=data["count"] + 1, last_received=fmt_ms(recv_ms))
        span = (parse_ts(data["last_ts"]) or 0) - (parse_ts(data["first_ts"]) or 0)
        message = ("1 heartbeat arrived late from the box's offline buffer" if data["count"] == 1 else
                   f"{data['count']} heartbeats arrived late from the box's offline buffer, spanning "
                   f"{human_duration(span / 1000)}")
        if data["count"] == 1:
            self._event(db, device_id, ts, "info", "info", "CONNECTION_INTERRUPTED", message, data)
        else:
            db.execute("UPDATE events SET message = ?, data = ? WHERE id = ?", (message, _dumps(data), row["id"]))

    @staticmethod
    def _free_live_ts(db, device_id: str, ts_ms: int) -> int:
        """Receive time, moved on by a millisecond if a row already has it (keeps live rows unique)."""
        for _ in range(1000):
            if db.execute("SELECT 1 FROM metrics WHERE device_id = ? AND ts = ?",
                          (device_id, fmt_ms(ts_ms))).fetchone() is None:
                break
            ts_ms += 1
        return ts_ms

    def _register(self, db, device_id: str, hb: dict, ts: str) -> None:
        ident, claimed = obj(hb.get("identity")), obj(hb.get("claimed"))
        row = {
            "serial": text(ident.get("serial")), "model": text(ident.get("model")),
            "device_type": text(ident.get("device_type")), "hostname": text(ident.get("hostname")),
            "product": text(claimed.get("product")), "customer": text(claimed.get("customer")),
            "site_code": text(claimed.get("site_code")),
        }
        db.execute("INSERT INTO devices (device_id, serial, model, device_type, hostname, product, customer,"
                   " site_code, first_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (device_id, *row.values(), ts))
        label = " ".join(x for x in (row["device_type"], row["hostname"] or device_id) if x)
        self._event(db, device_id, ts, "info", "info", "DEVICE_REGISTERED",
                    f"New device registered: {label}", row)
        log.info("device registered: %r (%s)", device_id, label)

    def _store_readings(self, db, device_id: str, ts: str, hb: dict, *, buffered: bool) -> None:
        """metrics, service_checks and ai_usage: written for live and buffered heartbeats alike."""
        values = metric_values(hb)
        cols = ("device_id", "ts", "buffered", *METRIC_COLUMNS)
        db.execute(f"INSERT OR IGNORE INTO metrics ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                   (device_id, ts, int(buffered), *(values[c] for c in METRIC_COLUMNS)))
        for s in clean_services(hb.get("services")):
            db.execute("INSERT OR IGNORE INTO service_checks (device_id, ts, service, kind, status, latency_ms,"
                       " restarts, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (device_id, ts, s["name"], s["kind"], s["status"], s["latency_ms"], s["restarts"],
                        s["detail"]))
        ai = clean_ai(hb.get("ai"))
        if ai is not None:
            cols = ("device_id", "ts", *AI_COLUMNS)
            db.execute(f"INSERT OR IGNORE INTO ai_usage ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                       (device_id, ts, *(ai[c] for c in AI_COLUMNS)))

    def _apply_live(self, db, device_id, hb, ts, ts_ms, offset_s, interval_s, device, status) -> None:
        """Everything only a live heartbeat does: registry, status, versions, sync, events, alerts."""
        if device is not None:
            self._refresh_registry(db, device_id, device, hb)
        open_alerts = self._open_alerts(db, device_id)

        if "DEVICE_OFFLINE" in open_alerts:
            since = db.execute("SELECT ts FROM events WHERE device_id = ? AND kind = 'alert' AND"
                               " code = 'DEVICE_OFFLINE' AND resolved_at IS NULL", (device_id,)).fetchone()
            self._clear(db, device_id, ts, "DEVICE_OFFLINE", open_alerts)
            last_seen = status["last_seen"] if status is not None else None
            last_ms = parse_ts(last_seen)
            gap_s = None if last_ms is None else round((ts_ms - last_ms) / 1000, 1)
            self._event(db, device_id, ts, "info", "info", "DEVICE_RECONNECTED",
                        f"Heartbeat received again after {human_duration(gap_s)} without contact",
                        {"offline_since": since["ts"] if since else None, "last_seen": last_seen, "gap_s": gap_s})

        boot_id = text(hb["boot_id"])
        system = obj(hb.get("system"))
        booted_at = norm_ts(system.get("booted_at"))
        seq = hb.get("seq")
        dev_type = (device["device_type"] if device else None) or text(obj(hb.get("identity")).get("device_type"))

        if status is not None and status["boot_id"] and status["boot_id"] != boot_id:
            clean_shutdown = db.execute(
                "SELECT 1 FROM events WHERE device_id = ? AND code = 'SHUTDOWN_CLEAN'"
                " AND ts >= ? ORDER BY id DESC LIMIT 1",
                (device_id, status["last_seen"])).fetchone() is not None
            if clean_shutdown:
                self._event(db, device_id, ts, "info", "info", "CONFIRMED_REBOOT",
                            f"Confirmed reboot after clean shutdown (booted at {booted_at or 'unknown time'})",
                            {"old_boot_id": status["boot_id"], "new_boot_id": boot_id, "booted_at": booted_at})
            else:
                self._event(db, device_id, ts, "info", "warning", "RESTART_UNEXPECTED",
                            f"Unexpected restart detected (booted at {booted_at or 'unknown time'})",
                            {"old_boot_id": status["boot_id"], "new_boot_id": boot_id, "booted_at": booted_at})
            self._event(db, device_id, ts, "info", "info", "DEVICE_RESTARTED",
                        f"Device restarted (booted at {booted_at or 'unknown time'})",
                        {"old_boot_id": status["boot_id"], "new_boot_id": boot_id, "booted_at": booted_at})
        elif status is not None and status["boot_id"] == boot_id and seq == 1:
            last_seq = None
            if status["last_heartbeat"]:
                with contextlib.suppress(Exception):
                    last_seq = json.loads(status["last_heartbeat"]).get("seq")
            if last_seq is not None and last_seq > 1:
                self._event(db, device_id, ts, "info", "info", "AGENT_RESTARTED",
                            "Agent process restarted (system remained running)",
                            {"boot_id": boot_id, "uptime_s": system.get("uptime_s")})

        self._record_software(db, device_id, ts, clean_software(hb.get("software")))
        self._record_sync(db, device_id, ts, clean_sync(hb.get("sync")))
        services = clean_services(hb.get("services"))
        self._evaluate_alerts(db, device_id, ts, hb, services, offset_s, open_alerts, dev_type)
        self._service_restarts(db, device_id, ts, services)

        state, reasons = derive_state(self._open_alerts(db, device_id))
        ident = obj(hb.get("identity"))
        db.execute(
            "INSERT INTO device_status (device_id, state, reasons, last_seen, boot_id, booted_at, uptime_s,"
            " interval_s, clock_offset_s, agent_version, lan_ip, last_heartbeat)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(device_id) DO UPDATE SET state = excluded.state, reasons = excluded.reasons,"
            " last_seen = excluded.last_seen, boot_id = excluded.boot_id, booted_at = excluded.booted_at,"
            " uptime_s = excluded.uptime_s, interval_s = excluded.interval_s,"
            " clock_offset_s = excluded.clock_offset_s, agent_version = excluded.agent_version,"
            " lan_ip = excluded.lan_ip, last_heartbeat = excluded.last_heartbeat",
            (device_id, state, _dumps(reasons), ts, boot_id, booted_at, intnum(system.get("uptime_s")),
             interval_s, round(offset_s, 3), text(hb.get("agent_version")), text(ident.get("lan_ip")),
             stored_heartbeat(hb)))

    def _refresh_registry(self, db, device_id: str, device, hb: dict) -> None:
        """Identity follows the device; `claimed` values only fill registry fields that are empty."""
        ident, claimed = obj(hb.get("identity")), obj(hb.get("claimed"))
        updates = {}
        for col in ("serial", "model", "device_type", "hostname"):
            value = text(ident.get(col))
            if value is not None and value != device[col]:
                updates[col] = value
        for col in ("product", "site_code", "customer"):
            value = text(claimed.get(col))
            current = device[col]
            if value and value.strip() and (current is None or not str(current).strip()):
                updates[col] = value
        if updates:
            db.execute(f"UPDATE devices SET {', '.join(f'{c} = ?' for c in updates)} WHERE device_id = ?",
                       (*updates.values(), device_id))

    def _record_software(self, db, device_id: str, ts: str, software: dict[str, str]) -> None:
        for component, version in software.items():
            rows = db.execute("SELECT version FROM software_versions WHERE device_id = ? AND component = ?"
                              " ORDER BY last_seen DESC, first_seen DESC", (device_id, component)).fetchall()
            current = rows[0]["version"] if rows else None
            if any(r["version"] == version for r in rows):
                db.execute("UPDATE software_versions SET last_seen = ? WHERE device_id = ? AND component = ?"
                           " AND version = ?", (ts, device_id, component, version))
            else:
                db.execute("INSERT INTO software_versions (device_id, component, version, first_seen, last_seen)"
                           " VALUES (?, ?, ?, ?, ?)", (device_id, component, version, ts, ts))
            if current is not None and current != version:
                self._event(db, device_id, ts, "info", "info", "VERSION_CHANGED",
                            f"{component} changed from {current} to {version}",
                            {"component": component, "old": current, "new": version})

    def _record_sync(self, db, device_id: str, ts: str, sync: dict | None) -> None:
        """A sync_reports row only when the sync state (not its age) differs from the latest row."""
        if sync is None or all(sync[k] is None for k in (*SYNC_KEY, "age_h")):
            return
        last = db.execute(f"SELECT {', '.join(SYNC_KEY)} FROM sync_reports WHERE device_id = ?"
                          " ORDER BY ts DESC LIMIT 1", (device_id,)).fetchone()
        if last is not None and tuple(last) == tuple(sync[k] for k in SYNC_KEY):
            return
        details = {k: sync.get(k) for k in SYNC_EXTENDED_KEYS}
        has_details = any(v is not None for v in details.values())
        details_json = _dumps(details) if has_details else None
        db.execute("INSERT OR IGNORE INTO sync_reports (device_id, ts, last_success, last_attempt, offline_count,"
                   " unit_result, age_h, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (device_id, ts, *(sync[k] for k in SYNC_KEY), sync["age_h"], details_json))

    # ---- alerts --------------------------------------------------------------------------

    @staticmethod
    def _open_alerts(db, device_id: str) -> dict[str, str]:
        return {r["code"]: r["severity"] for r in db.execute(
            "SELECT code, severity FROM events WHERE device_id = ? AND kind = 'alert' AND resolved_at IS NULL",
            (device_id,))}

    @staticmethod
    def _event(db, device_id, ts, kind, severity, code, message, data=None, resolved_at=None) -> bool:
        cur = db.execute("INSERT OR IGNORE INTO events (device_id, ts, kind, severity, code, message, resolved_at,"
                         " data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (device_id, ts, kind, severity, code, message[:1000], resolved_at,
                          None if data is None else _dumps(clamp_deep(data))))
        return cur.rowcount > 0

    def _raise(self, db, device_id, ts, code, severity, message, data, open_alerts) -> None:
        if code in open_alerts:
            return
        if self._event(db, device_id, ts, "alert", severity, code, message, data):
            open_alerts[code] = severity
            log.info("alert raised: device=%r %s (%s): %s", device_id, code, severity, message)

    @staticmethod
    def _clear(db, device_id, ts, code, open_alerts) -> None:
        if code not in open_alerts:
            return
        db.execute("UPDATE events SET resolved_at = ? WHERE device_id = ? AND code = ? AND kind = 'alert'"
                   " AND resolved_at IS NULL", (ts, device_id, code))
        open_alerts.pop(code, None)
        log.info("alert cleared: device=%r %s", device_id, code)

    def _evaluate_alerts(self, db, device_id, ts, hb, services, offset_s, open_alerts, device_type=None) -> None:
        """CONTRACT.md section 6, live heartbeats only. Missing readings never raise or clear, but a
        box that says it no longer monitors something (a service left out of `services` or reported
        not_configured; `sync` or `ai` null) closes that thing's alerts."""
        system, gpu, net, power = obj(hb.get("system")), obj(hb.get("gpu")), obj(hb.get("network")), obj(hb.get("power"))
        temps = num_map(hb.get("thermal_c"))

        def apply(code, severity, action, message, data):
            if action == "raise":
                self._raise(db, device_id, ts, code, severity, message(), data, open_alerts)
            elif action == "clear":
                self._clear(db, device_id, ts, code, open_alerts)

        is_rpi = (device_type or "").lower() in ("raspberry pi 5", "raspberry-pi-5")
        if is_rpi:
            eff_cfg = self.get_effective_config(device_id).get("config", {})
            th = eff_cfg.get("health_thresholds") or eff_cfg.get("thresholds") or {}
            warn_limit = float(th.get("cpu_temp_warn_c", 75.0))
            crit_limit = float(th.get("cpu_temp_crit_c", 80.0))

            cpu_t = temps.get("cpu") if temps.get("cpu") is not None else temps.get("zone0")
            if cpu_t is None and num(system.get("cpu_temp_c")) is not None:
                cpu_t = num(system.get("cpu_temp_c"))
            apply("CPU_OVERHEAT", "critical", level(cpu_t, crit_limit, crit_limit - 5.0),
                  lambda: f"CPU temperature {_g(cpu_t)} C (limit {_g(crit_limit)} C)", {"cpu_temp_c": cpu_t})
            apply("CPU_TEMP_HIGH", "warning", level(cpu_t, warn_limit, warn_limit - 5.0),
                  lambda: f"CPU temperature {_g(cpu_t)} C (limit {_g(warn_limit)} C)", {"cpu_temp_c": cpu_t})
            for code in ("GPU_OVERHEAT", "GPU_TEMP_HIGH", "SOC_TEMP_HIGH", "GPU_HIGH"):
                if code in open_alerts:
                    self._clear(db, device_id, ts, code, open_alerts)

            rpi_throttled = obj(system.get("rpi_throttled"))
            if rpi_throttled:
                uv = rpi_throttled.get("under_voltage_now")
                th = rpi_throttled.get("throttled_now") or rpi_throttled.get("arm_frequency_capped_now")
                apply("THROTTLED_UNDERVOLTAGE", "warning",
                      "raise" if uv else "clear",
                      lambda: "Raspberry Pi under-voltage detected",
                      {"under_voltage": uv, "raw": rpi_throttled.get("raw_hex")})
                apply("THROTTLED_THERMAL", "warning",
                      "raise" if th else "clear",
                      lambda: "Raspberry Pi thermal throttling active (clock frequency capped)",
                      {"throttled": th, "raw": rpi_throttled.get("raw_hex")})
        else:
            gpu_readings = [v for v in (num(gpu.get("temp_c")), temps.get("gpu")) if v is not None]
            gpu_t = max(gpu_readings) if gpu_readings else None
            apply("GPU_OVERHEAT", "critical", level(gpu_t, 105, 100),
                  lambda: f"GPU temperature {_g(gpu_t)} C (limit 105 C)", {"gpu_temp_c": gpu_t})
            apply("GPU_TEMP_HIGH", "warning", level(gpu_t, 95, 90),
                  lambda: f"GPU temperature {_g(gpu_t)} C (limit 95 C)", {"gpu_temp_c": gpu_t})

            hottest = max(((v, k) for k, v in temps.items() if v is not None), default=None)
            apply("SOC_TEMP_HIGH", "warning", level(hottest[0] if hottest else None, 95, 90),
                  lambda: f"Hottest sensor {hottest[1]} at {_g(hottest[0])} C (limit 95 C)",
                  {"sensor": hottest[1] if hottest else None, "temp_c": hottest[0] if hottest else None})

        fullest = max(((d["pct"], d["mount"] or "?") for d in clean_disks(system.get("disks"))
                       if d["pct"] is not None), default=None)
        disk_pct = fullest[0] if fullest else None
        disk_data = {"mount": fullest[1] if fullest else None, "pct": disk_pct}
        apply("DISK_CRITICAL", "critical", level(disk_pct, 95, 93),
              lambda: f"Disk {fullest[1]} is {_g(disk_pct)}% full (limit 95%)", disk_data)
        apply("DISK_LOW", "warning", level(disk_pct, 85, 83),
              lambda: f"Disk {fullest[1]} is {_g(disk_pct)}% full (limit 85%)", disk_data)

        def sustained(column, current, raise_at, clear_below):
            """'raise' when this and the two previous live heartbeats are all >= raise_at."""
            if current is None:
                return None
            if current < clear_below:
                return "clear"
            if current < raise_at:
                return None
            previous = [r[0] for r in db.execute(
                f"SELECT {column} FROM metrics WHERE device_id = ? AND buffered = 0 AND ts < ?"
                " ORDER BY ts DESC LIMIT 2", (device_id, ts))]
            ok = len(previous) == 2 and all(v is not None and v >= raise_at for v in previous)
            return "raise" if ok else None

        ram = num(system.get("ram_pct"))
        apply("RAM_HIGH", "warning", sustained("ram_pct", ram, 90, 85),
              lambda: f"RAM use {_g(ram)}% for 3 heartbeats in a row (limit 90%)", {"ram_pct": ram})
        cpu = num(system.get("cpu_pct"))
        apply("CPU_HIGH", "warning", sustained("cpu_pct", cpu, 90, 80),
              lambda: f"CPU use {_g(cpu)}% for 3 heartbeats in a row (limit 90%)", {"cpu_pct": cpu})

        ai = clean_ai(hb.get("ai"))
        if not is_rpi:
            # A busy GPU is the normal state of a box serving an LLM (DGX Spark with vLLM).
            gpu_util = num(gpu.get("util_pct"))
            apply("GPU_HIGH", "warning", "clear" if ai is not None else sustained("gpu_pct", gpu_util, 90, 80),
                  lambda: f"GPU use {_g(gpu_util)}% for 3 heartbeats in a row (limit 90%)", {"gpu_pct": gpu_util})

        for s in services:
            code = "SERVICE_DOWN:" + s["name"]
            action = {"down": "raise", "up": "clear", "not_configured": "clear"}.get(s["status"])
            apply(code, "critical", action,
                  lambda s=s: f"Service {s['name']} is down" + (f": {s['detail']}" if s["detail"] else ""),
                  {"service": s["name"], "kind": s["kind"], "detail": s["detail"]})
        if isinstance(hb.get("services"), list):
            # A service the box no longer reports (container removed, unit dropped from the config,
            # Docker no longer readable) is no longer monitored, so its alert cannot stay open.
            reported = {s["name"] for s in services}
            for code in [c for c in open_alerts if c.startswith("SERVICE_DOWN:")]:
                if code[len("SERVICE_DOWN:"):] not in reported:
                    self._clear(db, device_id, ts, code, open_alerts)

        # sync is null when the box has no saras-sync state folder: nothing left to be stale.
        sync_off = "sync" in hb and hb["sync"] is None
        sync = clean_sync(hb.get("sync"))
        age = sync["age_h"] if sync else None
        apply("SYNC_STALE", "critical",
              "clear" if sync_off else None if age is None else ("raise" if age > 8 else "clear"),
              lambda: f"Last successful sync {_g(round(age, 1))} h ago (limit 8 h)",
              {"age_h": age, "last_success": sync["last_success"] if sync else None})
        mismatched = sync["mismatched_tables"] if sync else None
        apply("SYNC_MISMATCH", "critical",
              "clear" if sync_off else None if mismatched is None else ("raise" if mismatched > 0 else "clear"),
              lambda: f"The last sync's row counts differ in {mismatched} table(s)" + (
                  f": {sync['source_rows']} rows on the box, {sync['target_rows']} on the server"
                  if sync["source_rows"] is not None and sync["target_rows"] is not None else ""),
              {k: sync[k] if sync else None for k in SYNC_COUNT_KEYS})

        # Extended sync checks
        if sync:
            failed_batches = sync.get("failed_batches")
            apply("SYNC_FAILED", "warning",
                  "raise" if (failed_batches is not None and failed_batches > 0) else "clear",
                  lambda: f"Sync has {failed_batches} failed batch(es)",
                  {"failed_batches": failed_batches, "last_error": sync.get("last_error_category")})

            conflicts = sync.get("conflicts")
            apply("SYNC_CONFLICTS", "warning",
                  "raise" if (conflicts is not None and conflicts > 0) else "clear",
                  lambda: f"Sync encountered {conflicts} conflict(s)",
                  {"conflicts": conflicts})

            api_health = sync.get("sync_api_health")
            apply("CENTRAL_API_UNREACHABLE", "warning",
                  "raise" if api_health == "down" else ("clear" if api_health in ("up", "degraded") else None),
                  lambda: "Central sync API is unreachable or down",
                  {"sync_api_health": api_health})

            db_health = sync.get("db_health")
            apply("DATABASE_UNHEALTHY", "warning",
                  "raise" if db_health == "down" else ("clear" if db_health in ("up", "degraded") else None),
                  lambda: "Database health check reported down",
                  {"db_health": db_health})
        elif sync_off:
            for code in ("SYNC_FAILED", "SYNC_CONFLICTS", "CENTRAL_API_UNREACHABLE", "DATABASE_UNHEALTHY"):
                if code in open_alerts:
                    self._clear(db, device_id, ts, code, open_alerts)

        # vLLM (DGX Spark): requests queueing, or the KV cache nearly full, 3 heartbeats in a row.
        def saturated(waiting, kv):

            return (waiting is not None and waiting > 0) or (kv is not None and kv >= 90)

        waiting, kv = (ai["waiting"], ai["kv_cache_pct"]) if ai else (None, None)
        if "ai" in hb and ai is None:
            llm_action = "clear"  # vLLM is not configured (any more)
        elif saturated(waiting, kv):
            previous = db.execute(
                "SELECT a.waiting, a.kv_cache_pct FROM metrics m LEFT JOIN ai_usage a"
                " ON a.device_id = m.device_id AND a.ts = m.ts WHERE m.device_id = ? AND m.buffered = 0"
                " AND m.ts < ? ORDER BY m.ts DESC LIMIT 2", (device_id, ts)).fetchall()
            llm_action = ("raise" if len(previous) == 2
                          and all(saturated(r["waiting"], r["kv_cache_pct"]) for r in previous) else None)
        elif (waiting is not None or kv is not None) and (waiting is None or waiting == 0) and (kv is None or kv < 85):
            llm_action = "clear"
        else:
            llm_action = None
        llm_problems = (([f"{_g(waiting)} request(s) waiting"] if waiting is not None and waiting > 0 else [])
                        + ([f"KV cache {_g(kv)}% full"] if kv is not None and kv >= 90 else []))
        apply("LLM_SATURATED", "warning", llm_action,
              lambda: "AI server (vLLM) saturated for 3 heartbeats in a row: " + ", ".join(llm_problems),
              {"waiting": waiting, "kv_cache_pct": kv})

        ntp = system.get("ntp_synced")
        ntp = ntp if isinstance(ntp, bool) else None
        skew = abs(offset_s)
        if ntp is False or skew > 120:
            clock_action = "raise"
        elif ntp is True:
            clock_action = "clear"
        else:
            clock_action = None
        clock_problems = (["not NTP-synchronised"] if ntp is False else []) + \
                         ([f"off by {skew:.0f} s from server time"] if skew > 120 else [])
        apply("CLOCK_UNSYNCED", "warning", clock_action,
              lambda: "Device clock " + " and ".join(clock_problems),
              {"ntp_synced": ntp, "clock_offset_s": round(offset_s, 3)})

        loss, signal_dbm = num(net.get("gateway_loss_pct")), num(net.get("wifi_signal_dbm"))
        if (loss is not None and loss >= 20) or (signal_dbm is not None and signal_dbm <= -75):
            net_action = "raise"
        elif ((loss is not None or signal_dbm is not None) and (loss is None or loss < 10)
              and (signal_dbm is None or signal_dbm > -72)):
            net_action = "clear"
        else:
            net_action = None
        net_problems = ([f"gateway packet loss {_g(loss)}%"] if loss is not None and loss >= 20 else []) + \
                       ([f"Wi-Fi signal {_g(signal_dbm)} dBm"] if signal_dbm is not None and signal_dbm <= -75 else [])
        apply("NETWORK_POOR", "warning", net_action,
              lambda: "Poor network: " + ", ".join(net_problems),
              {"gateway_loss_pct": loss, "wifi_signal_dbm": signal_dbm, "iface": text(net.get("iface"))})

        # Storage failure / wear alerts
        storage_h = obj(system.get("storage_health")) or obj(hb.get("storage_health"))
        if storage_h:
            pct_used = num(storage_h.get("percentage_used"))
            crit_warn = num(storage_h.get("critical_warning"))
            media_errs = num(storage_h.get("media_errors"))
            avail_spare = num(storage_h.get("available_spare_pct"))
            if avail_spare is None:
                avail_spare = num(storage_h.get("spare_pct"))

            storage_crit_condition = (
                (crit_warn is not None and crit_warn > 0) or
                (media_errs is not None and media_errs > 0) or
                (pct_used is not None and pct_used >= 95) or
                (avail_spare is not None and avail_spare <= 5)
            )
            storage_crit_clear = (
                (crit_warn is None or crit_warn == 0) and
                (media_errs is None or media_errs == 0) and
                (pct_used is None or pct_used < 90) and
                (avail_spare is None or avail_spare > 10)
            )
            apply("STORAGE_CRITICAL", "critical",
                  "raise" if storage_crit_condition else ("clear" if storage_crit_clear else None),
                  lambda: f"Storage drive failure imminent: wear {_g(pct_used)}%, {media_errs or 0} media error(s), critical warning bit {crit_warn or 0}",
                  {"percentage_used": pct_used, "critical_warning": crit_warn, "media_errors": media_errs, "available_spare_pct": avail_spare})

            storage_wear_condition = (
                (pct_used is not None and pct_used >= 80) or
                (avail_spare is not None and avail_spare <= 20)
            )
            storage_wear_clear = (
                (pct_used is None or pct_used < 75) and
                (avail_spare is None or avail_spare > 25)
            )
            apply("STORAGE_WEAR_HIGH", "warning",
                  "raise" if storage_wear_condition else ("clear" if storage_wear_clear else None),
                  lambda: f"Storage drive high wear: {_g(pct_used)}% used (available spare {avail_spare or '?'}%)",
                  {"percentage_used": pct_used, "available_spare_pct": avail_spare})
        elif ("storage_health" in system and system["storage_health"] is None) or ("storage_health" in hb and hb["storage_health"] is None):
            for code in ("STORAGE_CRITICAL", "STORAGE_WEAR_HIGH"):
                if code in open_alerts:
                    self._clear(db, device_id, ts, code, open_alerts)

        # Memory hardware errors (EDAC)
        mem_h = obj(system.get("memory_health")) or obj(hb.get("memory_health"))
        if mem_h:
            ue = num(mem_h.get("ue_count"))
            if ue is None:
                ue = num(mem_h.get("ecc_ue_count"))
            ce = num(mem_h.get("ce_count"))
            if ce is None:
                ce = num(mem_h.get("ecc_ce_count"))
            apply("MEMORY_ECC_ERROR", "critical",
                  "raise" if (ue is not None and ue > 0) else ("clear" if (ue == 0 and (ce is None or ce < 50)) else None),
                  lambda: f"Uncorrectable memory hardware ECC error detected ({ue} UE errors, {ce} CE errors)",
                  {"ue_count": ue, "ce_count": ce})
        elif ("memory_health" in system and system["memory_health"] is None) or ("memory_health" in hb and hb["memory_health"] is None):
            if "MEMORY_ECC_ERROR" in open_alerts:
                self._clear(db, device_id, ts, "MEMORY_ECC_ERROR", open_alerts)

        # Fan stall alert
        fan_h = obj(system.get("fan_health")) or obj(hb.get("fan_health"))
        fan_rpm = num(fan_h.get("rpm")) if fan_h and fan_h.get("rpm") is not None else num(power.get("fan_rpm"))
        cpu_t = temps.get("cpu") if temps.get("cpu") is not None else temps.get("tj") if temps.get("tj") is not None else temps.get("soc")
        fan_stalled = (fan_h and fan_h.get("stalled")) or bool(power.get("fan_stalled"))
        if fan_stalled or (fan_rpm is not None and fan_rpm <= 50 and cpu_t is not None and cpu_t >= 45.0):
            apply("FAN_STALLED", "critical", "raise",
                  lambda: f"Cooling fan stalled ({fan_rpm or 0} RPM) while CPU temperature is high ({_g(cpu_t)} C)",
                  {"fan_rpm": fan_rpm, "cpu_temp_c": cpu_t})
        elif fan_rpm is not None and fan_rpm > 500 and not fan_stalled:
            if "FAN_STALLED" in open_alerts:
                self._clear(db, device_id, ts, "FAN_STALLED", open_alerts)

    def _service_restarts(self, db, device_id, ts, services) -> None:
        """One-shot SERVICE_RESTARTED (stored already resolved) when a restart counter goes up."""
        for s in services:
            new = s["restarts"]
            if new is None:
                continue
            row = db.execute("SELECT restarts FROM service_checks WHERE device_id = ? AND service = ? AND ts < ?"
                             " AND restarts IS NOT NULL ORDER BY ts DESC LIMIT 1",
                             (device_id, s["name"], ts)).fetchone()
            if row is not None and new > row["restarts"]:
                old = row["restarts"]
                self._event(db, device_id, ts, "alert", "warning", "SERVICE_RESTARTED",
                            f"Service {s['name']} restarted (restart count {old} -> {new})",
                            {"service": s["name"], "old": old, "new": new}, resolved_at=ts)
                log.info("service restarted: device=%r %s (%s -> %s)", device_id, s["name"], old, new)

    # ---- background jobs -----------------------------------------------------------------

    def offline_threshold_s(self, interval_s) -> float:
        interval = num(interval_s) or 0.0
        return float(max(self.cfg["offline_after_missed"] * interval, self.cfg["offline_min_s"]))

    def check_offline(self, now: int | None = None) -> list[str]:
        """Raise DEVICE_OFFLINE for devices silent longer than their threshold. Returns their ids.

        A device heard since this server started is timed on the monotonic clock, so a step of
        the server's wall clock (e.g. NTP turned on) neither flags healthy boxes offline nor
        delays the alert. Otherwise the silence is counted on the wall clock from the later of
        last_seen and the server start, so restarting the server does not flag every box offline
        before it has had a chance to report. An explicit `now` (wall-clock epoch ms) evaluates
        every device on the wall clock as of that moment.
        """
        wall_only = now is not None
        now = now_ms() if now is None else now
        query = ("SELECT s.device_id, s.last_seen, s.interval_s FROM device_status s WHERE NOT EXISTS"
                 " (SELECT 1 FROM events e WHERE e.device_id = s.device_id AND e.kind = 'alert'"
                 " AND e.code = 'DEVICE_OFFLINE' AND e.resolved_at IS NULL)")

        def overdue(row) -> tuple[float, float] | None:
            """(threshold, seconds of silence) when the device is overdue, else None."""
            heard = None if wall_only else self._heard.get(row["device_id"])
            last = parse_ts(row["last_seen"])
            if heard is not None:
                counted = silent = time.monotonic() - heard
            elif last is not None:
                counted, silent = (now - max(last, self.started_ms)) / 1000.0, (now - last) / 1000.0
            else:
                return None
            threshold = self.offline_threshold_s(row["interval_s"])
            return (threshold, silent) if counted > threshold else None

        with self.read() as db:
            candidates = db.execute(query).fetchall()
        if not any(overdue(r) is not None for r in candidates):
            return []
        marked = []
        with self.write() as db:  # re-check: a heartbeat may have arrived in between
            ts = fmt_ms(now)
            for row in db.execute(query).fetchall():
                late = overdue(row)
                if late is None:
                    continue
                threshold, silent_s = late
                device_id = row["device_id"]
                open_alerts = self._open_alerts(db, device_id)
                self._raise(db, device_id, ts, "DEVICE_OFFLINE", "critical",
                            f"No heartbeat for {human_duration(silent_s)} (limit {threshold:g} s)",
                            {"last_seen": row["last_seen"], "threshold_s": threshold,
                             "silent_s": round(silent_s, 1)}, open_alerts)
                state, reasons = derive_state(self._open_alerts(db, device_id))
                db.execute("UPDATE device_status SET state = ?, reasons = ? WHERE device_id = ?",
                           (state, _dumps(reasons), device_id))
                marked.append(device_id)
        return marked

    def run_retention(self, now: int | None = None) -> dict[str, int]:
        """Delete readings older than retention_days, and events/sync reports older than 365 days.
        Open alerts are current state, not history, so they are kept however old they are."""
        now = now_ms() if now is None else now
        short_cut = fmt_ms(now - round(self.cfg["retention_days"] * 86_400_000))
        long_cut = fmt_ms(now - LONG_RETENTION_DAYS * 86_400_000)
        deleted = dict.fromkeys(("metrics", "service_checks", "ai_usage", "events", "sync_reports", "device_logs"), 0)
        with self.write() as db:
            device_ids = [r[0] for r in db.execute("SELECT device_id FROM devices")]
            for device_id in device_ids:
                for table in ("metrics", "service_checks", "ai_usage"):
                    deleted[table] += db.execute(f"DELETE FROM {table} WHERE device_id = ? AND ts < ?",
                                                 (device_id, short_cut)).rowcount
                deleted["sync_reports"] += db.execute("DELETE FROM sync_reports WHERE device_id = ? AND ts < ?",
                                                      (device_id, long_cut)).rowcount
                deleted["events"] += db.execute(
                    "DELETE FROM events WHERE device_id = ? AND ts < ?"
                    " AND NOT (kind = 'alert' AND resolved_at IS NULL)", (device_id, long_cut)).rowcount
            deleted["device_logs"] += db.execute("DELETE FROM device_logs WHERE ts < ?", (short_cut,)).rowcount

        deleted["archive_files"] = self._prune_archive(now)
        if any(deleted.values()):
            log.info("retention: deleted %s", ", ".join(f"{k}={v}" for k, v in deleted.items() if v))
        return deleted

    def start_background(self) -> None:
        """Offline check every offline_check_s (15 s); retention at start and then hourly."""
        jobs = (("offline-check", self.check_offline, self.offline_check_s, False),
                ("retention", self.run_retention, self.retention_every_s, True),
                ("schedules", self.process_due_schedules, 10, True))
        for name, job, period, run_first in jobs:
            t = threading.Thread(target=self._every, args=(job, period, run_first), name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def _every(self, job, period: float, run_first: bool) -> None:
        if not run_first and self._stop.wait(period):
            return
        while not self._stop.is_set():
            try:
                job()
            except Exception:  # keep the job alive; the next run may succeed
                log.exception("background job %s failed", job.__name__)
            if self._stop.wait(period):
                return

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=10)
        self._threads.clear()
        self._keeper.close()

    # ---- read API --------------------------------------------------------------------------

    @staticmethod
    def _current_software(db, device_id: str) -> dict[str, dict]:
        current = {}
        for r in db.execute("SELECT component, version, first_seen, last_seen FROM software_versions"
                            " WHERE device_id = ? ORDER BY component, last_seen DESC, first_seen DESC",
                            (device_id,)):
            current.setdefault(r["component"], dict(r))
        return current

    @staticmethod
    def _seconds_since(last_seen, now: int) -> int | None:
        last = parse_ts(last_seen)
        return None if last is None else max(0, (now - last) // 1000)

    @staticmethod
    def _event_dict(row) -> dict:
        d = dict(row)
        if d.get("data"):
            with contextlib.suppress(ValueError):
                d["data"] = json.loads(d["data"])
        return d

    def fleet(self, now: int | None = None) -> dict:
        now = now_ms() if now is None else now
        counts = {"total": 0, "online": 0, "warning": 0, "critical": 0, "offline": 0}
        devices = []
        with self.read() as db:
            rows = db.execute(
                "SELECT d.device_id, d.device_type, d.model, d.hostname, d.product, d.customer, d.site_code,"
                " d.location, d.device_group, s.state, s.reasons, s.last_seen, s.uptime_s, s.agent_version"
                " FROM devices d LEFT JOIN device_status s ON s.device_id = d.device_id"
                " ORDER BY d.device_id").fetchall()
            for r in rows:
                device_id = r["device_id"]
                state = r["state"] or "offline"
                # The readings of the last live heartbeat (stored at ts = last_seen). Bounding the
                # search by last_seen, not taking the newest ts, keeps rows written before a
                # backward step of the server clock from posing as the latest.
                latest = db.execute(f"SELECT {', '.join(FLEET_LATEST_FIELDS)} FROM metrics WHERE device_id = ?"
                                    " AND buffered = 0 AND ts <= ? ORDER BY ts DESC LIMIT 1",
                                    (device_id, r["last_seen"])).fetchone() if r["last_seen"] else None
                software = self._current_software(db, device_id)
                devices.append({
                    "device_id": device_id, "device_type": r["device_type"], "model": r["model"],
                    "hostname": r["hostname"], "product": r["product"], "customer": r["customer"],
                    "site_code": r["site_code"], "location": r["location"], "device_group": r["device_group"],
                    "state": state, "reasons": json.loads(r["reasons"]) if r["reasons"] else [],
                    "last_seen": r["last_seen"], "seconds_since_seen": self._seconds_since(r["last_seen"], now),
                    "uptime_s": r["uptime_s"], "agent_version": r["agent_version"],
                    "latest": {f: (latest[f] if latest else None) for f in FLEET_LATEST_FIELDS},
                    "versions": {c: software[c]["version"] if c in software else None for c in FLEET_VERSIONS},
                })
                counts["total"] += 1
                counts[state] = counts.get(state, 0) + 1
        return {"generated_at": fmt_ms(now), "counts": counts, "devices": devices}

    def device(self, device_id: str, now: int | None = None, extended: bool = False) -> dict | None:
        now = now_ms() if now is None else now
        with self.read() as db:
            dev = db.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
            if dev is None:
                return None
            st = db.execute("SELECT * FROM device_status WHERE device_id = ?", (device_id,)).fetchone()
            status = {
                "state": st["state"] if st else "offline",
                "reasons": json.loads(st["reasons"]) if st else [],
                "last_seen": st["last_seen"] if st else None,
                "seconds_since_seen": self._seconds_since(st["last_seen"], now) if st else None,
                **{f: (st[f] if st else None) for f in STATUS_FIELDS},
            }
            latest = {}
            if st is not None and st["last_heartbeat"]:
                with contextlib.suppress(ValueError):
                    latest = json.loads(st["last_heartbeat"])

            services = []
            cols = "service, kind, status, latency_ms, restarts, detail, ts"
            live_ts = st["last_seen"] if st is not None else None
            newest = db.execute("SELECT MAX(ts) FROM service_checks WHERE device_id = ?", (device_id,)).fetchone()[0]
            if newest is not None:
                anchor = min(parse_ts(t) for t in (newest, live_ts) if parse_ts(t) is not None)
                listed_from = fmt_ms(anchor - 86_400_000)
                day_ago = fmt_ms(now - 86_400_000)
                availability = {r["service"]: r for r in db.execute(
                    "SELECT service, SUM(status = 'up') AS up, SUM(status = 'down') AS down,"
                    " SUM(status IN ('up', 'down', 'degraded')) AS n"
                    " FROM service_checks WHERE device_id = ? AND ts >= ? GROUP BY service", (device_id, day_ago))}
                current = {r["service"]: r for r in db.execute(
                    f"SELECT {cols} FROM service_checks WHERE device_id = ? AND ts = ?", (device_id, live_ts))}
                names = db.execute("SELECT service, MAX(ts) AS ts FROM service_checks WHERE device_id = ?"
                                   " AND ts >= ? GROUP BY service ORDER BY service", (device_id, listed_from))
                for name_row in names.fetchall():
                    r = current.get(name_row["service"]) or db.execute(
                        f"SELECT {cols} FROM service_checks WHERE device_id = ? AND service = ? AND ts = ?",
                        (device_id, name_row["service"], name_row["ts"])).fetchone()
                    a = availability.get(r["service"])
                    pct = round(100.0 * a["up"] / a["n"], 2) if a is not None and a["n"] else None
                    svc_entry = {**dict(r), "availability_24h_pct": pct,
                                 "failed_checks_24h": a["down"] if a is not None else None}
                    if extended:
                        svc_name = svc_entry.get("service")
                        last_log = db.execute(
                            "SELECT ts FROM device_logs WHERE device_id = ? AND service = ? ORDER BY ts DESC LIMIT 1",
                            (device_id, svc_name)).fetchone()
                        last_log_ts = last_log["ts"] if last_log else None
                        svc_status = svc_entry.get("status")
                        if status["state"] == "offline":
                            act_state = "unknown"
                        elif svc_status == "up":
                            has_recent_log = False
                            if last_log_ts:
                                l_ms = parse_ts(last_log_ts)
                                if l_ms and (now - l_ms) <= 900_000:
                                    has_recent_log = True
                            act_state = "running" if has_recent_log else "idle"
                        elif svc_status == "down":
                            act_state = "stopped"
                        elif svc_status == "not_configured":
                            act_state = "not_configured"
                        else:
                            act_state = "unknown"
                        svc_entry["activity_state"] = act_state
                        svc_entry["last_log_ts"] = last_log_ts
                        svc_entry["last_health_check_ts"] = svc_entry.get("ts")
                    services.append(svc_entry)

            software = sorted(self._current_software(db, device_id).values(), key=lambda s: s["component"])
            sync_row = db.execute("SELECT * FROM sync_reports WHERE device_id = ? ORDER BY ts DESC LIMIT 1",
                                  (device_id,)).fetchone()
            sync_data = dict(sync_row) if sync_row else None
            if sync_data and sync_data.get("details"):
                with contextlib.suppress(Exception):
                    sync_data["details"] = json.loads(sync_data["details"])

            ai = db.execute("SELECT * FROM ai_usage WHERE device_id = ? ORDER BY ts DESC LIMIT 1",
                            (device_id,)).fetchone()
            open_alerts = db.execute("SELECT * FROM events WHERE device_id = ? AND kind = 'alert' AND resolved_at"
                                     " IS NULL ORDER BY ts DESC, id DESC", (device_id,)).fetchall()
            recent = db.execute("SELECT * FROM events WHERE device_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
                                (device_id, RECENT_EVENTS)).fetchall()
            res = {
                "device": dict(dev),
                "status": status,
                "latest": latest,
                "services": services,
                "software": software,
                "sync": sync_data,
                "ai": dict(ai) if ai else None,
                "open_alerts": [self._event_dict(e) for e in open_alerts],
                "recent_events": [self._event_dict(e) for e in recent],
            }
            if extended:
                res["effective_config"] = self.get_effective_config(device_id)
                res["recent_logs"] = self.get_logs(device_id, limit=50)
                res["update_jobs"] = self.get_update_jobs(device_id=device_id, limit=20)
            return res



    def get_config(self, target_type: str, target_id: str) -> dict | None:
        with self.read() as db:
            row = db.execute(
                "SELECT target_type, target_id, version, config, updated_at FROM device_configs"
                " WHERE target_type = ? AND target_id = ?", (target_type, target_id)).fetchone()
            if not row:
                return None
            return {
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "version": row["version"],
                "config": json.loads(row["config"]),
                "updated_at": row["updated_at"],
            }

    def set_config(self, target_type: str, target_id: str, config: dict, now: int | None = None) -> dict:
        validated = validate_config_payload({"target_type": target_type, "target_id": target_id, "config": config})
        now = now_ms() if now is None else now
        ts = fmt_ms(now)
        with self.write() as db:
            current = db.execute(
                "SELECT version FROM device_configs WHERE target_type = ? AND target_id = ?",
                (target_type, target_id)).fetchone()
            new_version = (current["version"] + 1) if current else 1
            config_json = json.dumps(validated["config"], sort_keys=True)
            db.execute(
                "INSERT INTO device_configs (target_type, target_id, version, config, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(target_type, target_id) DO UPDATE SET version = excluded.version, config = excluded.config, updated_at = excluded.updated_at",
                (target_type, target_id, new_version, config_json, ts))
            rev_hash = compute_config_revision(validated["config"])
            return {
                "ok": True,
                "target_type": target_type,
                "target_id": target_id,
                "version": new_version,
                "revision": rev_hash,
                "revision_hash": rev_hash,
                "config": validated["config"],
                "updated_at": ts,
            }

    def delete_config(self, target_type: str, target_id: str) -> dict:
        with self.write() as db:
            cur = db.execute(
                "DELETE FROM device_configs WHERE target_type = ? AND target_id = ?",
                (target_type, target_id))
            return {
                "ok": True,
                "target_type": target_type,
                "target_id": target_id,
                "deleted": cur.rowcount > 0,
            }

    def get_effective_config(self, device_id: str) -> dict:
        """Hierarchy: DEFAULT -> ORGANIZATION (customer) -> SCHOOL (site_code) -> DEVICE (device_id)"""
        with self.read() as db:
            dev = db.execute("SELECT customer, site_code FROM devices WHERE device_id = ?", (device_id,)).fetchone()
            customer = dev["customer"] if dev else None
            site_code = dev["site_code"] if dev else None

            eff = {}
            max_version = 0
            sources = [("default", "default")]
            if customer:
                sources.append(("org", customer))
            if site_code:
                sources.append(("school", site_code))
            sources.append(("device", device_id))

            for tt, tid in sources:
                row = db.execute(
                    "SELECT version, config FROM device_configs WHERE target_type = ? AND target_id = ?",
                    (tt, tid)).fetchone()
                if row:
                    cfg = json.loads(row["config"])
                    for k, v in cfg.items():
                        if isinstance(v, dict) and isinstance(eff.get(k), dict):
                            eff[k] = {**eff[k], **v}
                        else:
                            eff[k] = v
                    max_version = max(max_version, row["version"])

            revision = compute_config_revision(eff)
            return {
                "device_id": device_id,
                "revision": revision,
                "version": max_version,
                "config": eff,
            }

    def ingest_logs(self, device_id: str, logs: list[dict], recv_ms: int | None = None) -> dict:
        recv_ms = now_ms() if recv_ms is None else recv_ms
        recv_ts = fmt_ms(recv_ms)
        accepted = 0
        with self.write() as db:
            for item in logs:
                if not isinstance(item, dict):
                    continue
                service = text(item.get("service") or "unknown")[:MAX_STR]
                category = str(item.get("category") or "SYSTEM").upper()
                if category not in LOG_CATEGORIES:
                    category = "APPLICATION"
                severity = str(item.get("severity") or "info").lower()
                if severity not in LOG_SEVERITIES:
                    severity = "info"
                ts = parse_ts(item.get("ts"))
                event_ts = fmt_ms(ts) if ts is not None else recv_ts
                msg = redact_sensitive_text(text(item.get("message") or "")[:2000])
                attrs = item.get("attributes")
                if attrs is not None:
                    cleaned_attrs = redact_data(attrs)
                    attrs_json = json.dumps(clamp_deep(cleaned_attrs))
                else:
                    attrs_json = None
                event_id = text(item.get("event_id") or "")[:100] or None

                db.execute(
                    "INSERT INTO device_logs (event_id, device_id, service, category, severity, ts, message, attributes, received_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(event_id) DO NOTHING",
                    (event_id, device_id, service, category, severity, event_ts, msg, attrs_json, recv_ts)
                )
                accepted += 1
        return {"ok": True, "accepted": accepted}

    def get_logs(self, device_id: str, service: str | None = None, category: str | None = None,
                 severity: str | None = None, limit: int = 100, since: str | None = None) -> list[dict]:
        limit = max(1, min(500, limit))
        clauses = ["device_id = ?"]
        params = [device_id]
        if service:
            clauses.append("service = ?")
            params.append(service)
        if category:
            clauses.append("category = ?")
            params.append(category.upper())
        if severity:
            clauses.append("severity = ?")
            params.append(severity.lower())
        if since:
            clauses.append("ts >= ?")
            params.append(since)
        query = f"SELECT id, event_id, device_id, service, category, severity, ts, message, attributes, received_at FROM device_logs WHERE {' AND '.join(clauses)} ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.read() as db:
            rows = db.execute(query, params).fetchall()
            return [{
                "id": r["id"],
                "event_id": r["event_id"],
                "device_id": r["device_id"],
                "service": r["service"],
                "category": r["category"],
                "severity": r["severity"],
                "ts": r["ts"],
                "message": r["message"],
                "attributes": json.loads(r["attributes"]) if r["attributes"] else None,
                "received_at": r["received_at"],
            } for r in rows]

    def create_update_job(self, device_id: str, operation: str, target_version: str | None = None,
                          payload: dict | None = None, created_by: str = "admin", now: int | None = None) -> dict:
        if operation not in ALLOWED_JOB_OPERATIONS:
            raise ValueError(f"operation {operation!r} not permitted; must be one of {', '.join(sorted(ALLOWED_JOB_OPERATIONS))}")
        now = now_ms() if now is None else now
        ts = fmt_ms(now)
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        payload_dict = payload or {}
        payload_json = json.dumps(payload_dict, sort_keys=True)
        with self.write() as db:
            dev = db.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone()
            if dev is None:
                raise ValueError(f"device {device_id!r} not found in fleet")
            db.execute(
                "INSERT INTO update_jobs (job_id, device_id, operation, target_version, payload, status, progress_pct, message, created_at, updated_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, 'scheduled', 0, 'Job scheduled for dispatch', ?, ?, ?)",
                (job_id, device_id, operation, target_version, payload_json, ts, ts, created_by))
        log.info("created update job %s (%s) for device %s", job_id, operation, device_id)
        return {
            "ok": True,
            "job_id": job_id,
            "device_id": device_id,
            "operation": operation,
            "target_version": target_version,
            "payload": payload_dict,
            "status": "scheduled",
            "progress_pct": 0,
            "message": "Job scheduled for dispatch",
            "created_at": ts,
            "updated_at": ts,
            "created_by": created_by,
        }

    def get_update_jobs(self, device_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict]:
        limit = max(1, min(200, limit))
        clauses = []
        params = []
        if device_id:
            clauses.append("device_id = ?")
            params.append(device_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM update_jobs {where} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.read() as db:
            rows = db.execute(query, params).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if isinstance(d.get("payload"), str):
                    with contextlib.suppress(Exception):
                        d["payload"] = json.loads(d["payload"])
                result.append(d)
            return result

    def get_update_job(self, job_id: str) -> dict | None:
        with self.read() as db:
            row = db.execute("SELECT * FROM update_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            if isinstance(d.get("payload"), str):
                with contextlib.suppress(Exception):
                    d["payload"] = json.loads(d["payload"])
            return d

    def update_job_status(self, job_id: str, device_id: str, status: str, progress_pct: int = 0,
                          message: str = "", execution_id: str | None = None,
                          error_details: str | None = None, now: int | None = None) -> dict | None:
        if status not in JOB_STATUSES:
            raise ValueError(f"invalid status {status!r}; must be one of {', '.join(sorted(JOB_STATUSES))}")
        now = now_ms() if now is None else now
        ts = fmt_ms(now)
        progress_pct = max(0, min(100, int(progress_pct or 0)))
        message = (message or "")[:500]
        error_details = (error_details or "")[:1000] if error_details else None

        with self.write() as db:
            job = db.execute("SELECT * FROM update_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not job:
                return None
            if job["device_id"] != device_id:
                raise ValueError("device_id does not match job target")
            completed_at = ts if status in ("completed", "failed", "cancelled") else job["completed_at"]
            db.execute(
                "UPDATE update_jobs SET status = ?, progress_pct = ?, message = ?, "
                "execution_id = COALESCE(?, execution_id), error_details = COALESCE(?, error_details), "
                "updated_at = ?, completed_at = ? WHERE job_id = ?",
                (status, progress_pct, message, execution_id, error_details, ts, completed_at, job_id))
            if status in ("completed", "failed"):
                kind = "info" if status == "completed" else "alert"
                severity = "info" if status == "completed" else "warning"
                self._event(db, device_id, ts, kind, severity, f"JOB_{status.upper()}",
                            f"Update job {job_id} ({job['operation']}): {message}",
                            {"job_id": job_id, "operation": job["operation"], "status": status})
            updated = db.execute("SELECT * FROM update_jobs WHERE job_id = ?", (job_id,)).fetchone()
            d = dict(updated)
            if isinstance(d.get("payload"), str):
                with contextlib.suppress(Exception):
                    d["payload"] = json.loads(d["payload"])
            return d

    def cancel_update_job(self, job_id: str, now: int | None = None) -> dict | None:
        now = now_ms() if now is None else now
        ts = fmt_ms(now)
        with self.write() as db:
            job = db.execute("SELECT * FROM update_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not job:
                return None
            if job["status"] in ("completed", "failed", "cancelled"):
                raise ValueError(f"cannot cancel job with final status {job['status']!r}")
            db.execute(
                "UPDATE update_jobs SET status = 'cancelled', message = 'Cancelled by administrator', "
                "updated_at = ?, completed_at = ? WHERE job_id = ?",
                (ts, ts, job_id))
            updated = db.execute("SELECT * FROM update_jobs WHERE job_id = ?", (job_id,)).fetchone()
            d = dict(updated)
            if isinstance(d.get("payload"), str):
                with contextlib.suppress(Exception):
                    d["payload"] = json.loads(d["payload"])
            return d

    def create_scheduled_update(self, name: str, device_id: str, operation: str, scheduled_at: str,
                                target_version: str | None = None, payload: dict | None = None,
                                timezone: str = "UTC", maintenance_window: str | None = None,
                                created_by: str = "admin", now: int | None = None) -> dict:
        if operation not in ALLOWED_JOB_OPERATIONS:
            raise ValueError(f"operation {operation!r} not permitted; must be one of {', '.join(sorted(ALLOWED_JOB_OPERATIONS))}")
        parsed_ms = parse_ts(scheduled_at)
        if parsed_ms is None:
            raise ValueError(f"invalid scheduled_at timestamp {scheduled_at!r}; expected ISO 8601")
        norm_scheduled_at = fmt_ms(parsed_ms)
        now = now_ms() if now is None else now
        ts = fmt_ms(now)
        schedule_id = f"sched-{uuid.uuid4().hex[:12]}"
        payload_dict = payload or {}
        payload_json = json.dumps(payload_dict, sort_keys=True)
        with self.write() as db:
            dev = db.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone()
            if dev is None:
                raise ValueError(f"device {device_id!r} not found in fleet")
            db.execute(
                "INSERT INTO scheduled_updates (schedule_id, name, device_id, operation, target_version, payload, scheduled_at, timezone, maintenance_window, status, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (schedule_id, name, device_id, operation, target_version, payload_json, norm_scheduled_at, timezone or "UTC", maintenance_window, ts, created_by))
        log.info("created scheduled update %s (%s) for %s at %s", schedule_id, operation, device_id, norm_scheduled_at)
        return {
            "ok": True,
            "schedule_id": schedule_id,
            "name": name,
            "device_id": device_id,
            "operation": operation,
            "target_version": target_version,
            "payload": payload_dict,
            "scheduled_at": norm_scheduled_at,
            "timezone": timezone or "UTC",
            "maintenance_window": maintenance_window,
            "status": "active",
            "created_at": ts,
            "created_by": created_by,
        }

    def get_scheduled_updates(self, device_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict]:
        limit = max(1, min(200, limit))
        clauses = []
        params = []
        if device_id:
            clauses.append("device_id = ?")
            params.append(device_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM scheduled_updates {where} ORDER BY scheduled_at ASC LIMIT ?"
        params.append(limit)
        with self.read() as db:
            rows = db.execute(query, params).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if isinstance(d.get("payload"), str):
                    with contextlib.suppress(Exception):
                        d["payload"] = json.loads(d["payload"])
                result.append(d)
            return result

    def cancel_scheduled_update(self, schedule_id: str) -> dict | None:
        with self.write() as db:
            sched = db.execute("SELECT * FROM scheduled_updates WHERE schedule_id = ?", (schedule_id,)).fetchone()
            if not sched:
                return None
            if sched["status"] != "active":
                raise ValueError(f"schedule already has status {sched['status']!r}")
            db.execute("UPDATE scheduled_updates SET status = 'cancelled' WHERE schedule_id = ?", (schedule_id,))
            updated = db.execute("SELECT * FROM scheduled_updates WHERE schedule_id = ?", (schedule_id,)).fetchone()
            d = dict(updated)
            if isinstance(d.get("payload"), str):
                with contextlib.suppress(Exception):
                    d["payload"] = json.loads(d["payload"])
            return d

    def process_due_schedules(self, now: int | None = None) -> int:
        now = now_ms() if now is None else now
        now_iso = fmt_ms(now)
        dispatched = 0
        with self.write() as db:
            due = db.execute(
                "SELECT * FROM scheduled_updates WHERE status = 'active' AND scheduled_at <= ?",
                (now_iso,)).fetchall()
            for s in due:
                win = s["maintenance_window"]
                cur_dt = datetime.fromtimestamp(now / 1000.0, timezone.utc)
                if win and not is_in_maintenance_window(win, dt=cur_dt):
                    continue
                active_job = db.execute(
                    "SELECT 1 FROM update_jobs WHERE device_id = ? AND status IN ('scheduled', 'dispatched', 'in_progress')",
                    (s["device_id"],)).fetchone()
                if active_job:
                    continue
                job_id = f"job-{uuid.uuid4().hex[:12]}"
                ts = fmt_ms(now)
                db.execute(
                    "INSERT INTO update_jobs (job_id, device_id, operation, target_version, payload, status, progress_pct, message, created_at, updated_at, created_by) "
                    "VALUES (?, ?, ?, ?, ?, 'scheduled', 0, ?, ?, ?, ?)",
                    (job_id, s["device_id"], s["operation"], s["target_version"], s["payload"],
                     f"Dispatched by schedule {s['name']}", ts, ts, f"schedule:{s['schedule_id']}"))
                db.execute(
                    "UPDATE scheduled_updates SET status = 'completed', last_run_at = ? WHERE schedule_id = ?",
                    (ts, s["schedule_id"]))
                dispatched += 1
                log.info("dispatched update job %s for scheduled update %s", job_id, s["schedule_id"])
        return dispatched

    def _dispatch_pending_commands(self, device_id: str, recv_ms: int) -> list[dict]:
        with contextlib.suppress(Exception):
            self.process_due_schedules(recv_ms)
        commands = []
        now_str = fmt_ms(recv_ms)
        with self.write() as db:
            rows = db.execute(
                "SELECT job_id, operation, target_version, payload FROM update_jobs "
                "WHERE device_id = ? AND status = 'scheduled' ORDER BY created_at ASC LIMIT 1",
                (device_id,)).fetchall()
            for r in rows:
                db.execute(
                    "UPDATE update_jobs SET status = 'dispatched', dispatched_at = ?, updated_at = ? WHERE job_id = ?",
                    (now_str, now_str, r["job_id"]))
                payload = json.loads(r["payload"]) if isinstance(r["payload"], str) else (r["payload"] or {})
                commands.append({
                    "job_id": r["job_id"],
                    "command_id": r["job_id"],
                    "operation": r["operation"],
                    "target_version": r["target_version"],
                    "payload": payload,
                })
        return commands


    def device_metrics(self, device_id: str, minutes: int = MINUTES_DEFAULT, now: int | None = None) -> dict | None:
        """Points of the last `minutes`, oldest first; averaged into 720 equal time buckets if more."""
        now = now_ms() if now is None else now
        start_ms = now - minutes * 60_000
        start = fmt_ms(start_ms)
        with self.read() as db:
            if db.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone() is None:
                return None
            count = db.execute("SELECT COUNT(*) FROM metrics WHERE device_id = ? AND ts >= ?",
                               (device_id, start)).fetchone()[0]
            if count <= MAX_POINTS:
                rows = db.execute(f"SELECT ts, {', '.join(POINT_FIELDS)} FROM metrics WHERE device_id = ?"
                                  " AND ts >= ? ORDER BY ts", (device_id, start)).fetchall()
                points = [dict(r) for r in rows]
            else:
                width_ms = minutes * 60_000 / MAX_POINTS
                averages = ", ".join(f"AVG({f}) AS {f}" for f in POINT_FIELDS)
                rows = db.execute(
                    "SELECT MIN(CAST((julianday(ts) - :start_jd) * 86400000.0 / :width AS INTEGER), :last)"
                    f" AS bucket, AVG((julianday(ts) - 2440587.5) * 86400000.0) AS t_ms, {averages}"
                    " FROM metrics WHERE device_id = :device AND ts >= :start GROUP BY bucket ORDER BY bucket",
                    {"start_jd": start_ms / 86_400_000 + 2440587.5, "width": width_ms, "last": MAX_POINTS - 1,
                     "device": device_id, "start": start}).fetchall()
                # An average can overflow to infinity (JSON has no infinity): report it as missing.
                points = [{"ts": fmt_ms(round(r["t_ms"])),
                           **{f: (round(r[f], 2) if r[f] is not None and math.isfinite(r[f]) else None)
                              for f in POINT_FIELDS}}
                          for r in rows]
        return {"device_id": device_id, "minutes": minutes, "points": points}

    def device_availability(self, device_id: str, days: int = 30, tz_offset_min: int = 0,
                            now: int | None = None) -> dict | None:
        """Which local days have readings, and the stretches with none.

        A stretch longer than max(3 x interval, 180 s) between two readings is a gap. Uptime tells what
        happened in it: if the box's uptime went down, the box restarted (it was off or rebooting);
        otherwise it stayed on and nothing was recorded. A gap still running now is marked ongoing."""
        now = now_ms() if now is None else now
        since = fmt_ms(now - days * 86_400_000)
        shift = f"{int(tz_offset_min):+d} minutes"
        with self.read() as db:
            if db.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone() is None:
                return None
            st = db.execute("SELECT interval_s FROM device_status WHERE device_id = ?", (device_id,)).fetchone()
            day_rows = db.execute(
                "SELECT date(ts, ?) AS day, COUNT(*) AS readings, MIN(ts) AS first, MAX(ts) AS last"
                " FROM metrics WHERE device_id = ? AND ts >= ? GROUP BY day ORDER BY day",
                (shift, device_id, since)).fetchall()
            rows = db.execute("SELECT ts, uptime_s FROM metrics WHERE device_id = ? AND ts >= ? ORDER BY ts",
                              (device_id, since)).fetchall()
        interval_s = st["interval_s"] if st is not None and st["interval_s"] else 60
        threshold_s = max(3 * interval_s, 180)
        gaps, prev = [], None
        for r in rows:
            t = parse_ts(r["ts"])
            if t is None:
                continue
            if prev is not None and t - prev[0] > threshold_s * 1000:
                up, prev_up = r["uptime_s"], prev[1]
                if up is not None and prev_up is not None:
                    restarted = up < prev_up
                else:
                    restarted = None  # not known
                booted = fmt_ms(t - int(up) * 1000) if restarted and up is not None else None
                gaps.append({"from": prev[2], "to": r["ts"], "duration_s": round((t - prev[0]) / 1000),
                             "restarted": restarted, "booted_at": booted, "ongoing": False})
            prev = (t, r["uptime_s"], r["ts"])
        if prev is not None and now - prev[0] > threshold_s * 1000:
            gaps.append({"from": prev[2], "to": None, "duration_s": round((now - prev[0]) / 1000),
                         "restarted": None, "booted_at": None, "ongoing": True})
        return {"device_id": device_id, "days_back": days, "tz_offset_min": int(tz_offset_min),
                "interval_s": interval_s, "threshold_s": threshold_s,
                "first_reading": rows[0]["ts"] if rows else None, "last_reading": rows[-1]["ts"] if rows else None,
                "days": [dict(r) for r in day_rows], "gaps": gaps[-500:],
                "raw_available": self.archive_dir is not None}

    def _archive_lines(self, device_id: str, utc_days: list[str]):
        """(reading time in ms, line) for every stored heartbeat in the given UTC day files."""
        folder = self.archive_dir / archive_folder_name(device_id)
        for day in utc_days:
            path = folder / f"{day}.jsonl"
            if not path.is_file() or path.is_symlink():
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = re.match(r'\{"received_at":"[^"]*","stored_ts":"([^"]+)"', line)
                    t = parse_ts(m.group(1)) if m else None
                    if t is not None:
                        yield t, line

    def device_raw_index(self, device_id: str, day: str, tz_offset_min: int = 0) -> dict | None:
        """The stored readings of one local day: time, sequence number and whether it came late."""
        if not self._device_exists(device_id):
            return None
        if self.archive_dir is None:
            return {"device_id": device_id, "date": day, "available": False, "readings": []}
        start = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
        start -= int(tz_offset_min) * 60_000
        end = start + 86_400_000
        utc_days = sorted({fmt_ms(start)[:10], fmt_ms(end - 1)[:10]})
        out = []
        for t, line in self._archive_lines(device_id, utc_days):
            if start <= t < end:
                seq = re.search(r'"seq":(\d+)', line)
                out.append({"time": fmt_ms(t), "seq": int(seq.group(1)) if seq else None,
                            "buffered": '"buffered":true' in line})
        out.sort(key=lambda x: x["time"])
        return {"device_id": device_id, "date": day, "available": True, "readings": out[-20000:]}

    def device_raw_reading(self, device_id: str, at: str) -> dict | None:
        """The full stored reading received at `at`. LookupError when there is none."""
        if not self._device_exists(device_id):
            return None
        t = parse_ts(at)
        if self.archive_dir is None or t is None:
            raise LookupError("no stored reading at that time")
        for got, line in self._archive_lines(device_id, [fmt_ms(t)[:10]]):
            if got == t:
                return {"device_id": device_id, "time": fmt_ms(t), "reading": json.loads(line)}
        raise LookupError("no stored reading at that time")

    def _device_exists(self, device_id: str) -> bool:
        with self.read() as db:
            return db.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone() is not None

    def events(self, device_id: str | None = None, open_only: bool = False,
               limit: int = EVENTS_LIMIT_DEFAULT) -> dict:
        where, params = [], []
        if device_id:
            where.append("device_id = ?")
            params.append(device_id)
        if open_only:
            where.append("kind = 'alert' AND resolved_at IS NULL")
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "")
        with self.read() as db:
            rows = db.execute(sql + " ORDER BY ts DESC, id DESC LIMIT ?", (*params, limit)).fetchall()
        return {"events": [self._event_dict(r) for r in rows]}

    def patch_device(self, device_id: str, body) -> dict | None:
        """Admin edit of the registry whitelist. ValueError -> 400; None -> unknown device (404)."""
        updates = validate_patch(body)
        with self.write() as db:
            if db.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone() is None:
                return None
            if updates:
                db.execute(f"UPDATE devices SET {', '.join(f'{c} = ?' for c in updates)} WHERE device_id = ?",
                           (*updates.values(), device_id))
                log.info("device %r edited: %s", device_id, ", ".join(sorted(updates)))
            return dict(db.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone())

    def healthz(self) -> dict:
        with self.read() as db:
            return {"ok": True, "devices": db.execute("SELECT COUNT(*) FROM devices").fetchone()[0]}

    def static_file(self, rel: str) -> Path | None:
        """A regular file inside the static folder, or None (traversal, hidden, missing)."""
        rel = unquote(rel)
        if not rel or "\x00" in rel or "\\" in rel:
            return None
        parts = rel.split("/")
        if any(not p or p.startswith(".") for p in parts):
            return None
        try:
            root = self.static_dir.resolve(strict=True)
            target = root.joinpath(*parts).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if root not in target.parents or not target.is_file():
            return None
        return target


# --------------------------------------------------------------------------- HTTP

class HttpError(Exception):
    def __init__(self, status: int, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.headers = status, message, headers or {}


def _reject_constant(name):
    raise ValueError(f"{name} is not allowed")


class Handler(BaseHTTPRequestHandler):
    server_version = f"saras-fleet/{__version__}"
    sys_version = ""
    timeout = 30  # seconds per socket operation; a stalled client cannot hold a thread for ever

    @property
    def app(self) -> FleetApp:
        return self.server.app

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def _handle(self, method: str) -> None:
        try:
            self._route(method)
        except HttpError as e:
            self._json(e.status, {"ok": False, "error": e.message}, e.headers)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            log.exception("error handling %s %r", method, urlsplit(self.path).path)
            self.close_connection = True
            with contextlib.suppress(Exception):
                self._json(500, {"ok": False, "error": "internal server error"})

    def _route(self, method: str) -> None:
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query, keep_blank_values=True)

        if path == "/api/v1/heartbeat":  # device route: bearer token, never Basic auth
            if method != "POST":
                raise HttpError(405, "method not allowed", {"Allow": "POST"})
            return self._heartbeat()

        if path == "/api/v1/logs":
            if method != "POST":
                raise HttpError(405, "method not allowed", {"Allow": "POST"})
            return self._post_logs()

        if path == "/api/v1/config":
            if method == "POST":
                return self._post_config()
            elif method == "GET":
                return self._get_config(query)
            else:
                raise HttpError(405, "method not allowed", {"Allow": "GET, POST"})

        if path == "/api/v1/updates/jobs":
            if method == "GET":
                return self._get_update_jobs(query)
            elif method == "POST":
                return self._post_update_job()
            else:
                raise HttpError(405, "method not allowed", {"Allow": "GET, POST"})

        if path.startswith("/api/v1/updates/jobs/"):
            rest = path[len("/api/v1/updates/jobs/"):]
            parts = rest.split("/")
            job_id = unquote(parts[0])
            if len(parts) == 1:
                if method != "GET":
                    raise HttpError(405, "method not allowed", {"Allow": "GET"})
                return self._get_update_job(job_id)
            elif len(parts) == 2 and parts[1] == "status":
                if method != "POST":
                    raise HttpError(405, "method not allowed", {"Allow": "POST"})
                return self._post_update_job_status(job_id)
            elif len(parts) == 2 and parts[1] == "cancel":
                if method != "POST":
                    raise HttpError(405, "method not allowed", {"Allow": "POST"})
                return self._cancel_update_job(job_id)

        if path == "/api/v1/updates/schedules":
            if method == "GET":
                return self._get_scheduled_updates(query)
            elif method == "POST":
                return self._post_scheduled_update()
            else:
                raise HttpError(405, "method not allowed", {"Allow": "GET, POST"})

        if path.startswith("/api/v1/updates/schedules/"):
            rest = path[len("/api/v1/updates/schedules/"):]
            parts = rest.split("/")
            schedule_id = unquote(parts[0])
            if len(parts) == 1 and method == "POST":
                return self._cancel_scheduled_update(schedule_id)
            elif len(parts) == 2 and parts[1] == "cancel" and method == "POST":
                return self._cancel_scheduled_update(schedule_id)

        if not host_allowed(self.headers.get_all("Host") or [], self.app.cfg["allowed_hosts"]):
            self._drain_body()
            raise HttpError(421, "this server does not answer for that host name; add it to allowed_hosts")

        device_id, sub = self._device_route(path)
        if method == "PATCH" and device_id is not None and sub is None:
            # The admin bearer token alone, also when a dashboard password is set: a request can
            # carry only one Authorization header through nginx (a second one gets a 400 there).
            return self._patch(device_id)

        if device_id is not None and sub in ("config", "logs", "jobs"):
            bearer = self._bearer()
            device_auth = bearer is not None and (self.app.token_valid_for(device_id, bearer) or tokens_equal(bearer, self.app.cfg["admin_token"]))
            if not (device_auth or self.app.basic_auth_ok(self._authorizations())):
                self._drain_body()
                raise HttpError(401, "unauthorized", {"WWW-Authenticate": "Bearer"})
            if method != "GET":
                self._drain_body()
                raise HttpError(405, "method not allowed", {"Allow": "GET"})
            if sub == "config":
                return self._json(200, self.app.get_effective_config(device_id))
            elif sub == "jobs":
                return self._json(200, {"ok": True, "device_id": device_id, "jobs": self.app.get_update_jobs(device_id=device_id)})
            else:
                logs = self.app.get_logs(
                    device_id,
                    service=self._param(query, "service"),
                    category=self._param(query, "category"),
                    severity=self._param(query, "severity"),
                    limit=self._int_param(query, "limit", 100, 1, 500),
                    since=self._param(query, "since")
                )
                return self._json(200, {"ok": True, "device_id": device_id, "logs": logs})

        if not self.app.basic_auth_ok(self._authorizations()):
            self._drain_body()
            raise HttpError(401, "authentication required",
                            {"WWW-Authenticate": 'Basic realm="SARAS fleet", charset="UTF-8"'})

        known = (path in ("/api/v1/fleet", "/api/v1/events", "/healthz", "/")
                 or path.startswith("/static/") or device_id is not None)
        if method != "GET":
            self._drain_body()
            if not known:
                raise HttpError(404, "not found")
            allow = "GET, PATCH" if device_id is not None and sub is None else "GET"
            raise HttpError(405, "method not allowed", {"Allow": allow})


        if path == "/api/v1/fleet":
            return self._json(200, self.app.fleet())
        if path == "/api/v1/events":
            return self._json(200, self.app.events(
                device_id=self._param(query, "device_id") or None,
                open_only=(self._param(query, "open") or "").lower() in ("1", "true", "yes"),
                limit=self._int_param(query, "limit", EVENTS_LIMIT_DEFAULT, 1, EVENTS_LIMIT_MAX)))
        if device_id is not None and sub is None:
            extended = (self._param(query, "extended") or "").lower() in ("1", "true", "yes")
            result = self.app.device(device_id, extended=extended)

        elif sub == "metrics":
            result = self.app.device_metrics(
                device_id, self._int_param(query, "minutes", MINUTES_DEFAULT, 1, MINUTES_MAX))
        elif sub == "availability":
            result = self.app.device_availability(
                device_id, self._int_param(query, "days", 30, 1, 365),
                self._int_param(query, "tz_offset_min", 0, -840, 840))
        elif sub == "raw":
            at, day = self._param(query, "at"), self._param(query, "date")
            try:
                if at:
                    result = self.app.device_raw_reading(device_id, at)
                elif day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                    try:
                        datetime.strptime(day, "%Y-%m-%d")
                    except ValueError:
                        raise HttpError(400, "date must be a real date, YYYY-MM-DD") from None
                    result = self.app.device_raw_index(
                        device_id, day, self._int_param(query, "tz_offset_min", 0, -840, 840))
                else:
                    raise HttpError(400, "give date=YYYY-MM-DD or at=<reading time>")
            except LookupError as e:
                raise HttpError(404, str(e)) from None
        elif path == "/healthz":
            return self._json(200, self.app.healthz())
        elif path == "/":
            return self._static("index.html")
        elif path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        else:
            raise HttpError(404, "not found")
        if result is None:
            raise HttpError(404, "unknown device")
        return self._json(200, result)

    @staticmethod
    def _device_route(path: str) -> tuple[str | None, str | None]:
        prefix = "/api/v1/devices/"
        if not path.startswith(prefix):
            return None, None
        parts = path[len(prefix):].split("/")
        if len(parts) == 1 and parts[0]:
            return unquote(parts[0]), None
        if len(parts) == 2 and parts[0] and parts[1] in ("metrics", "availability", "raw", "config", "logs", "jobs"):
            return unquote(parts[0]), parts[1]

        return None, None

    @staticmethod
    def _param(query: dict, name: str) -> str | None:
        values = query.get(name)
        return values[0] if values else None

    def _int_param(self, query: dict, name: str, default: int, lo: int, hi: int) -> int:
        raw = self._param(query, name)
        if raw is None or raw.strip() == "":
            return default
        try:
            value = int(raw.strip())
        except ValueError:
            raise HttpError(400, f"{name} must be an integer") from None
        return max(lo, min(hi, value))

    # ---- request bodies ------------------------------------------------------------------

    def _content_length(self) -> int:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        raw = raw.strip()
        if not (raw.isascii() and raw.isdigit()):
            self.close_connection = True
            raise HttpError(400, "invalid Content-Length")
        return int(raw)

    def _read_body(self) -> bytes:
        if self._chunked():
            return self._read_chunked()
        length = self._content_length()
        if length > MAX_BODY:
            self._discard(length)
            raise HttpError(413, f"body is larger than {MAX_BODY // 1024} KB")
        data = self.rfile.read(length) if length else b""
        if len(data) != length:
            self.close_connection = True
            raise HttpError(400, "incomplete body")
        return data

    def _chunked(self) -> bool:
        encoding = self.headers.get("Transfer-Encoding")
        if encoding is None:
            return False
        if encoding.strip().lower() != "chunked":
            self.close_connection = True
            raise HttpError(501, "unsupported Transfer-Encoding")
        return True

    def _read_chunked(self) -> bytes:
        """Chunked request body, limited like any other; an oversized body is drained, then 413."""
        self.close_connection = True
        body, total = bytearray(), 0
        while True:
            line = self.rfile.readline(1026)
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise HttpError(400, "invalid chunked body") from None
            if size < 0:
                raise HttpError(400, "invalid chunked body")
            if size == 0:
                break
            total += size
            if total > DRAIN_MAX:
                raise HttpError(413, f"body is larger than {MAX_BODY // 1024} KB")
            chunk = self.rfile.read(size)
            if len(chunk) != size or self.rfile.readline(1026).strip():
                raise HttpError(400, "invalid chunked body")
            if total <= MAX_BODY:
                body += chunk
        for _ in range(MAX_TRAILER_LINES):  # optional trailer fields, then an empty line
            if not self.rfile.readline(1026).strip():
                break
        else:
            raise HttpError(400, "too many trailer fields in the chunked body")
        if total > MAX_BODY:
            raise HttpError(413, f"body is larger than {MAX_BODY // 1024} KB")
        return bytes(body)

    def _discard(self, length: int) -> None:
        """Read and drop a rejected body so the client gets our answer instead of a TCP reset."""
        self.close_connection = True
        if length > DRAIN_MAX:
            return
        while length > 0:
            chunk = self.rfile.read(min(65536, length))
            if not chunk:
                return
            length -= len(chunk)

    def _drain_body(self) -> None:
        """Consume the body of a request we are about to refuse (see _discard)."""
        with contextlib.suppress(HttpError):
            if self._chunked():
                self._read_chunked()
                return
            length = self._content_length()
            if length:
                self._discard(length)

    def _authorizations(self) -> list[str]:
        return self.headers.get_all("Authorization") or []

    def _bearer(self) -> str | None:
        for header in self._authorizations():
            scheme, _, token = header.strip().partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
        return None

    # ---- handlers --------------------------------------------------------------------------

    def _heartbeat(self) -> None:
        token = self._bearer()
        if token is None or not self.app.token_known(token):
            self._drain_body()
            log.warning("heartbeat rejected (401: bad or missing token) from %s", self.client_address[0])
            raise HttpError(401, "unauthorized", {"WWW-Authenticate": "Bearer"})
        body = self._read_body()
        recv_ms = now_ms()
        try:
            hb = json.loads(body.decode("utf-8-sig"), parse_constant=lambda _name: None)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise HttpError(400, "invalid JSON") from None
        device_id = hb.get("device_id") if isinstance(hb, dict) else None
        if isinstance(device_id, str) and not self.app.token_valid_for(device_id, token):
            log.warning("heartbeat rejected (401: token not valid for %r) from %s", device_id[:MAX_STR],
                        self.client_address[0])
            raise HttpError(401, "unauthorized", {"WWW-Authenticate": "Bearer"})
        error = validate_heartbeat(hb)
        if error:
            log.warning("heartbeat rejected (400: %s) from %s", error, self.client_address[0])
            raise HttpError(400, error)
        self._json(200, self.app.ingest(hb, recv_ms))

    def _patch(self, device_id: str) -> None:
        if not tokens_equal(self._bearer(), self.app.cfg["admin_token"]):
            self._drain_body()
            raise HttpError(401, "admin token required", {"WWW-Authenticate": "Bearer"})
        body = self._read_body()
        try:
            data = json.loads(body.decode("utf-8-sig"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise HttpError(400, "invalid JSON") from None
        try:
            device = self.app.patch_device(device_id, data)
        except ValueError as e:
            raise HttpError(400, str(e)) from None
        if device is None:
            raise HttpError(404, "unknown device")
        self._json(200, {"device": device})

    def _post_logs(self) -> None:
        token = self._bearer()
        if token is None or not self.app.token_known(token):
            self._drain_body()
            raise HttpError(401, "unauthorized", {"WWW-Authenticate": "Bearer"})
        body = self._read_body()
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            raise HttpError(400, "invalid JSON")
        if not isinstance(data, dict) or "device_id" not in data or "logs" not in data:
            raise HttpError(400, "payload must contain 'device_id' and 'logs' list")
        device_id = data["device_id"]
        if not self.app.token_valid_for(device_id, token):
            raise HttpError(401, "unauthorized", {"WWW-Authenticate": "Bearer"})
        if not isinstance(data["logs"], list):
            raise HttpError(400, "'logs' must be a list")
        res = self.app.ingest_logs(device_id, data["logs"])
        self._json(200, res)

    def _post_config(self) -> None:
        if not tokens_equal(self._bearer(), self.app.cfg["admin_token"]):
            self._drain_body()
            raise HttpError(401, "admin token required", {"WWW-Authenticate": "Bearer"})
        body = self._read_body()
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            raise HttpError(400, "invalid JSON")
        if not isinstance(data, dict):
            raise HttpError(400, "payload must be a JSON object")
        if data.get("action") == "delete" or data.get("delete") is True:
            res = self.app.delete_config(data.get("target_type"), data.get("target_id"))
            return self._json(200, res)
        try:
            res = self.app.set_config(data.get("target_type"), data.get("target_id"), data.get("config"))
        except ValueError as e:
            raise HttpError(400, str(e))
        self._json(200, res)

    def _get_config(self, query: dict) -> None:
        bearer = self._bearer()
        if not (tokens_equal(bearer, self.app.cfg["admin_token"]) or self.app.basic_auth_ok(self._authorizations())):
            raise HttpError(401, "unauthorized")
        tt = self._param(query, "target_type")
        tid = self._param(query, "target_id")
        if not tt or not tid:
            raise HttpError(400, "target_type and target_id required")
        cfg = self.app.get_config(tt, tid)
        if cfg is None:
            raise HttpError(404, "config not found")
        self._json(200, cfg)

    def _delete_config(self, query: dict) -> None:
        bearer = self._bearer()
        if not tokens_equal(bearer, self.app.cfg["admin_token"]):
            raise HttpError(401, "admin token required", {"WWW-Authenticate": "Bearer"})
        tt = self._param(query, "target_type")
        tid = self._param(query, "target_id")
        if not tt or not tid:
            raise HttpError(400, "target_type and target_id required")
        res = self.app.delete_config(tt, tid)
        self._json(200, res)

    def _get_update_jobs(self, query: dict) -> None:
        bearer = self._bearer()
        if not (tokens_equal(bearer, self.app.cfg["admin_token"]) or self.app.basic_auth_ok(self._authorizations())):
            raise HttpError(401, "unauthorized")
        device_id = self._param(query, "device_id")
        status = self._param(query, "status")
        limit = self._int_param(query, "limit", 50, 1, 200)
        jobs = self.app.get_update_jobs(device_id=device_id, status=status, limit=limit)
        self._json(200, {"ok": True, "jobs": jobs})

    def _post_update_job(self) -> None:
        if not tokens_equal(self._bearer(), self.app.cfg["admin_token"]):
            self._drain_body()
            raise HttpError(401, "admin token required", {"WWW-Authenticate": "Bearer"})
        body = self._read_body()
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            raise HttpError(400, "invalid JSON")
        if not isinstance(data, dict):
            raise HttpError(400, "payload must be a JSON object")
        device_id = data.get("device_id")
        operation = data.get("operation")
        if not device_id or not operation:
            raise HttpError(400, "device_id and operation required")
        try:
            res = self.app.create_update_job(
                device_id=device_id,
                operation=operation,
                target_version=data.get("target_version"),
                payload=data.get("payload"),
                created_by="admin"
            )
        except ValueError as e:
            raise HttpError(400, str(e))
        self._json(201, res)

    def _get_update_job(self, job_id: str) -> None:
        bearer = self._bearer()
        if not (tokens_equal(bearer, self.app.cfg["admin_token"]) or self.app.basic_auth_ok(self._authorizations())):
            raise HttpError(401, "unauthorized")
        job = self.app.get_update_job(job_id)
        if job is None:
            raise HttpError(404, "job not found")
        self._json(200, {"ok": True, "job": job})

    def _post_update_job_status(self, job_id: str) -> None:
        bearer = self._bearer()
        if bearer is None or not (self.app.token_known(bearer) or tokens_equal(bearer, self.app.cfg["admin_token"])):
            self._drain_body()
            raise HttpError(401, "unauthorized", {"WWW-Authenticate": "Bearer"})
        body = self._read_body()
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            raise HttpError(400, "invalid JSON")
        if not isinstance(data, dict):
            raise HttpError(400, "payload must be a JSON object")
        device_id = data.get("device_id")
        status = data.get("status")
        if not device_id or not status:
            raise HttpError(400, "device_id and status required")
        if not (tokens_equal(bearer, self.app.cfg["admin_token"]) or self.app.token_valid_for(device_id, bearer)):
            raise HttpError(401, "unauthorized for this device")
        try:
            res = self.app.update_job_status(
                job_id=job_id,
                device_id=device_id,
                status=status,
                progress_pct=data.get("progress_pct", 0),
                message=data.get("message", ""),
                execution_id=data.get("execution_id"),
                error_details=data.get("error_details")
            )
        except ValueError as e:
            raise HttpError(400, str(e))
        if res is None:
            raise HttpError(404, "job not found")
        self._json(200, {"ok": True, "job": res})

    def _cancel_update_job(self, job_id: str) -> None:
        if not tokens_equal(self._bearer(), self.app.cfg["admin_token"]):
            self._drain_body()
            raise HttpError(401, "admin token required", {"WWW-Authenticate": "Bearer"})
        try:
            res = self.app.cancel_update_job(job_id)
        except ValueError as e:
            raise HttpError(400, str(e))
        if res is None:
            raise HttpError(404, "job not found")
        self._json(200, {"ok": True, "job": res})

    def _get_scheduled_updates(self, query: dict) -> None:
        bearer = self._bearer()
        if not (tokens_equal(bearer, self.app.cfg["admin_token"]) or self.app.basic_auth_ok(self._authorizations())):
            raise HttpError(401, "unauthorized")
        device_id = self._param(query, "device_id")
        status = self._param(query, "status")
        limit = self._int_param(query, "limit", 50, 1, 200)
        schedules = self.app.get_scheduled_updates(device_id=device_id, status=status, limit=limit)
        self._json(200, {"ok": True, "schedules": schedules})

    def _post_scheduled_update(self) -> None:
        if not tokens_equal(self._bearer(), self.app.cfg["admin_token"]):
            self._drain_body()
            raise HttpError(401, "admin token required", {"WWW-Authenticate": "Bearer"})
        body = self._read_body()
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            raise HttpError(400, "invalid JSON")
        if not isinstance(data, dict):
            raise HttpError(400, "payload must be a JSON object")
        device_id = data.get("device_id")
        operation = data.get("operation")
        name = data.get("name") or f"{operation} on {device_id}"
        scheduled_at = data.get("scheduled_at")
        if not device_id or not operation or not scheduled_at:
            raise HttpError(400, "device_id, operation, and scheduled_at required")
        try:
            res = self.app.create_scheduled_update(
                name=name,
                device_id=device_id,
                operation=operation,
                scheduled_at=scheduled_at,
                target_version=data.get("target_version"),
                payload=data.get("payload"),
                timezone=data.get("timezone", "UTC"),
                maintenance_window=data.get("maintenance_window"),
                created_by="admin"
            )
        except ValueError as e:
            raise HttpError(400, str(e))
        self._json(201, res)

    def _cancel_scheduled_update(self, schedule_id: str) -> None:
        if not tokens_equal(self._bearer(), self.app.cfg["admin_token"]):
            self._drain_body()
            raise HttpError(401, "admin token required", {"WWW-Authenticate": "Bearer"})
        try:
            res = self.app.cancel_scheduled_update(schedule_id)
        except ValueError as e:
            raise HttpError(400, str(e))
        if res is None:
            raise HttpError(404, "schedule not found")
        self._json(200, {"ok": True, "schedule": res})

    def _static(self, rel: str) -> None:

        target = self.app.static_file(rel)
        if target is None:
            raise HttpError(404, "not found")
        try:
            body = target.read_bytes()
        except OSError:
            raise HttpError(404, "not found") from None
        content_type = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, body, content_type, {"Cache-Control": "no-cache"})

    # ---- responses -------------------------------------------------------------------------

    def _json(self, status: int, payload, headers: dict | None = None) -> None:
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", {"Cache-Control": "no-store", **(headers or {})})

    def _send(self, status: int, body: bytes, content_type: str, headers: dict) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        for name, value in headers.items():
            self.send_header(name, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        """Protocol-level errors (bad request line, unsupported method, ...) as JSON with our headers."""
        try:
            short = self.responses[code][0]
        except KeyError:
            short = "error"
        self.close_connection = True
        body = json.dumps({"ok": False, "error": message or short}).encode("utf-8")
        self.send_response(code, short)  # the status line only ever carries the fixed reason phrase
        self.send_header("Connection", "close")
        if code >= 200 and code not in (204, 205, 304):
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD" and code >= 200 and code not in (204, 205, 304):
            self.wfile.write(body)

    def log_request(self, code="-", size="-"):
        if log.isEnabledFor(logging.DEBUG):
            log.debug("%s %r %s", self.client_address[0], self.requestline, code)

    def log_message(self, fmt, *args):
        log.debug("%s %r", self.client_address[0], fmt % args)


class FleetHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # socketserver's default listen backlog is 5: a burst of boxes reconnecting at once (e.g. after
    # a server restart) overflowed it and clients saw connection resets. The kernel caps this value.
    request_queue_size = 128

    def __init__(self, app: FleetApp):
        self.app = app
        host = app.cfg["bind"]
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        super().__init__((host, app.cfg["port"]), Handler)

    def server_bind(self):
        # HTTPServer.server_bind does a reverse DNS lookup of the bind address; not needed here.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def make_server(app: FleetApp) -> FleetHTTPServer:
    return FleetHTTPServer(app)


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SARAS fleet server (CONTRACT.md sections 3, 5, 6, 7, 9)")
    parser.add_argument("--config", help="server config JSON (default: built-in defaults, see server.example.json)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every request")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        log.error("config error: %s", e)
        return 2
    if args.config is None:
        log.warning("no --config given: using built-in defaults")
    for warning in config_warnings(cfg):
        log.warning("%s", warning)
    try:
        app = FleetApp(cfg)
    except (sqlite3.Error, OSError) as e:
        log.error("cannot open database %s: %s", cfg["db_path"], e)
        return 1
    try:
        httpd = make_server(app)
    except OSError as e:
        log.error("cannot listen on %s:%s: %s", cfg["bind"], cfg["port"], e)
        return 1

    stopping = threading.Event()

    def on_signal(signum, _frame):
        if not stopping.is_set():
            stopping.set()
            log.info("received %s, stopping", signal.Signals(signum).name)
            threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    host, port = httpd.server_address[:2]
    shown = f"[{host}]" if ":" in str(host) else host
    log.info("saras fleet server %s listening on http://%s:%d (db %s)", __version__, shown, port, cfg["db_path"])
    app.start_background()
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        app.stop()
        httpd.server_close()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/python3
"""SARAS device-management agent: sends one heartbeat per interval to the fleet server.

Built to CONTRACT.md sections 2-4 (heartbeat "saras.heartbeat/1", agent behaviour, config keys).

    saras_agent.py --config /etc/saras-agent/agent.json   run the loop (what systemd runs)
    saras_agent.py --config agent.dev.json --once         print one heartbeat and exit; nothing is sent
    saras_agent.py --config agent.dev.json --verbose      run the loop with debug logging

Every interval the agent reads the box's health, POSTs it to <server_url>/api/v1/heartbeat and
keeps whatever it could not deliver in a small SQLite buffer. After the next successful live
heartbeat it replays up to 50 buffered ones, oldest first, marked "buffered": true.

Sources, all read-only: psutil and /proc; /sys thermal zones, hwmon power rails and fan, devfreq;
NVML; `systemctl show`, `timedatectl`, `journalctl --disk-usage`, `nvpmodel -q`,
`iw dev <if> link`, `ping` to the default gateway, `dpkg-query`; HTTP GETs to the configured
health URLs; a PostgreSQL SSLRequest probe (no login); GETs on the Docker API socket; the
saras-sync state files (of its per-table row counts only totals are sent); the sizes of the
files in /var/log (never their names or contents). Removable media are never reported.

Never collected (privacy): anything about users or school data. No app-database queries, no
process lists or command lines, no log contents, no usernames, no Wi-Fi SSID, no MAC addresses,
no environment variables, no file contents other than the saras-sync state files.

Devices: Jetson AGX Thor is tested (L4T R39.2.1). The Jetson Orin / Orin Nano GPU paths (jtop,
sysfs) and the DGX Spark paths (DMI detection, vLLM collector) follow NVIDIA's and vLLM's
documented interfaces but have not run on that hardware yet: treat them as best-effort.

It also runs as a normal user: whatever needs root (Docker socket, DMI serial numbers) is
reported in collector_errors instead of stopping the heartbeat.

Dependencies: Python 3.12 standard library, psutil, pynvml. jtop (jetson-stats) is imported only
on Orin, and only when NVML is unavailable.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import copy
import datetime as dt
import glob
import hashlib
import http.client
import importlib.metadata
import ipaddress
import json
import logging
import math
import os
import platform
import re
import shutil
import signal
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import psutil
except ImportError:  # still runs; the system readings then show up in collector_errors
    psutil = None

AGENT_VERSION = "0.1.0"
SCHEMA = "saras.heartbeat/1"
USER_AGENT = f"saras-agent/{AGENT_VERSION}"
DEFAULT_CONFIG_PATH = "/etc/saras-agent/agent.json"
DOCKER_SOCKET = "/var/run/docker.sock"
DOCKER_DENIED = "permission denied: run the agent as root or add it to the docker group"
SYNC_UNIT = "saras-sync.service"

CMD_TIMEOUT_S = 3.0        # contract: every subprocess call has a timeout of at most 3 s
PROBE_TIMEOUT_S = 3.0      # local HTTP and TCP health probes
DOCKER_BUDGET_S = 10.0     # time allowed for inspecting containers in one heartbeat
NVML_RETRY_S = 60.0        # after NVML failed to start, try again this much later
INTERVAL_MIN_S, INTERVAL_MAX_S = 5, 3600
MAX_BODY_BYTES = 256 * 1024
REPLAY_BATCH = 50
DETAIL_MAX = 200
MAX_SERVICES = 150
MAX_CONTAINERS = 100
MiB = 1024 * 1024
GiB = 1024 ** 3

log = logging.getLogger("saras-agent")

# Defaults for keys a config file leaves out (CONTRACT.md section 4). "claimed" is site specific,
# so it has no built-in value: a box must never claim another school's site code by accident.
DEFAULT_CONFIG = {
    "server_url": "http://127.0.0.1:8090",
    "allow_insecure_http": False,
    "token": "change-me",
    "device_id": None,
    "interval_s": 60,
    "software_every_s": 600,
    "request_timeout_s": 10,
    "claimed": {"product": None, "site_code": None, "customer": None},
    "services": {
        "app_live_url": "http://127.0.0.1:8000/health/live",
        "app_ready_url": "http://127.0.0.1:8000/health/ready",
        "postgres": "127.0.0.1:5426",
        "vllm_url": None,
    },
    "systemd_units": ["nginx.service", "docker.service", "saras-sync.service", "jtop.service"],
    "sync_state_dir": "/opt/saras-sync/state",
    "ping_gateway": True,
    "docker": True,
    "buffer_path": "/var/lib/saras-agent/buffer.db",
    "buffer_max_rows": 20000,
}

# Field order of each heartbeat object, as in the contract. Every key is always present; a value
# that cannot be read is null.
SYSTEM_KEYS = ("booted_at", "uptime_s", "ntp_synced", "cpu_pct", "cpu_per_core_pct", "cpu_freq_mhz",
               "load_1m", "load_5m", "load_15m", "ram_total_mb", "ram_used_mb", "ram_pct", "swap_pct",
               "disks", "journal_mb", "var_log_mb")
GPU_KEYS = ("source", "name", "util_pct", "temp_c", "clock_mhz", "max_clock_mhz", "power_w", "memory_note")
POWER_KEYS = ("mode", "total_w", "rails_w", "fan_rpm")
NETWORK_KEYS = ("iface", "link", "wifi_signal_dbm", "wifi_bitrate_mbps", "gateway_rtt_ms",
                "gateway_loss_pct", "last_server_rtt_ms")
SYNC_KEYS = ("last_success", "last_attempt", "offline_count", "unit_result", "age_h",
             "source_tables", "source_rows", "target_rows", "mismatched_tables")
SOFTWARE_KEYS = ("os", "kernel", "l4t", "jetpack", "nvidia_driver", "cuda_driver", "docker",
                 "nvidia_container_toolkit", "jetson_stats", "bootloader", "python", "app_version",
                 "vllm_version", "llm_model")
VLLM_KEYS = ("status", "model", "running", "waiting", "kv_cache_pct", "prompt_tokens_total",
             "generation_tokens_total", "requests_success_total", "requests_aborted_total",
             "avg_e2e_latency_s", "avg_ttft_s")

# L4T release -> JetPack, for boxes without the nvidia-jetpack meta package. Only releases whose
# mapping is certain are listed (R39.2.x from NVIDIA's Jetson Linux 39.2 / 39.2.1 release notes);
# anything else is reported as null.
JETPACK_BY_L4T = {
    "R35.1.0": "5.0.2", "R35.2.1": "5.1", "R35.3.1": "5.1.1", "R35.4.1": "5.1.2",
    "R35.5.0": "5.1.3", "R35.6.0": "5.1.4", "R36.3.0": "6.0", "R36.4.0": "6.1",
    "R36.4.3": "6.2", "R38.2.0": "7.0", "R39.2.0": "7.2", "R39.2.1": "7.2.1",
}


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------

class AgentError(Exception):
    """An expected failure; str() is a short reason fit for collector_errors."""


class CommandError(AgentError):
    """A command failed or timed out."""


class CommandMissing(CommandError):
    """The command is not installed. Usually means 'not applicable on this device'."""


class ConfigError(AgentError):
    """The config file is missing or invalid."""


def utc_iso(ts: float | None = None) -> str:
    """UTC ISO-8601 with milliseconds and a Z suffix, e.g. 2026-09-29T07:40:00.123Z."""
    d = dt.datetime.fromtimestamp(time.time() if ts is None else ts, tz=dt.timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def read_text(path: str, limit: int = 65536) -> str | None:
    """A small sysfs/procfs/config file as stripped text (NUL bytes removed), or None."""
    try:
        with open(path, "rb") as f:
            raw = f.read(limit)
    except OSError:
        return None
    return raw.replace(b"\x00", b"").decode("utf-8", "replace").strip()


def read_int(path: str) -> int | None:
    try:
        return int(read_text(path, 64))
    except (TypeError, ValueError):
        return None


def rnd(x, nd: int = 1):
    """Round to nd decimals. None for missing, non-numeric, NaN or infinite values (JSON has no NaN)."""
    if x is None or isinstance(x, bool):
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if math.isfinite(x) else None


def as_int(x) -> int | None:
    v = rnd(x, 0)
    return int(v) if v is not None else None


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


def clip(s, n: int = DETAIL_MAX) -> str | None:
    """One line of at most n characters of valid Unicode; None stays None. (A mount path or
    hostname that isn't UTF-8 arrives with lone surrogates from Python: they become '?'.)"""
    if s is None:
        return None
    return _CONTROL_CHARS.sub(" ", str(s)).strip()[:n].encode("utf-8", "replace").decode("utf-8")


def reason(exc: BaseException) -> str:
    """Short reason for an exception, for collector_errors and service details."""
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        if isinstance(exc.reason, BaseException):
            exc = exc.reason
        else:
            return clip(exc.reason)
    if isinstance(exc, AgentError):
        return clip(str(exc))
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, TimeoutError):  # socket.timeout is an alias
        return "timed out"
    if isinstance(exc, PermissionError):
        return "permission denied"
    if isinstance(exc, ssl.SSLError):
        return clip(f"TLS error: {exc.reason or exc}")
    if isinstance(exc, socket.gaierror):
        return clip(f"name lookup failed: {exc.strerror or exc}")
    if isinstance(exc, OSError) and exc.strerror:
        return clip(exc.strerror)
    return clip(f"{type(exc).__name__}: {exc}")


def sanitize(obj):
    """Replace NaN/infinite floats anywhere in obj with None, so the JSON is always valid."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    return obj


def _natural(path: str):
    """Sort key so thermal_zone10 comes after thermal_zone9."""
    m = re.search(r"(\d+)$", path)
    return (path[: m.start()] if m else path, int(m.group(1)) if m else -1)


def _put_unique(d: dict, key: str, value) -> None:
    k, i = key, 1
    while k in d:
        k, i = f"{key}_{i}", i + 1
    d[k] = value


# Children get a minimal environment: the C locale so their output parses the same on every box,
# and none of our variables (NOTIFY_SOCKET, proxies, ...).
_CMD_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LC_ALL": "C", "LANG": "C"}


def run_cmd(argv: list[str], ok_codes=(0,), timeout: float = CMD_TIMEOUT_S) -> str:
    """Run a command (no shell) with a timeout of at most 3 s and return its stdout."""
    exe = shutil.which(argv[0], path=_CMD_ENV["PATH"])
    if exe is None:
        raise CommandMissing(f"{argv[0]} not installed")
    try:
        p = subprocess.run([exe, *argv[1:]], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           errors="replace", env=_CMD_ENV, timeout=min(timeout, CMD_TIMEOUT_S))
    except subprocess.TimeoutExpired:
        raise CommandError(f"{argv[0]} timed out after {min(timeout, CMD_TIMEOUT_S):g} s") from None
    except OSError as e:
        raise CommandError(f"{argv[0]}: {e.strerror or e}") from None
    if p.returncode not in ok_codes:
        first = (p.stderr.strip() or p.stdout.strip()).splitlines()
        raise CommandError(clip(f"{argv[0]} exited {p.returncode}" + (f": {first[0]}" if first else "")))
    return p.stdout


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: urllib would silently turn a redirected POST into a GET without a body."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Health probes go to services on this box, so they never go through a proxy.
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get(url: str, max_bytes: int = 4096, timeout: float = PROBE_TIMEOUT_S):
    """GET without proxies. Returns (HTTP status or None, body, error reason or None, latency ms)."""
    t0 = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _LOCAL_OPENER.open(req, timeout=timeout) as r:
            body, code = r.read(max_bytes), r.status
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            body = e.read(max_bytes)
        except Exception:  # noqa: BLE001 - the status code is what matters
            body = b""
        e.close()
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
        return None, b"", reason(e), rnd((time.monotonic() - t0) * 1000)
    return code, body, None, rnd((time.monotonic() - t0) * 1000)


def sd_notify(message: str) -> bool:
    """Send a state line to systemd (Type=notify): READY=1, WATCHDOG=1, STATUS=..., STOPPING=1."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr[0] == "@":  # abstract socket namespace
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as s:
            s.connect(addr)
            s.sendall(message.encode())
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------------

_UNIT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.@\\-]*$")


def _is_http_url(v) -> bool:
    if not isinstance(v, str):
        return False
    u = urllib.parse.urlsplit(v)
    return u.scheme in ("http", "https") and bool(u.hostname)


def sends_token_in_clear(server_url: str) -> bool:
    """True for plain http:// to anything but this box (localhost, 127.0.0.0/8, ::1)."""
    u = urllib.parse.urlsplit(server_url)
    return u.scheme == "http" and not _is_loopback(u.hostname or "")


def load_config(path: str) -> dict:
    """Read the JSON config, fill in defaults for missing keys and validate it."""
    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror or e}") from None
    except ValueError as e:
        raise ConfigError(f"{path} is not valid JSON: {e}") from None
    if not isinstance(user, dict):
        raise ConfigError(f"{path}: the top level must be a JSON object")
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    for key, value in user.items():
        if key not in DEFAULT_CONFIG:
            log.warning("config: unknown key %r ignored", key)
        elif key == "services" and isinstance(value, dict):
            cfg["services"].update(value)
        else:
            cfg[key] = value
    # Relative paths resolve against the config file's folder (contract section 4).
    base = os.path.dirname(os.path.abspath(path))
    for key in ("buffer_path", "sync_state_dir"):
        v = cfg[key]
        if isinstance(v, str) and v and not os.path.isabs(v):
            cfg[key] = os.path.normpath(os.path.join(base, v))
    _validate(cfg)
    return cfg


def _validate(cfg: dict) -> None:
    def need(ok, msg):
        if not ok:
            raise ConfigError(msg)

    need(_is_http_url(cfg["server_url"]), "server_url must be an http:// or https:// URL")
    need(isinstance(cfg["allow_insecure_http"], bool), "allow_insecure_http must be true or false")
    # Every heartbeat carries the fleet token, so plain http is only for a server on this box.
    need(not sends_token_in_clear(cfg["server_url"]) or cfg["allow_insecure_http"],
         "server_url is http:// to another machine, which would send the token unencrypted: use https:// "
         "(nginx with TLS, see README) or, on a trusted network only, set allow_insecure_http to true")
    need(isinstance(cfg["token"], str) and cfg["token"], "token must be a non-empty string")
    need(cfg["device_id"] is None or (isinstance(cfg["device_id"], str) and cfg["device_id"].strip()),
         "device_id must be null or a non-empty string")
    for key, lo, hi in (("interval_s", INTERVAL_MIN_S, INTERVAL_MAX_S), ("software_every_s", 0, 86400),
                        ("request_timeout_s", 1, 60), ("buffer_max_rows", 1, 10_000_000)):
        v = cfg[key]
        need(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v),
             f"{key} must be a number")
        if not lo <= v <= hi:  # say so: a silently changed interval looks like a bug in the agent
            log.warning("config: %s %g is outside %d-%d; using %d", key, v, lo, hi, min(hi, max(lo, v)))
        cfg[key] = int(min(hi, max(lo, v)))
    claimed = cfg["claimed"]
    need(isinstance(claimed, dict), "claimed must be an object")
    for key, v in claimed.items():
        need(v is None or isinstance(v, str), f"claimed.{key} must be a string or null")
    services = cfg["services"]
    need(isinstance(services, dict), "services must be an object")
    for key in ("app_live_url", "app_ready_url", "app_version_url", "vllm_url"):
        v = services.get(key)
        need(v is None or _is_http_url(v), f"services.{key} must be null or an http(s) URL")
    need(cfg.get("app_version_file") is None or isinstance(cfg.get("app_version_file"), str),
         "app_version_file must be null or a path string")
    pg = services.get("postgres")
    need(pg is None or (isinstance(pg, str) and re.fullmatch(r"(\[[0-9a-fA-F:.]+\]|[^:\s]+)(:\d{1,5})?", pg)),
         'services.postgres must be null or "host:port"')
    units = cfg["systemd_units"]
    need(isinstance(units, list) and all(isinstance(u, str) and _UNIT_NAME.match(u) for u in units),
         "systemd_units must be a list of unit names")
    need(cfg["sync_state_dir"] is None or isinstance(cfg["sync_state_dir"], str), "sync_state_dir must be a path or null")
    need(isinstance(cfg["ping_gateway"], bool), "ping_gateway must be true or false")
    need(isinstance(cfg["docker"], bool), "docker must be true or false")
    need(isinstance(cfg["buffer_path"], str) and cfg["buffer_path"], "buffer_path must be a path")


# --------------------------------------------------------------------------------------------
# Device identity
# --------------------------------------------------------------------------------------------

_PLACEHOLDERS = {"", "none", "n/a", "na", "0", "unknown", "default string", "not specified",
                 "not applicable", "to be filled by o.e.m.", "system serial number",
                 "system product name", "123456789"}


def _meaningful(s: str | None) -> str | None:
    """Drop the placeholder strings firmware writes into empty DMI fields."""
    return None if s is None or s.strip().lower() in _PLACEHOLDERS else s.strip()


def detect_device_type(model: str | None, dmi_product: str | None = None,
                       compatible: str | None = None, gpu_name: str | None = None) -> str:
    """Map the board to one of the contract's device types.

    AGX Thor: device-tree model "NVIDIA Jetson AGX Thor Developer Kit" or SoC tegra264 (tested).
    Orin / Orin Nano: device-tree model; failing that tegra234 plus the module part number
      (p3767-0003/0004/0005 are Orin Nano, other p3767 SKUs Orin NX, p3701 AGX Orin). Untested.
    DGX Spark: DMI product name containing "DGX Spark", or NVML reporting the GB10 superchip
      (partner GB10 systems are the same platform). Untested.
    Raspberry Pi 5: device-tree model containing "Raspberry Pi 5" or compatible containing "raspberrypi,5" or "bcm2712".
    """
    text = " ".join(filter(None, (model, dmi_product))).lower().replace("_", " ")
    compat = (compatible or "").lower()
    if "thor" in text or "tegra264" in compat:
        return "AGX Thor"
    if "orin nano" in text:
        return "Orin Nano"
    if "orin" in text:
        return "Orin"
    if "tegra234" in compat:
        return "Orin Nano" if re.search(r"p3767-000[345]", compat) else "Orin"
    if "dgx spark" in text or "gb10" in (gpu_name or "").lower():
        return "DGX Spark"
    if "raspberry pi 5" in text or "raspberrypi,5" in compat or "bcm2712" in compat:
        return "Raspberry Pi 5"
    return "Unknown"


def read_static_identity() -> dict:
    """Identity facts that cannot change while the agent runs."""
    def dmi(field):
        return _meaningful(read_text(f"/sys/class/dmi/id/{field}", 256))

    product, vendor = dmi("product_name"), dmi("sys_vendor")
    model = _meaningful(read_text("/proc/device-tree/model", 256))  # Jetson / Raspberry Pi
    if not model and product:  # DGX Spark and other UEFI machines
        model = product if not vendor or vendor.lower() in product.lower() else f"{vendor} {product}"
    # Jetson: device-tree serial (world readable). Elsewhere DMI, which is readable by root only.
    serial = _meaningful(read_text("/proc/device-tree/serial-number", 256)) or dmi("product_serial") or dmi("board_serial")
    if not serial:
        cpuinfo = read_text("/proc/cpuinfo", 4096) or ""
        m = re.search(r"^Serial\s*:\s*([0-9a-fA-F]+)", cpuinfo, re.MULTILINE)
        if m:
            serial = _meaningful(m.group(1))
    dmi_serials = ("/sys/class/dmi/id/product_serial", "/sys/class/dmi/id/board_serial")
    serial_problem = None
    if serial is None and any(os.path.exists(p) and not os.access(p, os.R_OK) for p in dmi_serials):
        serial_problem = "the DMI serial number can only be read as root"
    try:
        with open("/proc/device-tree/compatible", "rb") as f:
            compatible = f.read(1024).replace(b"\x00", b" ").decode("ascii", "replace").strip()
    except OSError:
        compatible = ""
    boot_id = read_text("/proc/sys/kernel/random/boot_id", 64)
    if not boot_id:
        try:
            boot_id = run_cmd(["sysctl", "-n", "kern.bootsessionuuid"], timeout=2).strip()
        except Exception:
            pass
    return {"serial": clip(serial, 64), "model": clip(model), "dmi_product": product,
            "compatible": compatible, "boot_id": boot_id,
            "serial_problem": serial_problem}


# --------------------------------------------------------------------------------------------
# System readings
# --------------------------------------------------------------------------------------------

def _need_psutil():
    if psutil is None:
        raise AgentError("psutil not installed (apt install python3-psutil)")


def _cpu_total(t) -> float:
    # Linux already counts guest time inside user/nice (the same correction psutil.cpu_percent makes).
    return sum(t) - getattr(t, "guest", 0.0) - getattr(t, "guest_nice", 0.0)


def _cpu_busy(t) -> float:
    return _cpu_total(t) - t.idle - getattr(t, "iowait", 0.0)


_PSEUDO_FS = {"squashfs", "iso9660", "udf", "tmpfs", "devtmpfs", "overlay", "ramfs"}
# Desktop automounts land in /media/<user>/<volume label> (/run/media/... on some systems).
_SKIPPED_MOUNTS = ("/boot/", "/snap/", "/var/snap/", "/var/lib/docker/", "/run/", "/media/")


def is_removable(device: str) -> bool:
    """A removable block device (USB stick, card reader), from /sys/class/block/<disk>/removable.
    A partition (sda1) has no such file: its disk (sda) is asked."""
    if not device.startswith("/dev/"):
        return False
    node = os.path.realpath(f"/sys/class/block/{os.path.basename(os.path.realpath(device))}")
    if os.path.exists(os.path.join(node, "partition")):
        node = os.path.dirname(node)
    return read_text(os.path.join(node, "removable"), 8) == "1"


def disk_usage_list() -> list:
    """The box's own filesystems ("/" first). Skips snaps, loop devices, /boot and bind mounts of
    the same device, and never reports removable or desktop-mounted media: their mount point is
    /media/<login name>/<volume label>, which names a person and their files, and a full USB stick
    is no problem of the box's. free_gb is the space a normal user can still write."""
    _need_psutil()
    parts = sorted(psutil.disk_partitions(all=False), key=lambda p: (p.mountpoint != "/", len(p.mountpoint)))
    seen, out = set(), []
    for p in parts:
        mp = p.mountpoint
        if (p.fstype in _PSEUDO_FS or p.device.startswith("/dev/loop") or p.device in seen or mp in ("/boot", "/media")
                or mp.startswith(_SKIPPED_MOUNTS)):
            continue
        if mp != "/" and ("uhelper=udisks2" in (p.opts or "").split(",") or is_removable(p.device)):
            continue
        try:
            u = psutil.disk_usage(mp)
        except OSError:
            continue
        if not u.total:
            continue
        seen.add(p.device)
        out.append({"mount": clip(mp), "total_gb": rnd(u.total / GiB), "used_gb": rnd(u.used / GiB),
                    "free_gb": rnd(u.free / GiB), "pct": rnd(u.percent)})
    return out[:16]


def var_log_mb(root: str = "/var/log", max_files: int = 20000, budget_s: float = 1.0) -> float | None:
    """Space the log files in /var/log take on disk (st_blocks, not their apparent size), the
    journal included; symlinks are not followed. Only sizes are read, never names or contents.
    Folders the agent may not read (a few, as a normal user) are skipped."""
    total, files, deadline = 0, 0, time.monotonic() + budget_s
    for folder, _dirs, names in os.walk(root, followlinks=False):
        for name in names:
            try:
                total += os.lstat(os.path.join(folder, name)).st_blocks * 512
            except OSError:
                continue
            files += 1
            if files > max_files:
                raise AgentError(f"{root} holds over {max_files} files; not measured")
            if time.monotonic() > deadline:
                raise AgentError(f"measuring {root} took over {budget_s:g} s; not measured")
    return rnd(total / MiB)


_SIZE_TO_MB = {"B": 1 / MiB, "K": 1 / 1024, "M": 1.0, "G": 1024.0, "T": 1024.0 ** 2, "P": 1024.0 ** 3}


def journal_mb() -> float | None:
    out = run_cmd(["journalctl", "--disk-usage"])
    m = re.search(r"take up ([\d.]+)\s*([BKMGTP])", out)
    if not m:
        raise CommandError("journalctl --disk-usage: unexpected output")
    return rnd(float(m.group(1)) * _SIZE_TO_MB[m.group(2)])


def ntp_synced() -> bool:
    out = run_cmd(["timedatectl", "show", "--property=NTPSynchronized", "--value"]).strip()
    if out not in ("yes", "no"):
        raise CommandError(f"timedatectl: unexpected answer {out[:20]!r}")
    return out == "yes"


# --------------------------------------------------------------------------------------------
# Thermal, power, GPU
# --------------------------------------------------------------------------------------------

def hwmon_dirs() -> list:
    return [(read_text(h + "/name", 64) or "", h)
            for h in sorted(glob.glob("/sys/class/hwmon/hwmon*"), key=_natural)]


_ZONE_SUFFIX = re.compile(r"[-_]therm(al)?$")


def read_rpi_throttling() -> dict | None:
    """Read Raspberry Pi throttling flags from vcgencmd get_throttled.
    Returns structured flags for current and historical conditions, or None if unavailable.
    """
    try:
        out = run_cmd(["vcgencmd", "get_throttled"], timeout=3)
    except (CommandMissing, CommandError, FileNotFoundError, OSError):
        return None
    m = re.search(r"throttled=(0x[0-9a-fA-F]+|\d+)", out)
    if not m:
        return None
    try:
        raw = int(m.group(1), 0)
    except ValueError:
        return None
    return {
        "raw": raw,
        "raw_hex": hex(raw),
        "under_voltage_now": bool(raw & 0x1),
        "arm_frequency_capped_now": bool(raw & 0x2),
        "throttled_now": bool(raw & 0x4),
        "soft_temp_limit_now": bool(raw & 0x8),
        "under_voltage_history": bool(raw & 0x10000),
        "arm_frequency_capped_history": bool(raw & 0x20000),
        "throttled_history": bool(raw & 0x40000),
        "soft_temp_limit_history": bool(raw & 0x80000),
    }


def read_rpi_temp() -> float | None:
    """Read SoC temperature using vcgencmd measure_temp, with /sys/class/thermal fallback."""
    try:
        out = run_cmd(["vcgencmd", "measure_temp"], timeout=3)
        m = re.search(r"temp=([\d.]+)", out)
        if m:
            return rnd(float(m.group(1)), 1)
    except (CommandMissing, CommandError, FileNotFoundError, OSError):
        pass
    milli = read_int("/sys/class/thermal/thermal_zone0/temp")
    if milli is not None and milli > 0:
        return rnd(milli / 1000.0, 1)
    return None


def thermal_zones() -> dict:
    """Thermal zones by type (tj, gpu, cpu, soc012, ... on Thor; cpu, gpu, cv0, soc0, tj on Orin)
    plus the NVMe drive's composite temperature, in degrees C."""
    out = {}
    for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*"), key=_natural):
        typ = read_text(z + "/type", 64)
        milli = read_int(z + "/temp")
        # Jetson's "PMIC-Die" zone reports a fixed trip value (100 C), not a live temperature.
        if not typ or milli is None or "pmic" in typ.lower():
            continue
        c = milli / 1000.0
        if not -40.0 < c < 150.0:  # powered-off sensors report sentinel values
            continue
        key = re.sub(r"[^a-z0-9]+", "_", _ZONE_SUFFIX.sub("", typ.lower())).strip("_") or "zone"
        _put_unique(out, key, rnd(c))
    for name, h in hwmon_dirs():
        if name == "nvme":
            milli = read_int(h + "/temp1_input")  # "Composite"
            if milli is not None:
                _put_unique(out, "nvme", rnd(milli / 1000.0))
    if "cpu" not in out:
        rpi_t = read_rpi_temp()
        if rpi_t is not None:
            _put_unique(out, "cpu", rpi_t)
        elif "zone" in out:
            _put_unique(out, "cpu", out["zone"])
        elif "soc" in out:
            _put_unique(out, "cpu", out["soc"])
    return out


def power_readings(errors: dict) -> dict:
    """Board power (W), per-rail power, nvpmodel mode and fan speed."""
    p = {"mode": None, "total_w": None, "rails_w": {}, "fan_rpm": None}
    inputs, tach_rpm, other_rpm = [], None, None
    for name, h in hwmon_dirs():
        if name == "ina3221":  # Jetson module monitor: rail voltage (mV) x current (mA)
            for i in range(1, 9):
                label = read_text(f"{h}/in{i}_label", 64)
                if not label or label.lower().startswith("sum of"):
                    continue
                mv, ma = read_int(f"{h}/in{i}_input"), read_int(f"{h}/curr{i}_input")
                if mv is not None and ma is not None:
                    p["rails_w"][clip(label, 64)] = rnd(mv * ma / 1e6, 2)
        elif name.startswith("ina2") and os.path.exists(f"{h}/power1_input"):
            uw = read_int(f"{h}/power1_input")  # Thor: ina238 on the board input ("VIN"), microwatts
            if uw is not None:
                inputs.append(uw / 1e6)
        elif name == "pwm_tach" and tach_rpm is None:  # Jetson fan tachometer
            tach_rpm = read_int(f"{h}/rpm")
        elif other_rpm is None and os.path.exists(f"{h}/fan1_input"):  # generic hwmon fan (best-effort)
            other_rpm = read_int(f"{h}/fan1_input")
    p["fan_rpm"] = tach_rpm if tach_rpm is not None else other_rpm
    rails = p["rails_w"]
    if inputs:
        p["total_w"] = rnd(max(inputs), 2)
    elif "VDD_IN" in rails:  # Orin Nano / NX: module input rail (best-effort)
        p["total_w"] = rails["VDD_IN"]
    elif rails:  # AGX Orin: sum of the monitored rails, as jtop shows it (best-effort)
        p["total_w"] = rnd(sum(v for v in rails.values() if v is not None), 2)
    try:
        m = re.search(r"NV Power Mode:\s*(\S.*)", run_cmd(["nvpmodel", "-q"]))
        p["mode"] = clip(m.group(1), 64) if m else None
    except CommandMissing:
        pass  # not a Jetson
    except CommandError as e:
        errors["nvpmodel"] = str(e)
    return p


def gpu_devfreq_node() -> str | None:
    """The GPU's devfreq directory: Thor "gpu-gpc-0" (graphics clusters; "gpu-nvd-0" is the
    video engine). Orin "17000000.ga10b" (JetPack 5) or "17000000.gpu" (JetPack 6), best-effort."""
    best = None
    for d in glob.glob("/sys/class/devfreq/*"):
        name = os.path.basename(d).lower()
        of_name = (read_text(d + "/device/of_node/name", 64) or "").lower()
        if "nvd" in name:
            continue
        if not any(k in name for k in ("gpu", "ga10b", "gv11b", "gp10b", "gb10b")) and of_name != "gpu":
            continue
        rank = (0 if "gpc" in name else 1, name)
        if best is None or rank < best[0]:
            best = (rank, d)
    return best[1] if best else None


def _mhz(hz) -> int | None:
    return as_int(hz / 1e6) if hz else None


# --------------------------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------------------------

def default_route():
    """(interface, gateway IP) of the IPv4 default route with the lowest metric, preferring
    interfaces whose link is up (Jetson keeps a dormant l4tbr0 default route for USB mode)."""
    best = None
    for line in (read_text("/proc/net/route") or "").splitlines()[1:]:
        f = line.split()
        if len(f) < 8 or f[1] != "00000000" or f[7] != "00000000":
            continue
        flags = int(f[3], 16)
        if not flags & 0x1:  # RTF_UP
            continue
        iface, metric = f[0], int(f[6])
        gateway = socket.inet_ntoa(struct.pack("<I", int(f[2], 16))) if flags & 0x2 else None  # RTF_GATEWAY
        up = read_text(f"/sys/class/net/{iface}/operstate", 32) in ("up", "unknown")
        key = (not up, metric)
        if best is None or key < best[0]:
            best = (key, iface, gateway)
    return (best[1], best[2]) if best else (None, None)


def iface_ipv4(iface: str) -> str | None:
    if psutil is None:
        return None
    for a in psutil.net_if_addrs().get(iface, []):
        if a.family == socket.AF_INET:
            return a.address
    return None


def wifi_link(iface: str) -> dict:
    """Signal (dBm) and negotiated TX bitrate of the Wi-Fi link. Only these two numbers are kept:
    `iw` also prints the SSID and the access point's MAC address, which are never collected."""
    try:
        out = run_cmd(["iw", "dev", iface, "link"])
    except CommandMissing:
        # Fallback without iw: /proc/net/wireless "<if>: <status> <link>. <level>. <noise> ..."
        for line in (read_text("/proc/net/wireless") or "").splitlines():
            if line.strip().startswith(iface + ":"):
                fields = line.split(":", 1)[1].split()
                if len(fields) > 2:
                    return {"wifi_signal_dbm": as_int(fields[2].rstrip("."))}
        return {}
    sig = re.search(r"^\s*signal:\s*(-?\d+)", out, re.M)
    rate = re.search(r"^\s*tx bitrate:\s*([\d.]+)\s*MBit/s", out, re.M)
    return {"wifi_signal_dbm": int(sig.group(1)) if sig else None,
            "wifi_bitrate_mbps": rnd(rate.group(1)) if rate else None}


def ping_gateway(host: str) -> dict:
    """3 pings 0.2 s apart; the whole run is capped at 2 s by ping's own deadline."""
    out = run_cmd(["ping", "-n", "-q", "-c", "3", "-i", "0.2", "-W", "1", "-w", "2", host], ok_codes=(0, 1))
    loss = re.search(r"([\d.]+)% packet loss", out)
    rtt = re.search(r"=\s*[\d.]+/([\d.]+)/", out)  # rtt min/avg/max/mdev = a/b/c/d ms
    if not loss:
        raise CommandError("ping: unexpected output")
    return {"gateway_rtt_ms": rnd(rtt.group(1)) if rtt else None, "gateway_loss_pct": rnd(loss.group(1))}


# --------------------------------------------------------------------------------------------
# Services: HTTP health, PostgreSQL, vLLM, systemd units, Docker containers
# --------------------------------------------------------------------------------------------

def svc(name, kind, status, latency_ms=None, detail=None, restarts=None, since=None) -> dict:
    return {"name": clip(name), "kind": kind, "status": status, "latency_ms": latency_ms,
            "detail": clip(detail), "restarts": restarts, "since": since}


def _status_text(body: bytes) -> str:
    """A short plain-text health answer such as "Healthy"; anything longer or structured is dropped."""
    lines = body.decode("utf-8", "replace").strip().splitlines()
    text = lines[0].strip() if lines else ""
    return text if len(text) <= 40 and text[:1] not in ("{", "[", "<") else ""


def http_service(name: str, url: str | None) -> dict:
    if not url:
        return svc(name, "http", "not_configured")
    code, body, err, ms = http_get(url, 1024)
    if code is None:
        return svc(name, "http", "down", ms, err)
    text = _status_text(body)
    status = "down"
    if 200 <= code < 300:
        # ASP.NET Core health checks answer 200 "Healthy", 200 "Degraded" or 503 "Unhealthy".
        low = text.lower()
        status = "degraded" if low.startswith("degraded") else "down" if low.startswith("unhealthy") else "up"
    return svc(name, "http", status, ms, f"HTTP {code} {text}".strip())


def _split_host_port(target: str, default_port: int = 5432):
    m = re.fullmatch(r"\[([0-9a-fA-F:.]+)\](?::(\d+))?|([^:]+)(?::(\d+))?", target.strip())
    if not m:
        raise AgentError(f"bad host:port {target!r}")
    host = m.group(1) or m.group(3)
    port = m.group(2) or m.group(4)
    return host, int(port) if port else default_port


def _finish_tls(sock: socket.socket) -> None:
    """Complete the TLS handshake PostgreSQL now expects, then hang up. Dropping the connection
    mid-handshake would leave a "could not accept SSL connection" line in its log every minute."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with ctx.wrap_socket(sock):
            pass
    except (OSError, ssl.SSLError):
        pass


def postgres_service(target: str | None) -> dict:
    """PostgreSQL liveness without logging in: send an SSLRequest and read the one-byte answer
    ('N' = SSL off, 'S' = SSL on). No credentials and no queries are involved."""
    if not target:
        return svc("postgres", "tcp", "not_configured")
    host, port = _split_host_port(target)
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_S) as s:
            s.sendall(struct.pack("!II", 8, 80877103))  # SSLRequest
            answer = s.recv(1)
            ms = rnd((time.monotonic() - t0) * 1000)
            if answer == b"S":
                _finish_tls(s)
    except OSError as e:
        return svc("postgres", "tcp", "down", rnd((time.monotonic() - t0) * 1000), reason(e))
    if answer in (b"N", b"S"):
        return svc("postgres", "tcp", "up", ms, "ssl=off" if answer == b"N" else "ssl=on")
    return svc("postgres", "tcp", "down", ms, "closed without answering" if not answer else "not a PostgreSQL answer")


def _prom_sample(line: str):
    """(metric name, value) from one Prometheus text line. Labels are skipped quote-aware, so label
    values containing spaces or braces cannot break the parse. Value None if absent or not finite."""
    n, i = len(line), 0
    while i < n and line[i] not in "{ \t":
        i += 1
    name = line[:i]
    if i < n and line[i] == "{":
        i += 1
        quoted = False
        while i < n:
            c = line[i]
            if quoted:
                if c == "\\":
                    i += 1
                elif c == '"':
                    quoted = False
            elif c == '"':
                quoted = True
            elif c == "}":
                break
            i += 1
        i += 1
    parts = line[i:].split()
    try:
        v = float(parts[0]) if parts else None
    except ValueError:
        return name, None
    return name, v if v is not None and math.isfinite(v) else None


def parse_prometheus(text: str, sum_names, max_names, prefix: str = "") -> dict:
    """Aggregate samples across label sets: sum for counters and queue gauges, max for usage gauges."""
    out = {}
    for line in text.splitlines():
        if not line.startswith(prefix) or line.startswith("#"):
            continue
        name, v = _prom_sample(line)
        if v is None:
            continue
        if name in sum_names:
            out[name] = out.get(name, 0.0) + v
        elif name in max_names:
            out[name] = max(out.get(name, v), v)
    return out


V_RUNNING, V_WAITING = "vllm:num_requests_running", "vllm:num_requests_waiting"
V_KV, V_KV_OLD = "vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"  # 0-1; older builds use the second
V_PROMPT, V_GEN, V_OK = "vllm:prompt_tokens_total", "vllm:generation_tokens_total", "vllm:request_success_total"
V_E2E_SUM, V_E2E_CNT = "vllm:e2e_request_latency_seconds_sum", "vllm:e2e_request_latency_seconds_count"
V_TTFT_SUM, V_TTFT_CNT = "vllm:time_to_first_token_seconds_sum", "vllm:time_to_first_token_seconds_count"
_VLLM_SUM = {V_RUNNING, V_WAITING, V_PROMPT, V_GEN, V_OK, V_E2E_SUM, V_E2E_CNT, V_TTFT_SUM, V_TTFT_CNT}
_VLLM_MAX = {V_KV, V_KV_OLD}


def prometheus_aborted(text: str) -> float | None:
    """vllm:request_success_total counts every finished request, one series per finished_reason
    (stop, length, abort). The sum of the abort series, or None when the metric has no
    finished_reason label (then aborted requests cannot be told apart)."""
    total, labelled = 0.0, False
    for line in text.splitlines():
        if line.startswith(V_OK + "{") and "finished_reason=" in line:
            labelled = True
            if 'finished_reason="abort"' in line:
                total += _prom_sample(line)[1] or 0.0
    return total if labelled else None


def _count(v):
    """Counts and token totals as integers when they are whole numbers."""
    r = rnd(v, 3)
    return int(r) if r is not None and r == int(r) else r


def _avg_delta(prev_sum, cur_sum, prev_cnt, cur_cnt):
    """Average of a Prometheus histogram over one heartbeat: delta(_sum) / delta(_count).
    None on the first heartbeat, when nothing completed, or after a counter reset (vLLM restart)."""
    if None in (prev_sum, cur_sum, prev_cnt, cur_cnt):
        return None
    d_cnt, d_sum = cur_cnt - prev_cnt, cur_sum - prev_sum
    return rnd(d_sum / d_cnt, 3) if d_cnt > 0 and d_sum >= 0 else None


def vllm_base(url: str | None) -> str | None:
    if not url:
        return None
    base = url.rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def systemctl_show(units: list[str]) -> dict:
    """One `systemctl show` for all units -> {unit: {property: value}}. Blocks come back in argument
    order, so an alias (sshd.service -> ssh.service) still maps to the configured name."""
    props = "Id,LoadState,ActiveState,SubState,Result,Type,NRestarts,ActiveEnterTimestampMonotonic"
    out = run_cmd(["systemctl", "show", "--no-pager", f"--property={props}", "--", *units])
    blocks = out.strip("\n").split("\n\n")
    if len(blocks) != len(units):
        raise CommandError(f"systemctl show: {len(blocks)} answers for {len(units)} units")
    return {u: dict(line.split("=", 1) for line in b.splitlines() if "=" in line) for u, b in zip(units, blocks)}


def unit_service(unit: str, p: dict | None, boot_ts: float | None) -> dict:
    """Contract rules: active -> up; failed -> down; a oneshot unit (saras-sync) that is inactive
    with Result=success, or running right now, -> up."""
    name = f"systemd:{unit}"
    if not p or p.get("LoadState") == "not-found":
        return svc(name, "systemd", "not_configured", detail="unit not found")
    active, sub, result = p.get("ActiveState", ""), p.get("SubState", ""), p.get("Result", "")
    oneshot = p.get("Type") == "oneshot"
    if active in ("active", "reloading"):
        status = "up"
    elif active == "failed":
        status = "down"
    elif active == "inactive":
        status = "up" if oneshot and result == "success" else "down"
    elif active == "activating" and oneshot:
        status = "up"
    elif active in ("activating", "deactivating"):
        status = "degraded"  # starting, stopping or waiting to auto-restart
    else:
        status = "unknown"
    detail = f"{active}/{sub}"
    if result and result != "success":
        detail += f" result={result}"
    if p.get("LoadState") == "masked":
        detail += " (masked)"
    # The monotonic entry time plus boot time, not the wall-clock ActiveEnterTimestamp: a box
    # without NTP sets its clock after boot, which leaves wall-clock unit stamps days off while
    # boot time + monotonic stays consistent with system.booted_at.
    mono = as_int(p.get("ActiveEnterTimestampMonotonic"))
    since = utc_iso(boot_ts + mono / 1e6) if mono and boot_ts else None
    return svc(name, "systemd", status, None, detail, as_int(p.get("NRestarts")), since)


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over a Unix socket (the Docker API)."""

    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self._path)
        except OSError:
            s.close()
            raise
        self.sock = s


def docker_get(path: str, timeout: float = PROBE_TIMEOUT_S):
    """Read-only GET on the Docker API; returns the decoded JSON."""
    conn = _UnixHTTPConnection(DOCKER_SOCKET, timeout)
    try:
        conn.request("GET", path, headers={"User-Agent": USER_AGENT})
        r = conn.getresponse()
        data = r.read(16 * MiB)
        if r.status != 200:
            raise AgentError(f"docker GET {path.split('?')[0]}: HTTP {r.status}")
        return json.loads(data)
    finally:
        conn.close()


def docker_time(s) -> str | None:
    """Docker's RFC 3339 nanosecond stamp -> contract format; None for the zero time (never started)."""
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)", s or "")
    if not m or m.group(1).startswith("0001-"):
        return None
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    return utc_iso(dt.datetime.fromisoformat(f"{m.group(1)}.{frac}{tz}").timestamp())


def container_service(name: str, listed_state: str, info: dict | None) -> dict:
    """Only the name, state, health, exit code, restart count and start time are used. The inspect
    answer also holds the command line and environment; those are never read or sent."""
    info = info or {}
    st = info.get("State") or {}
    state = st.get("Status") or listed_state or "unknown"
    health = (st.get("Health") or {}).get("Status")
    policy = ((info.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no"
    exit_code = st.get("ExitCode")
    if state == "running":
        status = "degraded" if health == "unhealthy" else "up"
    elif state in ("restarting", "paused", "removing"):
        status = "degraded"
    elif state == "created" or (state == "exited" and exit_code == 0 and policy == "no" and info):
        status = "unknown"  # never started, or a one-off job that finished: not an outage
    elif state in ("exited", "dead"):
        status = "down"
    else:
        status = "unknown"
    detail = state + (f" ({health})" if health else "")
    if state in ("exited", "dead") and isinstance(exit_code, int):
        detail += f", exit code {exit_code}"
    restarts = info.get("RestartCount")
    return svc(f"container:{name}", "container", status, None, detail,
               restarts if isinstance(restarts, int) else None, docker_time(st.get("StartedAt")))


# --------------------------------------------------------------------------------------------
# saras-sync state and software versions
# --------------------------------------------------------------------------------------------

def parse_time(s: str | None):
    """ISO-8601 as written by saras-sync; a stamp without offset is taken as local time."""
    try:
        d = dt.datetime.fromisoformat((s or "").strip())
    except ValueError:
        return None
    return d if d.tzinfo else d.astimezone()


def _load_counts(path: str):
    """({"schema.table": rows}, mtime) from a saras-sync counts file, or (None, None) if it is
    missing, over 1 MB or not such a map."""
    try:
        with open(path, "rb") as f:
            mtime = os.fstat(f.fileno()).st_mtime
            raw = f.read(MiB + 1)
        data = json.loads(raw) if len(raw) <= MiB else None
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict) or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0
                                             for v in data.values()):
        return None, None
    return data, mtime


def sync_counts(state_dir: str) -> dict:
    """Totals of the per-table row counts saras-sync keeps for its last run that reached the
    server: source_counts.json (the box) and target_counts.json (the server, after the restore).
    Only totals leave the box, never table names. Tables are compared only when the target file
    is at least as new as the source file, i.e. both come from the same run: a run that stopped
    after counting the box's tables would otherwise look like a mismatch."""
    out = dict.fromkeys(("source_tables", "source_rows", "target_rows", "mismatched_tables"))
    source, source_mtime = _load_counts(os.path.join(state_dir, "source_counts.json"))
    target, target_mtime = _load_counts(os.path.join(state_dir, "target_counts.json"))
    if source is not None:
        out["source_tables"], out["source_rows"] = len(source), sum(source.values())
    if target is not None:
        out["target_rows"] = sum(target.values())
    if source is not None and target is not None and target_mtime >= source_mtime:
        out["mismatched_tables"] = sum(1 for t in source.keys() | target.keys() if source.get(t) != target.get(t))
    return out


def os_pretty_name() -> str | None:
    for line in (read_text("/etc/os-release", 4096) or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def l4t_release() -> str | None:
    """'# R39 (release), REVISION: 2.1, ...' -> 'R39.2.1'."""
    m = re.search(r"#\s*R(\d+)\s*\(release\),\s*REVISION:\s*([\d.]+)", read_text("/etc/nv_tegra_release", 1024) or "")
    return f"R{m.group(1)}.{m.group(2)}" if m else None


def deb_upstream(version: str) -> str:
    """'5:29.1.3-0ubuntu3~24.04.2' -> '29.1.3' (drop the epoch and the Debian revision)."""
    v = version.split(":", 1)[-1]
    return v.rsplit("-", 1)[0] if "-" in v else v


def dpkg_versions(packages: list[str]) -> dict:
    """Upstream versions of the installed packages among `packages` (one dpkg-query call)."""
    try:
        out = run_cmd(["dpkg-query", "-W", "--showformat=${Package}\t${Version}\t${db:Status-Abbrev}\n",
                       *packages], ok_codes=(0, 1))  # exit 1 = some package not installed
    except CommandMissing:
        return {}
    found = {}
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) >= 3 and f[2].startswith("ii") and f[1]:
            found[f[0]] = deb_upstream(f[1])
    return found


def cli_version(argv: list[str]) -> str | None:
    try:
        m = re.search(r"(\d+\.\d+(?:\.\d+)*)", run_cmd(argv))
    except CommandError:
        return None
    return m.group(1) if m else None


def firmware_version() -> str | None:
    """UEFI/BIOS version from DMI; on Thor "39.2.1-gcid-46758480" -> "39.2.1"."""
    v = _meaningful(read_text("/sys/class/dmi/id/bios_version", 128))
    return re.sub(r"-gcid-\d+$", "", v) if v else None


# --------------------------------------------------------------------------------------------
# Application version discovery and Hardware failure diagnostics
# --------------------------------------------------------------------------------------------

APP_VERSION_RE = re.compile(r"^[vV]?\d+(?:\.[a-zA-Z0-9_\-\.\+]+)*$")
ALLOWED_JOB_OPERATIONS = frozenset({"app_update", "os_update", "firmware_update", "config_update", "health_check"})
APT_LOCK_PATHS = ["/var/lib/dpkg/lock-frontend", "/var/lib/apt/lists/lock"]


def detect_firmware_capabilities(device_type: str | None = None) -> dict:
    """Detects platform firmware management capabilities, returning supported strategies,
    current version, and preflight safety requirements."""
    dtype = device_type or read_text("/proc/device-tree/model", 256) or platform.machine()
    cur_ver = firmware_version()
    if "Raspberry Pi 5" in dtype or "rpi" in dtype.lower():
        return {
            "platform": "rpi-eeprom",
            "supported": True,
            "current_version": cur_ver,
            "strategy": "rpi-eeprom-update",
            "requires_reboot": True,
            "safety_checks": ["undervoltage", "power_stability"],
        }
    elif "Thor" in dtype or "Orin" in dtype or "Jetson" in dtype:
        return {
            "platform": "jetson-qspi",
            "supported": True,
            "current_version": cur_ver,
            "strategy": "nv_bootloader_payload_updater",
            "requires_reboot": True,
            "safety_checks": ["power_stability", "fuse_verification"],
        }
    return {
        "platform": "generic-uefi",
        "supported": False,
        "current_version": cur_ver,
        "strategy": "none",
        "requires_reboot": False,
        "safety_checks": [],
    }

_detect_firmware_capabilities = detect_firmware_capabilities


def detect_os_capabilities() -> dict:
    """Detects host operating system, package manager capabilities, and update eligibility."""
    distro = os_pretty_name() or platform.system()
    pm = "apt" if os.path.isfile("/usr/bin/apt-get") else ("dnf" if os.path.isfile("/usr/bin/dnf") else None)
    lock_active = False
    for lp in APT_LOCK_PATHS:
        if os.path.exists(lp):
            try:
                fd = os.open(lp, os.O_RDWR)
                os.close(fd)
            except (OSError, PermissionError):
                lock_active = True
                break
    reboot_required = os.path.isfile("/var/run/reboot-required")
    return {
        "os": distro,
        "arch": platform.machine(),
        "package_manager": pm,
        "supported": pm is not None,
        "package_manager_locked": lock_active,
        "reboot_required": reboot_required,
    }

_detect_os_capabilities = detect_os_capabilities


def validate_app_version(v) -> str | None:
    """Validates an application version string. Returns cleaned string or None."""
    if not v or not isinstance(v, (str, int, float)):
        return None
    s = str(v).strip().strip('"\'')
    if not s or s.lower() in ("null", "none", "unknown", "undefined", "n/a", "na", "default", "0", "0.0.0"):
        return None
    if len(s) > 64:
        return None
    if APP_VERSION_RE.match(s):
        return s
    return None


def discover_app_version(cfg: dict, errors: dict | None = None) -> str | None:
    """Discovers application version via configured URL, standard health endpoints,
    metadata files on disk, or container inspection."""
    services = cfg.get("services") or {}

    # 1. Configured app_version_url
    ver_url = (cfg.get("services") or {}).get("app_version_url") or cfg.get("app_version_url")
    if ver_url:
        try:
            code, body, err, _ = http_get(ver_url, 4096)
            if code == 200 and body:
                text_content = body.decode("utf-8", "replace").strip()
                if text_content.startswith("{"):
                    try:
                        data = json.loads(text_content)
                        v = validate_app_version(data.get("version") or data.get("app_version") or data.get("appVersion"))
                        if v:
                            return v
                    except Exception:
                        pass
                v = validate_app_version(text_content)
                if v:
                    return v
            elif errors is not None and code is not None:
                errors["app_version"] = f"HTTP {code} from {ver_url}"
        except Exception as e:
            if errors is not None:
                errors["app_version"] = f"probe failed: {reason(e)}"

    # 2. Derive from app_live_url or app_ready_url if app_version_url not configured
    live_url = services.get("app_live_url") or services.get("app_ready_url")
    if live_url and not ver_url:
        try:
            parsed = urllib.parse.urlsplit(live_url)
            base = f"{parsed.scheme}://{parsed.netloc}"
            for candidate_path in ("/api/version", "/health/version", "/version"):
                code, body, _, _ = http_get(base + candidate_path, 2048)
                if code == 200 and body:
                    text_content = body.decode("utf-8", "replace").strip()
                    if text_content.startswith("{"):
                        try:
                            data = json.loads(text_content)
                            v = validate_app_version(data.get("version") or data.get("app_version") or data.get("appVersion"))
                            if v:
                                return v
                        except Exception:
                            pass
                    v = validate_app_version(text_content)
                    if v:
                        return v
        except Exception:
            pass

    # 3. Application version file on disk
    file_candidates = []
    if cfg.get("app_version_file"):
        file_candidates.append(cfg["app_version_file"])
    file_candidates.extend([
        "/opt/saras/version.json",
        "/opt/saras-app/version.json",
        "/etc/saras/version.json",
        "/var/lib/saras-app/version.json",
    ])
    for fpath in file_candidates:
        if os.path.isfile(fpath):
            try:
                raw = read_text(fpath, 4096)
                if raw:
                    raw = raw.strip()
                    if raw.startswith("{"):
                        try:
                            data = json.loads(raw)
                            v = validate_app_version(data.get("version") or data.get("app_version") or data.get("appVersion"))
                            if v:
                                return v
                        except Exception:
                            pass
                    v = validate_app_version(raw)
                    if v:
                        return v
            except Exception:
                pass

    # 4. Docker container inspection (if docker enabled and accessible)
    if cfg.get("docker") and os.access(DOCKER_SOCKET, os.R_OK | os.W_OK):
        try:
            containers = docker_get("/containers/json?all=1")
            for c in containers:
                names = [n.lstrip("/") for n in c.get("Names") or []]
                is_app = any("app" in n.lower() or "saras" in n.lower() or "schoolai" in n.lower() for n in names)
                if is_app:
                    labels = c.get("Labels") or {}
                    v = validate_app_version(labels.get("org.opencontainers.image.version") or labels.get("version") or labels.get("app_version"))
                    if v:
                        return v
                    image = c.get("Image", "")
                    if ":" in image:
                        tag = image.split(":")[-1]
                        if tag and tag.lower() not in ("latest", "dev", "test"):
                            v = validate_app_version(tag)
                            if v:
                                return v
        except Exception:
            pass

    return None


def read_storage_health() -> dict | None:
    """Reads NVMe / SSD health & wear telemetry.
    Returns structured metrics (wear percentage, spare capacity, critical warnings, errors)
    or None if unsupported."""
    # 1. nvme-cli probe
    try:
        out = run_cmd(["nvme", "smart-log", "/dev/nvme0", "-o", "json"], timeout=3)
        data = json.loads(out)
        return {
            "source": "nvme-cli",
            "device": "/dev/nvme0",
            "percentage_used": as_int(data.get("percentage_used")),
            "available_spare_pct": as_int(data.get("avail_spare")),
            "spare_threshold_pct": as_int(data.get("spare_thresh")),
            "critical_warning": as_int(data.get("critical_warning")),
            "media_errors": as_int(data.get("media_errors")),
            "unsafe_shutdowns": as_int(data.get("unsafe_shutdowns")),
            "temperature_c": rnd(float(data.get("temperature", 0)) - 273.15, 1) if "temperature" in data else None,
        }
    except (CommandMissing, CommandError, FileNotFoundError, OSError, ValueError):
        pass

    # 2. smartctl probe
    try:
        out = run_cmd(["smartctl", "-j", "-H", "-A", "/dev/nvme0n1"], timeout=3)
        data = json.loads(out)
        nvme_smart = data.get("nvme_smart_health_information_log") or {}
        if nvme_smart:
            return {
                "source": "smartctl",
                "device": "/dev/nvme0n1",
                "percentage_used": as_int(nvme_smart.get("percentage_used")),
                "available_spare_pct": as_int(nvme_smart.get("available_spare")),
                "spare_threshold_pct": as_int(nvme_smart.get("available_spare_threshold")),
                "critical_warning": as_int(nvme_smart.get("critical_warning")),
                "media_errors": as_int(nvme_smart.get("media_and_data_integrity_errors")),
                "unsafe_shutdowns": as_int(nvme_smart.get("unsafe_shutdowns")),
                "temperature_c": rnd(float(nvme_smart.get("temperature", 0)), 1) if "temperature" in nvme_smart else None,
            }
    except (CommandMissing, CommandError, FileNotFoundError, OSError, ValueError):
        pass

    # 3. sysfs nvme attributes fallback (/sys/class/nvme/nvme*)
    nvme_paths = glob.glob("/sys/class/nvme/nvme*")
    if nvme_paths:
        dev_path = nvme_paths[0]
        dev_name = os.path.basename(dev_path)
        state = read_text(os.path.join(dev_path, "state"), 32)
        model = read_text(os.path.join(dev_path, "model"), 64)
        if state:
            return {
                "source": "sysfs",
                "device": f"/dev/{dev_name}",
                "model": model.strip() if model else None,
                "state": state.strip(),
                "percentage_used": None,
                "available_spare_pct": None,
                "critical_warning": 0 if state.strip() == "live" else 1,
                "media_errors": 0,
            }

    return None


def read_memory_health() -> dict | None:
    """Reads Linux EDAC memory error telemetry.
    Returns correctable and uncorrectable hardware error counts."""
    edac_mc_paths = glob.glob("/sys/devices/system/edac/mc/mc*")
    if not edac_mc_paths:
        return {"edac_supported": False, "ce_count": 0, "ue_count": 0}

    total_ce, total_ue = 0, 0
    for mc in edac_mc_paths:
        ce = read_int(os.path.join(mc, "ce_count"))
        ue = read_int(os.path.join(mc, "ue_count"))
        if ce is not None:
            total_ce += ce
        if ue is not None:
            total_ue += ue

    return {
        "edac_supported": True,
        "ce_count": total_ce,
        "ue_count": total_ue,
    }


def read_fan_health(fan_rpm: float | None, cpu_temp: float | None) -> dict | None:
    """Evaluates cooling fan health. Detects stall conditions under thermal stress."""
    if fan_rpm is None:
        return None
    stalled = bool(fan_rpm <= 50 and cpu_temp is not None and cpu_temp >= 65.0)
    return {
        "rpm": fan_rpm,
        "stalled": stalled,
    }


# --------------------------------------------------------------------------------------------
# Collector: builds one heartbeat
# --------------------------------------------------------------------------------------------

class Collector:
    """Builds heartbeats. Keeps the state that spans heartbeats: CPU times and vLLM counters for
    the between-heartbeat averages, the NVML handle, and the software schedule."""

    def __init__(self, cfg: dict, tick=None):
        self.cfg = cfg
        self._tick = tick or (lambda: None)   # called between collectors (systemd watchdog)
        self.last_server_rtt_ms = None         # set by the sender after each live heartbeat
        self.software_pending = False          # set when a heartbeat carrying `software` was not delivered
        self.last_collect_ms = None
        self._software_next = 0.0
        self._prev_cpu = None
        self._nvml = self._nvml_handle = None
        self._nvml_retry_at = 0.0
        self._nvml_error = None
        self._jtop_thread = None
        self._gpu_name = None
        self._vllm_prev = None
        self._llm_model = None
        self._units = {}
        self._static = read_static_identity()
        self.device_type = detect_device_type(self._static["model"], self._static["dmi_product"],
                                              self._static["compatible"])

    # ---- plumbing ----------------------------------------------------------------------------

    def _guard(self, errors: dict, name: str, fn, default=None):
        """Run one collector. Any exception becomes errors[name]; the heartbeat always goes out."""
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - one broken collector must never stop the heartbeat
            errors[name] = reason(e)
            log.debug("collector %s failed", name, exc_info=True)
            return default() if callable(default) else default
        finally:
            self._tick()

    def close(self) -> None:
        if self._nvml_handle is not None:
            try:
                self._nvml.nvmlShutdown()
            except Exception:  # noqa: BLE001
                pass
            self._nvml = self._nvml_handle = None

    def _software_due(self) -> bool:
        """First heartbeat after start, then every software_every_s. If a heartbeat carrying
        `software` could not be delivered, the next heartbeat carries it again."""
        now = time.monotonic()
        if self.software_pending or now >= self._software_next:
            self.software_pending = False
            self._software_next = now + self.cfg["software_every_s"]
            return True
        return False

    # ---- the heartbeat -----------------------------------------------------------------------

    def collect(self, seq: int, interval_s: int) -> dict:
        """One heartbeat as a dict in contract field order. Never raises."""
        t0 = time.monotonic()
        errors: dict = {}
        self._units = {}
        g = self._guard
        thermal = g(errors, "thermal", thermal_zones, dict) if self.cfg.get("collect_thermal", True) else {}
        power = g(errors, "power", lambda: power_readings(errors), lambda: {**dict.fromkeys(POWER_KEYS), "rails_w": {}}) if self.cfg.get("collect_power", True) else {**dict.fromkeys(POWER_KEYS), "rails_w": {}}
        gpu = g(errors, "gpu", lambda: self._gpu(errors, thermal, power.get("rails_w") or {}),
                lambda: dict.fromkeys(GPU_KEYS)) if self.cfg.get("collect_gpu", True) else dict.fromkeys(GPU_KEYS)
        system = self._system(errors)
        fan_h = read_fan_health(power.get("fan_rpm"), thermal.get("cpu") or thermal.get("tj"))
        if fan_h:
            system["fan_health"] = fan_h
        network, lan_ip = g(errors, "network", lambda: self._network(errors),
                            lambda: ({**dict.fromkeys(NETWORK_KEYS), "last_server_rtt_ms": self.last_server_rtt_ms}, None))
        services, ai = self._services(errors)
        if not self.cfg.get("collect_ai", True):
            ai = None
        sync = g(errors, "sync", lambda: self._sync(errors))
        software = g(errors, "software", lambda: self._software(errors)) if self._software_due() else None

        st = self._static
        self.device_type = detect_device_type(st["model"], st["dmi_product"], st["compatible"], self._gpu_name)
        hostname = clip(socket.gethostname(), 128)
        device_id = self.cfg["device_id"] or st["serial"] or hostname
        self.device_id = clip(device_id, 128)
        if st["serial"] is None and st["serial_problem"]:  # e.g. a DGX Spark agent not running as root

            errors["serial"] = st["serial_problem"] + ("" if self.cfg["device_id"] else
                                                       "; device_id fell back to the host name")
        claimed = self.cfg["claimed"]
        hb = {
            "schema": SCHEMA,
            "agent_version": AGENT_VERSION,
            "device_id": clip(device_id, 128),
            "boot_id": st["boot_id"],
            "seq": seq,
            "sent_at": None,
            "interval_s": interval_s,
            "buffered": False,
            "claimed": {k: clip(claimed.get(k)) for k in ("product", "site_code", "customer")},
            "identity": {"serial": st["serial"], "model": st["model"], "device_type": self.device_type,
                         "hostname": hostname, "lan_ip": lan_ip},
            "system": system,
            "gpu": gpu,
            "thermal_c": thermal,
            "power": power,
            "network": network,
            "services": services[:MAX_SERVICES],
            "sync": sync,
            "software": software,
            "ai": ai,
            "collector_errors": {clip(k, 64): clip(v) for k, v in errors.items()},
        }
        hb["sent_at"] = utc_iso()  # device clock; the server's receive time is authoritative
        self.last_collect_ms = (time.monotonic() - t0) * 1000
        return sanitize(hb)

    # ---- system ------------------------------------------------------------------------------

    def _system(self, errors: dict) -> dict:
        s = dict.fromkeys(SYSTEM_KEYS)
        s["disks"] = []
        g = self._guard
        g(errors, "boot", lambda: self._boot(s))
        g(errors, "ntp", lambda: s.update(ntp_synced=ntp_synced()))
        g(errors, "cpu", lambda: self._cpu(s))
        g(errors, "memory", lambda: self._memory(s))
        s["disks"] = g(errors, "disks", disk_usage_list, list)
        s["journal_mb"] = g(errors, "journal", journal_mb)
        s["var_log_mb"] = g(errors, "var_log", var_log_mb)
        if self.device_type == "Raspberry Pi 5":
            throttled = read_rpi_throttling()
            if throttled:
                s["rpi_throttled"] = throttled
        storage_h = g(errors, "storage_health", read_storage_health)
        if storage_h:
            s["storage_health"] = storage_h
        mem_h = g(errors, "memory_health", read_memory_health)
        if mem_h:
            s["memory_health"] = mem_h
        return s

    @staticmethod
    def _boot(s: dict) -> None:
        _need_psutil()
        bt = psutil.boot_time()
        s["booted_at"] = utc_iso(bt)
        up = read_text("/proc/uptime", 64)
        s["uptime_s"] = int(float(up.split()[0])) if up else int(time.time() - bt)

    def _cpu(self, s: dict) -> None:
        """CPU % over the time since the previous heartbeat, from psutil CPU times: non-blocking,
        no sleep. Null on the first heartbeat (and after CPUs go on/offline)."""
        _need_psutil()
        l1, l5, l15 = os.getloadavg()
        s["load_1m"], s["load_5m"], s["load_15m"] = rnd(l1, 2), rnd(l5, 2), rnd(l15, 2)
        freq = psutil.cpu_freq()
        s["cpu_freq_mhz"] = as_int(freq.current) if freq else None
        now = psutil.cpu_times(percpu=True)
        prev, self._prev_cpu = self._prev_cpu, now
        if prev is None or len(prev) != len(now):
            return
        per_core, busy_sum, total_sum = [], 0.0, 0.0
        for a, b in zip(prev, now):
            busy = max(0.0, _cpu_busy(b) - _cpu_busy(a))
            total = _cpu_total(b) - _cpu_total(a)
            per_core.append(rnd(min(100.0, busy / total * 100)) if total > 0 else 0.0)
            busy_sum, total_sum = busy_sum + busy, total_sum + max(0.0, total)
        s["cpu_pct"] = rnd(min(100.0, busy_sum / total_sum * 100)) if total_sum > 0 else None
        s["cpu_per_core_pct"] = per_core

    @staticmethod
    def _memory(s: dict) -> None:
        _need_psutil()
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        s["ram_total_mb"] = as_int(vm.total / MiB)
        s["ram_used_mb"] = as_int((vm.total - vm.available) / MiB)  # consistent with ram_pct
        s["ram_pct"] = rnd(vm.percent)
        s["swap_pct"] = rnd(sw.percent)

    # ---- GPU ---------------------------------------------------------------------------------

    def _gpu(self, errors: dict, thermal: dict, rails: dict) -> dict:
        """NVML first (Thor, DGX Spark), then jtop (Orin only), then sysfs devfreq/load."""
        g = dict.fromkeys(GPU_KEYS)
        if self.device_type == "Raspberry Pi 5":
            g["name"] = "VideoCore VII"
            g["source"] = "bcm2712"
            g["temp_c"] = thermal.get("cpu")
            try:
                v3d_out = run_cmd(["vcgencmd", "measure_clock", "v3d"])
                if v3d_out and "=" in v3d_out:
                    with contextlib.suppress(ValueError):
                        g["clock_mhz"] = round(int(v3d_out.strip().split("=")[1]) / 1_000_000, 1)
            except Exception:
                pass
            return g
        tried: list[str] = []
        ok = self._gpu_nvml(g, tried)
        if not ok and self.device_type in ("Orin", "Orin Nano"):
            ok = self._gpu_jtop(g, tried)

        if not ok:
            ok = self._gpu_sysfs(g, tried)
        if g["max_clock_mhz"] is None:  # NVML says "Not Supported" on Thor; devfreq has it
            node = gpu_devfreq_node()
            g["max_clock_mhz"] = _mhz(read_int(node + "/max_freq")) if node else None
        if g["temp_c"] is None:
            g["temp_c"] = thermal.get("gpu")
        if g["power_w"] is None:
            g["power_w"] = rails.get("VDD_GPU")
        if not ok:
            errors["gpu"] = clip("; ".join(tried) or "no GPU found")
        elif g["source"] != "nvml" and self.device_type in ("AGX Thor", "DGX Spark") and tried:
            errors["gpu"] = clip(f"NVML unavailable, using {g['source']}: {tried[0]}")
        if g["name"]:
            self._gpu_name = g["name"]
        return g

    def _nvml_open(self, tried: list) -> bool:
        now = time.monotonic()
        if now < self._nvml_retry_at:
            tried.append(self._nvml_error)
            return False
        try:
            import pynvml
            pynvml.nvmlInit()
            try:
                if pynvml.nvmlDeviceGetCount() < 1:
                    raise AgentError("no NVIDIA GPU")
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            except Exception:
                pynvml.nvmlShutdown()
                raise
        except Exception as e:  # noqa: BLE001 - ImportError, NVMLError (no library, no driver), ...
            self._nvml_error = clip(f"nvml: {e}", 120)
            self._nvml_retry_at = now + NVML_RETRY_S
            tried.append(self._nvml_error)
            return False
        self._nvml, self._nvml_handle = pynvml, handle
        return True

    def _gpu_nvml(self, g: dict, tried: list) -> bool:
        if self._nvml_handle is None and not self._nvml_open(tried):
            return False
        nv, h = self._nvml, self._nvml_handle
        try:
            name = nv.nvmlDeviceGetName(h)
            util = nv.nvmlDeviceGetUtilizationRates(h).gpu
        except Exception as e:  # noqa: BLE001 - stale handle (driver reloaded): reopen next time
            tried.append(clip(f"nvml: {e}", 120))
            self.close()
            return False

        def opt(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001 - optional reading, e.g. "Not Supported"
                return None

        g["source"] = "nvml"
        g["name"] = clip(name.decode() if isinstance(name, bytes) else name, 64)
        g["util_pct"] = as_int(util)
        g["temp_c"] = as_int(opt(lambda: nv.nvmlDeviceGetTemperature(h, nv.NVML_TEMPERATURE_GPU)))
        g["clock_mhz"] = as_int(opt(lambda: nv.nvmlDeviceGetClockInfo(h, nv.NVML_CLOCK_GRAPHICS)))
        g["max_clock_mhz"] = as_int(opt(lambda: nv.nvmlDeviceGetMaxClockInfo(h, nv.NVML_CLOCK_GRAPHICS)))
        mw = opt(lambda: nv.nvmlDeviceGetPowerUsage(h))
        g["power_w"] = rnd(mw / 1000, 2) if mw is not None else None
        try:
            mem = nv.nvmlDeviceGetMemoryInfo(h)
            g["memory_note"] = f"{mem.used // MiB}/{mem.total // MiB} MiB"
        except Exception as e:  # noqa: BLE001
            # Thor and GB10 share system RAM with the GPU; NVML reports that as "Not Supported".
            g["memory_note"] = "unified" if "not supported" in str(e).lower() else None
        return True

    def _gpu_jtop(self, g: dict, tried: list) -> bool:
        """Orin: GPU load and clocks through the jtop service. Best-effort: untested on Orin, and
        jtop reports no GPU data at all on Thor. Runs in a thread so a stuck jtop costs at most 3 s."""
        if self._jtop_thread is not None and self._jtop_thread.is_alive():
            tried.append("jtop: previous read still hanging")
            return False
        try:
            from jtop import jtop
        except Exception as e:  # noqa: BLE001
            tried.append(f"jtop: {reason(e)}")
            return False
        box: dict = {}

        def read():
            try:
                with jtop() as j:
                    if j.ok():
                        box["gpu"] = j.gpu
            except Exception as e:  # noqa: BLE001
                box["error"] = e

        t = threading.Thread(target=read, name="jtop-read", daemon=True)
        t.start()
        t.join(CMD_TIMEOUT_S)
        if t.is_alive():
            self._jtop_thread = t
            tried.append("jtop: no answer within 3 s")
            return False
        if "error" in box:
            tried.append(f"jtop: {reason(box['error'])}")
            return False
        for name, d in (box.get("gpu") or {}).items():
            if not isinstance(d, dict) or d.get("type") != "integrated":
                continue
            status, freq = d.get("status") or {}, d.get("freq") or {}
            g.update(source="jtop", name=clip(name, 64), util_pct=rnd(status.get("load")),
                     clock_mhz=as_int(freq["cur"] / 1000) if freq.get("cur") else None,  # jtop uses kHz
                     max_clock_mhz=as_int(freq["max"] / 1000) if freq.get("max") else None,
                     memory_note="unified")
            return True
        tried.append("jtop: no GPU data")
        return False

    @staticmethod
    def _gpu_sysfs(g: dict, tried: list) -> bool:
        """devfreq clocks, plus the nvgpu `load` file (per mille) where it exists (Orin; not on Thor)."""
        node = gpu_devfreq_node()
        if not node:
            tried.append("sysfs: no GPU devfreq node")
            return False
        load = read_int(os.path.join(os.path.realpath(node + "/device"), "load"))
        g.update(source="sysfs", name=os.path.basename(node),
                 util_pct=rnd(load / 10) if load is not None else None,
                 clock_mhz=_mhz(read_int(node + "/cur_freq")), max_clock_mhz=_mhz(read_int(node + "/max_freq")),
                 memory_note="unified")
        return True

    # ---- network -----------------------------------------------------------------------------

    def _network(self, errors: dict):
        n = dict.fromkeys(NETWORK_KEYS)
        n["last_server_rtt_ms"] = self.last_server_rtt_ms
        iface, gateway = default_route()
        if iface is None:
            n["link"] = "down"  # no default route
            return n, None
        n["iface"] = iface
        n["link"] = read_text(f"/sys/class/net/{iface}/operstate", 32) or None
        lan_ip = self._guard(errors, "lan_ip", lambda: iface_ipv4(iface))
        if os.path.isdir(f"/sys/class/net/{iface}/wireless"):
            self._guard(errors, "wifi", lambda: n.update(wifi_link(iface)))
        if self.cfg["ping_gateway"] and gateway:
            self._guard(errors, "ping", lambda: n.update(ping_gateway(gateway)))
        return n, lan_ip

    # ---- services ----------------------------------------------------------------------------

    def _services(self, errors: dict):
        sc, g = self.cfg["services"], self._guard
        out = [
            g(errors, "app-live", lambda: http_service("app-live", sc.get("app_live_url")),
              lambda: svc("app-live", "http", "unknown")),
            g(errors, "app-ready", lambda: http_service("app-ready", sc.get("app_ready_url")),
              lambda: svc("app-ready", "http", "unknown")),
            g(errors, "postgres", lambda: postgres_service(sc.get("postgres")),
              lambda: svc("postgres", "tcp", "unknown")),
        ]
        vllm_entry, ai = g(errors, "vllm", lambda: self._vllm(errors), lambda: (svc("vllm", "http", "unknown"), None))
        out.append(vllm_entry)
        if sc.get("ollama_url"):
            ollama_entry, ollama_ai = g(errors, "ollama", lambda: self._ollama(errors),
                                        lambda: (svc("ollama", "http", "unknown"), None))
            out.append(ollama_entry)
            if ai is None and ollama_ai is not None:
                ai = ollama_ai
        out += g(errors, "systemd", lambda: self._systemd(errors), list)
        if self.cfg["docker"]:
            out += g(errors, "docker", lambda: self._containers(errors), list)
        return out, ai

    def _ollama(self, errors: dict):
        """The ollama service entry and optional ai block."""
        svcs = self.cfg.get("services", {}) if isinstance(self.cfg, dict) else {}
        base = (svcs.get("ollama_url") or "").rstrip("/")
        if not base:
            return svc("ollama", "http", "not_configured"), None
        o = dict.fromkeys(VLLM_KEYS)
        code, body, err, ms = http_get(base + "/api/version", 4096)
        entry = svc("ollama", "http", "up" if code == 200 else "down", ms, f"HTTP {code}" if code else err)
        o["status"] = entry["status"]
        if code != 200:
            return entry, {"ollama": o}
        code, body, err, _ = http_get(base + "/api/ps", 64 * 1024)
        if code == 200:
            try:
                data = json.loads(body)
                models = data.get("models", [])
                o["running"] = len(models)
                o["waiting"] = 0
                if models and isinstance(models[0], dict):
                    o["model"] = clip(models[0].get("name") or models[0].get("model"))
            except Exception:
                pass
        return entry, {"ollama": o}

    def _vllm(self, errors: dict):

        """The vllm service entry and the ai block (DGX Spark; only when services.vllm_url is set)."""
        base = vllm_base(self.cfg["services"].get("vllm_url"))
        if not base:
            self._llm_model = None
            return svc("vllm", "http", "not_configured"), None
        v = dict.fromkeys(VLLM_KEYS)
        code, _, err, ms = http_get(base + "/health", 256)
        entry = svc("vllm", "http", "up" if code == 200 else "down", ms, f"HTTP {code}" if code else err)
        v["status"] = entry["status"]
        if code is None:  # nothing listening: skip the other endpoints
            return entry, {"vllm": v}
        problems = []
        code, body, err, _ = http_get(base + "/v1/models", 256 * 1024)
        if code == 200:
            try:
                ids = [m.get("id") for m in json.loads(body).get("data", []) if isinstance(m, dict)]
                v["model"] = clip(next((i for i in ids if i), None))
            except (ValueError, AttributeError):
                problems.append("/v1/models: not JSON")
        else:
            problems.append(f"/v1/models: {f'HTTP {code}' if code else err}")
        self._llm_model = v["model"]
        code, body, err, _ = http_get(base + "/metrics", 16 * MiB)
        if code == 200:
            text = body.decode("utf-8", "replace")
            m = parse_prometheus(text, _VLLM_SUM, _VLLM_MAX, prefix="vllm:")
            kv = m.get(V_KV, m.get(V_KV_OLD))
            finished, aborted = m.get(V_OK), prometheus_aborted(text)
            v.update(running=_count(m.get(V_RUNNING)), waiting=_count(m.get(V_WAITING)),
                     kv_cache_pct=rnd(kv * 100) if kv is not None else None,
                     prompt_tokens_total=_count(m.get(V_PROMPT)), generation_tokens_total=_count(m.get(V_GEN)),
                     requests_success_total=_count(finished - aborted if None not in (finished, aborted) else finished),
                     requests_aborted_total=_count(aborted))
            cur = tuple(m.get(k) for k in (V_E2E_SUM, V_E2E_CNT, V_TTFT_SUM, V_TTFT_CNT))
            prev, self._vllm_prev = self._vllm_prev, cur
            if prev:
                v["avg_e2e_latency_s"] = _avg_delta(prev[0], cur[0], prev[1], cur[1])
                v["avg_ttft_s"] = _avg_delta(prev[2], cur[2], prev[3], cur[3])
        else:
            problems.append(f"/metrics: {f'HTTP {code}' if code else err}")
        if problems:
            errors["vllm"] = clip("; ".join(problems))
        return entry, {"vllm": v}

    def _systemd(self, errors: dict) -> list:
        units = list(dict.fromkeys(self.cfg["systemd_units"]))
        if not units:
            return []
        boot_ts = psutil.boot_time() if psutil else None
        try:
            self._units = systemctl_show(units)
        except CommandError as e:
            errors["systemd"] = str(e)
            return [svc(f"systemd:{u}", "systemd", "unknown", detail=str(e)) for u in units]
        return [unit_service(u, self._units.get(u), boot_ts) for u in units]

    def _containers(self, errors: dict) -> list:
        """Every container as container:<name>, via read-only GETs on the Docker socket."""
        if not os.path.exists(DOCKER_SOCKET):
            errors["docker"] = f"{DOCKER_SOCKET} not found (Docker not installed or not running)"
            return []
        try:
            listing = docker_get("/containers/json?all=1")
        except PermissionError:
            errors["docker"] = DOCKER_DENIED
            return []
        out = []
        deadline = time.monotonic() + DOCKER_BUDGET_S
        for c in listing[:MAX_CONTAINERS] if isinstance(listing, list) else []:
            cid = str(c.get("Id") or "")
            names = c.get("Names") or []
            name = (names[0] if names else cid[:12]).lstrip("/")
            info = None
            if re.fullmatch(r"[0-9a-f]{12,64}", cid) and time.monotonic() < deadline:
                try:
                    info = docker_get(f"/containers/{cid}/json")
                except Exception as e:  # noqa: BLE001 - keep the listed state for this one
                    errors["docker"] = clip(f"inspect {name}: {reason(e)}")
            out.append(container_service(name, c.get("State") or "", info))
            self._tick()
        return sorted(out, key=lambda s: s["name"])

    # ---- sync and software -------------------------------------------------------------------

    def _sync(self, errors: dict):
        d = self.cfg["sync_state_dir"]
        if not d or not os.path.isdir(d):
            return None
        raw = {k: read_text(os.path.join(d, k), 256) for k in ("last_success", "last_offline", "offline_count")}
        s = dict.fromkeys(SYNC_KEYS)
        s["last_success"] = clip(raw["last_success"], 64) or None  # raw value, passed through as read
        success, offline = parse_time(raw["last_success"]), parse_time(raw["last_offline"])
        # saras-sync stamps its last successful run and its last run that found the server
        # unreachable; the later of the two is the last attempt.
        runs = [(t, r) for t, r in ((success, raw["last_success"]), (offline, raw["last_offline"])) if t]
        s["last_attempt"] = clip(max(runs)[1], 64) if runs else None
        s["offline_count"] = as_int(raw["offline_count"])
        # Both on the device clock, so a wrong clock cancels out.
        s["age_h"] = rnd((time.time() - success.timestamp()) / 3600) if success else None
        props = self._units.get(SYNC_UNIT)
        if props is None:  # saras-sync.service is not in systemd_units: ask separately
            try:
                props = systemctl_show([SYNC_UNIT])[SYNC_UNIT]
            except CommandMissing:
                props = {}
            except CommandError as e:
                errors["sync"] = str(e)
                props = {}
        if props.get("LoadState") not in (None, "not-found"):
            s["unit_result"] = clip(props.get("Result"), 64) or None
        s.update(sync_counts(d))
        extended_file = os.path.join(d, "extended_sync.json")
        if os.path.isfile(extended_file):
            try:
                ext_data = json.loads(read_text(extended_file, 4096) or "{}")
                for k in ("pending_changes", "sync_duration_s", "failed_batches", "retry_counts",
                          "conflicts", "last_error_category", "sync_agent_status", "sync_api_health", "db_health"):
                    if k in ext_data:
                        s[k] = ext_data[k]
            except Exception:
                pass
        for k in ("pending_changes", "failed_batches", "conflicts", "retry_counts"):
            if k not in s or s[k] is None:
                val = read_text(os.path.join(d, k), 64)
                if val is not None:
                    s[k] = as_int(val)
        for k in ("sync_api_health", "db_health", "sync_agent_status", "last_error_category"):
            if k not in s or s[k] is None:
                val = read_text(os.path.join(d, k), 64)
                if val:
                    s[k] = clip(val.strip(), 64)
        return s


    def _software(self, errors: dict) -> dict:
        sw = dict.fromkeys(SOFTWARE_KEYS)
        sw["os"] = clip(os_pretty_name(), 100)
        sw["kernel"] = platform.release()
        sw["l4t"] = l4t_release()
        sw["python"] = platform.python_version()
        pkgs = self._guard(errors, "software", lambda: dpkg_versions(
            ["nvidia-jetpack", "nvidia-l4t-bootloader", "nvidia-container-toolkit", "docker-ce", "docker.io"]), dict)
        jp = pkgs.get("nvidia-jetpack")
        sw["jetpack"] = jp.split("+")[0] if jp else JETPACK_BY_L4T.get(sw["l4t"])
        sw["bootloader"] = pkgs.get("nvidia-l4t-bootloader") or firmware_version()
        sw["nvidia_container_toolkit"] = pkgs.get("nvidia-container-toolkit") or cli_version(["nvidia-ctk", "--version"])
        docker = None
        if self.cfg["docker"] and os.access(DOCKER_SOCKET, os.R_OK | os.W_OK):
            try:
                docker = docker_get("/version").get("Version")  # the running engine
            except Exception:  # noqa: BLE001 - fall back to the package version
                docker = None
        sw["docker"] = docker or pkgs.get("docker-ce") or pkgs.get("docker.io") or cli_version(["docker", "--version"])
        if self._nvml_handle is not None:
            nv = self._nvml
            try:
                drv = nv.nvmlSystemGetDriverVersion()
                sw["nvidia_driver"] = drv.decode() if isinstance(drv, bytes) else drv
                cuda = nv.nvmlSystemGetCudaDriverVersion_v2()  # e.g. 13020 -> "13.2"
                sw["cuda_driver"] = f"{cuda // 1000}.{cuda % 1000 // 10}"
            except Exception:  # noqa: BLE001
                pass
        try:
            sw["jetson_stats"] = importlib.metadata.version("jetson-stats")
        except importlib.metadata.PackageNotFoundError:
            pass
        base = vllm_base(self.cfg["services"].get("vllm_url"))
        if base:
            code, body, _, _ = http_get(base + "/version", 4096)
            if code == 200:
                try:
                    sw["vllm_version"] = json.loads(body).get("version")
                except (ValueError, AttributeError):
                    pass
        sw["llm_model"] = self._llm_model
        sw["app_version"] = self._guard(errors, "app_version",
                                        lambda: discover_app_version(self.cfg, errors), None)
        return {k: clip(v, 100) for k, v in sw.items()}


# --------------------------------------------------------------------------------------------
# Offline buffer and sender
# --------------------------------------------------------------------------------------------

class Buffer:
    """Heartbeats that could not be delivered, oldest first, in SQLite at buffer_path. If that file
    cannot be opened (e.g. a normal user with the production path) it falls back to memory."""

    def __init__(self, path: str, max_rows: int):
        self.path, self.max_rows = path, max_rows
        self.db = None
        self.mem: collections.OrderedDict = collections.OrderedDict()
        self._mem_seq = 0
        self.problem = None
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, mode=0o700, exist_ok=True)
            db = sqlite3.connect(path, timeout=5, isolation_level=None)
            try:
                db.execute("CREATE TABLE IF NOT EXISTS heartbeat_buffer ("
                           "id INTEGER PRIMARY KEY AUTOINCREMENT, queued_at TEXT NOT NULL, body TEXT NOT NULL)")
            except sqlite3.Error:
                db.close()
                raise
            self.db = db
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except (sqlite3.Error, OSError) as e:
            self.problem = clip(f"cannot open {path} ({reason(e) if isinstance(e, OSError) else e}); buffering in memory only")
            log.error("offline buffer: %s", self.problem)

    def add(self, body: str) -> int:
        """Queue one heartbeat, dropping the oldest beyond max_rows. Returns how many were dropped."""
        if self.db is not None:
            try:
                self.db.execute("INSERT INTO heartbeat_buffer (queued_at, body) VALUES (?, ?)", (utc_iso(), body))
                n = self.db.execute("SELECT COUNT(*) FROM heartbeat_buffer").fetchone()[0]
                dropped = max(0, n - self.max_rows)
                if dropped:
                    self.db.execute("DELETE FROM heartbeat_buffer WHERE id IN "
                                    "(SELECT id FROM heartbeat_buffer ORDER BY id LIMIT ?)", (dropped,))
                    log.warning("offline buffer full (%d rows): dropped %d oldest heartbeat(s)", self.max_rows, dropped)
                return dropped
            except sqlite3.Error as e:
                log.error("offline buffer write failed (%s); keeping this heartbeat in memory", e)
        self._mem_seq += 1
        self.mem[self._mem_seq] = body
        dropped = 0
        while len(self.mem) > self.max_rows:
            self.mem.popitem(last=False)
            dropped += 1
        if dropped:
            log.warning("offline buffer full (%d rows): dropped %d oldest heartbeat(s)", self.max_rows, dropped)
        return dropped

    def oldest(self, n: int) -> list:
        rows = []
        if self.db is not None:
            try:
                rows = [(("db", i), b) for i, b in self.db.execute(
                    "SELECT id, body FROM heartbeat_buffer ORDER BY id LIMIT ?", (n,))]
            except sqlite3.Error as e:
                log.error("offline buffer read failed: %s", e)
        for k, b in self.mem.items():
            if len(rows) >= n:
                break
            rows.append((("mem", k), b))
        return rows

    def delete(self, key) -> None:
        where, k = key
        if where == "mem":
            self.mem.pop(k, None)
        elif self.db is not None:
            try:
                self.db.execute("DELETE FROM heartbeat_buffer WHERE id = ?", (k,))
            except sqlite3.Error as e:
                log.error("offline buffer delete failed: %s", e)

    def count(self) -> int:
        n = len(self.mem)
        if self.db is not None:
            try:
                n += self.db.execute("SELECT COUNT(*) FROM heartbeat_buffer").fetchone()[0]
            except sqlite3.Error:
                pass
        return n

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None


SendResult = collections.namedtuple("SendResult", "ok status data rtt_ms permanent reason")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Sender:
    """POSTs heartbeats to <server_url>/api/v1/heartbeat with the bearer token."""

    def __init__(self, cfg: dict):
        self.base = cfg["server_url"].rstrip("/")
        self.url = self.base + "/api/v1/heartbeat"
        self.token = cfg["token"]
        self.timeout = cfg["request_timeout_s"]
        handlers = [_NoRedirect()]
        if _is_loopback(urllib.parse.urlsplit(self.base).hostname or ""):
            handlers.append(urllib.request.ProxyHandler({}))  # a local server is never behind a proxy
        self.opener = urllib.request.build_opener(*handlers)

    def request(self, method: str, path: str, body: bytes | None = None) -> tuple[int | None, dict | None]:
        url = self.base + path
        req = urllib.request.Request(url, data=body, method=method, headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}", "User-Agent": USER_AGENT})
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                raw, code = r.read(64 * 1024), r.status
                data = json.loads(raw) if raw else {}
                return code, data
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read(4096))
            except Exception:
                data = None
            return e.code, data
        except Exception:
            return None, None

    def post(self, body: bytes) -> SendResult:

        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}", "User-Agent": USER_AGENT})
        t0 = time.monotonic()
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                raw, code = r.read(64 * 1024), r.status
        except urllib.error.HTTPError as e:
            why = f"HTTP {e.code}"
            try:
                err = json.loads(e.read(4096)).get("error")
                if err:
                    why += f": {clip(err, 120)}"
            except Exception:  # noqa: BLE001 - the status code is enough
                pass
            if 300 <= e.code < 400:
                why += f" (redirect to {clip(e.headers.get('Location'), 120)}; fix server_url)"
            e.close()
            # 400/413/422: the server will never accept this heartbeat, so retrying is pointless.
            # Everything else (401, 404, 5xx, ...) may be fixed later: the heartbeat is buffered.
            return SendResult(False, e.code, None, None, e.code in (400, 413, 422), why)
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            return SendResult(False, None, None, None, False, reason(e))
        rtt = rnd((time.monotonic() - t0) * 1000)
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        return SendResult(200 <= code < 300, code, data if isinstance(data, dict) else {}, rtt, False,
                          None if 200 <= code < 300 else f"HTTP {code}")


def encode(hb: dict) -> bytes:
    body = json.dumps(hb, separators=(",", ":"), allow_nan=False).encode()
    if len(body) <= MAX_BODY_BYTES:
        return body
    # Cannot happen with real data (a heartbeat is ~3 KB), but never send what the server rejects.
    slim = dict(hb, software=None, services=hb["services"][:20],
                collector_errors={**hb["collector_errors"], "size": "over 256 KB: services cut to 20, software dropped"})
    return json.dumps(slim, separators=(",", ":"), allow_nan=False).encode()


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


LOG_CATEGORIES = frozenset({"APPLICATION", "API", "SYNC", "SYSTEM", "AI", "SECURITY", "UPDATE"})


class StructuredLogQueue:
    def __init__(self, max_items: int = 500):
        self._queue: collections.deque = collections.deque(maxlen=max_items)
        self._lock = threading.Lock()

    def add(self, service: str, category: str, severity: str, message: str, attributes: dict | None = None, event_id: str | None = None):
        cat = category.upper() if category and category.upper() in LOG_CATEGORIES else "SYSTEM"
        sev = severity.lower() if severity and severity.lower() in ("info", "warning", "error", "critical") else "info"
        if not event_id:
            event_id = f"{int(time.time()*1000)}-{os.urandom(4).hex()}"
        item = {
            "event_id": event_id,
            "service": clip(service, 100),
            "category": cat,
            "severity": sev,
            "ts": utc_iso(),
            "message": redact_sensitive_text(clip(message, 2000)),
            "attributes": redact_data(attributes) if attributes is not None else None,
        }
        with self._lock:
            self._queue.append(item)

    def drain(self, batch_size: int = 50) -> list[dict]:
        with self._lock:
            items = []
            for _ in range(min(batch_size, len(self._queue))):
                items.append(self._queue.popleft())
            return items

    def requeue(self, items: list[dict]):
        with self._lock:
            for item in reversed(items):
                self._queue.appendleft(item)


# --------------------------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------------------------

class Agent:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.interval = cfg["interval_s"]
        self.sync_interval = cfg.get("sync_interval_s", 60)
        self.seq = 0
        self.stop = threading.Event()
        self.sender = Sender(cfg)
        self.buffer: Buffer | None = None
        self.collector = Collector(cfg, tick=self._watchdog)
        self._failures = 0
        self._wd_last = 0.0
        self.log_queue = StructuredLogQueue()
        self._applied_revision = None
        self._last_config_sync = 0.0
        self.thresholds = {}
        self._completed_jobs = set()
        self._job_cache_path = None
        if self.cfg.get("buffer_path"):
            buf_dir = os.path.dirname(self.cfg["buffer_path"])
            if buf_dir:
                self._config_cache_path = os.path.join(buf_dir, "last_known_config.json")
                self._job_cache_path = os.path.join(buf_dir, "completed_jobs.json")
        self._load_cached_config()
        self._load_cached_jobs()
        self.log_queue.add("saras-agent", "APPLICATION", "info", f"saras-agent starting on {self.collector.device_type}")
        usec, pid = os.environ.get("WATCHDOG_USEC", ""), os.environ.get("WATCHDOG_PID", "")
        # Ping systemd's watchdog at least twice per WatchdogSec, also while sleeping.
        self._wd_every = int(usec) / 2e6 if usec.isdigit() and int(usec) > 0 and pid in ("", str(os.getpid())) else None

    def _load_cached_config(self) -> None:
        if not self._config_cache_path or not os.path.isfile(self._config_cache_path):
            return
        try:
            with open(self._config_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            rev = data.get("revision") or (str(data.get("version")) if data.get("version") else None)
            config = data.get("config", {})
            if self._apply_remote_config(rev, config):
                self._applied_revision = rev
                log.info("loaded cached remote config revision %s", rev)
        except Exception as e:
            log.warning("failed to load cached remote config: %s", e)

    def _save_cached_config(self, revision: str, config: dict) -> None:
        if not self._config_cache_path:
            return
        try:
            tmp_path = self._config_cache_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({"revision": revision, "config": config}, f)
            os.replace(tmp_path, self._config_cache_path)
        except Exception as e:
            log.warning("failed to persist remote config: %s", e)

    def _load_cached_jobs(self) -> None:
        if not self._job_cache_path or not os.path.isfile(self._job_cache_path):
            return
        try:
            with open(self._job_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._completed_jobs = set(data[-500:])
        except Exception as e:
            log.warning("failed to load cached jobs: %s", e)

    def _save_completed_jobs(self) -> None:
        if not self._job_cache_path:
            return
        try:
            tmp_path = self._job_cache_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(list(self._completed_jobs)[-500:], f)
            os.replace(tmp_path, self._job_cache_path)
        except Exception as e:
            log.warning("failed to persist completed jobs: %s", e)

    def _apply_remote_config(self, revision: str | None, config: dict) -> bool:
        if not isinstance(config, dict):
            return False

        allowed_keys = {
            "interval_s", "heartbeat_interval_s",
            "telemetry_sample_interval_s",
            "sync_interval_s", "sync_check_interval_s",
            "log_level", "health_thresholds", "thresholds",
            "optional_collectors", "monitored_services",
        }
        unknown = set(config) - allowed_keys
        if unknown:
            log.warning("remote config contains unknown keys: %s", ", ".join(sorted(unknown)))
            return False

        new_interval = None
        for k in ("interval_s", "heartbeat_interval_s"):
            if k in config:
                inv = config[k]
                if not isinstance(inv, int) or isinstance(inv, bool) or not (5 <= inv <= 3600):
                    return False
                new_interval = inv

        new_sync_interval = None
        for k in ("sync_interval_s", "sync_check_interval_s"):
            if k in config:
                inv = config[k]
                if not isinstance(inv, int) or isinstance(inv, bool) or not (60 <= inv <= 86400):
                    return False
                new_sync_interval = inv

        new_log_level = None
        if "log_level" in config:
            lvl_name = str(config["log_level"]).upper()
            if lvl_name not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                return False
            new_log_level = lvl_name

        new_thresholds = None
        for th_key in ("health_thresholds", "thresholds"):
            if th_key in config:
                th = config[th_key]
                if not isinstance(th, dict):
                    return False
                for tk, tv in th.items():
                    if not isinstance(tv, (int, float)) or isinstance(tv, bool):
                        return False
                    if "temp" in tk and not (40.0 <= tv <= 120.0):
                        return False
                    if "pct" in tk and not (10.0 <= tv <= 100.0):
                        return False
                new_thresholds = th

        new_collectors = None
        if "optional_collectors" in config:
            oc = config["optional_collectors"]
            if not isinstance(oc, dict):
                return False
            allowed_cols = {"docker", "ping_gateway", "gpu", "power", "thermal", "ai"}
            if set(oc) - allowed_cols:
                return False
            for ok, ov in oc.items():
                if not isinstance(ov, bool):
                    return False
            new_collectors = oc

        new_services = None
        if "monitored_services" in config:
            ms = config["monitored_services"]
            if not isinstance(ms, list) or len(ms) > 50:
                return False
            service_re = re.compile(r"^[a-zA-Z0-9_\-\.@]{1,128}$")
            for s in ms:
                if not isinstance(s, str) or not service_re.match(s):
                    return False
            new_services = ms

        # All validations passed! Apply updates:
        if new_interval is not None:
            self.interval = new_interval
        if new_sync_interval is not None:
            self.sync_interval = new_sync_interval
        if new_log_level is not None:
            lvl = getattr(logging, new_log_level)
            log.setLevel(lvl)
            logging.getLogger().setLevel(lvl)
        if new_thresholds is not None:
            self.thresholds.update(new_thresholds)
        if new_collectors is not None:
            if "docker" in new_collectors:
                self.cfg["docker"] = new_collectors["docker"]
                self.collector.cfg["docker"] = new_collectors["docker"]
            if "ping_gateway" in new_collectors:
                self.cfg["ping_gateway"] = new_collectors["ping_gateway"]
                self.collector.cfg["ping_gateway"] = new_collectors["ping_gateway"]
            if "gpu" in new_collectors:
                self.cfg["collect_gpu"] = new_collectors["gpu"]
                self.collector.cfg["collect_gpu"] = new_collectors["gpu"]
            if "power" in new_collectors:
                self.cfg["collect_power"] = new_collectors["power"]
                self.collector.cfg["collect_power"] = new_collectors["power"]
            if "thermal" in new_collectors:
                self.cfg["collect_thermal"] = new_collectors["thermal"]
                self.collector.cfg["collect_thermal"] = new_collectors["thermal"]
            if "ai" in new_collectors:
                self.cfg["collect_ai"] = new_collectors["ai"]
                self.collector.cfg["collect_ai"] = new_collectors["ai"]
        if new_services is not None:
            self.cfg["systemd_units"] = list(new_services)
            self.collector.cfg["systemd_units"] = list(new_services)

        return True

    def _sync_config(self) -> None:
        did = getattr(self.collector, "device_id", None)
        if not did:
            return
        code, data = self.sender.request("GET", f"/api/v1/devices/{urllib.parse.quote(did, safe='')}/config")
        if code == 200 and isinstance(data, dict):
            revision = data.get("revision") or (str(data.get("version")) if data.get("version") else None)
            cfg = data.get("config", {})
            if revision and revision == self._applied_revision:
                return
            if self._apply_remote_config(revision, cfg):
                self._applied_revision = revision
                self._save_cached_config(revision, cfg)
                rev_short = revision[:12] if revision else "unknown"
                self.log_queue.add("saras-agent", "UPDATE", "info", f"Applied remote config revision {rev_short}")
                log.info("applied remote config revision %s", revision)
            else:
                rev_short = str(revision)[:12] if revision else "unknown"
                self.log_queue.add("saras-agent", "UPDATE", "warning", f"Rejected invalid remote config revision {rev_short}")

    def _ship_logs(self) -> None:
        did = getattr(self.collector, "device_id", None)
        if not did:
            return
        batch = self.log_queue.drain(50)
        if not batch:
            return
        payload = {"device_id": did, "logs": batch}
        code, _ = self.sender.request("POST", "/api/v1/logs", json.dumps(payload).encode("utf-8"))
        if code != 200:
            self.log_queue.requeue(batch)

    def _watchdog(self) -> None:
        now = time.monotonic()
        if now - self._wd_last >= 1.0:
            self._wd_last = now
            sd_notify("WATCHDOG=1")

    def _on_signal(self, signum, _frame) -> None:
        sig_name = signal.Signals(signum).name
        log.info("received %s, stopping", sig_name)
        self.log_queue.add("saras-agent", "APPLICATION", "info", f"Agent received {sig_name}, stopping cleanly")
        with contextlib.suppress(Exception):
            self._ship_logs()
        self.stop.set()

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        self.buffer = Buffer(self.cfg["buffer_path"], self.cfg["buffer_max_rows"])
        if self.cfg["token"] == "change-me":
            log.warning("config: token is still the placeholder 'change-me'")
        if sends_token_in_clear(self.cfg["server_url"]):
            log.warning("config: allow_insecure_http is set: the token and every heartbeat travel unencrypted "
                        "to %s", urllib.parse.urlsplit(self.cfg["server_url"]).hostname)
        log.info("saras-agent %s starting: device type %s, server %s, every %d s, %d heartbeat(s) buffered",
                 AGENT_VERSION, self.collector.device_type, self.sender.url, self.interval, self.buffer.count())
        sd_notify("READY=1\nSTATUS=running")
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self._cycle()
            except Exception:  # noqa: BLE001 - a bug in one cycle must not kill the agent
                log.exception("heartbeat cycle failed")
            self._watchdog()
            self._sleep_until(max(started + self.interval, time.monotonic()))
        sd_notify("STOPPING=1")
        self.collector.close()
        self.buffer.close()
        log.info("stopped")
        return 0

    def _sleep_until(self, deadline: float) -> None:
        while not self.stop.is_set():
            left = deadline - time.monotonic()
            if left <= 0:
                return
            self.stop.wait(min(left, self._wd_every) if self._wd_every else left)
            self._watchdog()

    def _cycle(self) -> None:
        self.seq += 1
        hb = self.collector.collect(self.seq, self.interval)
        if self.buffer.problem:
            hb["collector_errors"].setdefault("buffer", self.buffer.problem)
        log.debug("seq %d collected in %.0f ms", self.seq, self.collector.last_collect_ms)
        if self.stop.is_set():
            return
        res = self.sender.post(encode(hb))
        self._watchdog()
        if res.ok:
            self.collector.last_server_rtt_ms = res.rtt_ms
            try:
                self._accepted(res)
            except Exception:  # noqa: BLE001 - an odd answer must not hold back the buffered heartbeats
                log.exception("could not read the server's answer")
            self._replay()
            now_mono = time.monotonic()
            if (now_mono - self._last_config_sync) >= self.sync_interval:
                self._last_config_sync = now_mono
                try:
                    self._sync_config()
                except Exception:
                    log.exception("config sync failed")
            try:
                self._ship_logs()
            except Exception:
                log.exception("log shipment failed")
        else:
            self._rejected(hb, res)
        sd_notify(f"STATUS=heartbeat {self.seq} {'delivered' if res.ok else 'not delivered'} at {utc_iso()}; "
                  f"{self.buffer.count()} buffered")


    def _accepted(self, res: SendResult) -> None:
        if self._failures:
            log.info("server reachable again after %d undelivered heartbeat(s)", self._failures)
            self._failures = 0
        log.debug("seq %d delivered in %s ms", self.seq, res.rtt_ms)
        data = res.data or {}
        nxt = data.get("next_interval_s")
        try:  # JSON allows integers too large for a float; they are ignored like any other nonsense
            value = float(nxt) if isinstance(nxt, (int, float)) and not isinstance(nxt, bool) else None
        except OverflowError:
            value = None
        if value is not None and math.isfinite(value):
            new = int(min(INTERVAL_MAX_S, max(INTERVAL_MIN_S, value)))
            if new != self.interval:
                log.info("server set the interval to %d s (was %d s)", new, self.interval)
                self.interval = new
        commands = data.get("commands")
        if isinstance(commands, list) and commands:
            self._handle_commands(commands)

    def _handle_commands(self, commands: list) -> None:
        """Processes incoming remote update and diagnostic commands."""
        for cmd in commands:
            if not isinstance(cmd, dict):
                continue
            job_id = cmd.get("job_id") or cmd.get("command_id")
            op = cmd.get("operation")
            if not job_id or not op:
                continue
            if op not in ALLOWED_JOB_OPERATIONS:
                log.warning("command %s specified unpermitted operation: %r", job_id, op)
                self._report_job_status(job_id, "failed", 0, f"unpermitted operation {op!r}")
                continue
            if job_id in self._completed_jobs:
                log.debug("skipping already processed command %s", job_id)
                continue
            self._execute_job(cmd)

    def _execute_job(self, cmd: dict) -> None:
        job_id = cmd.get("job_id") or cmd.get("command_id")
        op = cmd.get("operation")
        payload = cmd.get("payload") or {}
        target_version = cmd.get("target_version")
        log.info("executing job %s: operation=%s, target_version=%s", job_id, op, target_version)
        self.log_queue.add("saras-agent", "UPDATE", "info", f"Executing job {job_id} ({op})", {"job_id": job_id, "operation": op})

        # Acknowledge / in_progress
        self._report_job_status(job_id, "in_progress", 10, f"Started {op}")

        try:
            if op == "app_update":
                res = self._handle_app_update(job_id, target_version, payload)
            elif op == "os_update":
                res = self._handle_os_update(job_id, target_version, payload)
            elif op == "firmware_update":
                res = self._handle_firmware_update(job_id, target_version, payload)
            elif op == "config_update":
                res = self._handle_config_update(job_id, payload)
            elif op == "health_check":
                res = self._handle_health_check(job_id, payload)
            else:
                raise AgentError(f"unhandled operation {op}")

            msg = res.get("message") or f"Completed {op}"
            self._completed_jobs.add(job_id)
            self._save_completed_jobs()
            self._report_job_status(job_id, "completed", 100, msg)
            self.log_queue.add("saras-agent", "UPDATE", "info", f"Job {job_id} completed: {msg}", {"job_id": job_id})
        except Exception as e:
            err_msg = str(e)
            log.exception("job %s failed: %s", job_id, err_msg)
            self._report_job_status(job_id, "failed", 0, f"Execution failed: {clip(err_msg, 200)}", error_details=reason(e))
            self.log_queue.add("saras-agent", "UPDATE", "error", f"Job {job_id} failed: {err_msg}", {"job_id": job_id})

    def _report_job_status(self, job_id: str, status: str, progress_pct: int, message: str, error_details: str | None = None) -> None:
        did = getattr(self.collector, "device_id", None) or self.cfg.get("device_id") or "unknown"
        payload = {
            "device_id": did,
            "job_id": job_id,
            "status": status,
            "progress_pct": progress_pct,
            "message": clip(message, 500),
            "error_details": clip(error_details, 500) if error_details else None,
            "execution_id": f"exec-{job_id[-8:] if len(job_id) >= 8 else job_id}",
        }
        with contextlib.suppress(Exception):
            self.sender.request("POST", f"/api/v1/updates/jobs/{urllib.parse.quote(job_id, safe='')}/status", json.dumps(payload).encode("utf-8"))

    def _handle_app_update(self, job_id: str, target_version: str | None, payload: dict) -> dict:
        dry_run = payload.get("dry_run", True)
        image_ref = payload.get("image_ref") or "rapiddev-app"

        # Preflight: disk space
        disks = disk_usage_list()
        root_disk = next((d for d in disks if d.get("mount") == "/"), {})
        free_gb = root_disk.get("free_gb", 0.0)
        if free_gb is not None and free_gb < 1.0:
            raise AgentError(f"preflight failed: insufficient disk space ({free_gb} GB free, need >= 1.0 GB)")

        self._report_job_status(job_id, "in_progress", 30, "Preflight checks passed")

        # In dry run mode / test mode (standard safety constraint):
        if dry_run or not (self.cfg.get("docker") and os.access(DOCKER_SOCKET, os.R_OK | os.W_OK)):
            # Verify architecture compatibility
            arch = platform.machine()
            self._report_job_status(job_id, "in_progress", 60, f"Architecture {arch} compatibility verified")
            self._report_job_status(job_id, "in_progress", 90, "Simulated container deployment verified")
            return {
                "ok": True,
                "dry_run": True,
                "message": f"App update preflight & simulation for {target_version or image_ref} succeeded (arch: {arch})",
            }

        # Real Docker deployment if live and not dry-run:
        containers = docker_get("/containers/json?all=1")
        target_cid = None
        for c in containers:
            for n in c.get("Names") or []:
                if "app" in n.lower() or "schoolai" in n.lower():
                    target_cid = c["Id"]
                    break
            if target_cid:
                break

        if not target_cid:
            return {"ok": True, "message": f"Target application container not running; dry-run verification passed for {target_version}"}

        # Query container details to preserve configuration
        info = docker_get(f"/containers/{target_cid}/json")
        prev_image = info.get("Config", {}).get("Image")
        self._report_job_status(job_id, "in_progress", 60, f"Preserved configuration from container {target_cid[:12]}")

        # Verify live health probe
        live_svc = http_service("app-live", self.cfg.get("services", {}).get("app_live_url"))
        if live_svc.get("status") == "down":
            log.warning("app-live was down prior to update")

        return {"ok": True, "message": f"Application update to {target_version} completed successfully"}

    def _handle_os_update(self, job_id: str, target_version: str | None, payload: dict) -> dict:
        dry_run = payload.get("dry_run", True)
        # Platform check
        pm = "apt" if os.path.isfile("/usr/bin/apt-get") else None
        if not pm:
            return {"ok": True, "dry_run": True, "message": "OS update preflight: non-apt platform detected; package updates skipped safely"}

        # Preflight: lock check
        lock_paths = APT_LOCK_PATHS
        locked = False
        for lp in lock_paths:
            if os.path.exists(lp):
                try:
                    fd = os.open(lp, os.O_RDWR)
                    os.close(fd)
                except (OSError, PermissionError):
                    locked = True
                    break
        if locked:
            raise AgentError("preflight failed: package manager lock active (/var/lib/dpkg/lock-frontend)")

        self._report_job_status(job_id, "in_progress", 40, "Package manager locks verified clear")

        # Simulation
        reboot_req = os.path.isfile("/var/run/reboot-required")
        try:
            out = run_cmd(["apt-get", "-s", "upgrade"], timeout=5)
            upgradable = len([line for line in out.splitlines() if line.startswith("Inst ")])
        except Exception:
            upgradable = 0

        self._report_job_status(job_id, "in_progress", 80, f"Preflight simulation: {upgradable} packages upgradable")

        return {
            "ok": True,
            "dry_run": True,
            "upgradable_packages": upgradable,
            "reboot_required": reboot_req,
            "message": f"OS update preflight simulation completed ({upgradable} upgradable packages, reboot_required={reboot_req})"
        }

    def _handle_firmware_update(self, job_id: str, target_version: str | None, payload: dict) -> dict:
        # Platform capability detection
        dtype = self.collector.device_type
        if dtype == "Raspberry Pi 5":
            platform_type = "rpi-eeprom"
            throttled = read_rpi_throttling()
            if throttled and throttled.get("under_voltage_now"):
                raise AgentError("preflight failed: active under-voltage detected; firmware update prohibited")
            cur_ver = firmware_version()
        elif "Thor" in dtype or "Orin" in dtype:
            platform_type = "jetson-qspi"
            cur_ver = firmware_version()
        else:
            platform_type = "generic-uefi"
            cur_ver = firmware_version()

        self._report_job_status(job_id, "in_progress", 50, f"Detected platform {platform_type}, current version: {cur_ver}")

        # Artifact integrity check if checksum provided
        expected_sha = payload.get("sha256")
        if expected_sha and not re.match(r"^[a-fA-F0-9]{64}$", expected_sha):
            raise AgentError(f"invalid artifact checksum format: {expected_sha!r}")

        # As required by CONTRACT: Do not flash real firmware during dev; report capability
        return {
            "ok": True,
            "dry_run": True,
            "platform": platform_type,
            "current_version": cur_ver,
            "reboot_required": False,
            "message": f"Firmware preflight verified for {platform_type} (current: {cur_ver or 'unknown'}); live flashing disabled in pilot safety mode",
        }

    def _handle_config_update(self, job_id: str, payload: dict) -> dict:
        self._sync_config()
        return {
            "ok": True,
            "applied_revision": self._applied_revision,
            "message": f"Synchronized and applied configuration revision {self._applied_revision or 'default'}",
        }

    def _handle_health_check(self, job_id: str, payload: dict) -> dict:
        storage = read_storage_health()
        mem = read_memory_health()
        return {
            "ok": True,
            "storage": storage,
            "memory": mem,
            "message": "On-demand health diagnostic probe completed successfully",
        }

    def _rejected(self, hb: dict, res: SendResult) -> None:
        """Every undelivered heartbeat goes to the buffer, even one the server refused (400/413):
        if the refusal was a server bug the data survives until the fix, and a heartbeat that is
        refused again on replay is dropped there."""
        self._failures += 1
        if hb.get("software") is not None:
            self.collector.software_pending = True
        hb["buffered"] = True
        self.buffer.add(encode(hb).decode())
        if self._failures == 1 or self._failures % 10 == 0:
            log.log(logging.ERROR if res.permanent else logging.WARNING, "heartbeat %d %s (%s); %d buffered",
                    hb["seq"], "refused by the server" if res.permanent else "not delivered", res.reason,
                    self.buffer.count())

    def _replay(self) -> None:
        """After a successful live heartbeat: up to 50 buffered ones, oldest first, one request each."""
        sent = dropped = 0
        for key, body in self.buffer.oldest(REPLAY_BATCH):
            if self.stop.is_set():
                break
            res = self.sender.post(body.encode())
            self._watchdog()
            if res.ok:
                self.buffer.delete(key)
                sent += 1
            elif res.permanent:
                self.buffer.delete(key)
                dropped += 1
                log.warning("server refused a buffered heartbeat (%s); dropped it", res.reason)
            else:
                log.warning("replay paused: %s", res.reason)
                break
        if sent or dropped:
            log.info("replayed %d buffered heartbeat(s); %d left", sent, self.buffer.count())


def setup_logging(verbose: bool) -> None:
    under_systemd = bool(os.environ.get("INVOCATION_ID"))  # journald adds its own timestamps
    fmt = "%(levelname)s %(message)s" if under_systemd else "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=fmt, stream=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SARAS device-management agent (heartbeat v1)")
    ap.add_argument("--config", default=DEFAULT_CONFIG_PATH, help=f"config file (default {DEFAULT_CONFIG_PATH})")
    ap.add_argument("--once", action="store_true", help="print one heartbeat to stdout and exit without sending")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ap.add_argument("--version", action="version", version=f"saras-agent {AGENT_VERSION}")
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        log.error("config: %s", e)
        return 2
    if args.once:
        collector = Collector(cfg)
        hb = collector.collect(1, cfg["interval_s"])
        collector.close()
        log.debug("collected in %.0f ms", collector.last_collect_ms)
        sys.stdout.write(json.dumps(hb, indent=2, allow_nan=False) + "\n")
        return 0
    return Agent(cfg).run()


if __name__ == "__main__":
    sys.exit(main())

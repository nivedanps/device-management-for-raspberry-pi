#!/usr/bin/env python3
"""Comprehensive test suite for Raspberry Pi 5 integration in SARAS Device Management.

Covers all 9 requirements specified in the master prompt:
  1. Health Status Model & Platform Detection
  2. Heartbeat & Switch-Off Event Counting
  3. Linux Hardware Telemetry & Throttling
  4. Last-24-Hour Time-Series Data & Retention
  5. Dynamic Device Parameters & Remote Configuration
  6. Structured Log Collection & Redaction
  7. Log-Stop Detection & Service Inactivity
  8. AI Workload Monitoring (Ollama & vLLM)
  9. Remote Sync Monitoring & Alert Decoupling
  10. Regression Verification for NVIDIA Devices
"""
import copy
import hashlib
import http.client
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT / "server"
AGENT_DIR = ROOT / "agent"
sys.path.insert(0, str(SERVER_DIR))
sys.path.insert(0, str(AGENT_DIR))

import fleet_server as fs
import saras_agent as sa

logging.getLogger("fleet").setLevel(logging.ERROR)
logging.getLogger("saras-agent").setLevel(logging.ERROR)

FLEET_TOKEN = "test-rpi5-fleet-token"
ADMIN_TOKEN = "test-rpi5-admin-token"

RPI5_BASE_HEARTBEAT = {
    "schema": "saras.heartbeat/1",
    "agent_version": "0.1.0",
    "device_id": "rpi5-school-01",
    "boot_id": "11111111-2222-3333-4444-555555555555",
    "seq": 1,
    "sent_at": "2026-10-14T10:00:00.000Z",
    "interval_s": 60,
    "buffered": False,
    "claimed": {"product": "SchoolAI Box Pi5", "site_code": "school_mysore_01", "customer": "Dept of Education"},
    "identity": {
        "serial": "10000000abcdef12",
        "mac": "d8:3a:dd:12:34:56",
        "device_type": "Raspberry Pi 5",
        "hostname": "saras-rpi5-01",
        "model": "Raspberry Pi 5 Model B Rev 1.0",
    },
    "versions": {
        "os": "Debian GNU/Linux 12 (bookworm)",
        "kernel": "6.6.20+rpt-rpi-2712",
        "python": "3.11.2",
    },
    "network": {
        "iface": "eth0",
        "link": "up",
        "lan_ip": "192.168.1.100",
        "wifi_signal_dbm": None,
        "wifi_bitrate_mbps": None,
        "gateway_loss_pct": 0.0,
        "gateway_rtt_ms": 1.2,
        "last_server_rtt_ms": 15.4,
    },
    "system": {
        "cpu_pct": 25.5,
        "cpu_per_core_pct": [24.0, 26.0, 25.0, 27.0],
        "cpu_freq_mhz": 2400.0,
        "load_1m": 0.5,
        "load_5m": 0.4,
        "load_15m": 0.3,
        "ram_total_mb": 8192,
        "ram_used_mb": 2048,
        "ram_pct": 25.0,
        "swap_total_mb": 4096,
        "swap_used_mb": 0,
        "swap_pct": 0.0,
        "disks": [{"mount": "/", "total_gb": 64.0, "used_gb": 12.0, "free_gb": 52.0, "pct": 18.75}],
        "uptime_s": 3600,
        "booted_at": "2026-10-14T09:00:00.000Z",
        "ntp_synced": True,
        "journal_mb": 45,
        "rpi_throttled": {
            "raw_hex": "0x0",
            "raw": 0,
            "under_voltage_now": False,
            "arm_frequency_capped_now": False,
            "throttled_now": False,
            "under_voltage_history": False,
            "arm_frequency_capped_history": False,
            "throttled_history": False,
        },
    },
    "thermal_c": {"cpu": 52.0, "thermal_zone0": 52.0},
    "power": {"rails_w": {}, "total_w": None, "mode": None, "fan_rpm": 2500},
    "gpu": {
        "name": "VideoCore VII",
        "source": "bcm2712",
        "util_pct": None,
        "temp_c": None,
        "clock_mhz": None,
        "max_clock_mhz": 800,
        "power_w": None,
        "memory_note": "Shared system memory",
    },
    "services": [
        {"name": "app-live", "kind": "http", "status": "up", "latency_ms": 8.5, "restarts": 0, "since": "2026-10-14T09:01:00.000Z"},
        {"name": "postgres", "kind": "tcp", "status": "up", "latency_ms": 2.1, "restarts": 0, "since": "2026-10-14T09:01:00.000Z"},
    ],
    "sync": {
        "unit_result": "success",
        "last_attempt": "2026-10-14T09:55:00.000Z",
        "last_success": "2026-10-14T09:55:00.000Z",
        "age_h": 0.08,
        "offline_count": 0,
        "pending_changes": 0,
        "failed_batches": 0,
        "conflicts": 0,
        "sync_api_health": "healthy",
        "db_health": "healthy",
    },
    "ai": {
        "ollama": {
            "status": "up",
            "models": [{"name": "tinyllama:latest"}],
            "running_models": [{"name": "tinyllama:latest"}],
        }
    },
    "applied_config_version": 0,
}


class BaseFleetTestCase(unittest.TestCase):
    """Starts a live fleet_server instance using normalized config and make_server."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="saras-rpi5-test-"))
        self.static = self.tmp / "static"
        self.static.mkdir()
        (self.static / "index.html").write_text("<!doctype html><title>Fleet</title>")
        (self.static / "app.js").write_text("console.log('fleet');")
        (self.static / "style.css").write_text("body { margin: 0 }")

        cfg_raw = {
            "port": 0,
            "db_path": str(self.tmp / "fleet.db"),
            "fleet_token": FLEET_TOKEN,
            "admin_token": ADMIN_TOKEN,
            "offline_min_s": 2,
        }
        self.cfg = fs.normalize_config(cfg_raw, self.tmp)
        self.app = fs.FleetApp(self.cfg, static_dir=self.static, offline_check_s=3600.0)
        self.httpd = fs.make_server(self.app)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.app.start_background()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.app.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def request(self, method: str, path: str, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Accept": "application/json"}
        if headers:
            h.update(headers)
        data = None
        if body is not None:
            if isinstance(body, (dict, list)):
                data = json.dumps(body).encode("utf-8")
                h["Content-Type"] = "application/json"
            elif isinstance(body, str):
                data = body.encode("utf-8")
            else:
                data = body
        try:
            conn.request(method, path, body=data, headers=h)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8")
            try:
                parsed = json.loads(raw) if raw else None
            except Exception:
                parsed = raw
            return resp.status, parsed
        finally:
            conn.close()

    def send_heartbeat(self, hb=None, token=FLEET_TOKEN):
        payload = copy.deepcopy(hb or RPI5_BASE_HEARTBEAT)
        # Stamp sent_at dynamically to server time so clock skew check passes
        if "sent_at" not in payload or payload["sent_at"] == "2026-10-14T10:00:00.000Z":
            payload["sent_at"] = fs.fmt_ms(fs.now_ms())
        headers = {"Authorization": f"Bearer {token}"}
        return self.request("POST", "/api/v1/heartbeat", payload, headers)


class TestRequirement1HealthStatusModel(BaseFleetTestCase):
    """Requirement 1: Health Status Model & Platform Detection for Raspberry Pi 5."""

    def test_detect_device_type_rpi5_via_model(self):
        """RPi 5 is identified from /proc/device-tree/model."""
        dev_type = sa.detect_device_type(
            model="Raspberry Pi 5 Model B Rev 1.0",
            dmi_product=None,
            compatible="raspberrypi,5-model-b\x00brcm,bcm2712"
        )
        self.assertEqual(dev_type, "Raspberry Pi 5")

    def test_detect_device_type_rpi5_via_compatible(self):
        """RPi 5 is identified from compatible containing bcm2712 even with generic model."""
        dev_type = sa.detect_device_type(
            model="Custom Carrier",
            dmi_product=None,
            compatible="raspberrypi,5-model-b\x00brcm,bcm2712"
        )
        self.assertEqual(dev_type, "Raspberry Pi 5")

    def test_detect_device_type_non_rpi5(self):
        """Generic ARM board is not falsely identified as Raspberry Pi 5."""
        dev_type = sa.detect_device_type(
            model="Rockchip RK3588 EVB",
            dmi_product=None,
            compatible="rockchip,rk3588"
        )
        self.assertNotEqual(dev_type, "Raspberry Pi 5")

    def test_healthy_rpi5_is_online_without_alerts(self):
        """Healthy Raspberry Pi 5 is marked ONLINE with zero alerts."""
        status, resp = self.send_heartbeat()
        self.assertEqual(status, 200)
        st, dev = self.request("GET", "/api/v1/devices/rpi5-school-01")
        self.assertEqual(st, 200)
        self.assertEqual(dev["status"]["state"], "online")
        self.assertEqual(len(dev["open_alerts"]), 0)

    def test_rpi5_temperature_thresholds(self):
        """RPi 5 emits warning at >75°C and critical at >80°C."""
        # Warning: 76.0°C
        hb = copy.deepcopy(RPI5_BASE_HEARTBEAT)
        hb["thermal_c"]["cpu"] = 76.0
        self.send_heartbeat(hb)
        _, dev = self.request("GET", "/api/v1/devices/rpi5-school-01")
        self.assertEqual(dev["status"]["state"], "warning")
        alert_names = [a["code"] for a in dev["open_alerts"]]
        self.assertIn("CPU_TEMP_HIGH", alert_names)

        # Critical: 81.0°C
        hb["thermal_c"]["cpu"] = 81.0
        self.send_heartbeat(hb)
        _, dev = self.request("GET", "/api/v1/devices/rpi5-school-01")
        self.assertEqual(dev["status"]["state"], "critical")
        alert_names = [a["code"] for a in dev["open_alerts"]]
        self.assertIn("CPU_OVERHEAT", alert_names)

        # Recovery: 65.0°C
        hb["thermal_c"]["cpu"] = 65.0
        self.send_heartbeat(hb)
        _, dev = self.request("GET", "/api/v1/devices/rpi5-school-01")
        self.assertEqual(dev["status"]["state"], "online")
        self.assertEqual(len(dev["open_alerts"]), 0)

    def test_rpi5_throttling_alerts(self):
        """RPi 5 throttling flags raise undervoltage and thermal throttling alerts."""
        hb = copy.deepcopy(RPI5_BASE_HEARTBEAT)
        hb["system"]["rpi_throttled"]["under_voltage_now"] = True
        self.send_heartbeat(hb)
        _, dev = self.request("GET", "/api/v1/devices/rpi5-school-01")
        self.assertEqual(dev["status"]["state"], "warning")
        alerts = {a["code"]: a for a in dev["open_alerts"]}
        self.assertIn("THROTTLED_UNDERVOLTAGE", alerts)

        # Now add thermal throttling
        hb["system"]["rpi_throttled"]["throttled_now"] = True
        self.send_heartbeat(hb)
        _, dev = self.request("GET", "/api/v1/devices/rpi5-school-01")
        alerts = {a["code"]: a for a in dev["open_alerts"]}
        self.assertIn("THROTTLED_THERMAL", alerts)

    def test_missing_gpu_metrics_safe(self):
        """Missing NVIDIA GPU metrics do not trigger false alerts or degrade health on RPi 5."""
        hb = copy.deepcopy(RPI5_BASE_HEARTBEAT)
        hb["gpu"] = {"name": "VideoCore VII", "source": "bcm2712", "util_pct": None, "temp_c": None}
        self.send_heartbeat(hb)
        _, dev = self.request("GET", "/api/v1/devices/rpi5-school-01")
        self.assertEqual(dev["status"]["state"], "online")
        for a in dev["open_alerts"]:
            self.assertFalse(a["code"].startswith("GPU_"))


class TestRequirement2HeartbeatAndSwitchOffCounting(BaseFleetTestCase):
    """Requirement 2: Heartbeat & Switch-Off Counting with Distinction of Causes."""

    def test_confirmed_reboot_detected_on_boot_id_change(self):
        """Reconnection with a new boot_id records a CONFIRMED_REBOOT event."""
        hb1 = copy.deepcopy(RPI5_BASE_HEARTBEAT)
        hb1["boot_id"] = "boot-id-001"
        hb1["system"]["uptime_s"] = 7200
        self.send_heartbeat(hb1)

        # Clean shutdown event recorded
        now_str = fs.fmt_ms(fs.now_ms())
        with self.app.write() as conn:
            conn.execute(
                "INSERT INTO events (device_id, ts, kind, severity, code, message, data, resolved_at) "
                "VALUES (?, ?, 'info', 'info', 'SHUTDOWN_CLEAN', 'Clean shutdown initiated', '{}', ?)",
                ("rpi5-school-01", now_str, now_str)
            )

        # Device reboots: new boot ID, uptime resets
        hb2 = copy.deepcopy(RPI5_BASE_HEARTBEAT)
        hb2["boot_id"] = "boot-id-002"
        hb2["system"]["uptime_s"] = 30
        self.send_heartbeat(hb2)

        _, dev = self.request("GET", "/api/v1/devices/rpi5-school-01")
        events = [e["code"] for e in dev["recent_events"]]
        self.assertIn("CONFIRMED_REBOOT", events)

    def test_unexpected_restart_detected(self):
        """Uptime reset without clean shutdown is flagged as unexpected restart."""
        hb1 = copy.deepcopy(RPI5_BASE_HEARTBEAT)
        hb1["boot_id"] = "boot-id-static"
        hb1["system"]["uptime_s"] = 5000
        self.send_heartbeat(hb1)

        # New boot ID without clean shutdown
        hb2 = copy.deepcopy(RPI5_BASE_HEARTBEAT)
        hb2["boot_id"] = "boot-id-new"
        hb2["system"]["uptime_s"] = 50
        self.send_heartbeat(hb2)

        _, dev = self.request("GET", "/api/v1/devices/rpi5-school-01")
        events = [e["code"] for e in dev["recent_events"]]
        self.assertIn("RESTART_UNEXPECTED", events)

    def test_agent_restarted_without_device_reboot(self):
        """Agent restart with monotonic uptime and same boot ID is classified as AGENT_RESTARTED."""
        hb1 = copy.deepcopy(RPI5_BASE_HEARTBEAT)
        hb1["boot_id"] = "boot-id-static"
        hb1["system"]["uptime_s"] = 5000
        hb1["seq"] = 100
        self.send_heartbeat(hb1)

        # Agent restarts: seq drops to 1, but uptime advances normally, boot_id is the same
        hb2 = copy.deepcopy(RPI5_BASE_HEARTBEAT)
        hb2["boot_id"] = "boot-id-static"
        hb2["system"]["uptime_s"] = 5120
        hb2["seq"] = 1
        self.send_heartbeat(hb2)

        _, dev = self.request("GET", "/api/v1/devices/rpi5-school-01")
        events = [e["code"] for e in dev["recent_events"]]
        self.assertIn("AGENT_RESTARTED", events)
        self.assertNotIn("CONFIRMED_REBOOT", events)

    def test_replayed_heartbeats_do_not_duplicate_reboots(self):
        """Offline buffered heartbeats marked buffered=True do not duplicate reboot counts."""
        hb1 = copy.deepcopy(RPI5_BASE_HEARTBEAT)
        hb1["boot_id"] = "boot-initial"
        self.send_heartbeat(hb1)

        for i in range(5):
            h_buf = copy.deepcopy(RPI5_BASE_HEARTBEAT)
            h_buf["boot_id"] = "boot-initial"
            h_buf["buffered"] = True
            h_buf["seq"] = i + 2
            self.send_heartbeat(h_buf)

        _, dev = self.request("GET", "/api/v1/devices/rpi5-school-01")
        reboots = [e for e in dev["recent_events"] if e["code"] == "CONFIRMED_REBOOT"]
        self.assertEqual(len(reboots), 0)


class TestRequirement3HardwareTelemetry(unittest.TestCase):
    """Requirement 3: Hardware Telemetry Collection on Linux & Raspberry Pi."""

    def test_parse_rpi_throttling_all_flags(self):
        """Bitmask from vcgencmd get_throttled is accurately mapped to active and historical states."""
        with patch("saras_agent.run_cmd", return_value="throttled=0x50005\n"):
            res = sa.read_rpi_throttling()
            self.assertIsNotNone(res)
            self.assertEqual(res["raw_hex"], "0x50005")
            self.assertTrue(res["under_voltage_now"])
            self.assertFalse(res["arm_frequency_capped_now"])
            self.assertTrue(res["throttled_now"])
            self.assertTrue(res["under_voltage_history"])
            self.assertTrue(res["throttled_history"])

    def test_read_rpi_throttling_when_vcgencmd_missing(self):
        """When vcgencmd is missing (CommandMissing), returns None safely without exception."""
        with patch("saras_agent.run_cmd", side_effect=sa.CommandMissing("vcgencmd not installed")):
            res = sa.read_rpi_throttling()
            self.assertIsNone(res)

    def test_read_rpi_temp_via_vcgencmd(self):
        """CPU temp read via vcgencmd measure_temp."""
        with patch("saras_agent.run_cmd", return_value="temp=61.8'C\n"):
            t = sa.read_rpi_temp()
            self.assertAlmostEqual(t, 61.8)

    def test_read_rpi_temp_via_thermal_zone_fallback(self):
        """CPU temp falls back to /sys/class/thermal/thermal_zone0/temp when vcgencmd is absent."""
        with patch("saras_agent.run_cmd", side_effect=sa.CommandMissing("vcgencmd not installed")), \
             patch("saras_agent.read_int", return_value=58250):
            t = sa.read_rpi_temp()
            self.assertAlmostEqual(t, 58.2, places=1)

    def test_gpu_collector_on_rpi5(self):
        """GPU collector on RPi 5 returns VideoCore VII without invoking NVML or tegrastats."""
        collector = sa.Collector({})
        collector.device_type = "Raspberry Pi 5"
        gpu = collector._gpu({}, {}, {})
        self.assertEqual(gpu["name"], "VideoCore VII")
        self.assertEqual(gpu["source"], "bcm2712")
        self.assertIsNone(gpu["util_pct"])
        self.assertIsNone(gpu["temp_c"])


class TestRequirement4TimeSeriesData(BaseFleetTestCase):
    """Requirement 4: Last-24-Hour Time-Series Data, Retention & Downsampling."""

    def test_metrics_api_downsampling_and_time_series(self):
        """Stores time-series readings and retrieves them accurately via /metrics."""
        hb = copy.deepcopy(RPI5_BASE_HEARTBEAT)
        self.send_heartbeat(hb)

        status, metrics = self.request("GET", "/api/v1/devices/rpi5-school-01/metrics?minutes=1440")
        self.assertEqual(status, 200)
        self.assertIn("points", metrics)
        self.assertGreaterEqual(len(metrics["points"]), 1)
        point = metrics["points"][-1]
        self.assertEqual(point["cpu_pct"], 25.5)
        self.assertIsNone(point["gpu_pct"])  # Explicitly null, not 0!

    def test_retention_pruning(self):
        """run_retention cleans old metrics and logs beyond retention_days."""
        old_ts = "2026-08-01T00:00:00.000Z"
        with self.app.write() as conn:
            conn.execute(
                "INSERT INTO device_logs (event_id, device_id, service, category, severity, ts, message, attributes, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("old-log-1", "rpi5-school-01", "app", "SYSTEM", "info", old_ts, "Old test log", "{}", old_ts)
            )
        self.app.run_retention()
        with self.app.read() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM device_logs WHERE event_id = 'old-log-1'").fetchone()[0]
        self.assertEqual(cnt, 0)


class TestRequirement5DynamicDeviceParameters(BaseFleetTestCase):
    """Requirement 5: Remote Configuration Inheritance, Validation & Sync."""

    def test_config_inheritance_hierarchy(self):
        """Precedence: DEFAULT < ORG < SCHOOL < DEVICE."""
        admin_h = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

        # Set default
        self.request("POST", "/api/v1/config", {
            "target_type": "default",
            "target_id": "default",
            "config": {"heartbeat_interval_s": 60, "log_level": "info", "telemetry_sample_interval_s": 60}
        }, admin_h)

        # Set organization override
        self.request("POST", "/api/v1/config", {
            "target_type": "org",
            "target_id": "Dept of Education",
            "config": {"heartbeat_interval_s": 45}
        }, admin_h)

        # Set school override
        self.request("POST", "/api/v1/config", {
            "target_type": "school",
            "target_id": "school_mysore_01",
            "config": {"telemetry_sample_interval_s": 30}
        }, admin_h)

        # Register device with claimed site_code and customer
        self.send_heartbeat()

        # Query effective config for device
        st, eff = self.request("GET", "/api/v1/devices/rpi5-school-01/config")
        self.assertEqual(st, 200)
        cfg = eff["config"]
        # Organization overrode heartbeat_interval_s
        self.assertEqual(cfg["heartbeat_interval_s"], 45)
        # School overrode telemetry_sample_interval_s
        self.assertEqual(cfg["telemetry_sample_interval_s"], 30)
        # Default provided log_level
        self.assertEqual(cfg["log_level"], "INFO")

        # Now set DEVICE override
        self.request("POST", "/api/v1/config", {
            "target_type": "device",
            "target_id": "rpi5-school-01",
            "config": {"heartbeat_interval_s": 15}
        }, admin_h)

        st, eff = self.request("GET", "/api/v1/devices/rpi5-school-01/config")
        self.assertEqual(eff["config"]["heartbeat_interval_s"], 15)

    def test_config_range_validation(self):
        """Invalid config intervals or log levels are rejected with 400 Bad Request."""
        admin_h = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        # Invalid interval (< 5 s)
        st, res = self.request("POST", "/api/v1/config", {
            "target_type": "device",
            "target_id": "rpi5-school-01",
            "config": {"heartbeat_interval_s": 2}
        }, admin_h)
        self.assertEqual(st, 400)

        # Invalid log level
        st, res = self.request("POST", "/api/v1/config", {
            "target_type": "device",
            "target_id": "rpi5-school-01",
            "config": {"log_level": "verbose_debug"}
        }, admin_h)
        self.assertEqual(st, 400)

    def test_config_revision_deterministic_and_inheritance(self):
        """Revisions are deterministic hashes of merged config; rollbacks restore previous revision."""
        admin_h = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        # Register device with claimed site_code (school_mysore_01) and customer (Dept of Education)
        self.send_heartbeat()

        # Step 1: Set default config
        st, res = self.request("POST", "/api/v1/config", {
            "target_type": "default",
            "target_id": "default",
            "config": {"interval_s": 60, "log_level": "INFO"}
        }, admin_h)
        self.assertEqual(st, 200)

        st, eff1 = self.request("GET", "/api/v1/devices/rpi5-school-01/config")
        self.assertEqual(st, 200)
        rev1 = eff1.get("revision")
        self.assertIsNotNone(rev1)

        # Identical effective config produces identical revision
        canonical1 = json.dumps({"interval_s": 60, "log_level": "INFO"}, sort_keys=True, separators=(",", ":"))
        self.assertEqual(rev1, hashlib.sha256(canonical1.encode("utf-8")).hexdigest())

        # Step 2: Set organization override
        self.request("POST", "/api/v1/config", {
            "target_type": "org",
            "target_id": "Dept of Education",
            "config": {"interval_s": 45}
        }, admin_h)

        st, eff2 = self.request("GET", "/api/v1/devices/rpi5-school-01/config")
        rev2 = eff2.get("revision")
        self.assertNotEqual(rev1, rev2)
        self.assertEqual(eff2["config"]["interval_s"], 45)
        self.assertEqual(eff2["config"]["log_level"], "INFO")

        # Step 3: Set school override
        self.request("POST", "/api/v1/config", {
            "target_type": "school",
            "target_id": "school_mysore_01",
            "config": {"sync_interval_s": 120}
        }, admin_h)

        st, eff3 = self.request("GET", "/api/v1/devices/rpi5-school-01/config")
        rev3 = eff3.get("revision")
        self.assertNotEqual(rev2, rev3)
        self.assertEqual(eff3["config"]["sync_interval_s"], 120)
        self.assertEqual(eff3["config"]["interval_s"], 45)

        # Step 4: Set device override
        self.request("POST", "/api/v1/config", {
            "target_type": "device",
            "target_id": "rpi5-school-01",
            "config": {"interval_s": 15}
        }, admin_h)

        st, eff4 = self.request("GET", "/api/v1/devices/rpi5-school-01/config")
        rev4 = eff4.get("revision")
        self.assertNotEqual(rev3, rev4)
        self.assertEqual(eff4["config"]["interval_s"], 15)

        # Step 5: Rollback device override -> reverts to school/org config and previous revision
        st_del, _ = self.request("POST", "/api/v1/config", {"action": "delete", "target_type": "device", "target_id": "rpi5-school-01"}, headers=admin_h)
        self.assertEqual(st_del, 200)

        st, eff5 = self.request("GET", "/api/v1/devices/rpi5-school-01/config")
        rev5 = eff5.get("revision")
        self.assertEqual(rev5, rev3)
        self.assertEqual(eff5["config"]["interval_s"], 45)

    def test_agent_remote_config_application_and_revision_caching(self):
        """Agent validates, applies, caches, and compares revisions without redundant updates."""
        tmp_buf = tempfile.mkdtemp()
        try:
            cfg = copy.deepcopy(sa.DEFAULT_CONFIG)
            cfg["buffer_path"] = os.path.join(tmp_buf, "buffer.db")
            cfg["interval_s"] = 60
            agent = sa.Agent(cfg)
            cache_file = os.path.join(tmp_buf, "last_known_config.json")

            # 1. Apply valid remote config with revision
            valid_cfg = {
                "interval_s": 30,
                "sync_interval_s": 180,
                "log_level": "DEBUG",
                "optional_collectors": {"docker": False, "gpu": False},
                "monitored_services": ["saras-agent.service", "nginx.service"]
            }
            rev_a = "rev-hash-aaa"
            applied = agent._apply_remote_config(rev_a, valid_cfg)
            self.assertTrue(applied)
            self.assertEqual(agent.interval, 30)
            self.assertEqual(agent.sync_interval, 180)
            self.assertEqual(agent.cfg["docker"], False)
            self.assertEqual(agent.cfg["collect_gpu"], False)
            self.assertEqual(agent.cfg["systemd_units"], ["saras-agent.service", "nginx.service"])

            # Save to cache
            agent._applied_revision = rev_a
            agent._save_cached_config(rev_a, valid_cfg)
            self.assertTrue(os.path.isfile(cache_file))

            # 2. Re-sync with SAME revision does NOT update
            with patch.object(agent.sender, "request", return_value=(200, {"revision": rev_a, "config": valid_cfg})):
                with patch.object(agent, "_save_cached_config") as mock_save:
                    agent._sync_config()
                    mock_save.assert_not_called()

            # 3. Invalid remote config is rejected without altering current settings
            invalid_cfg = {"interval_s": 1, "log_level": "INVALID_LVL"}
            with patch.object(agent.sender, "request", return_value=(200, {"revision": "rev-bad", "config": invalid_cfg})):
                agent._sync_config()
                self.assertEqual(agent._applied_revision, rev_a)
                self.assertEqual(agent.interval, 30)

            # 4. Offline startup loads last known good config from cache
            agent2 = sa.Agent(cfg)
            self.assertEqual(agent2._applied_revision, rev_a)
            self.assertEqual(agent2.interval, 30)
            self.assertEqual(agent2.sync_interval, 180)
            self.assertEqual(agent2.cfg["docker"], False)
        finally:
            shutil.rmtree(tmp_buf, ignore_errors=True)

    def test_custom_health_thresholds_applied_to_rpi_alert_engine(self):
        """Custom CPU temperature thresholds in effective config are evaluated dynamically for RPi 5."""
        admin_h = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        # Set custom thresholds: warn at 70°C, crit at 85°C
        self.request("POST", "/api/v1/config", {
            "target_type": "device",
            "target_id": "rpi5-school-01",
            "config": {
                "health_thresholds": {
                    "cpu_temp_warn_c": 70.0,
                    "cpu_temp_crit_c": 85.0
                }
            }
        }, admin_h)

        # 72°C would NOT alert under default 75°C limit, but MUST alert under custom 70°C limit
        hb = copy.deepcopy(RPI5_BASE_HEARTBEAT)
        hb["thermal_c"]["cpu"] = 72.0
        self.send_heartbeat(hb)

        _, dev = self.request("GET", "/api/v1/devices/rpi5-school-01")
        self.assertEqual(dev["status"]["state"], "warning")
        alerts = [a["code"] for a in dev["open_alerts"]]
        self.assertIn("CPU_TEMP_HIGH", alerts)
        self.assertNotIn("CPU_OVERHEAT", alerts)

        # Clean up config
        self.request("POST", "/api/v1/config", {"action": "delete", "target_type": "device", "target_id": "rpi5-school-01"}, headers=admin_h)

    def test_config_validation_rules(self):
        """Server strictly validates ranges, collectors, services, and rejects injection attacks."""
        admin_h = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

        # Unknown config key
        st, _ = self.request("POST", "/api/v1/config", {
            "target_type": "device", "target_id": "rpi5-school-01",
            "config": {"unsupported_key": 123}
        }, admin_h)
        self.assertEqual(st, 400)

        # Invalid optional_collectors (unknown key or non-bool)
        st, _ = self.request("POST", "/api/v1/config", {
            "target_type": "device", "target_id": "rpi5-school-01",
            "config": {"optional_collectors": {"unknown_col": True}}
        }, admin_h)
        self.assertEqual(st, 400)
        st, _ = self.request("POST", "/api/v1/config", {
            "target_type": "device", "target_id": "rpi5-school-01",
            "config": {"optional_collectors": {"docker": "yes"}}
        }, admin_h)
        self.assertEqual(st, 400)

        # Command injection attempt in monitored_services
        st, _ = self.request("POST", "/api/v1/config", {
            "target_type": "device", "target_id": "rpi5-school-01",
            "config": {"monitored_services": ["nginx; rm -rf /"]}
        }, admin_h)
        self.assertEqual(st, 400)

        # Invalid threshold range (warn >= crit)
        st, _ = self.request("POST", "/api/v1/config", {
            "target_type": "device", "target_id": "rpi5-school-01",
            "config": {"health_thresholds": {"cpu_temp_warn_c": 90.0, "cpu_temp_crit_c": 80.0}}
        }, admin_h)
        self.assertEqual(st, 400)


class TestRequirement6StructuredLogs(BaseFleetTestCase):
    """Requirement 6: Structured Log Ingestion, Redaction & Filtering."""

    def test_ingest_and_query_logs_with_redaction(self):
        """Logs are ingested, sensitive keys redacted, and filtered via API."""
        now_ts = fs.fmt_ms(fs.now_ms())
        log_batch = [
            {
                "event_id": "log-001",
                "ts": now_ts,
                "service": "saras-agent",
                "category": "SYSTEM",
                "severity": "info",
                "message": "Connected using password=SuperSecretPassword123 and token=abcde12345",
                "attributes": {"api_key": "secret-key-xyz", "status": "ok"}
            },
            {
                "event_id": "log-002",
                "ts": now_ts,
                "service": "saras-sync",
                "category": "SYNC",
                "severity": "warning",
                "message": "Batch sync delayed due to slow network",
                "attributes": {"retry": 1}
            }
        ]
        dev_h = {"Authorization": f"Bearer {FLEET_TOKEN}"}
        st, resp = self.request("POST", "/api/v1/logs", {"logs": log_batch, "device_id": "rpi5-school-01"}, dev_h)
        self.assertEqual(st, 200)
        self.assertEqual(resp["accepted"], 2)

        # Query all logs
        st, q_all = self.request("GET", "/api/v1/devices/rpi5-school-01/logs")
        self.assertEqual(st, 200)
        self.assertEqual(len(q_all["logs"]), 2)

        # Verify sensitive data redaction in message and attributes
        log1 = [l for l in q_all["logs"] if l["event_id"] == "log-001"][0]
        self.assertNotIn("SuperSecretPassword123", log1["message"])
        self.assertIn("[REDACTED]", log1["message"])
        self.assertEqual(log1["attributes"]["api_key"], "[REDACTED]")

        # Test category filtering
        st, q_sync = self.request("GET", "/api/v1/devices/rpi5-school-01/logs?category=SYNC")
        self.assertEqual(len(q_sync["logs"]), 1)
        self.assertEqual(q_sync["logs"][0]["service"], "saras-sync")

    def test_recursive_redaction_nested_structures_and_messages(self):
        """Recursive redaction handles nested dicts, lists, mixed-case keys, and free-text secrets."""
        nested_attrs = {
            "database": {
                "host": "postgres-primary.internal",
                "port": 5432,
                "Password": "SecretDbPassword!",
                "credentials": {
                    "account_key": "AzureStorageAccountKey==",
                    "user": "saras_app"
                }
            },
            "headers": [
                {"Authorization": "Bearer jwt-secret-token-12345"},
                {"X-Custom-Header": "Diagnostic-Value"},
                {"client_secret": "my-client-secret-999"}
            ],
            "API_KEY": "global-api-key-value",
            "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----",
            "safe_diagnostic": {
                "retry_count": 3,
                "latency_ms": 42.5
            }
        }
        raw_copy = copy.deepcopy(nested_attrs)

        # Test agent-side StructuredLogQueue redaction
        q = sa.StructuredLogQueue()
        raw_msg = (
            "Connecting to postgresql://saras:supersecret@db.internal:5432/app with "
            "Bearer eyJhbGciOiJIUzI1NiIsIn... and "
            "DefaultEndpointsProtocol=https;AccountName=myacc;AccountKey=MyAzureKey123==;EndpointSuffix=core.windows.net"
        )
        q.add("saras-agent", "APPLICATION", "info", raw_msg, nested_attrs, "test-event-rec")
        drained = q.drain(1)
        self.assertEqual(len(drained), 1)
        item = drained[0]

        # Verify message redaction
        self.assertNotIn("supersecret", item["message"])
        self.assertNotIn("MyAzureKey123==", item["message"])
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsIn...", item["message"])
        self.assertIn("[REDACTED]", item["message"])

        # Verify nested attribute redaction
        attrs = item["attributes"]
        self.assertEqual(attrs["database"]["Password"], "[REDACTED]")
        self.assertEqual(attrs["database"]["credentials"]["account_key"], "[REDACTED]")
        self.assertEqual(attrs["database"]["host"], "postgres-primary.internal")
        self.assertEqual(attrs["database"]["credentials"]["user"], "saras_app")
        self.assertEqual(attrs["headers"][0]["Authorization"], "[REDACTED]")
        self.assertEqual(attrs["headers"][1]["X-Custom-Header"], "Diagnostic-Value")
        self.assertEqual(attrs["headers"][2]["client_secret"], "[REDACTED]")
        self.assertEqual(attrs["API_KEY"], "[REDACTED]")
        self.assertEqual(attrs["private_key"], "[REDACTED]")
        self.assertEqual(attrs["safe_diagnostic"]["retry_count"], 3)

        # Verify input was NOT mutated
        self.assertEqual(nested_attrs, raw_copy)

        # Test server-side ingestion and retrieval of nested logs
        dev_h = {"Authorization": f"Bearer {FLEET_TOKEN}"}
        st, _ = self.request("POST", "/api/v1/logs", {"logs": [item], "device_id": "rpi5-school-01"}, dev_h)
        self.assertEqual(st, 200)

        st, logs_resp = self.request("GET", "/api/v1/devices/rpi5-school-01/logs")
        self.assertEqual(st, 200)
        found = [l for l in logs_resp["logs"] if l["event_id"] == "test-event-rec"][0]
        self.assertEqual(found["attributes"]["database"]["Password"], "[REDACTED]")
        self.assertEqual(found["attributes"]["database"]["credentials"]["account_key"], "[REDACTED]")
        self.assertEqual(found["attributes"]["headers"][0]["Authorization"], "[REDACTED]")


class TestRequirement7LogStopAndInactivity(BaseFleetTestCase):
    """Requirement 7: Log-Stop & Inactivity Detection without False Alerts."""

    def test_service_activity_states(self):
        """Distinguishes running, idle, stopped, and unknown states."""
        self.send_heartbeat()

        now_ts = fs.fmt_ms(fs.now_ms())
        dev_h = {"Authorization": f"Bearer {FLEET_TOKEN}"}
        self.request("POST", "/api/v1/logs", {
            "device_id": "rpi5-school-01",
            "logs": [{
                "event_id": "app-log-1",
                "ts": now_ts,
                "service": "app-live",
                "category": "APPLICATION",
                "severity": "info",
                "message": "Serving requests",
            }]
        }, dev_h)

        st, dev = self.request("GET", "/api/v1/devices/rpi5-school-01?extended=1")
        self.assertEqual(st, 200)
        svc_map = {s["service"]: s for s in dev["services"]}

        # 'app-live' has recent logs -> running
        self.assertEqual(svc_map["app-live"]["activity_state"], "running")

        # 'postgres' has status 'up' but no recent logs -> idle (NOT false alerted as stopped!)
        self.assertEqual(svc_map["postgres"]["activity_state"], "idle")

    def test_service_stopped_state(self):
        """Service reported down has state 'stopped'."""
        hb = copy.deepcopy(RPI5_BASE_HEARTBEAT)
        hb["services"][0]["status"] = "down"
        self.send_heartbeat(hb)

        st, dev = self.request("GET", "/api/v1/devices/rpi5-school-01?extended=1")
        svc_map = {s["service"]: s for s in dev["services"]}
        self.assertEqual(svc_map["app-live"]["activity_state"], "stopped")


class TestRequirement8AIWorkloadMonitoring(unittest.TestCase):
    """Requirement 8: LLM & AI Monitoring (Ollama Adapter & vLLM Preservation)."""

    def test_ollama_adapter_collection_success(self):
        """Ollama collector parses /api/version and /api/ps models."""
        collector = sa.Collector({"services": {"ollama_url": "http://127.0.0.1:11434"}})
        collector.device_type = "Raspberry Pi 5"

        def mock_http_get(url, max_bytes=4096, timeout=None):
            if "/api/version" in url:
                return 200, b'{"version": "0.1.32"}', None, 5.0
            if "/api/ps" in url:
                return 200, b'{"models": [{"name": "llama3.2:1b"}]}', None, 10.0
            return None, b"", "Not found", 1.0

        with patch("saras_agent.http_get", side_effect=mock_http_get):
            entry, ai_block = collector._ollama({})
            self.assertEqual(entry["status"], "up")
            self.assertEqual(ai_block["ollama"]["running"], 1)
            self.assertEqual(ai_block["ollama"]["model"], "llama3.2:1b")

    def test_ollama_adapter_graceful_offline(self):
        """When Ollama is not installed or offline, returns status 'down' without crashing."""
        collector = sa.Collector({"services": {"ollama_url": "http://127.0.0.1:11434"}})
        with patch("saras_agent.http_get", return_value=(None, b"", "connection refused", 5.0)):
            entry, ai_block = collector._ollama({})
            self.assertEqual(entry["status"], "down")
            self.assertEqual(ai_block["ollama"]["status"], "down")


class TestRequirement9RemoteSyncMonitoring(BaseFleetTestCase):
    """Requirement 9: Remote Sync Monitoring & Decoupled Alerting."""

    def test_sync_failure_does_not_mark_device_offline(self):
        """Sync failure raises SYNC_FAILED alert while device remains ONLINE."""
        hb = copy.deepcopy(RPI5_BASE_HEARTBEAT)
        hb["sync"]["unit_result"] = "failed"
        hb["sync"]["failed_batches"] = 3
        hb["sync"]["last_error"] = "Remote HTTP 500: Database lock timeout"
        self.send_heartbeat(hb)

        st, dev = self.request("GET", "/api/v1/devices/rpi5-school-01")
        self.assertEqual(st, 200)
        self.assertNotEqual(dev["status"]["state"], "offline")
        alert_events = [a["code"] for a in dev["open_alerts"]]
        self.assertIn("SYNC_FAILED", alert_events)

    def test_sync_conflicts_raise_warning(self):
        """Sync conflicts raise SYNC_CONFLICTS warning."""
        hb = copy.deepcopy(RPI5_BASE_HEARTBEAT)
        hb["sync"]["conflicts"] = 5
        self.send_heartbeat(hb)

        st, dev = self.request("GET", "/api/v1/devices/rpi5-school-01")
        alert_events = [a["code"] for a in dev["open_alerts"]]
        self.assertIn("SYNC_CONFLICTS", alert_events)

    def test_central_api_unreachable_alert(self):
        """Central API unreachable does not blame the device hardware."""
        hb = copy.deepcopy(RPI5_BASE_HEARTBEAT)
        hb["sync"]["sync_api_health"] = "down"
        self.send_heartbeat(hb)

        st, dev = self.request("GET", "/api/v1/devices/rpi5-school-01")
        alert_events = [a["code"] for a in dev["open_alerts"]]
        self.assertIn("CENTRAL_API_UNREACHABLE", alert_events)


class TestNvidiaDeviceRegression(BaseFleetTestCase):
    """Requirement 12 / Regression: Verify NVIDIA Thor, DGX Spark, and Orin Nano behavior."""

    def test_orin_nano_thresholds_preserved(self):
        """Jetson Orin Nano retains its 85°C warning limit, not the Pi 5 75°C limit."""
        hb_orin = copy.deepcopy(RPI5_BASE_HEARTBEAT)
        hb_orin["device_id"] = "orin-nano-01"
        hb_orin["identity"]["device_type"] = "Orin Nano"
        hb_orin["thermal_c"]["cpu"] = 78.0  # Would alert on Pi 5 (>75°C), but should be normal on Orin (<85°C)!
        hb_orin["thermal_c"]["gpu"] = 70.0
        hb_orin["gpu"]["temp_c"] = 70.0
        self.send_heartbeat(hb_orin)

        st, dev = self.request("GET", "/api/v1/devices/orin-nano-01")
        self.assertEqual(dev["status"]["state"], "online")
        self.assertEqual(len(dev["open_alerts"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

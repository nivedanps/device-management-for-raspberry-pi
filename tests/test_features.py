#!/usr/bin/env python3
"""Comprehensive test suite for the 8 extended feature areas in SARAS Device Management:
  1. Application Version (discovery, validation, collector, persistence)
  2. Firmware Updates (capability detection, undervoltage guard, simulation)
  3. Operating System Updates (capabilities, locks, preflight, simulation)
  4. Application & Docker Image Updates (preflight, container simulation, verification)
  5. Configuration Updates (hierarchical scopes, validation, revision hashing, auth)
  6. Remote Update Deployment (job lifecycle, auth, heartbeat dispatch, idempotency, cancel)
  7. Scheduled Updates (scheduling, maintenance windows, due processing, cancel)
  8. Hardware Failure Detection (NVMe SMART, fan stall, memory ECC alerts & recovery)
"""
import sys

sys.dont_write_bytecode = True

import copy
import hashlib
import http.client
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
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

FLEET_TOKEN = "test-fleet-token"
ADMIN_TOKEN = "test-admin-token"
DEVICE_ID = "test-dev-001"


def make_heartbeat(device_id=DEVICE_ID, **kwargs):
    hb = {
        "schema": "saras.heartbeat/1",
        "agent_version": "0.1.0",
        "device_id": device_id,
        "boot_id": "test-boot-1234",
        "seq": 1,
        "sent_at": fs.fmt_ms(fs.now_ms()),
        "interval_s": 1,
        "buffered": False,
        "claimed": {"product": "SARAS Test Box", "site_code": "test_site", "customer": "test_org"},
        "identity": {
            "serial": device_id,
            "model": "Raspberry Pi 5",
            "device_type": "Raspberry Pi 5",
            "hostname": "test-pi",
            "lan_ip": "192.168.1.100",
        },
        "system": {
            "booted_at": "2026-09-29T04:38:36.000Z",
            "uptime_s": 1200,
            "ntp_synced": True,
            "cpu_pct": 10.0,
            "cpu_per_core_pct": [10.0, 10.0],
            "cpu_freq_mhz": 2400,
            "load_1m": 0.5,
            "load_5m": 0.5,
            "load_15m": 0.5,
            "ram_total_mb": 8192,
            "ram_used_mb": 2048,
            "ram_pct": 25.0,
            "swap_pct": 0.0,
            "disks": [{"mount": "/", "total_gb": 64.0, "used_gb": 16.0, "pct": 25.0}],
            "journal_mb": 50.0,
        },
        "gpu": None,
        "thermal_c": {"soc": 48.0},
        "power": {
            "mode": "STANDARD",
            "fan_rpm": 2500,
            "fan_stalled": False,
            "throttling": {"undervoltage_now": False, "undervoltage_occurred": False},
        },
        "network": {
            "iface": "eth0",
            "link": "up",
            "gateway_rtt_ms": 1.2,
            "gateway_loss_pct": 0.0,
            "last_server_rtt_ms": 5.0,
        },
        "services": [
            {"name": "app-live", "kind": "http", "status": "up", "latency_ms": 2.0, "detail": "HTTP 200 Healthy", "restarts": None, "since": None}
        ],
        "sync": {
            "last_success": "2026-09-29T10:00:00.000Z",
            "last_attempt": "2026-09-29T10:00:00.000Z",
            "offline_count": 0,
            "unit_result": "success",
            "age_h": 0.1,
        },
        "software": {
            "os": "Debian GNU/Linux 12 (bookworm)",
            "kernel": "6.6.20+rpt-rpi-2712",
            "l4t": None,
            "jetpack": None,
            "nvidia_driver": None,
            "cuda_driver": None,
            "docker": "26.1.1",
            "nvidia_container_toolkit": None,
            "jetson_stats": None,
            "bootloader": "v2024.01",
            "python": "3.11.2",
            "app_version": "1.0.0",
            "vllm_version": None,
            "llm_model": None,
        },
    }
    for k, v in kwargs.items():
        parts = k.split("__")
        curr = hb
        for part in parts[:-1]:
            curr = curr[part]
        curr[parts[-1]] = v
    return hb


class BaseFeatureTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="saras-features-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.static = self.tmp / "static"
        self.static.mkdir()
        (self.static / "index.html").write_text("<!doctype html><p>test</p>")
        (self.static / "app.js").write_text("console.log(1);")
        (self.static / "style.css").write_text("body{}")

        cfg = {
            "port": 0,
            "db_path": str(self.tmp / "fleet.db"),
            "fleet_token": FLEET_TOKEN,
            "admin_token": ADMIN_TOKEN,
            "offline_min_s": 10,
        }
        self.cfg = fs.normalize_config(cfg, self.tmp)
        self.app = fs.FleetApp(self.cfg, static_dir=self.static)
        self.httpd = fs.make_server(self.app)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.app.start_background()
        self.addCleanup(self._stop)

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)
        self.app.stop()

    def make_agent(self, extra_cfg=None):
        agent_cfg = {
            **copy.deepcopy(sa.DEFAULT_CONFIG),
            "server_url": f"http://127.0.0.1:{self.port}",
            "token": FLEET_TOKEN,
            "interval_s": 60,
            "buffer_path": str(self.tmp / "agent_buf.db"),
            "device_id": DEVICE_ID,
            **(extra_cfg or {}),
        }
        return sa.Agent(agent_cfg)

    def request(self, method, path, body=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            if isinstance(body, (dict, list)):
                body_bytes = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            elif isinstance(body, str):
                body_bytes = body.encode("utf-8")
            else:
                body_bytes = body
        else:
            body_bytes = None
        conn.request(method, path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        parsed = None
        if resp.getheader("Content-Type", "").startswith("application/json"):
            try:
                parsed = json.loads(data.decode("utf-8-sig"))
            except Exception:
                pass
        conn.close()
        return resp.status, parsed, data

    def post_heartbeat(self, hb):
        return self.request("POST", "/api/v1/heartbeat", hb, token=FLEET_TOKEN)


class TestFeature1AppVersion(BaseFeatureTestCase):
    """Feature 1: Application Version discovery, validation, and persistence."""

    def test_validate_app_version(self):
        self.assertEqual(sa.validate_app_version("1.0.0"), "1.0.0")
        self.assertEqual(sa.validate_app_version("v2.4.1-rc1+build42"), "v2.4.1-rc1+build42")
        self.assertEqual(sa.validate_app_version("  2.0.0  "), "2.0.0")
        self.assertIsNone(sa.validate_app_version(None))
        self.assertIsNone(sa.validate_app_version(""))
        self.assertIsNone(sa.validate_app_version("a" * 70))  # too long (>64)
        self.assertIsNone(sa.validate_app_version("1.0; rm -rf /"))  # illegal chars

    def test_discover_app_version_from_file(self):
        vfile = self.tmp / "version.json"
        vfile.write_text(json.dumps({"version": "3.2.1"}))
        cfg = {"app_version_file": str(vfile)}
        found = sa.discover_app_version(cfg)
        self.assertEqual(found, "3.2.1")

    def test_discover_app_version_missing_and_malformed(self):
        cfg = {"app_version_file": str(self.tmp / "nonexistent.json")}
        self.assertIsNone(sa.discover_app_version(cfg))

        badfile = self.tmp / "bad.json"
        badfile.write_text("{not valid json}")
        cfg = {"app_version_file": str(badfile)}
        self.assertIsNone(sa.discover_app_version(cfg))

    def test_discover_app_version_from_url(self):
        import http.server

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"app_version": "4.5.6"}')

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            cfg = {"app_version_url": f"http://127.0.0.1:{port}/api/version"}
            found = sa.discover_app_version(cfg)
            self.assertEqual(found, "4.5.6")
        finally:
            server.shutdown()
            server.server_close()

    def test_server_persists_app_version(self):
        hb = make_heartbeat(software__app_version="2.1.0-alpha")
        status, parsed, _ = self.post_heartbeat(hb)
        self.assertEqual(status, 200)

        status, dev, _ = self.request("GET", f"/api/v1/devices/{DEVICE_ID}", token=ADMIN_TOKEN)
        self.assertEqual(status, 200)
        app_item = next((s for s in dev["software"] if s["component"] == "app_version"), None)
        self.assertIsNotNone(app_item)
        self.assertEqual(app_item["version"], "2.1.0-alpha")


class TestFeature2FirmwareUpdates(BaseFeatureTestCase):
    """Feature 2: Firmware Updates capability checks, undervoltage guard, and simulation."""

    def test_firmware_capability_detection(self):
        caps = sa._detect_firmware_capabilities()
        self.assertIn("platform", caps)
        self.assertIn("supported", caps)
        self.assertIn("strategy", caps)

    def test_firmware_update_undervoltage_guard(self):
        agent = self.make_agent()
        with patch.object(agent.collector, "device_type", "Raspberry Pi 5"), \
             patch("saras_agent.read_rpi_throttling", return_value={"under_voltage_now": True}):
            with self.assertRaises(sa.AgentError) as ctx:
                agent._handle_firmware_update("job-fw-1", "v2024.10", {"dry_run": True})
            self.assertIn("under-voltage", str(ctx.exception).lower())

    def test_firmware_update_dry_run_simulation(self):
        agent = self.make_agent()
        with patch.object(agent.collector, "device_type", "Raspberry Pi 5"), \
             patch("saras_agent.read_rpi_throttling", return_value={"under_voltage_now": False}):
            res = agent._handle_firmware_update("job-fw-2", "v2024.10", {"dry_run": True})
            self.assertTrue(res.get("ok"))
            self.assertTrue(res.get("dry_run"))
            self.assertIn("rpi-eeprom", res.get("platform"))


class TestFeature3OSUpdates(BaseFeatureTestCase):
    """Feature 3: Operating System Updates capabilities, preflight checks, and simulation."""

    def test_os_capability_detection(self):
        caps = sa._detect_os_capabilities()
        self.assertIn("os", caps)
        self.assertIn("package_manager", caps)

    def test_os_update_lock_detection(self):
        agent = self.make_agent()
        lock_file = self.tmp / "dpkg_lock"
        lock_file.write_text("locked")
        with patch("saras_agent.APT_LOCK_PATHS", [str(lock_file)]), \
             patch("os.path.isfile", return_value=True), \
             patch("os.open", side_effect=PermissionError("locked")):
            with self.assertRaises(sa.AgentError) as ctx:
                agent._handle_os_update("job-os-1", None, {"dry_run": True})
            self.assertIn("lock", str(ctx.exception).lower())

    def test_os_update_dry_run_simulation(self):
        agent = self.make_agent()
        with patch("saras_agent.APT_LOCK_PATHS", []), \
             patch("os.path.isfile", return_value=True):
            res = agent._handle_os_update("job-os-2", None, {"dry_run": True})
            self.assertTrue(res.get("ok"))
            self.assertTrue(res.get("dry_run"))
            self.assertIn("simulation", res.get("message").lower())


class TestFeature4AppDockerUpdates(BaseFeatureTestCase):
    """Feature 4: Application and Docker Image Updates."""

    def test_app_update_preflight_disk_space(self):
        agent = self.make_agent()
        mock_disks = [{"mount": "/", "total_gb": 10.0, "free_gb": 0.2}]
        with patch("saras_agent.disk_usage_list", return_value=mock_disks):
            with self.assertRaises(sa.AgentError) as ctx:
                agent._handle_app_update("job-app-1", "2.0.0", {"dry_run": True})
            self.assertIn("insufficient disk space", str(ctx.exception).lower())

    def test_app_update_simulation(self):
        agent = self.make_agent()
        mock_disks = [{"mount": "/", "total_gb": 100.0, "free_gb": 50.0}]
        with patch("saras_agent.disk_usage_list", return_value=mock_disks):
            res = agent._handle_app_update("job-app-2", "2.0.0", {"dry_run": True})
            self.assertTrue(res.get("ok"))
            self.assertTrue(res.get("dry_run"))
            self.assertIn("2.0.0", res.get("message"))


class TestFeature5ConfigurationUpdates(BaseFeatureTestCase):
    """Feature 5: Configuration Updates, hierarchical inheritance, validation, revision hashing."""

    def test_config_auth_required(self):
        # No token
        status, _, _ = self.request("POST", "/api/v1/config", {"target_type": "default", "target_id": "default", "config": {}})
        self.assertEqual(status, 401)
        # Invalid token
        status, _, _ = self.request("POST", "/api/v1/config", {"target_type": "default", "target_id": "default", "config": {}}, token="bad-token")
        self.assertEqual(status, 401)

    def test_config_hierarchy_and_revision_hash(self):
        # 1. Set org config
        payload = {
            "target_type": "org",
            "target_id": "test_org",
            "config": {"heartbeat_interval_s": 30},
        }
        status, res, _ = self.request("POST", "/api/v1/config", payload, token=ADMIN_TOKEN)
        self.assertEqual(status, 200)
        self.assertIn("revision_hash", res)
        self.assertEqual(len(res["revision_hash"]), 64)

        # 2. Ingest device heartbeat
        self.post_heartbeat(make_heartbeat())

        # 3. Check effective config for device
        status, eff_cfg, _ = self.request("GET", f"/api/v1/devices/{DEVICE_ID}/config", token=ADMIN_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(eff_cfg["config"].get("heartbeat_interval_s"), 30)

        # 4. Override at device level
        dev_payload = {
            "target_type": "device",
            "target_id": DEVICE_ID,
            "config": {"heartbeat_interval_s": 15},
        }
        status, res, _ = self.request("POST", "/api/v1/config", dev_payload, token=ADMIN_TOKEN)
        self.assertEqual(status, 200)

        status, eff_cfg2, _ = self.request("GET", f"/api/v1/devices/{DEVICE_ID}/config", token=ADMIN_TOKEN)
        self.assertEqual(eff_cfg2["config"].get("heartbeat_interval_s"), 15)

        # 5. Delete device level override
        del_payload = {
            "target_type": "device",
            "target_id": DEVICE_ID,
            "action": "delete",
        }
        status, _, _ = self.request("POST", "/api/v1/config", del_payload, token=ADMIN_TOKEN)
        self.assertEqual(status, 200)

        status, eff_cfg3, _ = self.request("GET", f"/api/v1/devices/{DEVICE_ID}/config", token=ADMIN_TOKEN)
        self.assertEqual(eff_cfg3["config"].get("heartbeat_interval_s"), 30)

    def test_config_validation_rules(self):
        # heartbeat_interval_s must be within [5, 3600]
        bad_payload = {
            "target_type": "device",
            "target_id": DEVICE_ID,
            "config": {"heartbeat_interval_s": 1},
        }
        status, res, _ = self.request("POST", "/api/v1/config", bad_payload, token=ADMIN_TOKEN)
        self.assertEqual(status, 400)


class TestFeature6RemoteUpdateDeployment(BaseFeatureTestCase):
    """Feature 6: Remote Update Deployment, job creation, delivery via heartbeat, idempotency."""

    def test_create_and_fetch_job(self):
        # 1. Ingest device
        self.post_heartbeat(make_heartbeat())

        # 2. Create job
        job_req = {
            "device_id": DEVICE_ID,
            "operation": "app_update",
            "target_version": "2.5.0",
            "payload": {"dry_run": True},
        }
        status, job, _ = self.request("POST", "/api/v1/updates/jobs", job_req, token=ADMIN_TOKEN)
        self.assertEqual(status, 201)
        job_id = job["job_id"]
        self.assertEqual(job["status"], "scheduled")

        # 3. Fetch single job
        status, fetched, _ = self.request("GET", f"/api/v1/updates/jobs/{job_id}", token=ADMIN_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(fetched["job"]["job_id"], job_id)

        # 4. Device sends heartbeat -> receives pending job in commands
        status, hb_res, _ = self.post_heartbeat(make_heartbeat())
        self.assertEqual(status, 200)
        commands = hb_res.get("commands", [])
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["job_id"], job_id)
        self.assertEqual(commands[0]["operation"], "app_update")

        # 5. Device updates status to completed
        status_update = {
            "device_id": DEVICE_ID,
            "status": "completed",
            "progress_pct": 100,
            "message": "Update simulated successfully",
        }
        status, updated, _ = self.request("POST", f"/api/v1/updates/jobs/{job_id}/status", status_update, token=FLEET_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(updated["job"]["status"], "completed")

        # 6. Next heartbeat has no more pending commands
        status, hb_res2, _ = self.post_heartbeat(make_heartbeat())
        self.assertEqual(hb_res2.get("commands", []), [])

    def test_cancel_update_job(self):
        self.post_heartbeat(make_heartbeat())
        job_req = {"device_id": DEVICE_ID, "operation": "os_update"}
        status, job, _ = self.request("POST", "/api/v1/updates/jobs", job_req, token=ADMIN_TOKEN)
        self.assertEqual(status, 201)
        job_id = job["job_id"]

        status, cancelled, _ = self.request("POST", f"/api/v1/updates/jobs/{job_id}/cancel", token=ADMIN_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(cancelled["job"]["status"], "cancelled")

    def test_agent_command_execution_and_idempotency(self):
        agent = self.make_agent()
        cmd = {
            "job_id": "job-test-idem-1",
            "operation": "health_check",
            "target_version": None,
            "payload": {},
        }
        # First execution executes cleanly
        agent._handle_commands([cmd])
        self.assertIn("job-test-idem-1", agent._completed_jobs)

        # Second execution skips already completed job without re-running
        with patch.object(agent, "_execute_job") as mock_exec:
            agent._handle_commands([cmd])
            mock_exec.assert_not_called()


class TestFeature7ScheduledUpdates(BaseFeatureTestCase):
    """Feature 7: Scheduled Updates, timezone handling, maintenance windows, and trigger."""

    def test_create_and_cancel_schedule(self):
        self.post_heartbeat(make_heartbeat())
        sched_req = {
            "name": "Nightly OS Update",
            "device_id": DEVICE_ID,
            "operation": "os_update",
            "scheduled_at": "2029-01-01T02:00:00Z",
            "maintenance_window": "01:00-05:00",
        }
        status, sched, _ = self.request("POST", "/api/v1/updates/schedules", sched_req, token=ADMIN_TOKEN)
        self.assertEqual(status, 201)
        sched_id = sched["schedule_id"]
        self.assertEqual(sched["status"], "active")

        # Cancel schedule
        status, cancelled, _ = self.request("POST", f"/api/v1/updates/schedules/{sched_id}/cancel", token=ADMIN_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(cancelled["schedule"]["status"], "cancelled")

    def test_due_schedule_triggers_job_dispatch(self):
        self.post_heartbeat(make_heartbeat())
        # Schedule in past
        sched_req = {
            "name": "Immediate Update",
            "device_id": DEVICE_ID,
            "operation": "app_update",
            "target_version": "3.0.0",
            "payload": {"dry_run": True},
            "scheduled_at": "2020-01-01T00:00:00Z",
        }
        status, sched, _ = self.request("POST", "/api/v1/updates/schedules", sched_req, token=ADMIN_TOKEN)
        self.assertEqual(status, 201)

        # Trigger scheduler
        self.app.process_due_schedules()

        # Verify job was created
        jobs = self.app.get_update_jobs(device_id=DEVICE_ID)
        self.assertTrue(any(j["operation"] == "app_update" and j["target_version"] == "3.0.0" for j in jobs))

    def test_maintenance_window_enforcement(self):
        # 12:00 UTC is between 10:00 and 14:00
        t1 = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(fs.is_in_maintenance_window("10:00-14:00", dt=t1))

        # 08:00 UTC is not between 10:00 and 14:00
        t2 = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)
        self.assertFalse(fs.is_in_maintenance_window("10:00-14:00", dt=t2))

        # Overnight window: 22:00 to 04:00 -> 01:00 is inside
        t3 = datetime(2026, 9, 29, 1, 0, tzinfo=timezone.utc)
        self.assertTrue(fs.is_in_maintenance_window("22:00-04:00", dt=t3))


class TestFeature8HardwareFailureDetection(BaseFeatureTestCase):
    """Feature 8: Hardware Failure Detection (NVMe SMART, fan stall, memory ECC)."""

    def test_nvme_storage_critical_alert(self):
        # Heartbeat with NVMe critical warning
        hb = make_heartbeat(
            storage_health={"spare_pct": 5, "percentage_used": 98, "media_errors": 12, "critical_warning": 1}
        )
        self.post_heartbeat(hb)

        status, dev, _ = self.request("GET", f"/api/v1/devices/{DEVICE_ID}", token=ADMIN_TOKEN)
        self.assertEqual(status, 200)
        alerts = [a.get("code") or a.get("rule") for a in dev["open_alerts"]]
        self.assertIn("STORAGE_CRITICAL", alerts)

        # Clear alert on normal heartbeat
        hb_normal = make_heartbeat(
            storage_health={"spare_pct": 100, "percentage_used": 1, "media_errors": 0, "critical_warning": 0}
        )
        self.post_heartbeat(hb_normal)

        status, dev2, _ = self.request("GET", f"/api/v1/devices/{DEVICE_ID}", token=ADMIN_TOKEN)
        alerts2 = [a.get("code") or a.get("rule") for a in dev2["open_alerts"]]
        self.assertNotIn("STORAGE_CRITICAL", alerts2)

    def test_fan_stalled_alert(self):
        # Fan stalled with temperature > 45°C
        hb = make_heartbeat(
            power__fan_stalled=True,
            thermal_c={"soc": 55.0},
        )
        self.post_heartbeat(hb)

        status, dev, _ = self.request("GET", f"/api/v1/devices/{DEVICE_ID}", token=ADMIN_TOKEN)
        alerts = [a.get("code") or a.get("rule") for a in dev["open_alerts"]]
        self.assertIn("FAN_STALLED", alerts)

    def test_memory_ecc_alert(self):
        # Uncorrectable ECC error
        hb = make_heartbeat(
            memory_health={"ecc_ce_count": 0, "ecc_ue_count": 2}
        )
        self.post_heartbeat(hb)

        status, dev, _ = self.request("GET", f"/api/v1/devices/{DEVICE_ID}", token=ADMIN_TOKEN)
        alerts = [a.get("code") or a.get("rule") for a in dev["open_alerts"]]
        self.assertIn("MEMORY_ECC_ERROR", alerts)


if __name__ == "__main__":
    unittest.main()

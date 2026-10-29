#!/usr/bin/env python3
"""Tests for server/fleet_server.py (stdlib unittest).

Run from the device-management folder:

    python3 -m unittest -v tests/test_server.py

Every test starts the real HTTP server on an OS-assigned port on 127.0.0.1 with a fresh
temporary database, and talks to it over HTTP. Fixtures are built from the example heartbeat
in CONTRACT.md section 3, the shape the agent sends.
"""
import sys

sys.dont_write_bytecode = True  # keep __pycache__ out of the source tree

import base64  # noqa: E402
import copy  # noqa: E402
import http.client  # noqa: E402
import itertools  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import queue  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import socket  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
import unittest.mock  # noqa: E402
from collections import namedtuple  # noqa: E402
from pathlib import Path  # noqa: E402
from urllib.parse import quote  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT / "server"
sys.path.insert(0, str(SERVER_DIR))
import fleet_server as fs  # noqa: E402

# Rejections are logged as warnings on purpose; show only real errors while testing.
logging.getLogger("fleet").setLevel(logging.DEBUG if os.environ.get("FLEET_TEST_LOG") else logging.ERROR)

# CONTRACT.md section 3, verbatim.
EXAMPLE_HEARTBEAT = json.loads(r"""
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
    "disks": [ { "mount": "/", "total_gb": 936.0, "used_gb": 49.1, "pct": 5.5 } ],
    "journal_mb": 395.1
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
    "age_h": 90.2
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
      "avg_e2e_latency_s": 4.2, "avg_ttft_s": 0.35
    }
  },
  "collector_errors": { "docker": "permission denied: run the agent as root or add it to the docker group" }
}
""")

FLEET_TOKEN = "test-fleet-token"
ADMIN_TOKEN = "test-admin-token"
DEVICE = EXAMPLE_HEARTBEAT["device_id"]
DAY_MS = 86_400_000
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
DEVICE_COLUMNS = {"device_id", "serial", "model", "device_type", "hostname", "product", "customer", "site_code",
                  "location", "latitude", "longitude", "installed_on", "support_contact", "device_group", "notes",
                  "first_seen"}

Response = namedtuple("Response", "status headers body json")


def set_path(doc, path, value):
    keys = path.split(".")
    for key in keys[:-1]:
        doc = doc[int(key)] if isinstance(doc, list) else doc[key]
    if isinstance(doc, list):
        doc[int(keys[-1])] = value
    else:
        doc[keys[-1]] = value


_SEQ = itertools.count(1000)


def heartbeat(device_id=DEVICE, *, healthy=True, sent_ms=None, **paths):
    """The contract's example heartbeat, sent now, with interval_s 1 and a seq of its own.

    healthy=True clears the two problems the example carries (clock not NTP-synced, last sync
    90 h ago) so a test starts from 'online'. Keyword arguments set nested values, with '__'
    as the path separator: heartbeat(gpu__temp_c=106, system__disks__0__pct=96).
    Every call gets a new seq, like the agent's, so two heartbeats built in the same
    millisecond are never mistaken for one heartbeat delivered twice.
    """
    hb = copy.deepcopy(EXAMPLE_HEARTBEAT)
    hb["device_id"] = device_id
    hb["seq"] = next(_SEQ)
    hb["sent_at"] = fs.fmt_ms(fs.now_ms() if sent_ms is None else sent_ms)
    hb["interval_s"] = 1
    if healthy:
        hb["system"]["ntp_synced"] = True
        hb["sync"]["age_h"] = 1.5
    for path, value in paths.items():
        set_path(hb, path.replace("__", "."), value)
    return hb


def with_service(hb, name, **fields):
    for s in hb["services"]:
        if s["name"] == name:
            s.update(fields)
            return hb
    raise KeyError(name)


class ServerTestCase(unittest.TestCase):
    """Starts FleetApp + its HTTP server (and background jobs) for each test."""

    config = {}
    offline_check_s = 3600.0  # the offline thread is only exercised by OfflineThreadTest
    static_files = {"index.html": "<!doctype html><title>Fleet</title><p>dashboard</p>",
                    "app.js": "console.log('fleet');", "style.css": "body { margin: 0 }"}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fleet-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "secret.txt").write_text("TOP-SECRET")
        self.static = self.tmp / "static"
        if self.static_files is not None:
            self.static.mkdir()
            for name, content in self.static_files.items():
                (self.static / name).write_text(content)
        cfg = {"port": 0, "db_path": str(self.tmp / "fleet.db"), "fleet_token": FLEET_TOKEN,
               "admin_token": ADMIN_TOKEN, "offline_min_s": 2, **self.config}
        self.cfg = fs.normalize_config(cfg, self.tmp)
        self.app = fs.FleetApp(self.cfg, static_dir=self.static, offline_check_s=self.offline_check_s)
        self.httpd = fs.make_server(self.app)
        self.port = self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.app.start_background()
        self.addCleanup(self._stop, thread)

    def _stop(self, thread):
        self.httpd.shutdown()
        self.httpd.server_close()
        thread.join(5)
        self.app.stop()

    # ---- HTTP ----

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            parsed = None
            if (resp.getheader("Content-Type") or "").startswith("application/json") and data:
                parsed = json.loads(data)
            return Response(resp.status, resp.headers, data, parsed)
        finally:
            conn.close()

    def get(self, path, headers=None):
        return self.request("GET", path, headers=headers)

    def get_json(self, path):
        r = self.get(path)
        self.assertEqual(r.status, 200, r.body)
        return r.json

    def post(self, hb, token=FLEET_TOKEN):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self.request("POST", "/api/v1/heartbeat", hb, headers)

    def send(self, hb, token=FLEET_TOKEN):
        r = self.post(hb, token)
        self.assertEqual(r.status, 200, r.body)
        return r.json

    def patch(self, body, token=ADMIN_TOKEN, device=DEVICE):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self.request("PATCH", f"/api/v1/devices/{quote(device, safe='')}", body, headers)

    # ---- database ----

    def q(self, sql, *params):
        conn = sqlite3.connect(self.cfg["db_path"], timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def execute(self, sql, rows):
        conn = sqlite3.connect(self.cfg["db_path"], timeout=15)
        try:
            with conn:
                conn.executemany(sql, rows)
        finally:
            conn.close()

    def count(self, table, where="1", *params):
        return self.q(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", *params)[0]["n"]

    def open_codes(self, device=DEVICE):
        return sorted(r["code"] for r in self.q(
            "SELECT code FROM events WHERE device_id = ? AND kind = 'alert' AND resolved_at IS NULL", device))

    def events_of(self, code, device=DEVICE):
        return self.q("SELECT * FROM events WHERE device_id = ? AND code = ? ORDER BY id", device, code)

    def status_row(self, device=DEVICE):
        rows = self.q("SELECT * FROM device_status WHERE device_id = ?", device)
        return rows[0] if rows else None

    def state(self, device=DEVICE):
        row = self.status_row(device)
        return row["state"], json.loads(row["reasons"])


# =========================================================================== section 3

class HeartbeatAuthTest(ServerTestCase):
    config = {"device_tokens": {"box-with-own-token": "own-token"}}

    def test_missing_token_is_401(self):
        r = self.post(heartbeat(), token=None)
        self.assertEqual(r.status, 401)
        self.assertEqual(r.json["ok"], False)
        self.assertIn("error", r.json)
        self.assertEqual(self.count("devices"), 0)

    def test_wrong_token_is_401(self):
        self.assertEqual(self.post(heartbeat(), token="not-the-token").status, 401)
        self.assertEqual(self.count("devices"), 0)
        self.assertEqual(self.count("metrics"), 0)

    def test_non_bearer_scheme_is_401(self):
        r = self.request("POST", "/api/v1/heartbeat", heartbeat(), {"Authorization": f"Basic {FLEET_TOKEN}"})
        self.assertEqual(r.status, 401)

    def test_device_token_replaces_fleet_token_for_that_device(self):
        own = heartbeat("box-with-own-token")
        self.assertEqual(self.post(own, FLEET_TOKEN).status, 401)
        self.assertEqual(self.post(own, "own-token").status, 200)
        self.assertEqual(self.post(heartbeat("other-box"), "own-token").status, 401)
        self.assertEqual(self.post(heartbeat("other-box"), FLEET_TOKEN).status, 200)
        self.assertEqual({r["device_id"] for r in self.q("SELECT device_id FROM devices")},
                         {"box-with-own-token", "other-box"})

    def test_ok_response_shape(self):
        r = self.post(heartbeat(interval_s=60))
        self.assertEqual(r.status, 200)
        self.assertEqual(set(r.json), {"ok", "server_time", "device_id", "next_interval_s", "commands"})
        self.assertIs(r.json["ok"], True)
        self.assertRegex(r.json["server_time"], TS_RE)
        self.assertEqual(r.json["device_id"], DEVICE)
        self.assertEqual(r.json["next_interval_s"], 60)
        self.assertEqual(r.json["commands"], [])


class HeartbeatValidationTest(ServerTestCase):
    def auth(self):
        return {"Authorization": f"Bearer {FLEET_TOKEN}", "Content-Type": "application/json"}

    def test_body_over_256kb_is_413(self):
        raw = json.dumps(heartbeat()).encode()
        body = raw + b" " * (fs.MAX_BODY - len(raw) + 1)
        self.assertEqual(len(body), 256 * 1024 + 1)
        r = self.request("POST", "/api/v1/heartbeat", body, self.auth())
        self.assertEqual(r.status, 413)
        self.assertEqual(r.json["ok"], False)
        self.assertEqual(self.count("devices"), 0)

    def test_body_of_exactly_256kb_is_accepted(self):
        raw = json.dumps(heartbeat()).encode()
        body = raw + b" " * (fs.MAX_BODY - len(raw))
        self.assertEqual(len(body), 256 * 1024)
        self.assertEqual(self.request("POST", "/api/v1/heartbeat", body, self.auth()).status, 200)

    def test_invalid_json_is_400(self):
        for body in (b"{not json", b"", b"\xff\xfe\x00", b'{"a": 1'):
            with self.subTest(body=body):
                r = self.request("POST", "/api/v1/heartbeat", body, self.auth())
                self.assertEqual(r.status, 400)
                self.assertEqual(r.json, {"ok": False, "error": "invalid JSON"})

    def test_json_that_is_not_an_object_is_400(self):
        r = self.request("POST", "/api/v1/heartbeat", b"[1, 2]", self.auth())
        self.assertEqual(r.status, 400)
        self.assertIs(r.json["ok"], False)

    def test_each_missing_required_field_is_400(self):
        for field in ("schema", "device_id", "boot_id", "seq", "sent_at", "interval_s", "identity", "system"):
            for how in ("absent", "null"):
                with self.subTest(field=field, how=how):
                    hb = heartbeat()
                    if how == "absent":
                        del hb[field]
                    else:
                        hb[field] = None
                    r = self.post(hb)
                    self.assertEqual(r.status, 400)
                    self.assertIs(r.json["ok"], False)
                    self.assertIn(field, r.json["error"])
        self.assertEqual(self.count("devices"), 0)
        self.assertEqual(self.count("metrics"), 0)

    def test_malformed_required_fields_are_400(self):
        for field, value in {"sent_at": "yesterday", "interval_s": "60", "seq": "42", "identity": [],
                             "system": "busy", "device_id": "", "boot_id": 5, "schema": 1}.items():
            with self.subTest(field=field):
                r = self.post(heartbeat(**{field: value}))
                self.assertEqual(r.status, 400)
                self.assertIn(field, r.json["error"])
        self.assertEqual(self.count("devices"), 0)

    def test_optional_sections_may_be_null(self):
        hb = heartbeat()
        for key in ("claimed", "gpu", "thermal_c", "power", "network", "services", "sync", "software", "ai",
                    "collector_errors"):
            hb[key] = None
        hb["system"] = {k: None for k in hb["system"]}
        self.send(hb)
        m = self.q("SELECT * FROM metrics")[0]
        self.assertIsNone(m["cpu_pct"])
        self.assertIsNone(m["max_temp_c"])
        self.assertEqual(self.state(), ("online", []))

    def test_nan_and_infinity_are_stored_as_null(self):
        raw = json.dumps(heartbeat())
        raw = raw.replace('"cpu_pct": 5.3', '"cpu_pct": NaN').replace('"total_w": 26.47', '"total_w": Infinity')
        r = self.request("POST", "/api/v1/heartbeat", raw.encode(), self.auth())
        self.assertEqual(r.status, 200)
        m = self.q("SELECT cpu_pct, power_total_w FROM metrics")[0]
        self.assertEqual(m, {"cpu_pct": None, "power_total_w": None})
        self.assertEqual(self.get_json(f"/api/v1/devices/{DEVICE}")["latest"]["system"]["cpu_pct"], None)

    def post_chunked(self, chunks):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request("POST", "/api/v1/heartbeat", body=iter(chunks),
                         headers={**self.auth(), "Transfer-Encoding": "chunked"}, encode_chunked=True)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def test_chunked_body_is_accepted_within_the_limit(self):
        raw = json.dumps(heartbeat()).encode()
        status, body = self.post_chunked([raw[:100], raw[100:1000], raw[1000:]])
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertEqual(self.count("devices"), 1)
        status, body = self.post_chunked([b" " * 65536] * 4 + [b"{}"])  # 256 KB + 2 bytes
        self.assertEqual((status, body["ok"]), (413, False))

    def test_chunked_trailer_is_limited(self):
        """A client cannot keep a handler reading trailer lines for ever, with or without a token.
        No empty line ends the trailer here, so only the server's limit can end the read."""
        for token, expected in ((FLEET_TOKEN, b" 400 "), (None, b" 401 ")):
            with self.subTest(token=token), socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
                auth = f"Authorization: Bearer {token}\r\n" if token else ""
                s.sendall((f"POST /api/v1/heartbeat HTTP/1.1\r\nHost: 127.0.0.1\r\n{auth}"
                           "Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n").encode() + b"X-Pad: x\r\n" * 40)
                self.assertIn(expected, s.makefile("rb").readline())

    def test_lone_surrogates_are_400_in_device_id_and_replaced_elsewhere(self):
        """json.loads accepts an escaped lone surrogate, but SQLite cannot store one."""
        r = self.request("POST", "/api/v1/heartbeat", json.dumps(heartbeat("box-\udce9")).encode(), self.auth())
        self.assertEqual((r.status, r.json["ok"]), (400, False))
        hb = heartbeat(identity__hostname="host-\udce9",
                       system__disks=[{"mount": "/data-\udce9", "total_gb": 10.0, "used_gb": 9.6, "pct": 96.0}])
        with_service(hb, "postgres", detail="bad \udce9 byte")
        hb["services"].append({"name": "svc-\udce9", "kind": "systemd", "status": "down"})
        r = self.request("POST", "/api/v1/heartbeat", json.dumps(hb).encode(), self.auth())
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(self.q("SELECT hostname FROM devices")[0]["hostname"], "host-?")
        self.assertEqual(self.q("SELECT detail FROM service_checks WHERE service = 'postgres'")[0]["detail"],
                         "bad ? byte")
        self.assertEqual(self.open_codes(), ["DISK_CRITICAL", "DISK_LOW", "SERVICE_DOWN:svc-?"])
        self.assertIn("/data-?", self.events_of("DISK_CRITICAL")[0]["message"])

    def test_control_characters_cannot_forge_log_lines(self):
        forged = "app\n2026-09-29 18:00:00,000 WARNING heartbeat rejected (401: bad or missing token) from 203.0.113.66"
        hb = heartbeat(identity__hostname=forged, identity__device_type="AGX\rThor")
        hb["services"].append({"name": forged, "kind": "systemd", "status": "down", "detail": "one\ntwo"})
        with self.assertLogs("fleet", level="INFO") as logs:
            self.send(hb)
        self.assertTrue(any("device registered" in line for line in logs.output))
        self.assertTrue(any("alert raised" in line for line in logs.output))
        for line in logs.output:
            self.assertNotIn("\n", line)
            self.assertNotIn("\r", line)
        one_line = forged.replace("\n", " ")
        self.assertEqual(self.q("SELECT hostname, device_type FROM devices")[0],
                         {"hostname": one_line, "device_type": "AGX Thor"})
        self.assertIn("SERVICE_DOWN:" + one_line, self.open_codes())
        self.assertEqual(self.q("SELECT detail FROM service_checks WHERE service = ?", one_line)[0]["detail"],
                         "one two")


# =========================================================================== section 5

class RegistrationTest(ServerTestCase):
    def test_first_heartbeat_registers_the_device(self):
        self.send(heartbeat())
        devices = self.q("SELECT * FROM devices")
        self.assertEqual(len(devices), 1)
        d = devices[0]
        self.assertEqual(set(d), DEVICE_COLUMNS)
        self.assertEqual((d["device_id"], d["serial"], d["model"], d["device_type"], d["hostname"]),
                         (DEVICE, "1424225065010", "NVIDIA Jetson AGX Thor Developer Kit", "AGX Thor", "sarasschoolai"))
        self.assertEqual((d["product"], d["site_code"], d["customer"]), ("SchoolAI in a Box", "eps_koorgalli", None))
        self.assertRegex(d["first_seen"], TS_RE)
        registered = self.events_of("DEVICE_REGISTERED")
        self.assertEqual(len(registered), 1)
        self.assertEqual((registered[0]["kind"], registered[0]["severity"]), ("info", "info"))
        self.assertIsNone(registered[0]["resolved_at"])
        self.send(heartbeat())
        self.assertEqual(len(self.events_of("DEVICE_REGISTERED")), 1)
        self.assertEqual(self.count("devices"), 1)

    def test_claimed_values_only_fill_empty_registry_fields(self):
        self.send(heartbeat())
        self.assertEqual(self.patch({"site_code": "blr-02"}).status, 200)
        self.send(heartbeat(claimed__customer="Excelsoft", claimed__site_code="eps_koorgalli",
                            claimed__product="Other product"))
        d = self.q("SELECT product, site_code, customer FROM devices")[0]
        self.assertEqual(d, {"product": "SchoolAI in a Box", "site_code": "blr-02", "customer": "Excelsoft"})
        self.send(heartbeat(claimed__customer="Someone else"))
        self.assertEqual(self.q("SELECT customer FROM devices")[0]["customer"], "Excelsoft")
        # An admin can empty a field; the device's claim then fills it again.
        self.assertEqual(self.patch({"customer": None}).status, 200)
        self.send(heartbeat(claimed__customer="Claimed again"))
        self.assertEqual(self.q("SELECT customer FROM devices")[0]["customer"], "Claimed again")

    def test_identity_follows_the_device(self):
        self.send(heartbeat())
        self.send(heartbeat(identity__hostname="renamed-box"))
        self.assertEqual(self.q("SELECT hostname FROM devices")[0]["hostname"], "renamed-box")
        self.send(heartbeat(identity__hostname=None))  # unreadable -> keep what we know
        self.assertEqual(self.q("SELECT hostname FROM devices")[0]["hostname"], "renamed-box")


class MetricsRowTest(ServerTestCase):
    def test_metrics_row_values(self):
        hb = heartbeat()
        hb["system"]["disks"] = [{"mount": "/data", "total_gb": 100.0, "used_gb": 70.0, "pct": 70.0},
                                 {"mount": "/", "total_gb": 936.0, "used_gb": 49.1, "pct": 5.5}]
        self.send(hb)
        rows = self.q("SELECT * FROM metrics")
        self.assertEqual(len(rows), 1)
        m = rows[0]
        self.assertEqual(m["cpu_temp_c"], 40.4)      # thermal_c.cpu
        self.assertEqual(m["max_temp_c"], 40.9)      # hottest thermal zone
        self.assertEqual(m["disk_pct"], 5.5)         # the "/" entry, not /data
        self.assertEqual(m["disk_used_gb"], 49.1)
        self.assertEqual(m["server_rtt_ms"], 12.3)   # network.last_server_rtt_ms
        expected = {"buffered": 0, "uptime_s": 12345, "cpu_pct": 5.3, "cpu_freq_mhz": 2601, "load_1m": 0.8,
                    "gpu_pct": 0, "gpu_temp_c": 41, "gpu_clock_mhz": 315, "gpu_power_w": 2.77, "ram_pct": 7.7,
                    "ram_used_mb": 9700, "power_total_w": 26.47, "fan_rpm": 1833, "wifi_signal_dbm": -66,
                    "gateway_rtt_ms": 5.5, "gateway_loss_pct": 0.0}
        self.assertEqual({k: m[k] for k in expected}, expected)
        extra = json.loads(m["extra"])
        self.assertEqual(set(extra), {"cpu_per_core_pct", "thermal_c", "rails_w", "disks", "boot_id", "seq",
                                      "sent_at"})
        self.assertEqual((extra["boot_id"], extra["seq"], extra["sent_at"]),
                         (hb["boot_id"], hb["seq"], hb["sent_at"]))
        self.assertEqual(extra["cpu_per_core_pct"], [12.9, 1.0, 11.0])
        self.assertEqual(extra["thermal_c"], hb["thermal_c"])
        self.assertEqual(extra["rails_w"], hb["power"]["rails_w"])
        self.assertEqual([d["mount"] for d in extra["disks"]], ["/data", "/"])
        self.assertRegex(m["ts"], TS_RE)
        self.assertEqual(m["ts"], self.status_row()["last_seen"])

    def test_gpu_temperature_falls_back_to_thermal_zone(self):
        self.send(heartbeat(gpu__temp_c=None))
        self.assertEqual(self.q("SELECT gpu_temp_c FROM metrics")[0]["gpu_temp_c"], 40.9)

    def test_service_checks_one_row_per_service(self):
        self.send(heartbeat())
        rows = self.q("SELECT * FROM service_checks ORDER BY service")
        self.assertEqual([r["service"] for r in rows],
                         sorted(s["name"] for s in EXAMPLE_HEARTBEAT["services"]))
        by_name = {r["service"]: r for r in rows}
        self.assertEqual(by_name["postgres"]["status"], "up")
        self.assertEqual(by_name["postgres"]["kind"], "tcp")
        self.assertEqual(by_name["postgres"]["latency_ms"], 0.8)
        self.assertEqual(by_name["postgres"]["detail"], "ssl=off")
        self.assertEqual(by_name["vllm"]["status"], "not_configured")
        self.assertEqual(by_name["systemd:nginx.service"]["restarts"], 0)
        self.assertEqual({r["ts"] for r in rows}, {self.status_row()["last_seen"]})

    def test_ai_usage_row(self):
        self.send(heartbeat())
        ai = self.q("SELECT * FROM ai_usage")
        self.assertEqual(len(ai), 1)
        self.assertEqual((ai[0]["status"], ai[0]["model"], ai[0]["kv_cache_pct"], ai[0]["avg_ttft_s"]),
                         ("up", "Qwen/Qwen3.5-35B", 23.5, 0.35))
        self.send(heartbeat(ai=None))
        self.assertEqual(self.count("ai_usage"), 1)

    def test_device_strings_are_clamped_to_200(self):
        hb = heartbeat(identity__hostname="h" * 500, agent_version="v" * 300)
        with_service(hb, "postgres", detail="d" * 500)
        hb["services"].append({"name": "n" * 300, "kind": "http", "status": "down"})
        self.send(hb)
        self.assertEqual(len(self.q("SELECT hostname FROM devices")[0]["hostname"]), 200)
        self.assertEqual(len(self.q("SELECT detail FROM service_checks WHERE service = 'postgres'")[0]["detail"]), 200)
        self.assertEqual(self.count("service_checks", "service = ?", "n" * 200), 1)
        self.assertEqual(len(self.status_row()["agent_version"]), 200)
        latest = self.get_json(f"/api/v1/devices/{DEVICE}")["latest"]
        self.assertEqual(len(latest["identity"]["hostname"]), 200)
        self.assertIn("SERVICE_DOWN:" + "n" * 200, self.open_codes())

    def test_stored_heartbeat_is_at_most_64kb(self):
        hb = heartbeat()
        hb["services"] = [{"name": f"container:c{i:04d}", "kind": "container", "status": "up", "latency_ms": None,
                           "detail": "x" * 150, "restarts": 0, "since": None} for i in range(900)]
        body = json.dumps(hb).encode()
        self.assertLess(len(body), fs.MAX_BODY)
        self.assertGreater(len(body), 64 * 1024)
        self.send(hb)
        stored = self.status_row()["last_heartbeat"]
        self.assertLessEqual(len(stored.encode()), 64 * 1024)
        doc = json.loads(stored)
        self.assertIs(doc["_truncated"], True)
        self.assertEqual(doc["device_id"], DEVICE)
        self.assertEqual(self.count("service_checks"), fs.MAX_ITEMS)


class BufferedTest(ServerTestCase):
    def test_buffered_ts_uses_clock_offset_and_changes_no_status_or_alerts(self):
        self.send(heartbeat(sent_ms=fs.now_ms() - 300_000))  # device clock 300 s behind the server
        status_before = self.status_row()
        offset = status_before["clock_offset_s"]
        self.assertAlmostEqual(offset, 300.0, delta=5)
        before = {t: self.q(f"SELECT * FROM {t}") for t in ("events", "software_versions", "sync_reports", "devices")}

        sent_ms = fs.now_ms() - 3_600_000 - 300_000  # an hour ago, on the device clock
        hb = heartbeat(sent_ms=sent_ms, buffered=True, boot_id="another-boot", gpu__temp_c=110,
                       system__ntp_synced=False, system__ram_pct=99, sync__age_h=99.0,
                       sync__last_attempt="2026-09-29T11:00:00+05:30", software__app_version="9.9",
                       identity__hostname="other-name", claimed__customer="Someone")
        with_service(hb, "postgres", status="down")
        with_service(hb, "systemd:nginx.service", restarts=7)
        hb["system"]["disks"][0]["pct"] = 99.0
        self.send(hb)

        expected_ts = fs.fmt_ms(sent_ms + round(offset * 1000))
        rows = self.q("SELECT * FROM metrics WHERE buffered = 1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ts"], expected_ts)
        self.assertEqual(rows[0]["gpu_temp_c"], 110)
        checks = self.q("SELECT service, status FROM service_checks WHERE ts = ?", expected_ts)
        self.assertEqual(len(checks), len(hb["services"]))
        self.assertIn({"service": "postgres", "status": "down"}, checks)
        self.assertEqual(self.count("ai_usage", "ts = ?", expected_ts), 1)
        # Nothing else moved: no status overwrite, no alert, no version or sync row. The only new
        # event is the informational note that heartbeats arrived late.
        self.assertEqual(self.status_row(), status_before)
        new_events = [e for e in self.q("SELECT * FROM events") if e not in before["events"]]
        self.assertEqual([(e["kind"], e["code"], e["ts"]) for e in new_events],
                         [("info", "CONNECTION_INTERRUPTED", expected_ts)])
        for table, rows_before in before.items():
            if table != "events":
                self.assertEqual(self.q(f"SELECT * FROM {table}"), rows_before, table)

    def test_buffered_replays_are_idempotent(self):
        self.send(heartbeat())
        replay = heartbeat(sent_ms=fs.now_ms() - 600_000, buffered=True)
        for _ in range(3):
            self.send(replay)
        self.assertEqual(self.count("metrics", "buffered = 1"), 1)
        ts = self.q("SELECT ts FROM metrics WHERE buffered = 1")[0]["ts"]
        self.assertEqual(self.count("service_checks", "ts = ?", ts), len(replay["services"]))
        self.assertEqual(self.count("ai_usage", "ts = ?", ts), 1)
        self.assertEqual(self.count("metrics"), 2)

    def test_a_heartbeat_delivered_twice_is_stored_once(self):
        """The agent resends a heartbeat whose 200 it never saw. A replay is stamped with the box's
        latest clock offset, which moves by a few ms with every live heartbeat, so the second copy
        would land at a new ts: it is recognised by (boot_id, seq, sent_at) instead."""
        now = fs.now_ms()
        self.app.ingest(heartbeat(sent_ms=now - 12), recv_ms=now)  # clock offset 0.012 s
        replay = heartbeat(sent_ms=now - 600_000, buffered=True)
        self.app.ingest(replay, recv_ms=now + 50)
        self.app.ingest(heartbeat(sent_ms=now + 60_000 - 15), recv_ms=now + 60_000)  # offset now 0.015 s
        self.app.ingest(replay, recv_ms=now + 60_050)  # the same heartbeat again
        self.assertEqual(self.count("metrics", "buffered = 1"), 1)
        self.assertEqual(self.count("metrics"), 3)
        self.assertEqual(self.count("service_checks"), 3 * len(replay["services"]))
        self.assertEqual(self.count("ai_usage"), 3)
        self.assertEqual(json.loads(self.events_of("CONNECTION_INTERRUPTED")[0]["data"])["count"], 1)

    def test_a_live_heartbeat_replayed_after_a_lost_answer_is_stored_once(self):
        now = fs.now_ms()
        live = heartbeat(sent_ms=now - 10)
        self.app.ingest(live, recv_ms=now)  # stored, but say the 200 never reached the agent
        self.app.ingest({**live, "buffered": True}, recv_ms=now + 60_000)  # so it is buffered and replayed
        self.assertEqual(self.count("metrics"), 1)
        self.assertEqual(self.count("metrics", "buffered = 1"), 0)
        self.assertEqual(self.events_of("CONNECTION_INTERRUPTED"), [])

    def test_same_seq_after_an_agent_restart_is_a_new_heartbeat(self):
        """seq restarts at 1 when the agent restarts within the same boot; sent_at tells them apart."""
        now = fs.now_ms()
        self.app.ingest(heartbeat(sent_ms=now - 60_000, seq=1), recv_ms=now - 60_000)
        self.app.ingest(heartbeat(sent_ms=now, seq=1), recv_ms=now)
        self.assertEqual(self.count("metrics"), 2)

    def test_late_heartbeats_leave_one_connection_interrupted_event_per_burst(self):
        now = fs.now_ms()
        self.app.ingest(heartbeat(sent_ms=now, interval_s=10), recv_ms=now)  # offline threshold 30 s
        # A 40 s drop: after the next live heartbeat the agent replays the three it kept.
        self.app.ingest(heartbeat(sent_ms=now + 40_000, interval_s=10), recv_ms=now + 40_000)
        for i in range(3):
            self.app.ingest(heartbeat(sent_ms=now + 10_000 * (i + 1), interval_s=10, buffered=True),
                            recv_ms=now + 40_050 + i)
        events = self.events_of("CONNECTION_INTERRUPTED")
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual((e["kind"], e["severity"], e["resolved_at"]), ("info", "info", None))
        self.assertEqual(e["ts"], fs.fmt_ms(now + 10_000))  # when the first missed reading was taken
        self.assertEqual(e["message"], "3 heartbeats arrived late from the box's offline buffer, spanning 20 s")
        data = json.loads(e["data"])
        self.assertEqual((data["count"], data["first_ts"], data["last_ts"]),
                         (3, fs.fmt_ms(now + 10_000), fs.fmt_ms(now + 30_000)))
        # A later drop is a new event; readings are unaffected and nothing alerts.
        self.app.ingest(heartbeat(sent_ms=now + 600_000, interval_s=10), recv_ms=now + 600_000)
        self.app.ingest(heartbeat(sent_ms=now + 590_000, interval_s=10, buffered=True), recv_ms=now + 600_050)
        self.assertEqual([json.loads(e["data"])["count"] for e in self.events_of("CONNECTION_INTERRUPTED")], [3, 1])
        self.assertEqual(self.state(), ("online", []))
        points = self.app.device_metrics(DEVICE, 60, now=now + 600_100)["points"]
        self.assertEqual([p["buffered"] for p in points], [0, 1, 1, 1, 0, 1, 0])

    def test_buffered_heartbeat_from_unknown_device_falls_back_to_sent_at(self):
        sent_ms = fs.now_ms() - 120_000
        self.send(heartbeat("never-live", sent_ms=sent_ms, buffered=True))
        self.assertEqual(len(self.events_of("DEVICE_REGISTERED", "never-live")), 1)
        self.assertEqual(self.events_of("CONNECTION_INTERRUPTED", "never-live"), [])  # no baseline to compare
        self.assertIsNone(self.status_row("never-live"))
        self.assertEqual(self.q("SELECT ts FROM metrics WHERE device_id = 'never-live'")[0]["ts"],
                         fs.fmt_ms(sent_ms))
        d = self.get_json("/api/v1/fleet")["devices"][0]
        self.assertEqual((d["device_id"], d["state"], d["last_seen"]), ("never-live", "offline", None))


# =========================================================================== section 6

class AlertRulesTest(ServerTestCase):
    def alert(self, code):
        rows = self.events_of(code)
        self.assertEqual(len(rows), 1, f"expected exactly one {code} event, got {rows}")
        return rows[0]

    def assertOpen(self, *codes):
        for code in codes:
            self.assertIn(code, self.open_codes())

    def assertClosed(self, *codes):
        for code in codes:
            self.assertNotIn(code, self.open_codes())

    def test_healthy_heartbeat_is_online(self):
        self.send(heartbeat())
        self.assertEqual(self.state(), ("online", []))
        self.assertEqual(self.open_codes(), [])

    def test_contract_example_raises_clock_unsynced_and_sync_stale(self):
        self.send(heartbeat(healthy=False))
        self.assertEqual(self.state(), ("critical", ["CLOCK_UNSYNCED", "SYNC_STALE"]))
        self.assertEqual(self.alert("SYNC_STALE")["severity"], "critical")
        self.assertEqual(self.alert("CLOCK_UNSYNCED")["severity"], "warning")

    def test_gpu_overheat_raise_and_clear(self):
        self.send(heartbeat(gpu__temp_c=106))
        self.assertOpen("GPU_OVERHEAT", "GPU_TEMP_HIGH")
        a = self.alert("GPU_OVERHEAT")
        self.assertEqual((a["kind"], a["severity"]), ("alert", "critical"))
        self.assertEqual(json.loads(a["data"])["gpu_temp_c"], 106)
        self.assertEqual(self.state()[0], "critical")
        self.send(heartbeat(gpu__temp_c=101, thermal_c__gpu=101))  # hysteresis: not yet < 100
        self.assertOpen("GPU_OVERHEAT")
        self.assertOpen("SOC_TEMP_HIGH")  # the gpu zone is now also the hottest thermal zone
        self.send(heartbeat(gpu__temp_c=99, thermal_c__gpu=99))
        self.assertClosed("GPU_OVERHEAT")
        self.assertRegex(self.alert("GPU_OVERHEAT")["resolved_at"], TS_RE)
        self.assertEqual(self.state(), ("warning", ["GPU_TEMP_HIGH", "SOC_TEMP_HIGH"]))  # 99 is still >= 90
        self.send(heartbeat(gpu__temp_c=41))
        self.assertEqual(self.state(), ("online", []))

    def test_gpu_overheat_from_the_thermal_zone(self):
        self.send(heartbeat(gpu__temp_c=41, thermal_c__gpu=105))
        self.assertOpen("GPU_OVERHEAT")

    def test_gpu_temp_high_raise_and_clear(self):
        self.send(heartbeat(gpu__temp_c=96))
        self.assertEqual(self.open_codes(), ["GPU_TEMP_HIGH"])
        self.assertEqual(self.alert("GPU_TEMP_HIGH")["severity"], "warning")
        self.assertEqual(self.state(), ("warning", ["GPU_TEMP_HIGH"]))
        self.send(heartbeat(gpu__temp_c=92))
        self.assertOpen("GPU_TEMP_HIGH")
        self.send(heartbeat(gpu__temp_c=89.9))
        self.assertEqual(self.state(), ("online", []))

    def test_soc_temp_high_raise_and_clear(self):
        self.send(heartbeat(thermal_c__tj=96))
        self.assertEqual(self.open_codes(), ["SOC_TEMP_HIGH"])
        self.send(heartbeat(thermal_c__tj=91))
        self.assertOpen("SOC_TEMP_HIGH")
        self.send(heartbeat(thermal_c__tj=89))
        self.assertEqual(self.open_codes(), [])

    def test_disk_low_raise_and_clear(self):
        self.send(heartbeat(system__disks__0__pct=86))
        self.assertEqual(self.open_codes(), ["DISK_LOW"])
        self.assertEqual(self.alert("DISK_LOW")["severity"], "warning")
        self.send(heartbeat(system__disks__0__pct=84))
        self.assertOpen("DISK_LOW")
        self.send(heartbeat(system__disks__0__pct=82))
        self.assertEqual(self.open_codes(), [])

    def test_disk_low_on_any_disk(self):
        hb = heartbeat()
        hb["system"]["disks"].append({"mount": "/data", "total_gb": 100.0, "used_gb": 88.0, "pct": 88.0})
        self.send(hb)
        self.assertEqual(self.open_codes(), ["DISK_LOW"])
        self.assertEqual(json.loads(self.alert("DISK_LOW")["data"])["mount"], "/data")

    def test_disk_critical_raise_and_clear(self):
        self.send(heartbeat(system__disks__0__pct=96))
        self.assertEqual(self.open_codes(), ["DISK_CRITICAL", "DISK_LOW"])
        self.assertEqual(self.alert("DISK_CRITICAL")["severity"], "critical")
        self.assertEqual(self.state()[0], "critical")
        self.send(heartbeat(system__disks__0__pct=94))
        self.assertOpen("DISK_CRITICAL")
        self.send(heartbeat(system__disks__0__pct=92))
        self.assertEqual(self.state(), ("warning", ["DISK_LOW"]))
        self.send(heartbeat(system__disks__0__pct=80))
        self.assertEqual(self.state(), ("online", []))

    def test_ram_high_needs_three_heartbeats_in_a_row(self):
        self.send(heartbeat(system__ram_pct=91))
        self.send(heartbeat(system__ram_pct=92))
        self.assertClosed("RAM_HIGH")
        self.send(heartbeat(system__ram_pct=93))
        self.assertOpen("RAM_HIGH")
        self.assertEqual(self.alert("RAM_HIGH")["severity"], "warning")
        self.send(heartbeat(system__ram_pct=88))  # hysteresis: not yet < 85
        self.assertOpen("RAM_HIGH")
        self.send(heartbeat(system__ram_pct=84))
        self.assertClosed("RAM_HIGH")
        self.assertEqual(len(self.events_of("RAM_HIGH")), 1)

    def test_ram_high_streak_is_broken_by_a_lower_reading(self):
        for ram in (91, 91, 50, 91, 91):
            self.send(heartbeat(system__ram_pct=ram))
        self.assertClosed("RAM_HIGH")
        self.send(heartbeat(system__ram_pct=91))
        self.assertOpen("RAM_HIGH")

    def test_ram_high_counts_live_heartbeats_only(self):
        self.send(heartbeat(system__ram_pct=91))
        self.send(heartbeat(system__ram_pct=91, buffered=True, sent_ms=fs.now_ms() - 5000))
        self.send(heartbeat(system__ram_pct=91))
        self.assertClosed("RAM_HIGH")
        self.send(heartbeat(system__ram_pct=91))
        self.assertOpen("RAM_HIGH")

    def test_cpu_high_raise_and_clear(self):
        for cpu in (95, 96, 97):
            self.send(heartbeat(system__cpu_pct=cpu))
        self.assertEqual(self.open_codes(), ["CPU_HIGH"])
        self.send(heartbeat(system__cpu_pct=85))
        self.assertOpen("CPU_HIGH")
        self.send(heartbeat(system__cpu_pct=79))
        self.assertEqual(self.open_codes(), [])

    def test_service_down_raise_and_clear(self):
        self.send(with_service(heartbeat(), "postgres", status="down", detail="connection refused"))
        self.assertEqual(self.state(), ("critical", ["SERVICE_DOWN:postgres"]))
        a = self.alert("SERVICE_DOWN:postgres")
        self.assertEqual((a["kind"], a["severity"]), ("alert", "critical"))
        self.assertIn("connection refused", a["message"])
        self.assertEqual(json.loads(a["data"])["service"], "postgres")
        self.send(with_service(heartbeat(), "postgres", status="degraded"))  # only 'up' clears
        self.assertOpen("SERVICE_DOWN:postgres")
        self.send(heartbeat())
        self.assertEqual(self.open_codes(), [])
        hb = with_service(with_service(heartbeat(), "postgres", status="down"), "app-live", status="down")
        self.send(hb)
        self.assertEqual(self.state(), ("critical", ["SERVICE_DOWN:app-live", "SERVICE_DOWN:postgres"]))

    def test_service_down_clears_once_the_service_is_no_longer_monitored(self):
        self.send(with_service(heartbeat(), "vllm", status="down", detail="connection refused"))
        self.assertEqual(self.state(), ("critical", ["SERVICE_DOWN:vllm"]))
        self.send(with_service(heartbeat(), "vllm", status="unknown"))  # can't tell: no change
        self.assertOpen("SERVICE_DOWN:vllm")
        self.send(heartbeat())  # vllm_url set back to null: the example reports vllm as not_configured
        self.assertEqual(self.state(), ("online", []))
        # A container that stops being reported (removed, renamed, Docker no longer readable).
        hb = heartbeat()
        hb["services"].append({"name": "container:old", "kind": "container", "status": "down"})
        self.send(hb)
        self.assertOpen("SERVICE_DOWN:container:old")
        self.send(heartbeat(services=None))  # no service list at all: a missing reading, no change
        self.assertOpen("SERVICE_DOWN:container:old")
        self.send(heartbeat())
        self.assertEqual(self.state(), ("online", []))
        self.assertRegex(self.alert("SERVICE_DOWN:container:old")["resolved_at"], TS_RE)

    def test_gpu_high_needs_three_heartbeats_and_not_on_an_llm_server(self):
        for util in (95, 96):
            self.send(heartbeat(gpu__util_pct=util, ai=None))
        self.assertClosed("GPU_HIGH")
        self.send(heartbeat(gpu__util_pct=97, ai=None))
        self.assertEqual(self.open_codes(), ["GPU_HIGH"])
        a = self.alert("GPU_HIGH")
        self.assertEqual((a["severity"], json.loads(a["data"])), ("warning", {"gpu_pct": 97}))
        self.send(heartbeat(gpu__util_pct=85, ai=None))  # hysteresis: not yet < 80
        self.assertOpen("GPU_HIGH")
        self.send(heartbeat(gpu__util_pct=10, ai=None))
        self.assertEqual(self.open_codes(), [])
        for _ in range(3):  # a box serving an LLM (it sends an ai.vllm block) is busy by design
            self.send(heartbeat(gpu__util_pct=99))
        self.assertEqual(self.open_codes(), [])

    def test_llm_saturated_needs_three_heartbeats(self):
        self.send(heartbeat(ai__vllm__waiting=3))
        self.send(heartbeat(ai__vllm__kv_cache_pct=95))
        self.assertClosed("LLM_SATURATED")
        self.send(heartbeat(ai__vllm__waiting=2))
        self.assertEqual(self.open_codes(), ["LLM_SATURATED"])
        a = self.alert("LLM_SATURATED")
        self.assertEqual(a["severity"], "warning")
        self.assertIn("2 request(s) waiting", a["message"])
        self.send(heartbeat(ai__vllm__kv_cache_pct=88))  # nothing waiting, but not yet < 85 %
        self.assertOpen("LLM_SATURATED")
        self.send(heartbeat())  # nothing waiting, KV cache 23.5 %
        self.assertEqual(self.open_codes(), [])
        for _ in range(3):
            self.send(heartbeat(ai__vllm__waiting=1))
        self.assertOpen("LLM_SATURATED")
        self.send(heartbeat(ai=None))  # vLLM no longer configured
        self.assertEqual(self.open_codes(), [])

    def test_sync_stale_raise_and_clear(self):
        self.send(heartbeat(sync__age_h=8.5))
        self.assertEqual(self.open_codes(), ["SYNC_STALE"])
        self.assertEqual(self.alert("SYNC_STALE")["severity"], "critical")
        self.send(heartbeat(sync__age_h=None))  # age unreadable: no change
        self.assertOpen("SYNC_STALE")
        hb = heartbeat()
        del hb["sync"]  # the key missing altogether: no change either
        self.send(hb)
        self.assertOpen("SYNC_STALE")
        self.send(heartbeat(sync__age_h=8.0))
        self.assertEqual(self.open_codes(), [])

    def test_sync_stale_clears_when_the_box_has_no_sync_state(self):
        """sync is null when /opt/saras-sync/state is gone: nothing is left to be stale."""
        self.send(heartbeat(sync__age_h=20, sync__mismatched_tables=2))
        self.assertEqual(self.open_codes(), ["SYNC_MISMATCH", "SYNC_STALE"])
        self.send(heartbeat(sync=None))
        self.assertEqual(self.state(), ("online", []))

    def test_sync_mismatch_raise_and_clear(self):
        self.send(heartbeat(sync__source_tables=193, sync__source_rows=1620892, sync__target_rows=1620892,
                            sync__mismatched_tables=0))
        self.assertEqual(self.open_codes(), [])
        self.send(heartbeat(sync__source_rows=1620900, sync__target_rows=1620892, sync__mismatched_tables=1))
        self.assertEqual(self.state(), ("critical", ["SYNC_MISMATCH"]))
        a = self.alert("SYNC_MISMATCH")
        self.assertEqual((a["kind"], a["severity"]), ("alert", "critical"))
        self.assertIn("1 table(s)", a["message"])
        self.assertEqual(json.loads(a["data"])["mismatched_tables"], 1)
        self.send(heartbeat())  # counts not reported (older saras-sync): no change
        self.assertOpen("SYNC_MISMATCH")
        self.send(heartbeat(sync__mismatched_tables=0))
        self.assertEqual(self.open_codes(), [])

    def test_clock_unsynced_by_ntp(self):
        self.send(heartbeat(system__ntp_synced=False))
        self.assertEqual(self.state(), ("warning", ["CLOCK_UNSYNCED"]))
        self.send(heartbeat(system__ntp_synced=None))  # unknown: no change
        self.assertOpen("CLOCK_UNSYNCED")
        self.send(heartbeat(system__ntp_synced=True))
        self.assertEqual(self.open_codes(), [])

    def test_clock_unsynced_by_offset(self):
        self.send(heartbeat(sent_ms=fs.now_ms() - 200_000))  # synced, but 200 s off
        self.assertEqual(self.open_codes(), ["CLOCK_UNSYNCED"])
        self.assertGreater(json.loads(self.alert("CLOCK_UNSYNCED")["data"])["clock_offset_s"], 120)
        self.send(heartbeat(sent_ms=fs.now_ms() + 100_000))  # 100 s ahead: within 120 s
        self.assertEqual(self.open_codes(), [])

    def test_network_poor_raise_and_clear(self):
        self.send(heartbeat(network__gateway_loss_pct=25))
        self.assertEqual(self.open_codes(), ["NETWORK_POOR"])
        self.assertEqual(self.alert("NETWORK_POOR")["severity"], "warning")
        self.send(heartbeat(network__gateway_loss_pct=15))
        self.assertOpen("NETWORK_POOR")
        self.send(heartbeat(network__gateway_loss_pct=5, network__wifi_signal_dbm=-70))
        self.assertEqual(self.open_codes(), [])
        self.send(heartbeat(network__wifi_signal_dbm=-76))
        self.assertOpen("NETWORK_POOR")
        self.send(heartbeat(network__wifi_signal_dbm=-73))  # hysteresis: not yet > -72
        self.assertOpen("NETWORK_POOR")
        self.send(heartbeat(network__wifi_signal_dbm=-71, network__gateway_loss_pct=0))
        self.assertEqual(self.open_codes(), [])

    def test_network_poor_clears_on_wired_link(self):
        self.send(heartbeat(network__gateway_loss_pct=30))
        self.send(heartbeat(network__gateway_loss_pct=0, network__wifi_signal_dbm=None))
        self.assertEqual(self.open_codes(), [])

    def test_at_most_one_open_alert_per_code(self):
        for _ in range(3):
            self.send(heartbeat(gpu__temp_c=106))
        self.assertEqual(len(self.events_of("GPU_OVERHEAT")), 1)
        self.assertEqual(len(self.events_of("GPU_TEMP_HIGH")), 1)
        with self.assertRaises(sqlite3.IntegrityError):  # also enforced by the database
            self.execute("INSERT INTO events (device_id, ts, kind, severity, code, message) "
                         "VALUES (?, ?, 'alert', 'critical', 'GPU_OVERHEAT', 'dup')", [(DEVICE, fs.fmt_ms(fs.now_ms()))])


class EventsTest(ServerTestCase):
    def test_service_restarted_is_one_shot(self):
        name = "systemd:nginx.service"
        self.send(heartbeat())
        self.send(with_service(heartbeat(), name, restarts=2))
        rows = self.events_of("SERVICE_RESTARTED")
        self.assertEqual(len(rows), 1)
        ev = rows[0]
        self.assertEqual((ev["kind"], ev["severity"]), ("alert", "warning"))
        self.assertEqual(ev["resolved_at"], ev["ts"])
        self.assertEqual(json.loads(ev["data"]), {"service": name, "old": 0, "new": 2})
        self.assertEqual(self.state(), ("online", []))  # does not hold the device in warning
        self.send(with_service(heartbeat(), name, restarts=2))
        self.send(with_service(heartbeat(), name, restarts=0))  # counter reset (reboot): no event
        self.assertEqual(len(self.events_of("SERVICE_RESTARTED")), 1)
        self.send(with_service(heartbeat(), name, restarts=1))
        self.assertEqual(len(self.events_of("SERVICE_RESTARTED")), 2)

    def test_device_restarted_on_boot_id_change(self):
        self.send(heartbeat(boot_id="boot-a"))
        self.send(heartbeat(boot_id="boot-a"))
        self.assertEqual(self.events_of("DEVICE_RESTARTED"), [])
        self.send(heartbeat(boot_id="boot-b", system__booted_at="2026-09-29T09:00:00.000Z"))
        rows = self.events_of("DEVICE_RESTARTED")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["severity"]), ("info", "info"))
        self.assertEqual(json.loads(rows[0]["data"]),
                         {"old_boot_id": "boot-a", "new_boot_id": "boot-b", "booted_at": "2026-09-29T09:00:00.000Z"})
        self.assertEqual(self.status_row()["boot_id"], "boot-b")
        self.assertEqual(self.state(), ("online", []))

    def test_version_changed(self):
        self.send(heartbeat(software__app_version="1.0"))
        self.assertEqual(self.events_of("VERSION_CHANGED"), [])
        self.send(heartbeat(software=None))  # software is only sent every 10 min
        self.send(heartbeat(software__app_version="1.1"))
        rows = self.events_of("VERSION_CHANGED")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["severity"]), ("info", "info"))
        self.assertEqual(json.loads(rows[0]["data"]), {"component": "app_version", "old": "1.0", "new": "1.1"})
        versions = self.q("SELECT version FROM software_versions WHERE component = 'app_version' ORDER BY version")
        self.assertEqual([v["version"] for v in versions], ["1.0", "1.1"])
        self.assertEqual(self.count("software_versions", "component = 'jetpack'"), 0)  # null is not stored
        software = {s["component"]: s["version"] for s in self.get_json(f"/api/v1/devices/{DEVICE}")["software"]}
        self.assertEqual(software["app_version"], "1.1")
        self.assertEqual(self.get_json("/api/v1/fleet")["devices"][0]["versions"]["app_version"], "1.1")
        self.send(heartbeat(software__app_version="1.0"))  # rollback is a change too
        self.assertEqual(json.loads(self.events_of("VERSION_CHANGED")[-1]["data"]),
                         {"component": "app_version", "old": "1.1", "new": "1.0"})

    def test_state_derivation(self):
        self.send(heartbeat())
        self.assertEqual(self.state(), ("online", []))
        self.send(heartbeat(system__ntp_synced=False))
        self.assertEqual(self.state(), ("warning", ["CLOCK_UNSYNCED"]))
        self.send(heartbeat(system__ntp_synced=False, sync__age_h=20))
        self.assertEqual(self.state(), ("critical", ["CLOCK_UNSYNCED", "SYNC_STALE"]))
        last = fs.parse_ts(self.status_row()["last_seen"])
        self.assertEqual(self.app.check_offline(now=last + 60_000), [DEVICE])
        self.assertEqual(self.state(), ("offline", ["CLOCK_UNSYNCED", "DEVICE_OFFLINE", "SYNC_STALE"]))
        self.send(heartbeat())
        self.assertEqual(self.state(), ("online", []))

    def test_offline_threshold_is_max_of_missed_intervals_and_minimum(self):
        self.send(heartbeat())  # interval_s 1 -> threshold max(3 x 1, 2) = 3 s
        last = fs.parse_ts(self.status_row()["last_seen"])
        self.assertEqual(self.app.check_offline(now=last + 2900), [])
        self.assertEqual(self.app.check_offline(now=last + 3100), [DEVICE])
        self.send(heartbeat("slow-box", interval_s=10))  # threshold max(3 x 10, 2) = 30 s
        last = fs.parse_ts(self.status_row("slow-box")["last_seen"])
        self.assertEqual(self.app.check_offline(now=last + 29_000), [])
        self.assertEqual(self.app.check_offline(now=last + 31_000), ["slow-box"])
        self.assertEqual(self.app.check_offline(now=last + 60_000), [])  # already offline: no second alert
        self.assertEqual(len(self.events_of("DEVICE_OFFLINE", "slow-box")), 1)

    def test_offline_grace_after_server_start(self):
        self.send(heartbeat())
        last = fs.parse_ts(self.status_row()["last_seen"])
        self.app.started_ms = last + 10_000  # as if the server restarted 10 s after the last heartbeat
        self.assertEqual(self.app.check_offline(now=self.app.started_ms + 2000), [])
        self.assertEqual(self.app.check_offline(now=self.app.started_ms + 3100), [DEVICE])


class ClockStepTest(ServerTestCase):
    """The server's wall clock can be stepped, e.g. when NTP is turned on for the pilot box."""
    config = {"offline_min_s": 90}

    def test_backward_step_keeps_the_latest_readings_and_services_current(self):
        t = fs.now_ms()
        for i in range(3):
            self.app.ingest(heartbeat(interval_s=60, sent_ms=t + i * 60_000, system__cpu_pct=10.0),
                            recv_ms=t + i * 60_000)
        stepped = t + 3 * 60_000 - 600_000  # the next heartbeat arrives after a 10-minute step back
        hb = with_service(heartbeat(interval_s=60, sent_ms=stepped, system__cpu_pct=77.0), "postgres",
                          status="down", detail="connection refused")
        self.app.ingest(hb, recv_ms=stepped)
        now = stepped + 1000
        self.assertEqual(self.app.fleet(now=now)["devices"][0]["latest"]["cpu_pct"], 77.0)
        detail = self.app.device(DEVICE, now=now)
        self.assertEqual(detail["status"]["state"], "critical")
        pg = next(s for s in detail["services"] if s["service"] == "postgres")
        self.assertEqual((pg["status"], pg["detail"], pg["ts"]),
                         ("down", "connection refused", detail["status"]["last_seen"]))

    def test_forward_step_does_not_flag_healthy_boxes_offline(self):
        t = fs.now_ms()
        for device in ("box-a", "box-b"):
            self.app.ingest(heartbeat(device, interval_s=60), recv_ms=t)
        self.app.started_ms = t - 3_600_000  # the server has been up for an hour
        with unittest.mock.patch.object(fs, "now_ms", return_value=t + 320_000):  # wall clock +5 min
            self.assertEqual(self.app.check_offline(), [])
        self.app._heard["box-a"] -= 200  # box-a really has been silent for 200 s (threshold 180 s)
        self.assertEqual(self.app.check_offline(), ["box-a"])
        self.assertGreaterEqual(json.loads(self.events_of("DEVICE_OFFLINE", "box-a")[0]["data"])["silent_s"], 200)

    def test_backward_step_does_not_delay_the_offline_alert(self):
        t = fs.now_ms()
        self.app.ingest(heartbeat(interval_s=60), recv_ms=t)
        self.app._heard[DEVICE] -= 200  # silent for 200 s by the monotonic clock
        with unittest.mock.patch.object(fs, "now_ms", return_value=t - 3_600_000):  # wall clock -1 h
            self.assertEqual(self.app.check_offline(), [DEVICE])


class OfflineThreadTest(ServerTestCase):
    offline_check_s = 0.2

    def test_device_offline_then_reconnected(self):
        self.send(heartbeat())  # interval_s 1, offline_min_s 2 -> offline after 3 s of silence
        deadline = time.monotonic() + 15
        while "DEVICE_OFFLINE" not in self.open_codes() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.open_codes(), ["DEVICE_OFFLINE"])
        alert = self.events_of("DEVICE_OFFLINE")[0]
        self.assertEqual((alert["kind"], alert["severity"]), ("alert", "critical"))
        silent_ms = fs.parse_ts(alert["ts"]) - fs.parse_ts(self.status_row()["last_seen"])
        self.assertGreater(silent_ms, 3000)
        self.assertEqual(self.state(), ("offline", ["DEVICE_OFFLINE"]))
        fleet = self.get_json("/api/v1/fleet")
        self.assertEqual(fleet["counts"], {"total": 1, "online": 0, "warning": 0, "critical": 0, "offline": 1})
        self.assertGreaterEqual(fleet["devices"][0]["seconds_since_seen"], 3)

        self.send(heartbeat(buffered=True, sent_ms=fs.now_ms() - 2000))  # a replay does not bring it back
        self.assertEqual(self.state()[0], "offline")

        self.send(heartbeat())
        self.assertEqual(self.state(), ("online", []))
        self.assertRegex(self.events_of("DEVICE_OFFLINE")[0]["resolved_at"], TS_RE)
        reconnected = self.events_of("DEVICE_RECONNECTED")
        self.assertEqual(len(reconnected), 1)
        self.assertEqual((reconnected[0]["kind"], reconnected[0]["severity"]), ("info", "info"))
        self.assertEqual(json.loads(reconnected[0]["data"])["offline_since"], alert["ts"])


# =========================================================================== section 7

class ReadApiTest(ServerTestCase):
    def test_fleet_shape(self):
        self.send(heartbeat())
        fleet = self.get_json("/api/v1/fleet")
        self.assertEqual(set(fleet), {"generated_at", "counts", "devices"})
        self.assertRegex(fleet["generated_at"], TS_RE)
        self.assertEqual(fleet["counts"], {"total": 1, "online": 1, "warning": 0, "critical": 0, "offline": 0})
        d = fleet["devices"][0]
        self.assertEqual(set(d), {"device_id", "device_type", "model", "hostname", "product", "customer",
                                  "site_code", "location", "device_group", "state", "reasons", "last_seen",
                                  "seconds_since_seen", "uptime_s", "agent_version", "latest", "versions"})
        self.assertEqual((d["device_id"], d["device_type"], d["product"], d["site_code"], d["state"], d["reasons"]),
                         (DEVICE, "AGX Thor", "SchoolAI in a Box", "eps_koorgalli", "online", []))
        self.assertRegex(d["last_seen"], TS_RE)
        self.assertIsInstance(d["seconds_since_seen"], int)
        self.assertLessEqual(d["seconds_since_seen"], 5)
        self.assertEqual((d["uptime_s"], d["agent_version"]), (12345, "0.1.0"))
        self.assertEqual(d["latest"], {"cpu_pct": 5.3, "gpu_pct": 0, "gpu_temp_c": 41, "max_temp_c": 40.9,
                                       "ram_pct": 7.7, "disk_pct": 5.5, "power_total_w": 26.47})
        self.assertEqual(d["versions"], {"os": "Ubuntu 24.04.4 LTS", "l4t": "R39.2.1", "app_version": None})

    def test_fleet_counts_by_state(self):
        self.send(heartbeat("a-ok"))
        self.send(heartbeat("b-warn", system__ntp_synced=False))
        self.send(heartbeat("c-crit", sync__age_h=50))
        self.assertEqual(self.get_json("/api/v1/fleet")["counts"],
                         {"total": 3, "online": 1, "warning": 1, "critical": 1, "offline": 0})
        self.assertEqual([d["device_id"] for d in self.get_json("/api/v1/fleet")["devices"]],
                         ["a-ok", "b-warn", "c-crit"])

    def test_device_shape(self):
        for status in ("up", "down", "up"):
            self.send(with_service(heartbeat(), "postgres", status=status))
        last = heartbeat(system__ntp_synced=False)
        self.send(last)
        body = self.get_json(f"/api/v1/devices/{DEVICE}")
        self.assertEqual(set(body), {"device", "status", "latest", "services", "software", "sync", "ai",
                                     "open_alerts", "recent_events"})
        self.assertEqual(set(body["device"]), DEVICE_COLUMNS)
        self.assertEqual(set(body["status"]), {"state", "reasons", "last_seen", "seconds_since_seen", "boot_id",
                                               "booted_at", "uptime_s", "interval_s", "clock_offset_s",
                                               "agent_version", "lan_ip"})
        st = body["status"]
        self.assertEqual((st["state"], st["reasons"], st["boot_id"], st["booted_at"], st["uptime_s"],
                          st["interval_s"], st["agent_version"], st["lan_ip"]),
                         ("warning", ["CLOCK_UNSYNCED"], last["boot_id"], "2026-09-29T04:38:36.000Z", 12345, 1,
                          "0.1.0", "10.10.7.125"))
        self.assertIsInstance(st["clock_offset_s"], float)
        self.assertEqual(body["latest"], last)  # the last live heartbeat as received

        services = body["services"]
        self.assertEqual([s["service"] for s in services], sorted(s["name"] for s in last["services"]))
        for s in services:
            self.assertEqual(set(s), {"service", "kind", "status", "latency_ms", "restarts", "detail", "ts",
                                      "availability_24h_pct", "failed_checks_24h"})
        by_name = {s["service"]: s for s in services}
        self.assertEqual(by_name["postgres"]["availability_24h_pct"], 75.0)  # up, down, up, up
        self.assertEqual(by_name["postgres"]["failed_checks_24h"], 1)
        self.assertEqual(by_name["app-live"]["availability_24h_pct"], 100.0)
        self.assertEqual(by_name["app-live"]["failed_checks_24h"], 0)
        self.assertIsNone(by_name["vllm"]["availability_24h_pct"])  # not_configured is not counted
        self.assertEqual(by_name["vllm"]["failed_checks_24h"], 0)
        self.assertEqual(by_name["postgres"]["status"], "up")

        software = {s["component"]: s for s in body["software"]}
        self.assertEqual(software["os"]["version"], "Ubuntu 24.04.4 LTS")
        self.assertEqual(set(software["os"]), {"component", "version", "first_seen", "last_seen"})
        self.assertEqual(body["sync"]["last_success"], "2026-09-25T16:30:08+05:30")  # passed through as read
        self.assertEqual(body["sync"]["offline_count"], 1)
        self.assertEqual(body["ai"]["model"], "Qwen/Qwen3.5-35B")
        self.assertEqual([a["code"] for a in body["open_alerts"]], ["CLOCK_UNSYNCED"])
        self.assertIsNone(body["open_alerts"][0]["resolved_at"])
        codes = [e["code"] for e in body["recent_events"]]
        self.assertEqual(codes, ["CLOCK_UNSYNCED", "SERVICE_DOWN:postgres", "DEVICE_REGISTERED"])
        ids = [e["id"] for e in body["recent_events"]]
        self.assertEqual(ids, sorted(ids, reverse=True))  # newest first
        self.assertEqual(set(body["recent_events"][0]), {"id", "device_id", "ts", "kind", "severity", "code",
                                                         "message", "resolved_at", "data"})
        self.assertIsInstance(body["recent_events"][0]["data"], dict)

    def test_device_without_sync_or_ai_has_nulls(self):
        self.send(heartbeat(sync=None, ai=None))
        body = self.get_json(f"/api/v1/devices/{DEVICE}")
        self.assertIsNone(body["sync"])
        self.assertIsNone(body["ai"])

    def test_unknown_device_is_404(self):
        self.assertEqual(self.get("/api/v1/devices/nope").status, 404)
        self.assertEqual(self.get("/api/v1/devices/nope/metrics").status, 404)

    def test_device_id_is_url_decoded(self):
        self.send(heartbeat("site/box 1"))
        self.assertEqual(self.get_json("/api/v1/devices/site%2Fbox%201")["device"]["device_id"], "site/box 1")

    def test_recent_events_are_capped_at_50(self):
        self.send(heartbeat())
        for _ in range(30):
            self.send(heartbeat(gpu__temp_c=106))
            self.send(heartbeat(gpu__temp_c=40))
        body = self.get_json(f"/api/v1/devices/{DEVICE}")
        self.assertEqual(self.count("events"), 61)
        self.assertEqual(len(body["recent_events"]), 50)

    def test_sync_report_written_only_when_sync_state_changes(self):
        for age in (1.0, 1.1, 1.2):
            self.send(heartbeat(sync__age_h=age))
        self.assertEqual(self.count("sync_reports"), 1)
        self.send(heartbeat(sync__last_attempt="2026-09-29T14:27:09+05:30", sync__age_h=1.3))
        self.assertEqual(self.count("sync_reports"), 2)
        sync = self.get_json(f"/api/v1/devices/{DEVICE}")["sync"]
        self.assertEqual((sync["last_attempt"], sync["age_h"]), ("2026-09-29T14:27:09+05:30", 1.3))

    def test_metrics_endpoint_shape(self):
        for cpu in (10, 20, 30):
            self.send(heartbeat(system__cpu_pct=cpu))
        body = self.get_json(f"/api/v1/devices/{DEVICE}/metrics")
        self.assertEqual(set(body), {"device_id", "minutes", "points"})
        self.assertEqual((body["device_id"], body["minutes"]), (DEVICE, 60))
        self.assertEqual([p["cpu_pct"] for p in body["points"]], [10, 20, 30])  # oldest first
        self.assertEqual(set(body["points"][0]), {"ts", "cpu_pct", "cpu_temp_c", "gpu_pct", "gpu_temp_c", "ram_pct",
                                                  "disk_pct", "power_total_w", "gateway_rtt_ms", "gateway_loss_pct",
                                                  "wifi_signal_dbm", "fan_rpm", "max_temp_c", "buffered"})
        p = body["points"][0]
        self.assertEqual((p["cpu_temp_c"], p["max_temp_c"], p["fan_rpm"], p["wifi_signal_dbm"]), (40.4, 40.9, 1833, -66))
        self.assertEqual((p["gateway_loss_pct"], p["buffered"]), (0.0, 0))
        self.assertRegex(p["ts"], TS_RE)
        self.assertEqual(self.get_json(f"/api/v1/devices/{DEVICE}/metrics?minutes=5")["minutes"], 5)
        self.assertEqual(self.get_json(f"/api/v1/devices/{DEVICE}/metrics?minutes=0")["minutes"], 1)
        self.assertEqual(self.get_json(f"/api/v1/devices/{DEVICE}/metrics?minutes=99999")["minutes"], 10080)
        self.assertEqual(self.get(f"/api/v1/devices/{DEVICE}/metrics?minutes=abc").status, 400)

    def test_metrics_window_excludes_older_points(self):
        self.send(heartbeat())
        old = fs.fmt_ms(fs.now_ms() - 30 * 60_000)
        self.execute("INSERT INTO metrics (device_id, ts, buffered, cpu_pct) VALUES (?, ?, 1, 99)", [(DEVICE, old)])
        self.assertEqual(len(self.get_json(f"/api/v1/devices/{DEVICE}/metrics?minutes=10")["points"]), 1)
        self.assertEqual(len(self.get_json(f"/api/v1/devices/{DEVICE}/metrics?minutes=60")["points"]), 2)

    def test_metrics_downsampled_into_720_equal_buckets(self):
        self.send(heartbeat())
        self.execute("DELETE FROM metrics", [()])
        now = fs.now_ms()
        start = now - 60 * 60_000
        # 3600 rows, one per second; bucket j (5 s wide) holds k = 5j .. 5j+4 with cpu_pct = k
        self.execute("INSERT INTO metrics (device_id, ts, buffered, cpu_pct, fan_rpm) VALUES (?, ?, 0, ?, 1000)",
                     [(DEVICE, fs.fmt_ms(start + k * 1000 + 500), float(k)) for k in range(3600)])
        points = self.app.device_metrics(DEVICE, 60, now=now)["points"]
        self.assertEqual(len(points), 720)
        for j in (0, 1, 359, 719):
            self.assertAlmostEqual(points[j]["cpu_pct"], 5 * j + 2, places=6)
            self.assertEqual(points[j]["ts"], fs.fmt_ms(start + (5 * j + 2) * 1000 + 500))
            self.assertEqual(points[j]["fan_rpm"], 1000)
            self.assertIsNone(points[j]["gpu_pct"])
        stamps = [p["ts"] for p in points]
        self.assertEqual(stamps, sorted(set(stamps)))
        over_http = self.get_json(f"/api/v1/devices/{DEVICE}/metrics?minutes=60")["points"]
        self.assertLessEqual(len(over_http), 720)
        self.assertGreater(len(over_http), 700)
        self.assertEqual([p["ts"] for p in over_http], sorted(p["ts"] for p in over_http))

    def test_absurd_readings_are_null_and_averages_stay_valid_json(self):
        self.send(heartbeat(power__fan_rpm=1.7e308, system__uptime_s=5e9, system__cpu_pct=-2e9))
        self.assertEqual(self.q("SELECT fan_rpm, uptime_s, cpu_pct FROM metrics")[0],
                         {"fan_rpm": None, "uptime_s": None, "cpu_pct": None})
        # Rows stored before this rule existed can still hold such values: averaging a bucket of
        # them overflows to infinity, which must come back as null rather than an HTTP 500.
        self.execute("DELETE FROM metrics", [()])
        start = fs.now_ms() - 60 * 60_000
        # 800 rows one second apart: more than 720, so 5 s buckets of 5 rows each, all overflowing.
        self.execute("INSERT INTO metrics (device_id, ts, buffered, fan_rpm, cpu_pct) VALUES (?, ?, 0, ?, 5)",
                     [(DEVICE, fs.fmt_ms(start + k * 1000 + 500), 1.7e308) for k in range(800)])
        r = self.get(f"/api/v1/devices/{DEVICE}/metrics?minutes=60")
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(len(r.json["points"]), 160)
        self.assertEqual({p["fan_rpm"] for p in r.json["points"]}, {None})
        self.assertEqual({p["cpu_pct"] for p in r.json["points"]}, {5})

    def test_events_endpoint(self):
        self.send(heartbeat("box-a", system__ntp_synced=False))
        self.send(heartbeat("box-b", gpu__temp_c=106))
        self.send(heartbeat("box-b", gpu__temp_c=40))
        events = self.get_json("/api/v1/events")["events"]
        self.assertEqual(len(events), 5)  # 2 x DEVICE_REGISTERED, CLOCK_UNSYNCED, GPU_OVERHEAT, GPU_TEMP_HIGH
        self.assertEqual([e["id"] for e in events], sorted((e["id"] for e in events), reverse=True))
        self.assertEqual({e["device_id"] for e in self.get_json("/api/v1/events?device_id=box-a")["events"]},
                         {"box-a"})
        open_alerts = self.get_json("/api/v1/events?open=1")["events"]
        self.assertEqual([(e["device_id"], e["code"]) for e in open_alerts], [("box-a", "CLOCK_UNSYNCED")])
        self.assertTrue(all(e["kind"] == "alert" and e["resolved_at"] is None for e in open_alerts))
        self.assertEqual(len(self.get_json("/api/v1/events?limit=1")["events"]), 1)
        self.assertEqual(len(self.get_json("/api/v1/events?limit=0")["events"]), 1)  # clamped to 1
        self.assertEqual(len(self.get_json("/api/v1/events?limit=100000")["events"]), 5)
        self.assertEqual(self.get("/api/v1/events?limit=many").status, 400)

    def test_events_limit_is_capped_at_500(self):
        self.send(heartbeat())
        ts = fs.fmt_ms(fs.now_ms() - 1000)
        self.execute("INSERT INTO events (device_id, ts, kind, severity, code, message) "
                     "VALUES (?, ?, 'info', 'info', 'NOTE', 'n')", [(DEVICE, ts)] * 600)
        self.assertEqual(len(self.get_json("/api/v1/events?limit=100000")["events"]), 500)
        self.assertEqual(len(self.get_json("/api/v1/events")["events"]), 100)

    def test_healthz(self):
        self.assertEqual(self.get_json("/healthz"), {"ok": True, "devices": 0})
        self.send(heartbeat())
        self.assertEqual(self.get_json("/healthz"), {"ok": True, "devices": 1})

    def test_security_headers_on_every_response(self):
        self.send(heartbeat())
        responses = [self.get("/api/v1/fleet"), self.get(f"/api/v1/devices/{DEVICE}"), self.get("/healthz"),
                     self.get("/"), self.get("/static/app.js"), self.get("/nope"), self.post(heartbeat(), "bad"),
                     self.post(heartbeat()), self.request("DELETE", "/api/v1/fleet")]
        for r in responses:
            with self.subTest(status=r.status):
                self.assertEqual(r.headers["Content-Security-Policy"], "default-src 'self'")
                self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")

    def test_unknown_routes_and_wrong_methods(self):
        r = self.get("/api/v2/fleet")
        self.assertEqual((r.status, r.json["ok"]), (404, False))
        r = self.get("/api/v1/heartbeat")
        self.assertEqual((r.status, r.headers["Allow"]), (405, "POST"))
        self.assertEqual(self.request("POST", "/api/v1/fleet", b"{}").status, 405)
        r = self.request("DELETE", "/api/v1/fleet")
        self.assertEqual(r.status, 501)
        self.assertIs(r.json["ok"], False)

    def test_concurrent_heartbeats(self):
        results = []

        def worker(i):
            for _ in range(5):
                results.append(self.post(heartbeat(f"box-{i:02d}")).status)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(results, [200] * 60)
        self.assertEqual(self.count("devices"), 12)
        self.assertEqual(self.count("metrics"), 60)
        self.assertEqual(self.get_json("/api/v1/fleet")["counts"]["online"], 12)

    def test_burst_of_simultaneous_connections(self):
        """100 boxes connecting at the same moment (e.g. all retrying after a server restart)."""
        n = 100
        barrier = threading.Barrier(n)
        results = []

        def worker(i):
            body = json.dumps(heartbeat(f"burst-{i:03d}")).encode()
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            try:
                barrier.wait(30)
                conn.request("POST", "/api/v1/heartbeat", body=body,
                             headers={"Authorization": f"Bearer {FLEET_TOKEN}", "Content-Type": "application/json"})
                resp = conn.getresponse()
                resp.read()
                results.append(resp.status)
            except OSError as e:
                results.append(type(e).__name__)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(sorted(map(str, results)), ["200"] * n)
        self.assertEqual(self.count("devices"), n)


class PatchTest(ServerTestCase):
    FIELDS = {"product": "SchoolAI in a Box", "customer": "Excelsoft", "site_code": "blr-02",
              "location": "Mysuru lab", "latitude": 12.2958, "longitude": 76.6394, "installed_on": "2026-09-01",
              "support_contact": "ops@example.org", "device_group": "pilot", "notes": "rack 3"}

    def setUp(self):
        super().setUp()
        self.send(heartbeat())

    def test_requires_the_admin_token(self):
        for token in (None, FLEET_TOKEN, "wrong"):
            with self.subTest(token=token):
                r = self.patch({"notes": "x"}, token=token)
                self.assertEqual(r.status, 401)
        self.assertIsNone(self.q("SELECT notes FROM devices")[0]["notes"])

    def test_updates_whitelisted_fields(self):
        r = self.patch(self.FIELDS)
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(set(r.json), {"device"})
        self.assertEqual(set(r.json["device"]), DEVICE_COLUMNS)
        self.assertEqual({k: r.json["device"][k] for k in self.FIELDS}, self.FIELDS)
        stored = self.q("SELECT * FROM devices")[0]
        self.assertEqual({k: stored[k] for k in self.FIELDS}, self.FIELDS)
        self.assertEqual(stored["hostname"], "sarasschoolai")  # untouched
        fleet_device = self.get_json("/api/v1/fleet")["devices"][0]
        self.assertEqual((fleet_device["location"], fleet_device["device_group"]), ("Mysuru lab", "pilot"))

    def test_other_keys_are_400(self):
        for body in ({"hostname": "x"}, {"serial": "x", "notes": "ok"}, {"device_id": "y"}, {"first_seen": "z"}):
            with self.subTest(body=body):
                r = self.patch(body)
                self.assertEqual(r.status, 400)
                self.assertIs(r.json["ok"], False)
        self.assertIsNone(self.q("SELECT notes FROM devices")[0]["notes"])  # nothing applied

    def test_bad_values_are_400(self):
        for body in ({"latitude": 91}, {"longitude": -180.5}, {"latitude": "north"}, {"latitude": True},
                     {"notes": 5}, {"product": "p" * 201}, {"notes": "n" * 2001}, [1, 2]):
            with self.subTest(body=body):
                self.assertEqual(self.patch(body).status, 400)
        headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        self.assertEqual(self.request("PATCH", f"/api/v1/devices/{DEVICE}", b"{bad", headers).status, 400)
        self.assertEqual(self.request("PATCH", f"/api/v1/devices/{DEVICE}", b'{"latitude": NaN}', headers).status, 400)

    def test_null_clears_a_field(self):
        self.patch({"location": "somewhere", "latitude": 1.5})
        r = self.patch({"location": None, "latitude": None})
        self.assertEqual(r.status, 200)
        self.assertEqual((r.json["device"]["location"], r.json["device"]["latitude"]), (None, None))

    def test_unknown_device_is_404(self):
        self.assertEqual(self.patch({"notes": "x"}, device="nope").status, 404)

    def test_patch_on_metrics_path_is_405(self):
        r = self.request("PATCH", f"/api/v1/devices/{DEVICE}/metrics", b"{}",
                         {"Authorization": f"Bearer {ADMIN_TOKEN}"})
        self.assertEqual(r.status, 405)


# =========================================================================== section 9

class BasicAuthTest(ServerTestCase):
    config = {"dashboard_user": "ops", "dashboard_password": "s3cret:with:colons"}

    @staticmethod
    def basic(user, password):
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}

    def test_every_non_heartbeat_route_needs_basic_auth(self):
        self.send(heartbeat())
        for path in ("/api/v1/fleet", "/api/v1/events", f"/api/v1/devices/{DEVICE}",
                     f"/api/v1/devices/{DEVICE}/metrics", "/healthz", "/", "/static/app.js", "/nope"):
            with self.subTest(path=path):
                r = self.get(path)
                self.assertEqual(r.status, 401)
                self.assertTrue(r.headers["WWW-Authenticate"].startswith("Basic "))
                self.assertEqual(self.get(path, self.basic("ops", "wrong")).status, 401)
                self.assertEqual(self.get(path, self.basic("other", "s3cret:with:colons")).status, 401)
                self.assertEqual(self.get(path, {"Authorization": "Basic !!notbase64"}).status, 401)
                ok = self.get(path, self.basic("ops", "s3cret:with:colons"))
                self.assertEqual(ok.status, 404 if path == "/nope" else 200)

    def test_heartbeat_uses_its_bearer_token_only(self):
        self.assertEqual(self.post(heartbeat()).status, 200)

    def test_patch_uses_the_admin_token(self):
        """PATCH is the one non-heartbeat route without Basic auth (CONTRACT.md section 7): nginx
        answers 400 to a request with two Authorization headers, so it takes the admin token alone."""
        self.send(heartbeat())
        self.assertEqual(self.patch({"notes": "x"}).status, 200)
        self.assertEqual(self.patch({"notes": "x"}, token=None).status, 401)
        self.assertEqual(self.request("PATCH", f"/api/v1/devices/{DEVICE}", {"notes": "y"},
                                      self.basic("ops", "s3cret:with:colons")).status, 401)

    def raw_request(self, method, path, headers, body=b""):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path)
            for name, value in headers:
                conn.putheader(name, value)
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            return conn.getresponse().status
        finally:
            conn.close()

    def test_basic_and_bearer_headers_can_travel_together(self):
        self.send(heartbeat())
        basic = self.basic("ops", "s3cret:with:colons")["Authorization"]
        both = [("Authorization", basic), ("Authorization", f"Bearer {ADMIN_TOKEN}")]
        self.assertEqual(self.raw_request("PATCH", f"/api/v1/devices/{DEVICE}", both, b'{"notes": "both"}'), 200)
        self.assertEqual(self.q("SELECT notes FROM devices")[0]["notes"], "both")
        self.assertEqual(self.raw_request("GET", "/api/v1/fleet", list(reversed(both))), 200)


class HostHeaderTest(ServerTestCase):
    """DNS rebinding: a page on attacker.example re-pointed at this server is same-origin, but its
    requests still say 'Host: attacker.example'."""
    config = {"allowed_hosts": ["Fleet.Example.org."]}

    def test_browser_routes_answer_only_known_host_names(self):
        self.send(heartbeat())
        for host in ("attacker.example", f"attacker.example:{self.port}", "127.0.0.1.nip.io", "localhost.evil.test"):
            for path in ("/api/v1/fleet", f"/api/v1/devices/{DEVICE}", f"/api/v1/devices/{DEVICE}/metrics",
                         "/api/v1/events", "/healthz", "/", "/static/app.js"):
                with self.subTest(host=host, path=path):
                    r = self.get(path, {"Host": host})
                    self.assertEqual((r.status, r.json["ok"]), (421, False))
                    self.assertNotIn(DEVICE.encode(), r.body)
        r = self.request("PATCH", f"/api/v1/devices/{DEVICE}", {"notes": "x"},
                         {"Host": "attacker.example", "Authorization": f"Bearer {ADMIN_TOKEN}"})
        self.assertEqual(r.status, 421)
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}", "10.10.7.125:8090",
                     "fleet.example.org", "FLEET.example.org:443", "fleet.example.org."):
            with self.subTest(host=host):
                self.assertEqual(self.get("/api/v1/fleet", {"Host": host}).status, 200)

    def test_heartbeats_are_not_checked(self):
        """Boxes may address the server by any name; the bearer token protects that route."""
        r = self.request("POST", "/api/v1/heartbeat", heartbeat("box-b"),
                         {"Host": "fleet-box.school.lan", "Authorization": f"Bearer {FLEET_TOKEN}"})
        self.assertEqual(r.status, 200)


class StaticFilesTest(ServerTestCase):
    def test_index_and_assets(self):
        r = self.get("/")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.headers["Content-Type"].startswith("text/html"))
        self.assertIn(b"dashboard", r.body)
        r = self.get("/static/app.js")
        self.assertEqual((r.status, r.headers["Content-Type"]), (200, "text/javascript; charset=utf-8"))
        self.assertEqual(r.body, b"console.log('fleet');")
        self.assertEqual(self.get("/static/style.css").headers["Content-Type"], "text/css; charset=utf-8")
        self.assertEqual(self.get("/static/index.html").status, 200)
        self.assertEqual(self.get("/static/missing.js").status, 404)

    def test_path_traversal_is_rejected(self):
        (self.static / "sub").mkdir()
        (self.static / ".env").write_text("TOP-SECRET")
        for path in ("/static/../secret.txt", "/static/%2e%2e/secret.txt", "/static/..%2fsecret.txt",
                     "/static/%2e%2e%2fsecret.txt", "/static/sub/../../secret.txt", "/static//etc/passwd",
                     "/static/..\\secret.txt", "/static/.env", "/static/%2eenv", "/static/", "/static/sub",
                     "/static/%00", "/static/app.js%00.txt", "/static/../fleet.db", "/../secret.txt"):
            with self.subTest(path=path):
                r = self.get(path)
                self.assertEqual(r.status, 404)
                self.assertNotIn(b"TOP-SECRET", r.body)
                self.assertNotIn(b"SQLite", r.body)

    def test_symlink_out_of_static_is_rejected(self):
        os.symlink(self.tmp / "secret.txt", self.static / "link.txt")
        r = self.get("/static/link.txt")
        self.assertEqual(r.status, 404)
        self.assertNotIn(b"TOP-SECRET", r.body)


class MissingStaticTest(ServerTestCase):
    static_files = None

    def test_missing_dashboard_is_a_clean_404(self):
        for path in ("/", "/static/app.js", "/static/index.html"):
            with self.subTest(path=path):
                r = self.get(path)
                self.assertEqual(r.status, 404)
                self.assertEqual(r.json, {"ok": False, "error": "not found"})
        self.assertEqual(self.get("/healthz").status, 200)


class RetentionTest(ServerTestCase):
    def test_retention_deletes_old_rows(self):
        self.send(heartbeat())
        now = fs.now_ms()
        old, recent = fs.fmt_ms(now - 31 * DAY_MS), fs.fmt_ms(now - 29 * DAY_MS)
        very_old, year_ok = fs.fmt_ms(now - 366 * DAY_MS), fs.fmt_ms(now - 364 * DAY_MS)
        for ts in (old, recent):
            self.execute("INSERT INTO metrics (device_id, ts, buffered) VALUES (?, ?, 1)", [(DEVICE, ts)])
            self.execute("INSERT INTO service_checks (device_id, ts, service, status) VALUES (?, ?, 'x', 'up')",
                         [(DEVICE, ts)])
            self.execute("INSERT INTO ai_usage (device_id, ts) VALUES (?, ?)", [(DEVICE, ts)])
        for ts in (very_old, year_ok):
            self.execute("INSERT INTO sync_reports (device_id, ts) VALUES (?, ?)", [(DEVICE, ts)])
            self.execute("INSERT INTO events (device_id, ts, kind, severity, code, message) "
                         "VALUES (?, ?, 'info', 'info', 'OLD_INFO', 'm')", [(DEVICE, ts)])
            self.execute("INSERT INTO events (device_id, ts, kind, severity, code, message, resolved_at) "
                         "VALUES (?, ?, 'alert', 'warning', 'OLD_RESOLVED', 'm', ?)", [(DEVICE, ts, ts)])
        self.execute("INSERT INTO events (device_id, ts, kind, severity, code, message) "
                     "VALUES (?, ?, 'alert', 'critical', 'OLD_OPEN', 'm')", [(DEVICE, very_old)])

        self.app.run_retention(now=now)

        for table in ("metrics", "service_checks", "ai_usage"):
            with self.subTest(table=table):
                self.assertEqual(self.count(table, "ts = ?", old), 0)
                self.assertEqual(self.count(table, "ts = ?", recent), 1)
        self.assertEqual(self.count("metrics", "buffered = 0"), 1)  # today's live reading stays
        self.assertEqual(self.count("sync_reports", "ts = ?", very_old), 0)
        self.assertEqual(self.count("sync_reports", "ts = ?", year_ok), 1)
        self.assertEqual(self.count("events", "ts = ? AND code IN ('OLD_INFO', 'OLD_RESOLVED')", very_old), 0)
        self.assertEqual(self.count("events", "ts = ?", year_ok), 2)
        self.assertEqual(self.count("events", "code = 'OLD_OPEN'"), 1)  # open alerts are current state
        self.assertEqual(self.count("events", "code = 'DEVICE_REGISTERED'"), 1)


class RetentionDaysTest(ServerTestCase):
    config = {"retention_days": 7}

    def test_retention_days_setting(self):
        self.send(heartbeat())
        now = fs.now_ms()
        self.execute("INSERT INTO metrics (device_id, ts, buffered) VALUES (?, ?, 1)",
                     [(DEVICE, fs.fmt_ms(now - 8 * DAY_MS)), (DEVICE, fs.fmt_ms(now - 6 * DAY_MS))])
        self.app.run_retention(now=now)
        self.assertEqual(self.count("metrics"), 2)  # the 6-day-old row and the live one


class ArchiveFolderTest(ServerTestCase):
    """Every accepted heartbeat is also appended to <archive_dir>/<device>/<UTC date>.jsonl."""
    config = {"archive_dir": "archive"}  # relative: resolves against the test's temp folder

    def day_file(self, device_folder=DEVICE):
        return self.tmp / "archive" / device_folder / f"{fs.fmt_ms(fs.now_ms())[:10]}.jsonl"

    def lines(self, path):
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_live_and_buffered_heartbeats_are_appended(self):
        self.send(heartbeat())
        self.send(heartbeat(buffered=True, sent_ms=fs.now_ms() - 60_000))
        rows = self.lines(self.day_file())
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["buffered"] for r in rows], [False, True])
        for r in rows:
            self.assertEqual(r["device_id"], DEVICE)
            self.assertIsNotNone(fs.parse_ts(r["received_at"]))
            self.assertIsNotNone(fs.parse_ts(r["stored_ts"]))
        self.assertIn("system", rows[0])

    def test_rejected_heartbeats_are_not_archived(self):
        r = self.request("POST", "/api/v1/heartbeat", body=heartbeat(),
                         headers={"Authorization": "Bearer wrong", "Content-Type": "application/json"})
        self.assertEqual(r.status, 401)
        self.assertFalse((self.tmp / "archive").exists())

    def test_device_id_is_never_used_as_a_raw_path(self):
        for evil in ("../../escape", "/abs/path", "..", "a/b"):
            with self.subTest(device_id=evil):
                self.send(heartbeat(device_id=evil))
                name = fs.archive_folder_name(evil)
                self.assertNotIn("/", name)
                self.assertNotIn(name, ("", ".", ".."))
                self.assertTrue(self.day_file(name).is_file())
        self.assertNotEqual(fs.archive_folder_name("a/b"), fs.archive_folder_name("a_b"))
        self.assertFalse((self.tmp / "escape").exists())
        self.assertFalse(Path("/abs/path").exists())
        for p in (self.tmp / "archive").iterdir():
            self.assertEqual(p.resolve().parent, (self.tmp / "archive").resolve())

    def test_retention_deletes_old_day_files_only(self):
        self.send(heartbeat())
        folder = self.tmp / "archive" / DEVICE
        old = folder / "2020-01-01.jsonl"
        old.write_text("{}\n")
        gone = self.tmp / "archive" / "retired-box"
        gone.mkdir()
        (gone / "2020-01-02.jsonl").write_text("{}\n")
        deleted = self.app.run_retention()
        self.assertEqual(deleted["archive_files"], 2)
        self.assertFalse(old.exists())
        self.assertFalse(gone.exists())  # emptied device folders are removed
        self.assertTrue(self.day_file().is_file())  # today's file stays


class AvailabilityTest(ServerTestCase):
    """GET /api/v1/devices/{id}/availability: days with readings, gaps, and why each gap happened."""

    def test_days_and_gaps_with_restart_detection(self):
        self.send(heartbeat())
        self.execute("DELETE FROM metrics WHERE device_id = ?", [(DEVICE,)])
        base = fs.now_ms() - 2 * DAY_MS
        rows = [(base, 1000), (base + 60_000, 1060),             # normal readings
                (base + 3_600_000, 4600),                        # 1 h gap, uptime kept counting: stayed on
                (base + 5 * 3_600_000, 50)]                      # 4 h gap, uptime reset: restarted
        self.execute("INSERT INTO metrics (device_id, ts, uptime_s) VALUES (?, ?, ?)",
                     [(DEVICE, fs.fmt_ms(t), up) for t, up in rows])
        a = self.get_json(f"/api/v1/devices/{DEVICE}/availability?days=7")
        self.assertEqual(a["first_reading"], fs.fmt_ms(base))
        self.assertEqual(a["last_reading"], fs.fmt_ms(base + 5 * 3_600_000))
        self.assertEqual(sum(d["readings"] for d in a["days"]), 4)
        gaps = a["gaps"]
        self.assertEqual(len(gaps), 3)
        on, off, now_gap = gaps
        self.assertEqual((on["from"], on["to"], on["restarted"], on["booted_at"]),
                         (fs.fmt_ms(base + 60_000), fs.fmt_ms(base + 3_600_000), False, None))
        self.assertEqual(off["duration_s"], 4 * 3600)
        self.assertTrue(off["restarted"])
        self.assertEqual(off["booted_at"], fs.fmt_ms(base + 5 * 3_600_000 - 50_000))
        self.assertTrue(now_gap["ongoing"])
        self.assertIsNone(now_gap["to"])

    def test_days_follow_the_viewers_time_zone(self):
        self.send(heartbeat())
        self.execute("DELETE FROM metrics WHERE device_id = ?", [(DEVICE,)])
        # 20:00 UTC on a day is the next day in India (+05:30)
        t = fs.parse_ts("2030-01-01T20:00:00.000Z")
        self.execute("INSERT INTO metrics (device_id, ts, uptime_s) VALUES (?, ?, ?)", [(DEVICE, fs.fmt_ms(t), 10)])
        now = t + 60_000
        utc = self.app.device_availability(DEVICE, days=7, tz_offset_min=0, now=now)
        ist = self.app.device_availability(DEVICE, days=7, tz_offset_min=330, now=now)
        self.assertEqual([d["day"] for d in utc["days"]], ["2030-01-01"])
        self.assertEqual([d["day"] for d in ist["days"]], ["2030-01-02"])

    def test_unknown_device_and_bad_numbers(self):
        self.assertEqual(self.get("/api/v1/devices/nope/availability").status, 404)
        self.send(heartbeat())
        self.assertEqual(self.get(f"/api/v1/devices/{DEVICE}/availability?days=x").status, 400)
        self.assertEqual(self.get_json(f"/api/v1/devices/{DEVICE}/availability?days=9999")["days_back"], 365)


class RawReadingsTest(ServerTestCase):
    """GET /api/v1/devices/{id}/raw: the full stored readings from the plain-text archive."""
    config = {"archive_dir": "archive"}

    def test_index_and_one_full_reading(self):
        self.send(heartbeat())
        self.send(heartbeat(buffered=True, sent_ms=fs.now_ms() - 30_000))
        today = fs.fmt_ms(fs.now_ms())[:10]
        idx = self.get_json(f"/api/v1/devices/{DEVICE}/raw?date={today}&tz_offset_min=0")
        self.assertTrue(idx["available"])
        self.assertEqual(len(idx["readings"]), 2)
        self.assertEqual(sorted(r["buffered"] for r in idx["readings"]), [False, True])
        first = idx["readings"][0]["time"]
        one = self.get_json(f"/api/v1/devices/{DEVICE}/raw?at={quote(first)}")
        self.assertEqual(one["time"], first)
        self.assertEqual(one["reading"]["device_id"], DEVICE)
        self.assertIn("system", one["reading"])

    def test_bad_requests(self):
        self.send(heartbeat())
        base = f"/api/v1/devices/{DEVICE}/raw"
        self.assertEqual(self.get(base).status, 400)
        self.assertEqual(self.get(base + "?date=2026-02-30").status, 400)
        self.assertEqual(self.get(base + "?date=../../etc").status, 400)
        self.assertEqual(self.get(base + "?at=2001-01-01T00:00:00.000Z").status, 404)
        self.assertEqual(self.get("/api/v1/devices/nope/raw?date=2026-01-01").status, 404)

    def test_a_day_without_readings_is_empty(self):
        self.send(heartbeat())
        idx = self.get_json(f"/api/v1/devices/{DEVICE}/raw?date=2001-01-01")
        self.assertEqual(idx["readings"], [])


class RawWithoutArchiveTest(ServerTestCase):
    def test_says_not_available(self):
        self.send(heartbeat())
        idx = self.get_json(f"/api/v1/devices/{DEVICE}/raw?date=2026-01-01")
        self.assertFalse(idx["available"])


class NoArchiveByDefaultTest(ServerTestCase):
    def test_archive_off_when_not_configured(self):
        self.send(heartbeat())
        self.assertIsNone(self.app.archive_dir)
        self.assertEqual(self.app.run_retention()["archive_files"], 0)


# =========================================================================== files and CLI

class SchemaAndConfigFilesTest(unittest.TestCase):
    @staticmethod
    def tables(sql):
        sql = re.sub(r"--[^\n]*", "", sql)
        return {m.group(1): " ".join(m.group(2).split())
                for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\);", sql, re.S)}

    def test_schema_tables_are_exactly_the_contract_tables(self):
        contract = (ROOT / "CONTRACT.md").read_text(encoding="utf-8")
        block = re.search(r"```sql\n(.*?)```", contract, re.S).group(1)
        expected = self.tables(block)
        self.assertEqual(len(expected), 8)
        schema = (SERVER_DIR / "schema.sql").read_text(encoding="utf-8")
        self.assertEqual(self.tables(schema), expected)
        self.assertIn("PRAGMA journal_mode = WAL;", schema)

    def test_example_config_is_the_contract_config(self):
        example = json.loads((SERVER_DIR / "server.example.json").read_text())
        self.assertEqual(example, fs.DEFAULT_CONFIG)
        self.assertEqual(example, {"bind": "127.0.0.1", "port": 8090, "db_path": "./fleet.db",
                                   "fleet_token": "change-me", "device_tokens": {}, "admin_token": "change-me-too",
                                   "dashboard_user": None, "dashboard_password": None, "retention_days": 30,
                                   "offline_after_missed": 3, "offline_min_s": 90, "archive_dir": None,
                                   "allowed_hosts": []})
        contract = (ROOT / "CONTRACT.md").read_text(encoding="utf-8")
        block = re.search(r"### server config keys \(JSON\)\s+```json\n(.*?)```", contract, re.S).group(1)
        self.assertEqual(json.loads(block), example)

    def test_dev_config(self):
        dev = json.loads((SERVER_DIR / "server.dev.json").read_text())
        self.assertEqual(set(dev), set(fs.DEFAULT_CONFIG))
        self.assertEqual((dev["bind"], dev["port"], dev["db_path"], dev["fleet_token"], dev["admin_token"],
                          dev["archive_dir"]),
                         ("127.0.0.1", 8090, "../data/fleet.db", "dev-fleet-token", "dev-admin-token",
                          "../data/heartbeats"))
        cfg = fs.load_config(str(SERVER_DIR / "server.dev.json"))
        # relative to the config's folder: everything lands in device-management/data/
        self.assertEqual(cfg["db_path"], str(ROOT / "data" / "fleet.db"))
        self.assertEqual(cfg["archive_dir"], str(ROOT / "data" / "heartbeats"))

    def test_invalid_configs_are_refused(self):
        tmp = Path(tempfile.mkdtemp(prefix="fleet-cfg-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        real = {"fleet_token": "a-real-fleet-secret", "admin_token": "a-real-admin-secret"}
        for bad, says in (({"dashboard_user": "ops"}, "dashboard_user"), ({"port": "8090"}, "port"),
                          ({"retention_days": 0}, "retention_days"), ({"device_tokens": []}, "device_tokens"),
                          ({"offline_min_s": -1}, "offline_min_s"), ({"db_path": ":memory:"}, "db_path"),
                          ({"allowed_hosts": "fleet.example.org"}, "allowed_hosts"),
                          ({"allowed_hosts": ["https://fleet.example.org"]}, "allowed_hosts"),
                          ({"allowed_hosts": ["fleet.example.org:443"]}, "allowed_hosts"),
                          ({"allowed_hosts": [""]}, "allowed_hosts")):
            with self.subTest(bad=bad):
                path = tmp / "bad.json"
                path.write_text(json.dumps({**real, **bad}))
                with self.assertRaisesRegex(fs.ConfigError, says):
                    fs.load_config(str(path))
        path = tmp / "good.json"
        path.write_text(json.dumps({**real, "allowed_hosts": ["Fleet.Example.org.", "10.0.0.5", "[::1]"]}))
        self.assertEqual(fs.load_config(str(path))["allowed_hosts"], ["fleet.example.org", "10.0.0.5", "::1"])

    def test_example_placeholder_tokens_are_refused(self):
        """The placeholders are public, so the server refuses to start with them."""
        tmp = Path(tempfile.mkdtemp(prefix="fleet-cfg-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for bad in ({"admin_token": "a-real-admin-secret"},  # fleet_token left at its default
                    {"fleet_token": "a-real-fleet-secret", "admin_token": "change-me-too"},
                    {"fleet_token": "a-real-fleet-secret", "admin_token": "a-real-admin-secret",
                     "device_tokens": {"box-1": "change-me"}}):
            with self.subTest(bad=bad):
                path = tmp / "placeholder.json"
                path.write_text(json.dumps(bad))
                with self.assertRaisesRegex(fs.ConfigError, "placeholder"):
                    fs.load_config(str(path))
        with self.assertRaisesRegex(fs.ConfigError, "placeholder"):
            fs.load_config(str(SERVER_DIR / "server.example.json"))  # copied as is by a manual install
        with self.assertRaisesRegex(fs.ConfigError, "placeholder"):
            fs.load_config(None)  # the built-in defaults


class CommandLineTest(unittest.TestCase):
    def start(self, config):
        tmp = Path(tempfile.mkdtemp(prefix="fleet-cli-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "conf").mkdir()
        (tmp / "elsewhere").mkdir()
        cfg_path = tmp / "conf" / "server.json"
        cfg_path.write_text(json.dumps(config))
        proc = subprocess.Popen([sys.executable, str(SERVER_DIR / "fleet_server.py"), "--config", str(cfg_path)],
                                cwd=tmp / "elsewhere", stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

        lines = queue.Queue()
        reader = threading.Thread(target=lambda: [lines.put(line) for line in proc.stderr], daemon=True)
        reader.start()

        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.wait(10)
            reader.join(10)  # ends at EOF once the process is gone
            proc.stderr.close()

        self.addCleanup(cleanup)
        return proc, tmp, lines

    def test_runs_from_config_file_and_stops_on_sigterm(self):
        proc, tmp, lines = self.start({"port": 0, "db_path": "./cli.db", "fleet_token": "cli-token",
                                       "admin_token": "cli-admin"})
        port, deadline = None, time.monotonic() + 20
        while port is None and time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                self.assertIsNone(proc.poll(), "server exited early")
                continue
            m = re.search(r"listening on http://127\.0\.0\.1:(\d+)", line)
            port = int(m.group(1)) if m else None
        self.assertIsNotNone(port, "server did not report its port")

        def call(method, path, body=None, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.request(method, path, body=body, headers=headers or {})
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read())
            finally:
                conn.close()

        self.assertEqual(call("GET", "/healthz"), (200, {"ok": True, "devices": 0}))
        status, body = call("POST", "/api/v1/heartbeat", json.dumps(heartbeat()).encode(),
                            {"Authorization": "Bearer cli-token", "Content-Type": "application/json"})
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertEqual(call("GET", "/healthz"), (200, {"ok": True, "devices": 1}))
        self.assertTrue((tmp / "conf" / "cli.db").exists())  # relative to the config file, not the cwd
        self.assertFalse((tmp / "elsewhere" / "cli.db").exists())
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(15), 0)

    def test_bad_config_exits_with_2(self):
        proc, _tmp, _lines = self.start({"dashboard_user": "ops", "port": 0})
        self.assertEqual(proc.wait(15), 2)


if __name__ == "__main__":
    unittest.main()

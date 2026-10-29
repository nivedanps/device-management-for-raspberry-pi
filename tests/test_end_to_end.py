#!/usr/bin/env python3
"""End-to-end test: the real fleet server and the real agent together (stdlib unittest).

Run from the device-management folder:

    python3 -m unittest -v tests/test_end_to_end.py

It takes about a minute. server/fleet_server.py and agent/saras_agent.py run as subprocesses,
with their own configs, database, heartbeat archive and offline buffer in a temporary folder
($TMPDIR), on an OS-assigned port on 127.0.0.1. Nothing else is written, and both processes are
stopped at the end, also when a check fails (their logs are printed then).

The agent reads this box's real sensors and probes the real services exactly as
agent/agent.dev.json configures them: read-only GETs to the app's health URLs and a PostgreSQL
SSLRequest probe. Only its server address, token, interval and buffer path are changed.

The test walks through the pilot's three promises, in order:

 1. Live: within seconds the fleet API shows the box with real CPU, GPU temperature, power,
    RAM and disk readings; its services include app-live, app-ready, postgres and
    systemd:nginx.service; software versions are stored; CLOCK_UNSYNCED and SYNC_STALE are open
    exactly when the box's own heartbeat says so (both hold on the pilot Thor: no NTP, and the
    last saras-sync success is days old). The dashboard files are served.
 2. Nothing is lost: the server stops for 10 s while the agent keeps running. After a restart
    the missed heartbeats arrive as metrics rows with buffered = 1, stamped inside the outage,
    and the agent's buffer empties. The box is not marked offline for the server's own restart,
    but the drop leaves a trace: the metrics API flags the late readings (buffered = 1) and one
    CONNECTION_INTERRUPTED event counts them.
 3. Offline and back: the agent stops, DEVICE_OFFLINE opens once the box has been silent for
    longer than the threshold, the agent starts again, DEVICE_RECONNECTED is recorded and
    DEVICE_OFFLINE is resolved.

Interval: 5 s, the shortest the contract allows. The agent clamps interval_s to 5-3600 and so
does the server's next_interval_s (CONTRACT.md section 4), so a 3 s interval would silently
become 5 s. offline_min_s is 5, so the offline threshold is 3 x 5 = 15 s instead of 90 s. The
server looks for silent boxes every 15 s (section 6), so DEVICE_OFFLINE appears 15-30 s after
the last heartbeat.
"""
import sys

sys.dont_write_bytecode = True  # keep __pycache__ out of the source tree

import json  # noqa: E402
import os  # noqa: E402
import platform  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from urllib.parse import quote  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server" / "fleet_server.py"
AGENT = ROOT / "agent" / "saras_agent.py"
SERVER_DEV_CONFIG = ROOT / "server" / "server.dev.json"
AGENT_DEV_CONFIG = ROOT / "agent" / "agent.dev.json"
def _detect_python() -> str:
    for candidate in ("/usr/bin/python3", sys.executable):
        if os.access(candidate, os.X_OK):
            if subprocess.run([candidate, "-c", "import psutil"], capture_output=True).returncode == 0:
                return candidate
    return sys.executable

PYTHON = _detect_python()

INTERVAL_S = 5                  # the shortest interval the contract allows (see above)
OFFLINE_AFTER_MISSED = 3        # contract default
OFFLINE_MIN_S = 5               # contract default is 90
OFFLINE_THRESHOLD_S = max(OFFLINE_AFTER_MISSED * INTERVAL_S, OFFLINE_MIN_S)  # 15 s
OFFLINE_CHECK_S = 15            # the server's background check period (CONTRACT.md section 6)
SERVER_DOWN_S = 10

REAL_METRICS = ("cpu_pct", "gpu_temp_c", "power_total_w", "ram_pct", "disk_pct")
REQUIRED_SERVICES = {"app-live": "http", "app-ready": "http", "postgres": "tcp",
                     "systemd:nginx.service": "systemd"}

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy


def epoch(ts: str) -> float:
    """'2026-09-29T07:40:00.123Z' -> seconds since the epoch."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


class EndToEndTest(unittest.TestCase):
    """Server and agent as subprocesses; see the module docstring."""

    @classmethod
    def setUpClass(cls):
        probe = subprocess.run([PYTHON, "-c", "import psutil"], capture_output=True, timeout=30)
        if probe.returncode != 0:
            raise AssertionError(f"{PYTHON} cannot import psutil; the agent needs it for real readings "
                                 "(apt install python3-psutil)")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="saras-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.procs: dict[str, subprocess.Popen] = {}
        self.addCleanup(self._stop_all)  # cleanups run last-in first-out: processes before files
        self.token = secrets.token_hex(16)
        self.db_path = self.tmp / "fleet.db"
        self.archive_dir = self.tmp / "heartbeats"
        self.buffer_path = self.tmp / "agent-buffer.db"
        self.logs = {"server": self.tmp / "server.log", "agent": self.tmp / "agent.log"}
        self.port = None
        self.device_id = None
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID", "LISTEN_FDS", "LISTEN_PID")}
        self.env.update(PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")

    # ------------------------------------------------------------------ the test

    def test_live_buffered_offline_reconnected(self):
        try:
            summary = {}
            self.phase_live(summary)
            self.phase_server_outage(summary)
            self.phase_agent_offline(summary)
            self.stop("agent")
            self.stop("server")
        except BaseException:
            sys.stderr.write(self._diagnostics())
            raise
        sys.stderr.write(
            f"\n  end-to-end: box {self.device_id} ({summary['device_type']}) is {summary['state']} "
            f"{summary['reasons']}; full readings {summary['first_full_s']:.1f} s after the agent started; "
            f"{summary['buffered_rows']} heartbeat(s) buffered during a {SERVER_DOWN_S} s server outage and "
            f"delivered afterwards; DEVICE_OFFLINE {summary['offline_after_s']:.1f} s after the agent stopped "
            f"(threshold {OFFLINE_THRESHOLD_S} s); DEVICE_RECONNECTED {summary['reconnect_after_s']:.1f} s after "
            f"it restarted.\n")

    def phase_live(self, summary: dict) -> None:
        self.start_server(port=0)
        started = time.monotonic()
        self.start_agent()

        def box_with_real_metrics():
            fleet = self.get_json("/api/v1/fleet")
            self.assertLessEqual(fleet["counts"]["total"], 1, "only this box reports to the test server")
            if not fleet["devices"]:
                return None
            box = fleet["devices"][0]
            return box if all(box["latest"][f] is not None for f in REAL_METRICS) else None

        # cpu_pct is measured between two heartbeats, so the second heartbeat completes the set.
        box = self.wait_for("the box with real CPU, GPU temperature, power, RAM and disk readings in "
                            "/api/v1/fleet", box_with_real_metrics, timeout=45)
        summary["first_full_s"] = time.monotonic() - started
        self.device_id = box["device_id"]
        summary["device_type"] = box["device_type"]
        for field in REAL_METRICS:
            self.assertIsInstance(box["latest"][field], (int, float), field)
        self.assertTrue(0 <= box["latest"]["ram_pct"] <= 100 and 0 <= box["latest"]["disk_pct"] <= 100)
        self.assertGreater(box["latest"]["power_total_w"], 0)
        self.assertIsNotNone(box["versions"]["os"], "fleet view shows the OS version")
        self.assertIsNotNone(box["last_seen"])

        detail = self.device_detail()
        services = {s["service"]: s for s in detail["services"]}
        for name, kind in REQUIRED_SERVICES.items():
            self.assertIn(name, services, f"service {name} is reported")
            self.assertEqual(services[name]["kind"], kind, name)
            self.assertIn(services[name]["status"], ("up", "down", "degraded"), f"{name} was actually probed")
            self.assertIsNotNone(services[name]["availability_24h_pct"], name)

        software = {s["component"]: s["version"] for s in detail["software"]}
        for component in ("os", "kernel", "python"):
            self.assertTrue(software.get(component), f"software version '{component}' is stored")
        self.assertEqual(software["kernel"], platform.release())
        stored = self.sql("SELECT COUNT(*) FROM software_versions WHERE device_id = ?", self.device_id)[0][0]
        self.assertEqual(stored, len(software), "one software_versions row per component so far")

        # Alerts follow the box's own report. On the pilot Thor both are open: timedatectl says
        # NTPSynchronized=no, and saras-sync last succeeded days ago.
        latest, status = detail["latest"], detail["status"]
        open_alerts = {a["code"]: a for a in detail["open_alerts"]}
        ntp = latest["system"]["ntp_synced"]
        if ntp is False or abs(status["clock_offset_s"]) > 120:
            self.assertIn("CLOCK_UNSYNCED", open_alerts, f"ntp_synced={ntp}")
            self.assertEqual(open_alerts["CLOCK_UNSYNCED"]["severity"], "warning")
        elif ntp is True:
            self.assertNotIn("CLOCK_UNSYNCED", open_alerts)
        age_h = (latest.get("sync") or {}).get("age_h")
        if age_h is not None:
            self.assertEqual("SYNC_STALE" in open_alerts, age_h > 8, f"sync age {age_h} h")
            if age_h > 8:
                self.assertEqual(open_alerts["SYNC_STALE"]["severity"], "critical")
        severities = {a["severity"] for a in open_alerts.values()}
        expected_state = ("critical" if "critical" in severities else "warning" if "warning" in severities
                          else "online")
        self.assertEqual(status["state"], expected_state)
        self.assertEqual(status["reasons"], sorted(open_alerts))
        self.assertEqual(box["reasons"], sorted(open_alerts))
        summary["state"], summary["reasons"] = status["state"], status["reasons"]
        self.assertEqual(self.event_codes().count("DEVICE_REGISTERED"), 1)

        # The plain-text copy of every heartbeat (CONTRACT.md section 10).
        lines = [json.loads(line) for f in self.archive_dir.glob("*/*.jsonl") for line in f.read_text().splitlines()]
        mine = [line for line in lines if line.get("device_id") == self.device_id]
        self.assertTrue(mine, "heartbeats are archived as JSON lines")
        self.assertTrue(all("received_at" in line and "stored_ts" in line for line in mine))

        # The dashboard is served, with the security headers.
        status_code, headers, body = self.get_raw("/")
        self.assertEqual(status_code, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(headers["Content-Security-Policy"], "default-src 'self'")
        self.assertIn(b"<html", body.lower())
        for asset in ("/static/app.js", "/static/style.css"):
            self.assertEqual(self.get_raw(asset)[0], 200, asset)

    def phase_server_outage(self, summary: dict) -> None:
        self.assertEqual(self.sql("SELECT COUNT(*) FROM metrics WHERE buffered = 1")[0][0], 0)
        self.stop("server")
        down_at = time.time()
        # The agent keeps collecting; each heartbeat it cannot deliver goes to its SQLite buffer.
        self.wait_for("the agent to buffer a heartbeat while the server is down",
                      lambda: self.agent_buffer_rows() >= 1, timeout=SERVER_DOWN_S + INTERVAL_S)
        time.sleep(max(0.0, SERVER_DOWN_S - (time.time() - down_at)))
        self.start_server(port=self.port)
        up_at = time.time()

        def replayed():
            rows = self.sql("SELECT ts FROM metrics WHERE device_id = ? AND buffered = 1 ORDER BY ts",
                            self.device_id)
            return rows if rows and self.agent_buffer_rows() == 0 else None

        rows = self.wait_for("the buffered heartbeats to reach the server (metrics.buffered = 1) and the "
                             "agent's buffer to empty", replayed, timeout=2 * INTERVAL_S + 15)
        summary["buffered_rows"] = len(rows)
        # Stamped when they were measured (sent_at + the box's clock offset), not when replayed.
        for (ts,) in rows:
            self.assertTrue(down_at - INTERVAL_S - 1 <= epoch(ts) <= up_at + 1,
                            f"buffered reading at {ts} lies inside the outage")
        archived = [json.loads(line) for f in self.archive_dir.glob("*/*.jsonl") for line in f.read_text().splitlines()]
        self.assertTrue(any(line.get("buffered") is True for line in archived), "replays are archived too")
        # The drop is visible: the late readings are flagged in the API, and one event counts them.
        points = self.get_json(f"/api/v1/devices/{quote(self.device_id, safe='')}/metrics?minutes=30")["points"]
        self.assertEqual(sum(1 for p in points if p["buffered"]), len(rows), "late readings flagged in the API")
        interrupted = [e for e in self.events() if e["code"] == "CONNECTION_INTERRUPTED"]
        self.assertEqual(len(interrupted), 1, "one CONNECTION_INTERRUPTED event for one drop")
        self.assertEqual((interrupted[0]["kind"], interrupted[0]["data"]["count"]), ("info", len(rows)))
        # A server restart must not make the box look offline.
        self.assertNotIn("DEVICE_OFFLINE", self.event_codes())
        box = self.fleet_box()
        self.assertNotEqual(box["state"], "offline")
        self.assertGreaterEqual(epoch(box["last_seen"]), up_at - 1, "a live heartbeat arrived after the restart")

    def phase_agent_offline(self, summary: dict) -> None:
        self.stop("agent")
        stopped = time.monotonic()
        box = self.wait_for("DEVICE_OFFLINE once the box is silent for more than "
                            f"{OFFLINE_THRESHOLD_S} s", self._offline_box,
                            timeout=OFFLINE_THRESHOLD_S + OFFLINE_CHECK_S + 15)
        summary["offline_after_s"] = time.monotonic() - stopped
        self.assertEqual(box["state"], "offline")
        alert = next(e for e in self.events(open_only=True) if e["code"] == "DEVICE_OFFLINE")
        self.assertEqual(alert["kind"], "alert")
        self.assertEqual(alert["severity"], "critical")
        self.assertIsNone(alert["resolved_at"])
        self.assertEqual(alert["data"]["threshold_s"], OFFLINE_THRESHOLD_S)
        # silent_s is rounded to 0.1 s by the server, hence >= rather than >.
        self.assertGreaterEqual(alert["data"]["silent_s"], OFFLINE_THRESHOLD_S, "not raised before the threshold")

        self.start_agent()
        restarted = time.monotonic()
        reconnect = self.wait_for("DEVICE_RECONNECTED after the agent restarted", self._reconnected,
                                  timeout=2 * INTERVAL_S + 15)
        summary["reconnect_after_s"] = time.monotonic() - restarted
        self.assertEqual(reconnect["kind"], "info")
        self.assertGreaterEqual(reconnect["data"]["gap_s"], OFFLINE_THRESHOLD_S)
        offline = [e for e in self.events() if e["code"] == "DEVICE_OFFLINE"]
        self.assertEqual(len(offline), 1, "one DEVICE_OFFLINE alert for one outage")
        self.assertEqual(offline[0]["resolved_at"], reconnect["ts"], "resolved by the reconnecting heartbeat")
        box = self.fleet_box()
        self.assertNotEqual(box["state"], "offline")
        self.assertNotIn("DEVICE_OFFLINE", box["reasons"])
        codes = self.event_codes()
        self.assertNotIn("DEVICE_RESTARTED", codes, "an agent restart is not a device restart (same boot_id)")
        self.assertNotIn("VERSION_CHANGED", codes, "the restarted agent reports the same versions")

    # ------------------------------------------------------------------ API and database helpers

    def _offline_box(self):
        box = self.fleet_box()
        return box if box["state"] == "offline" and "DEVICE_OFFLINE" in box["reasons"] else None

    def _reconnected(self):
        return next((e for e in self.events() if e["code"] == "DEVICE_RECONNECTED"), None)

    def get_raw(self, path: str):
        try:
            with _OPENER.open(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read()

    def get_json(self, path: str):
        status, _, body = self.get_raw(path)
        self.assertEqual(status, 200, f"GET {path}: {body[:200]!r}")
        return json.loads(body)

    def fleet_box(self) -> dict:
        devices = self.get_json("/api/v1/fleet")["devices"]
        self.assertEqual([d["device_id"] for d in devices], [self.device_id])
        return devices[0]

    def device_detail(self) -> dict:
        return self.get_json(f"/api/v1/devices/{quote(self.device_id, safe='')}")

    def events(self, open_only: bool = False) -> list[dict]:
        query = f"device_id={quote(self.device_id, safe='')}&limit=500" + ("&open=1" if open_only else "")
        return self.get_json(f"/api/v1/events?{query}")["events"]

    def event_codes(self) -> list[str]:
        return [e["code"] for e in self.events()]

    def sql(self, query: str, *params):
        """Read-only query on the server's database, for what the API does not show (row counts)."""
        conn = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()

    def agent_buffer_rows(self) -> int:
        if not self.buffer_path.exists():
            return 0
        conn = sqlite3.connect(self.buffer_path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            return conn.execute("SELECT COUNT(*) FROM heartbeat_buffer").fetchone()[0]
        finally:
            conn.close()

    def wait_for(self, what: str, check, timeout: float, every: float = 0.5):
        """Poll check() until it returns something truthy; fail after `timeout` seconds."""
        deadline = time.monotonic() + timeout
        last_error = None
        while True:
            try:
                value = check()
                if value:
                    return value
            except (OSError, ValueError, sqlite3.Error) as e:  # e.g. the server is still starting
                last_error = e
            for name, proc in self.procs.items():
                if proc.poll() is not None:
                    self.fail(f"the {name} exited with code {proc.returncode} while waiting for {what}")
            if time.monotonic() >= deadline:
                self.fail(f"timed out after {timeout:g} s waiting for {what}"
                          + (f" (last error: {last_error!r})" if last_error else ""))
            time.sleep(every)

    # ------------------------------------------------------------------ processes

    def start_server(self, port: int) -> None:
        cfg = json.loads(SERVER_DEV_CONFIG.read_text(encoding="utf-8"))
        cfg.update(bind="127.0.0.1", port=port, db_path=str(self.db_path), archive_dir=str(self.archive_dir),
                   fleet_token=self.token, device_tokens={}, admin_token=secrets.token_hex(16),
                   dashboard_user=None, dashboard_password=None, offline_after_missed=OFFLINE_AFTER_MISSED,
                   offline_min_s=OFFLINE_MIN_S)
        config = self.tmp / "server.json"
        config.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        log = self.logs["server"]
        seen = log.stat().st_size if log.exists() else 0
        self._spawn("server", [PYTHON, str(SERVER), "--config", str(config)])

        def listening():
            text = log.read_bytes()[seen:].decode("utf-8", "replace")
            m = re.search(r"listening on http://127\.0\.0\.1:(\d+)", text)
            return int(m.group(1)) if m else None

        actual = self.wait_for("the server to start listening", listening, timeout=20, every=0.1)
        if port:
            self.assertEqual(actual, port)
        self.port = actual
        self.wait_for("GET /healthz", lambda: self.get_raw("/healthz")[0] == 200, timeout=10, every=0.1)

    def start_agent(self) -> None:
        cfg = json.loads(AGENT_DEV_CONFIG.read_text(encoding="utf-8"))
        cfg.update(server_url=f"http://127.0.0.1:{self.port}", token=self.token, interval_s=INTERVAL_S,
                   buffer_path=str(self.buffer_path))
        config = self.tmp / "agent.json"
        config.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        self._spawn("agent", [PYTHON, str(AGENT), "--config", str(config)])

    def _spawn(self, name: str, argv: list[str]) -> None:
        self.assertNotIn(name, self.procs, f"the {name} is already running")
        with open(self.logs[name], "ab") as log:
            self.procs[name] = subprocess.Popen(argv, cwd=self.tmp, env=self.env, stdin=subprocess.DEVNULL,
                                                stdout=subprocess.DEVNULL, stderr=log)

    def stop(self, name: str) -> None:
        """SIGTERM, as systemd stops it; both programs promise a clean exit with code 0."""
        proc = self.procs.pop(name)
        proc.send_signal(signal.SIGTERM)
        try:
            code = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            self.fail(f"the {name} did not stop within 20 s of SIGTERM")
        self.assertEqual(code, 0, f"the {name}'s exit code after SIGTERM")

    def _stop_all(self) -> None:
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        self.procs.clear()

    def _diagnostics(self) -> str:
        out = []
        for name, path in self.logs.items():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
            out.append(f"\n----- last {min(len(lines), 40)} lines of the {name} log -----\n" + "\n".join(lines[-40:]))
        return "\n".join(out) + "\n"


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env bash
# Install or update the SARAS fleet server (heartbeat API, alerts and dashboard) on this machine.
#
#   sudo ./install-server.sh              install or update, enable, and (re)start the service
#   sudo ./install-server.sh --no-start   install or update and enable; start it later yourself
#
# Safe to run again: it refreshes the code, the dashboard and the systemd unit, keeps an existing
# /etc/saras-fleet/server.json (it holds the tokens) and all collected data, and restarts the service.
#
#   /opt/saras-fleet/server/             fleet_server.py, schema.sql, static/ (the dashboard)
#   /etc/saras-fleet/server.json         config; the first run writes it with new random tokens and a
#                                        random dashboard password for the user "fleet" (mode 640)
#   /var/lib/saras-fleet/fleet.db        the monitoring database
#   /var/lib/saras-fleet/heartbeats/     plain-text copy of every heartbeat, one file per box per day
#   /etc/systemd/system/fleet-server.service
#
# The service runs as the system user saras-fleet and listens on 127.0.0.1:8090 only. Boxes on
# other networks reach it through nginx with TLS in front of it (README.md explains how).
set -euo pipefail

SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/saras-fleet
INSTALL_DIR=$APP_DIR/server
CONF_DIR=/etc/saras-fleet
CONF_FILE=$CONF_DIR/server.json
STATE_DIR=/var/lib/saras-fleet
SERVICE_USER=saras-fleet
UNIT_NAME=fleet-server.service
UNIT_FILE=/etc/systemd/system/$UNIT_NAME
PYTHON=/usr/bin/python3

START=1
for arg in "$@"; do
  case "$arg" in
    --no-start) START=0 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

die() { echo "install-server: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root: sudo $0"
for f in fleet_server.py schema.sql server.example.json fleet-server.service \
         static/index.html static/app.js static/style.css; do
  [[ -f $SRC_DIR/$f ]] || die "missing $SRC_DIR/$f"
done
[[ -x $PYTHON ]] || die "$PYTHON not found"
"$PYTHON" -c 'import sqlite3' 2>/dev/null || die "$PYTHON has no sqlite3 module"
"$PYTHON" -B -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$SRC_DIR/fleet_server.py" \
  || die "fleet_server.py has a syntax error"

# Service account: cannot log in, owns nothing but its state directory
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
  echo "created system user $SERVICE_USER"
fi

# Code and dashboard, owned by root so the service cannot change them. The dashboard folder is
# refilled, so a file removed from the source also disappears here.
install -d -m 0755 "$APP_DIR" "$INSTALL_DIR" "$INSTALL_DIR/static"
install -m 0644 "$SRC_DIR/fleet_server.py" "$SRC_DIR/schema.sql" "$SRC_DIR/server.example.json" "$INSTALL_DIR/"
find "$INSTALL_DIR/static" -mindepth 1 -delete
find "$SRC_DIR/static" -maxdepth 1 -type f ! -name '.*' -exec install -m 0644 {} "$INSTALL_DIR/static/" \;

# Config: never overwrite an existing one. A new one gets random tokens, a random dashboard
# password (so neither other users of this machine nor a web page can read the fleet without it)
# and keeps its data in the state directory, the only place the hardened service may write.
install -d -m 0750 -o root -g "$SERVICE_USER" "$CONF_DIR"
if [[ -e $CONF_FILE ]]; then
  echo "keeping existing $CONF_FILE"
else
  (umask 077; "$PYTHON" -B - "$SRC_DIR/server.example.json" "$CONF_FILE" "$STATE_DIR" <<'PY'
import json, secrets, sys
example, target, state = sys.argv[1:4]
with open(example, encoding="utf-8") as f:
    cfg = json.load(f)
cfg.update(db_path=f"{state}/fleet.db", archive_dir=f"{state}/heartbeats",
           fleet_token=secrets.token_hex(32), admin_token=secrets.token_hex(32),
           dashboard_user="fleet", dashboard_password=secrets.token_urlsafe(18))
with open(target, "x", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PY
  )
  chown "root:$SERVICE_USER" "$CONF_FILE"
  chmod 0640 "$CONF_FILE"
  echo "wrote $CONF_FILE with a new random fleet_token, admin_token and dashboard password (user: fleet)"
fi

# Check the config with the server's own rules before touching the running service.
"$PYTHON" -B - "$INSTALL_DIR" "$CONF_FILE" "$STATE_DIR" <<'PY' || die "$CONF_FILE has a problem (above); fix it and run again"
import sys
sys.path.insert(0, sys.argv[1])
import fleet_server
conf, state = sys.argv[2], sys.argv[3].rstrip("/") + "/"
try:
    cfg = fleet_server.load_config(conf)
except fleet_server.ConfigError as e:
    sys.exit(f"install-server: {e}")
for key, name in (("db_path", "fleet.db"), ("archive_dir", "heartbeats")):
    if cfg[key] is not None and not cfg[key].startswith(state):
        sys.exit(f"install-server: {key} is {cfg[key]}, but the service may only write inside {state}; "
                 f'set "{key}": "{state}{name}"')
for warning in fleet_server.config_warnings(cfg):
    print(f"install-server: warning: {warning}", file=sys.stderr)
PY

# State directory (systemd's StateDirectory= creates it too). The chown also covers a database
# that was first created by hand as root.
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$STATE_DIR"

# systemd unit
install -m 0644 "$SRC_DIR/fleet-server.service" "$UNIT_FILE"
systemctl daemon-reload
systemctl enable "$UNIT_NAME" >/dev/null
if [[ $START -eq 0 ]]; then
  echo "fleet server is installed and enabled; start it with: systemctl start $UNIT_NAME"
  exit 0
fi
systemctl restart "$UNIT_NAME"

# The unit is Type=simple, so systemctl returns before the port is open: ask the server itself.
"$PYTHON" -B - "$CONF_FILE" <<'PY' || die "the fleet server is not answering; see: journalctl -u $UNIT_NAME -n 50"
import json, sys, time, urllib.error, urllib.request
with open(sys.argv[1], encoding="utf-8") as f:
    cfg = json.load(f)
bind, port = cfg.get("bind") or "127.0.0.1", cfg.get("port", 8090)
host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(bind, bind)
base = f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 15
while True:
    try:
        with opener.open(base + "/healthz", timeout=2) as r:
            devices = json.load(r).get("devices")
        print(f"fleet server is up: {base}/ ({devices} box(es) registered)")
        break
    except urllib.error.HTTPError:  # any HTTP answer (e.g. 401 with a dashboard password) means it is up
        print(f"fleet server is up: {base}/")
        break
    except (OSError, ValueError):
        if time.monotonic() > deadline:
            sys.exit(1)
        time.sleep(0.5)
PY
echo "Boxes report to it with the fleet_token in $CONF_FILE (sudo grep fleet_token $CONF_FILE)."
echo "The dashboard asks for dashboard_user and dashboard_password (sudo grep dashboard_ $CONF_FILE)."
echo "Follow it with: journalctl -u $UNIT_NAME -f"

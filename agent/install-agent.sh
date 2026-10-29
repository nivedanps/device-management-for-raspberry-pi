#!/usr/bin/env bash
# Install or update the SARAS device-management agent on this box.
#
#   sudo ./install-agent.sh              install or update, enable, and (re)start the service
#   sudo ./install-agent.sh --no-start   install or update and enable; start it later yourself
#
# Safe to run again: it refreshes the code and the systemd unit, keeps an existing
# /etc/saras-agent/agent.json (it holds this box's token), and restarts the service.
#
#   /opt/saras-agent/saras_agent.py        the agent
#   /etc/saras-agent/agent.json            config (written from agent.example.json if absent; mode 600)
#   /var/lib/saras-agent/buffer.db         offline buffer
#   /etc/systemd/system/saras-agent.service
set -euo pipefail

SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR=/opt/saras-agent
CONF_DIR=/etc/saras-agent
CONF_FILE=$CONF_DIR/agent.json
STATE_DIR=/var/lib/saras-agent
UNIT_NAME=saras-agent.service
UNIT_FILE=/etc/systemd/system/$UNIT_NAME
PYTHON=/usr/bin/python3

START=1
for arg in "$@"; do
  case "$arg" in
    --no-start) START=0 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

die() { echo "install-agent: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root: sudo $0"
for f in saras_agent.py agent.example.json saras-agent.service; do
  [[ -f $SRC_DIR/$f ]] || die "missing $SRC_DIR/$f"
done
[[ -x $PYTHON ]] || die "$PYTHON not found"
"$PYTHON" -c 'import psutil' 2>/dev/null || die "Python module psutil is missing (apt install python3-psutil)"
"$PYTHON" -c 'import pynvml' 2>/dev/null \
  || echo "install-agent: note: pynvml is not importable; GPU readings will fall back to jtop/sysfs" >&2
"$PYTHON" -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$SRC_DIR/saras_agent.py" \
  || die "saras_agent.py has a syntax error"

# Code
install -d -m 0755 "$INSTALL_DIR"
install -m 0755 "$SRC_DIR/saras_agent.py" "$INSTALL_DIR/saras_agent.py"
install -m 0644 "$SRC_DIR/agent.example.json" "$INSTALL_DIR/agent.example.json"

# Config: never overwrite an existing one
install -d -m 0750 "$CONF_DIR"
if [[ -e $CONF_FILE ]]; then
  echo "keeping existing $CONF_FILE"
else
  install -m 0600 "$SRC_DIR/agent.example.json" "$CONF_FILE"
  echo "wrote $CONF_FILE from agent.example.json: set server_url, token and claimed.site_code for this box"
fi
"$PYTHON" -c 'import json, sys; json.load(open(sys.argv[1]))' "$CONF_FILE" 2>/dev/null \
  || die "$CONF_FILE is not valid JSON; fix it and run again"
if grep -q '"change-me"' "$CONF_FILE"; then
  echo "install-agent: warning: $CONF_FILE still has the placeholder token \"change-me\"" >&2
fi

# State directory for the offline buffer (systemd's StateDirectory= also creates it)
install -d -m 0700 "$STATE_DIR"

# Dry run: one collection with the installed code and config; nothing is sent
"$PYTHON" "$INSTALL_DIR/saras_agent.py" --config "$CONF_FILE" --once >/dev/null \
  || die "the agent failed a dry run (saras_agent.py --once); the service was not (re)started"

# systemd unit
install -m 0644 "$SRC_DIR/saras-agent.service" "$UNIT_FILE"
systemctl daemon-reload
systemctl enable "$UNIT_NAME" >/dev/null
if [[ $START -eq 1 ]]; then
  systemctl restart "$UNIT_NAME"
  echo "saras-agent is $(systemctl is-active "$UNIT_NAME" || true)."
  echo "Follow it with: journalctl -u $UNIT_NAME -f"
else
  echo "saras-agent is installed and enabled; start it with: systemctl start $UNIT_NAME"
fi

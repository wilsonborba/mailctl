#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
VENV_DIR="$ROOT_DIR/.venv"
PYTHON_BIN=${PYTHON_BIN:-python3}
SERVICE_NAME="mailctl-api"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CURRENT_USER=$(id -un)
CURRENT_GROUP=$(id -gn)
HOST=${MAILCTL_API_HOST:-0.0.0.0}
PORT=${MAILCTL_API_PORT:-18080}

log() {
  printf '[mailctl-install] %s\n' "$1"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

disable_legacy_user_service() {
  if systemctl --user list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1 \
    && systemctl --user is-enabled "${SERVICE_NAME}.service" >/dev/null 2>&1; then
    log "Disabling legacy per-user ${SERVICE_NAME}.service (replaced by the system-wide unit)."
    systemctl --user stop "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
    systemctl --user disable "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
  fi
}

install_systemd_service() {
  sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=mailctl API Service
After=network.target

[Service]
Type=simple
User=${CURRENT_USER}
Group=${CURRENT_GROUP}
WorkingDirectory=${ROOT_DIR}
ExecStart=${VENV_DIR}/bin/python -m mailctl api serve --host ${HOST} --port ${PORT}
Restart=always
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
UMask=027

[Install]
WantedBy=multi-user.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable "$SERVICE_NAME"
  sudo systemctl restart "$SERVICE_NAME"

  log "host/port in use: ${HOST}:${PORT}"
}

configure_ufw() {
  if ! command -v ufw >/dev/null 2>&1; then
    log "ufw not installed; skipping firewall configuration."
    return
  fi

  if ! sudo ufw status | grep -q 'Status: active'; then
    log "ufw installed but inactive; skipping firewall configuration."
    return
  fi

  if [ "$HOST" = "127.0.0.1" ] || [ "$HOST" = "localhost" ]; then
    log "API bound to ${HOST}; not LAN-reachable, skipping firewall rules."
    return
  fi

  sudo ufw allow proto tcp from 10.0.0.0/8 to any port "$PORT" comment 'mailctl-api-lan-10' >/dev/null || true
  sudo ufw allow proto tcp from 172.16.0.0/12 to any port "$PORT" comment 'mailctl-api-lan-172' >/dev/null || true
  sudo ufw allow proto tcp from 192.168.0.0/16 to any port "$PORT" comment 'mailctl-api-lan-192' >/dev/null || true
  sudo ufw deny "$PORT"/tcp comment 'mailctl-api-deny-public' >/dev/null || true
  log "ufw LAN-only rules ensured for port ${PORT}."
}

run_healthcheck() {
  local host="$HOST"
  [ "$host" = "0.0.0.0" ] && host=127.0.0.1
  "$VENV_DIR/bin/python" - <<PY
import time
from urllib.error import URLError
from urllib.request import urlopen

host = "${host}"
port = ${PORT}
last_error = None
for _ in range(10):
    try:
        with urlopen(f"http://{host}:{port}/health", timeout=5) as response:
            print(response.read().decode())
            break
    except URLError as exc:
        last_error = exc
        time.sleep(1)
else:
    raise SystemExit(f"health check failed: {last_error}")
PY
}

main() {
  require_command "$PYTHON_BIN"
  require_command sudo
  require_command systemctl

  log "Creating Python virtual environment."
  if [ ! -d "$VENV_DIR" ]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi

  log "Installing Python dependencies."
  "$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
  "$VENV_DIR/bin/pip" install -e "$ROOT_DIR" >/dev/null

  log "Checking for a legacy per-user service."
  disable_legacy_user_service

  log "Installing systemd unit."
  install_systemd_service

  log "Configuring firewall when available."
  configure_ufw

  log "Running health check."
  run_healthcheck

  log "Installation complete."
  log "Service name: ${SERVICE_NAME}"
}

main "$@"

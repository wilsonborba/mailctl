#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="mailctl-api"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

log() {
  printf '[mailctl-uninstall] %s\n' "$1"
}

remove_ufw_rules() {
  if ! command -v ufw >/dev/null 2>&1; then
    return
  fi

  if ! sudo ufw status | grep -q 'Status: active'; then
    return
  fi

  while read -r rule_number; do
    sudo ufw --force delete "$rule_number"
  done < <(sudo ufw status numbered | grep 'mailctl-api-' | sed -n 's/^\[ \{0,1\}\([0-9]\+\)\].*/\1/p' | sort -rn)
}

main() {
  if sudo systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
    sudo systemctl stop "$SERVICE_NAME" || true
    sudo systemctl disable "$SERVICE_NAME" || true
  fi

  if [ -f "$SERVICE_FILE" ]; then
    sudo rm -f "$SERVICE_FILE"
    sudo systemctl daemon-reload
  fi

  remove_ufw_rules

  log "Uninstall complete."
  log "The git clone and .venv were NOT removed."
}

main

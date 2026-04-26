#!/usr/bin/env bash
# =============================================================================
# uninstall_systemd.sh — Remove AlgoSoft systemd timer and service
#
# Stops the timer and service, disables them, and removes the unit files from
# /etc/systemd/system/. The project files are NOT touched.
#
# Usage:
#   sudo bash bot/scripts/uninstall_systemd.sh
# =============================================================================

set -euo pipefail

BOT_USER="${1:-$(whoami)}"
SERVICE="algosoft-bot@${BOT_USER}"
TIMER="algosoft-bot.timer"
SYSTEMD_DIR="/etc/systemd/system"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run with sudo."
    exit 1
fi

echo "→ Stopping and disabling ${TIMER}..."
systemctl stop  "${TIMER}"  2>/dev/null || true
systemctl disable "${TIMER}" 2>/dev/null || true

echo "→ Stopping ${SERVICE}..."
systemctl stop "${SERVICE}" 2>/dev/null || true

echo "→ Removing unit files..."
rm -f "${SYSTEMD_DIR}/algosoft-bot@.service"
rm -f "${SYSTEMD_DIR}/algosoft-bot.timer"

echo "→ Reloading systemd daemon..."
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

echo ""
echo "✅  AlgoSoft systemd units removed."
echo "    Project files in $(cd "$(dirname "$0")/../.." && pwd) are untouched."
echo ""

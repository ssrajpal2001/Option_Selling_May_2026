#!/usr/bin/env bash
# =============================================================================
# stop_bot.sh — Gracefully stop the AlgoSoft bot service
#
# Sends SIGTERM to the running service and waits up to 30 s for it to exit
# cleanly. Falls back to SIGKILL if it doesn't stop in time.
#
# Usage:
#   sudo bash bot/scripts/stop_bot.sh
# =============================================================================

set -euo pipefail

SERVICE="algosoft-bot"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run with sudo."
    exit 1
fi

if ! systemctl is-active --quiet "${SERVICE}" 2>/dev/null; then
    echo "ℹ  ${SERVICE} is not currently running."
    exit 0
fi

echo "→ Stopping ${SERVICE} (graceful SIGTERM, 30 s timeout)..."
systemctl stop "${SERVICE}"
echo "✅  ${SERVICE} stopped."
systemctl status "${SERVICE}" --no-pager -l 2>/dev/null || true

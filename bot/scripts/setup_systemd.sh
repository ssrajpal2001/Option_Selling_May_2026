#!/usr/bin/env bash
# =============================================================================
# setup_systemd.sh — One-time EC2 setup for AlgoSoft auto-start
#
# What this script does:
#   1. Copies algosoft-bot.service and algosoft-bot.timer into /etc/systemd/system/
#   2. Patches the WorkingDirectory and ExecStart paths to match THIS server
#   3. Reloads systemd, enables the timer (starts automatically on every boot)
#   4. Prints a status summary so you can confirm it worked
#
# Usage (run once, as root or with sudo):
#   bash bot/scripts/setup_systemd.sh
#
# To customise the run user or port, edit the variables below before running.
# =============================================================================

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
# Directory that contains the 'bot/' subfolder (parent of 'bot/')
INSTALL_ROOT="${INSTALL_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"

# Linux user the service will run as (must exist on the server)
BOT_USER="${BOT_USER:-$(whoami)}"

# Port uvicorn listens on
BOT_PORT="${BOT_PORT:-5000}"

# Python interpreter — default: .pythonlibs virtualenv inside the project
PYTHON_BIN="${PYTHON_BIN:-${INSTALL_ROOT}/.pythonlibs/bin/python}"
# Fallback to system python3 if the venv doesn't exist yet
if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(which python3)"
fi

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_SRC="${SCRIPTS_DIR}/algosoft-bot.service"
TIMER_SRC="${SCRIPTS_DIR}/algosoft-bot.timer"
SYSTEMD_DIR="/etc/systemd/system"
WORK_DIR="${INSTALL_ROOT}/bot"
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║      AlgoSoft — systemd auto-start setup             ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  Install root : ${INSTALL_ROOT}"
echo "  Bot dir      : ${WORK_DIR}"
echo "  Python       : ${PYTHON_BIN}"
echo "  Run as user  : ${BOT_USER}"
echo "  Port         : ${BOT_PORT}"
echo ""

# ── Preflight checks ─────────────────────────────────────────────────────────
if [ ! -f "$SERVICE_SRC" ] || [ ! -f "$TIMER_SRC" ]; then
    echo "ERROR: Service/timer files not found. Run from the project root:"
    echo "  bash bot/scripts/setup_systemd.sh"
    exit 1
fi

if [ ! -d "$WORK_DIR" ]; then
    echo "ERROR: Bot directory not found: ${WORK_DIR}"
    exit 1
fi

if ! command -v systemctl &>/dev/null; then
    echo "ERROR: systemd is not available on this system."
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root (use sudo)."
    exit 1
fi

# ── Copy and patch service file ───────────────────────────────────────────────
echo "→ Installing service unit..."
cp "$SERVICE_SRC" "${SYSTEMD_DIR}/algosoft-bot@.service"

# Patch paths into the service file
sed -i \
    -e "s|WorkingDirectory=.*|WorkingDirectory=${WORK_DIR}|g" \
    -e "s|/opt/algosoft/.pythonlibs/bin/python|${PYTHON_BIN}|g" \
    -e "s|--port 5000|--port ${BOT_PORT}|g" \
    -e "s|EnvironmentFile=-/opt/algosoft/|EnvironmentFile=-${INSTALL_ROOT}/|g" \
    "${SYSTEMD_DIR}/algosoft-bot@.service"

# ── Copy timer file ───────────────────────────────────────────────────────────
echo "→ Installing timer unit..."
cp "$TIMER_SRC" "${SYSTEMD_DIR}/algosoft-bot.timer"

# ── Reload, enable, start ─────────────────────────────────────────────────────
echo "→ Reloading systemd daemon..."
systemctl daemon-reload

echo "→ Enabling timer (auto-start on every boot)..."
systemctl enable algosoft-bot.timer

echo "→ Starting timer now..."
systemctl start algosoft-bot.timer

# ── Status summary ────────────────────────────────────────────────────────────
echo ""
echo "✅  Setup complete!"
echo ""
echo "Timer status:"
systemctl status algosoft-bot.timer --no-pager -l || true
echo ""
echo "Next scheduled run:"
systemctl list-timers algosoft-bot.timer --no-pager 2>/dev/null || true
echo ""
echo "Useful commands:"
echo "  sudo systemctl start  algosoft-bot@${BOT_USER}   # start right now"
echo "  sudo systemctl stop   algosoft-bot@${BOT_USER}   # stop gracefully"
echo "  sudo systemctl status algosoft-bot@${BOT_USER}   # check status"
echo "  sudo journalctl -u algosoft-bot@${BOT_USER} -f   # follow live logs"
echo "  sudo journalctl -u algosoft-bot@${BOT_USER} -n 100  # last 100 lines"
echo ""

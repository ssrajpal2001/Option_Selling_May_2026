#!/usr/bin/env bash
# =============================================================================
# setup_systemd.sh — One-time EC2 setup for AlgoSoft auto-start
#
# What this script does:
#   1. Copies algosoft-bot.service and algosoft-bot.timer into /etc/systemd/system/
#   2. Patches WorkingDirectory, ExecStart, User, and port to match THIS server
#   3. Reloads systemd and enables the timer (fires at 08:00 AM IST Mon–Fri)
#   4. Prints a status summary and a command cheatsheet
#
# Usage (run once, as root or with sudo):
#   sudo bash bot/scripts/setup_systemd.sh
#
# Override any default with environment variables:
#   sudo INSTALL_ROOT=/opt/algosoft BOT_USER=ubuntu BOT_PORT=5000 \
#       bash bot/scripts/setup_systemd.sh
# =============================================================================

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
# Project root — parent of the 'bot/' directory
INSTALL_ROOT="${INSTALL_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"

# Linux user the service will run as (must exist on the server).
# When run via sudo, prefer the original caller (SUDO_USER) over root.
BOT_USER="${BOT_USER:-${SUDO_USER:-$(id -un)}}"

# Port uvicorn listens on
BOT_PORT="${BOT_PORT:-5000}"

# Python interpreter — use the project's virtualenv if present; else system python3
PYTHON_BIN="${PYTHON_BIN:-${INSTALL_ROOT}/.pythonlibs/bin/python}"
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

# ── Preflight checks ──────────────────────────────────────────────────────────
if [ ! -f "$SERVICE_SRC" ] || [ ! -f "$TIMER_SRC" ]; then
    echo "ERROR: Service/timer files not found in ${SCRIPTS_DIR}/"
    echo "  Expected: algosoft-bot.service  algosoft-bot.timer"
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

# Verify the run user actually exists
if ! id "${BOT_USER}" &>/dev/null; then
    echo "ERROR: User '${BOT_USER}' does not exist on this server."
    echo "  Set BOT_USER to an existing account:  sudo BOT_USER=myuser bash setup_systemd.sh"
    exit 1
fi

# ── Install and patch service file ────────────────────────────────────────────
echo "→ Installing service unit (algosoft-bot.service)..."
cp "$SERVICE_SRC" "${SYSTEMD_DIR}/algosoft-bot.service"

# Patch all configurable values into the installed unit file
sed -i \
    -e "s|^User=.*|User=${BOT_USER}|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=${WORK_DIR}|" \
    -e "s|/opt/algosoft/.pythonlibs/bin/python|${PYTHON_BIN}|g" \
    -e "s|--port 5000|--port ${BOT_PORT}|g" \
    -e "s|EnvironmentFile=-/opt/algosoft/|EnvironmentFile=-${INSTALL_ROOT}/|g" \
    "${SYSTEMD_DIR}/algosoft-bot.service"

# ── Install timer file ────────────────────────────────────────────────────────
echo "→ Installing timer unit (algosoft-bot.timer)..."
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
echo "  sudo systemctl start  algosoft-bot    # start right now"
echo "  sudo systemctl stop   algosoft-bot    # stop gracefully"
echo "  sudo systemctl status algosoft-bot    # check status"
echo "  sudo journalctl -u algosoft-bot -f    # follow live logs"
echo "  sudo journalctl -u algosoft-bot -n 100  # last 100 lines"
echo ""
